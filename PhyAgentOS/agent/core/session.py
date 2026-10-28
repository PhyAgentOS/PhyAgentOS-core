"""SQLite-backed append-only session event store.

This is a concrete adapter for :class:`~PhyAgentOS.agent.core.protocols.SessionStore`.
It owns SQLite; the contracts in ``events.py`` / ``protocols.py`` stay free of
any storage dependency.

Storage rules:

* one append-only table, ``session_events``, with one row per event;
* ``sequence`` is per session, starts at 1, and is contiguous. It is assigned
  inside the same ``BEGIN IMMEDIATE`` transaction that inserts the row, while
  the session lock is held, so the assigned sequence matches durable commit
  order;
* ``append`` returns only after ``COMMIT`` (WAL with ``synchronous=FULL`` for
  file databases) and raises on any write failure, including a payload that
  is not JSON-serializable;
* the payload is serialized to JSON at append time, so what is stored is a
  snapshot of the payload as it was when the event was handed to the store;
* reads are ordered by ``sequence`` only. ``recorded_at`` is display data;
* ``read_conversation`` is a read-only projection over stored
  ``request.prepared`` / ``turn.completed`` payloads, not a second message
  table.

Recovery is descriptive. :meth:`SQLiteSessionStore.recover` reports turns
that started without a terminal ``turn.*`` event and model/tool calls that
started without a terminal counterpart, then appends the events from
:meth:`RecoveryReport.interruption_events`. A model attempt becomes
``model.attempt.interrupted``; a tool call that reached ``tool.dispatched``
is settled as ``unknown``; an intent-only call is settled as ``failed`` with
error code ``not_dispatched``, because its handler never ran. Recovery never
fabricates a finished turn or a success, never marks work for retry, and
never calls a model or tool handler. ``unfinished_turns`` is left for the
runtime to close.

Async surface, synchronous core: every public coroutine runs the SQLite work
on a worker thread via :func:`asyncio.to_thread`. A per-session reentrant lock
serializes writers of one session, and a connection lock serializes use of the
single connection. No event loops, timers, or background tasks are used here.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import CallKind, EventType, RecoveryReport, RuntimeEvent, UnfinishedCall
from .types import require_non_empty

__all__ = ["SessionStoreClosedError", "SQLiteSessionStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_events (
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    turn_id TEXT,
    step_id TEXT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (session_id, sequence)
)
"""

_TERMINAL_TURN_EVENTS = frozenset(
    {EventType.TURN_COMPLETED, EventType.TURN_FAILED, EventType.TURN_INTERRUPTED}
)
_TERMINAL_MODEL_EVENTS = frozenset(
    {
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.MODEL_ATTEMPT_FAILED,
        EventType.MODEL_ATTEMPT_INTERRUPTED,
    }
)
_MODEL_CALL_EVENTS = frozenset(
    {
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.MODEL_ATTEMPT_FAILED,
        EventType.MODEL_ATTEMPT_INTERRUPTED,
    }
)
_TOOL_START_EVENTS = frozenset({EventType.TOOL_INTENT, EventType.TOOL_DISPATCHED})


class SessionStoreClosedError(RuntimeError):
    """The store was closed; a closed store serves no more operations."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"session store is closed: {path}")


def _plain(value: Any) -> Any:
    """Convert read-only mappings into plain containers for JSON encoding.

    Only containers are unwrapped. Any other object is passed through so
    :func:`json.dumps` rejects it instead of coercing it with ``str()``.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _encode_payload(payload: Mapping[str, Any]) -> str:
    """Return the JSON text stored for ``payload``.

    Raises ``TypeError`` for a value JSON cannot represent and ``ValueError``
    for a non-finite float, both before anything is written.
    """
    return json.dumps(_plain(payload), ensure_ascii=False, allow_nan=False)


def _row_to_event(session_id: str, row: tuple[Any, ...]) -> RuntimeEvent:
    turn_id, step_id, event_type, payload_json, recorded_at, sequence = row
    return RuntimeEvent(
        type=EventType(event_type),
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        payload=json.loads(payload_json),
        sequence=int(sequence),
        recorded_at=datetime.fromisoformat(recorded_at),
    )


def _call_payload(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _unfinished_state(
    session_id: str, events: Sequence[RuntimeEvent]
) -> tuple[tuple[str, ...], dict[tuple[CallKind, str], UnfinishedCall]]:
    """Fold the log into unfinished turns and unfinished calls.

    The fold is order-sensitive by ``sequence``: a start event opens the call,
    a terminal event of the matching kind closes it. Calls still open at the
    end were interrupted by whatever stopped the writer.
    """
    open_turns: dict[str, None] = {}
    open_calls: dict[tuple[CallKind, str], UnfinishedCall] = {}

    for event in events:
        event_type = event.type
        turn_id = event.turn_id

        if event_type is EventType.TURN_STARTED:
            if turn_id is not None:
                open_turns.setdefault(turn_id, None)
            continue
        if event_type in _TERMINAL_TURN_EVENTS:
            if turn_id is not None:
                open_turns.pop(turn_id, None)
            continue
        if event_type in _MODEL_CALL_EVENTS:
            call_id = _call_payload(event.payload, "model_call_id")
            if call_id is None:
                continue
            key = (CallKind.MODEL, call_id)
            if event_type is EventType.MODEL_ATTEMPT_STARTED:
                if turn_id is not None and event.step_id is not None:
                    open_calls[key] = UnfinishedCall(
                        session_id=session_id,
                        turn_id=turn_id,
                        step_id=event.step_id,
                        kind=CallKind.MODEL,
                        call_id=call_id,
                    )
            elif event_type in _TERMINAL_MODEL_EVENTS:
                open_calls.pop(key, None)
            continue
        if event_type in _TOOL_START_EVENTS:
            call_id = _call_payload(event.payload, "tool_call_id")
            if call_id is None or turn_id is None or event.step_id is None:
                continue
            key = (CallKind.TOOL, call_id)
            previous = open_calls.get(key)
            # Later events for the same call update it in sequence order; once
            # a dispatch was durable the call may have run, even if a later
            # event of the same id only repeats the intent.
            open_calls[key] = UnfinishedCall(
                session_id=session_id,
                turn_id=turn_id,
                step_id=event.step_id,
                kind=CallKind.TOOL,
                call_id=call_id,
                dispatched=(
                    event_type is EventType.TOOL_DISPATCHED
                    or (previous is not None and previous.dispatched)
                ),
            )
            continue
        if event_type is EventType.TOOL_SETTLED:
            call_id = _call_payload(event.payload, "tool_call_id")
            if call_id is not None:
                open_calls.pop((CallKind.TOOL, call_id), None)

    return tuple(open_turns), open_calls


def _conversation_from_events(
    events: Sequence[RuntimeEvent],
) -> tuple[Mapping[str, Any], ...]:
    """Project the newest completed turn into a message history.

    Only a ``TURN_COMPLETED`` event whose ``turn.started`` is present counts;
    failed, interrupted and unfinished turns contribute nothing. The newest
    such turn is the last one in ``sequence`` order. Its history is the
    ``messages`` snapshot of that turn's last ``request.prepared``, copied,
    with leading system messages dropped, plus the ``turn.completed`` reply as
    an assistant message when a string reply exists. Nothing is invented: a
    completed turn without a request snapshot is a broken log and raises.
    """
    started: set[str] = set()
    completed: RuntimeEvent | None = None
    for event in events:
        turn_id = event.turn_id
        if turn_id is None:
            continue
        if event.type is EventType.TURN_STARTED:
            started.add(turn_id)
        elif event.type is EventType.TURN_COMPLETED and turn_id in started:
            completed = event
    if completed is None:
        return ()

    turn_id = completed.turn_id
    snapshot: RuntimeEvent | None = None
    for event in events:
        if event.type is EventType.REQUEST_PREPARED and event.turn_id == turn_id:
            snapshot = event
    if snapshot is None:
        raise ValueError(f"turn {turn_id!r} is completed but has no request.prepared snapshot")

    raw_messages = snapshot.payload.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError(f"turn {turn_id!r} request.prepared has no messages snapshot")

    messages: list[Mapping[str, Any]] = []
    for message in copy.deepcopy(raw_messages):
        if not isinstance(message, Mapping):
            raise ValueError("request.prepared messages must be mappings")
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("request.prepared messages require a non-empty role")
        messages.append(dict(message))

    while messages and messages[0]["role"] == "system":
        messages.pop(0)

    reply = completed.payload.get("reply")
    if isinstance(reply, str):
        messages.append({"role": "assistant", "content": reply})
    return tuple(messages)


class SQLiteSessionStore:
    """Append-only session event log on SQLite (WAL for file databases).

    ``path`` is injected; ``":memory:"`` gives a private in-memory database,
    a filesystem path a file database. The store never creates parent
    directories: a missing directory is an error from SQLite, not an implicit
    ``mkdir``. One store instance owns one connection; use one instance per
    process role, and close it with :meth:`aclose`.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = str(path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._closed = False
        # Serializes all use of the single SQLite connection.
        self._conn_lock = threading.RLock()
        # Per-session writer locks; guarded by ``_locks_guard``.
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.RLock] = {}
        self._conn = self._connect()

    # -- construction -------------------------------------------------

    @property
    def _is_memory(self) -> bool:
        return self._path == ":memory:" or "mode=memory" in self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path,
            timeout=max(self._busy_timeout_ms, 0) / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {max(self._busy_timeout_ms, 0)}")
            if not self._is_memory:
                mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise RuntimeError(f"SQLite refused WAL mode for {self._path}: {mode}")
                conn.execute("PRAGMA synchronous = FULL")
            conn.execute(_SCHEMA)
        except BaseException:
            conn.close()
            raise
        return conn

    # -- state --------------------------------------------------------

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise SessionStoreClosedError(self._path)

    def _session_lock(self, session_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[session_id] = lock
            return lock

    # -- public API ---------------------------------------------------

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        """Append one event and return it with its assigned ``sequence``.

        Returns after the row is committed. Raises when the payload is not
        JSON-serializable or when the write fails; in that case nothing is
        stored and the caller must treat the step as failed.
        """
        if not isinstance(event, RuntimeEvent):
            raise TypeError("event must be a RuntimeEvent")
        self._raise_if_closed()
        return await asyncio.to_thread(self._append_sync, event)

    async def read_turn(self, session_id: str, turn_id: str) -> Sequence[RuntimeEvent]:
        """Return one turn's events, ordered by ``sequence``."""
        require_non_empty("session_id", session_id)
        require_non_empty("turn_id", turn_id)
        self._raise_if_closed()
        return await asyncio.to_thread(self._select_sync, session_id, turn_id, 0)

    async def read_session(
        self, session_id: str, *, after_sequence: int = 0
    ) -> Sequence[RuntimeEvent]:
        """Return one session's events after ``after_sequence``, by ``sequence``."""
        require_non_empty("session_id", session_id)
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool):
            raise TypeError("after_sequence must be an int")
        if after_sequence < 0:
            raise ValueError("after_sequence must be >= 0")
        self._raise_if_closed()
        return await asyncio.to_thread(self._select_sync, session_id, None, after_sequence)

    async def read_conversation(self, session_id: str) -> Sequence[Mapping[str, Any]]:
        """Return the message history of the newest completed turn.

        Read-only projection: the last ``request.prepared`` messages of the
        last completed turn, leading system messages removed, plus the
        ``turn.completed`` reply as an assistant message. Returns an empty
        tuple when the session has no completed turn. Failed, interrupted and
        unfinished turns contribute nothing, and no work is resumed.
        """
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        return await asyncio.to_thread(self._conversation_sync, session_id)

    async def recover(self, session_id: str) -> RecoveryReport:
        """Report unfinished work and close it with recovery events.

        The interruption events from :meth:`RecoveryReport.interruption_events`
        are appended before the report is returned, so a second call finds the
        calls already closed and appends nothing. Unfinished turns are
        reported, not rewritten: closing them belongs to the runtime.
        """
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        return await asyncio.to_thread(self._recover_sync, session_id)

    async def aclose(self) -> None:
        """Close the connection. Idempotent; later operations raise."""
        await asyncio.to_thread(self._close_sync)

    # -- synchronous core ---------------------------------------------

    def _append_sync(self, event: RuntimeEvent) -> RuntimeEvent:
        self._raise_if_closed()
        # Encode before touching the database: an unserializable payload must
        # fail without a sequence or a row.
        payload_json = _encode_payload(event.payload)
        recorded_at = event.recorded_at.astimezone(timezone.utc).isoformat()

        with self._session_lock(event.session_id):
            with self._conn_lock:
                self._raise_if_closed()
                conn = self._conn
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM session_events"
                        " WHERE session_id = ?",
                        (event.session_id,),
                    ).fetchone()
                    sequence = int(row[0]) + 1
                    conn.execute(
                        "INSERT INTO session_events"
                        " (session_id, sequence, turn_id, step_id, event_type,"
                        "  payload, recorded_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            event.session_id,
                            sequence,
                            event.turn_id,
                            event.step_id,
                            event.type.value,
                            payload_json,
                            recorded_at,
                        ),
                    )
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
        return replace(event, sequence=sequence)

    def _select_sync(
        self, session_id: str, turn_id: str | None, after_sequence: int
    ) -> tuple[RuntimeEvent, ...]:
        sql = (
            "SELECT turn_id, step_id, event_type, payload, recorded_at, sequence"
            " FROM session_events WHERE session_id = ? AND sequence > ?"
        )
        params: list[Any] = [session_id, after_sequence]
        if turn_id is not None:
            sql += " AND turn_id = ?"
            params.append(turn_id)
        sql += " ORDER BY sequence ASC"
        with self._conn_lock:
            self._raise_if_closed()
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return tuple(_row_to_event(session_id, row) for row in rows)

    def _recover_sync(self, session_id: str) -> RecoveryReport:
        self._raise_if_closed()
        with self._session_lock(session_id):
            events = self._select_sync(session_id, None, 0)
            unfinished_turns, open_calls = _unfinished_state(session_id, events)
            report = RecoveryReport(
                session_id=session_id,
                unfinished_turns=unfinished_turns,
                unfinished_calls=tuple(open_calls.values()),
            )
            for event in report.interruption_events():
                self._append_sync(event)
            return report

    def _conversation_sync(self, session_id: str) -> tuple[Mapping[str, Any], ...]:
        return _conversation_from_events(self._select_sync(session_id, None, 0))

    def _close_sync(self) -> None:
        with self._conn_lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

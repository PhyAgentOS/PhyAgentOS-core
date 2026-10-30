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
  table. When a context checkpoint exists, the projection starts from the
  checkpoint (summary plus the retained recent messages) and only reads events
  after ``covers_sequence``; a completed turn after that point still wins with
  its own request snapshot, so the full log is not replayed on every turn;
* ``read_startup_snapshot`` reads recovery-relevant events, the newest
  checkpoint, and the head in one transaction. It omits streaming delta rows
  (neither recovery nor conversation projection consumes them) and writes
  recovery interruptions only through a head compare-and-swap. A concurrent
  append, or a checkpoint change at an unchanged head, makes it fold a fresh
  view instead of returning a stale report or writing a stale interruption;
* context checkpoints are a separate, append-only projection table. They never
  rewrite ``session_events`` and they are not ``context.compacted`` events:
  that event type belongs to the loop driver's turn, while a checkpoint is a
  stored read projection. A checkpoint is committed with a
  compare-and-swap on the session head sequence, so work appended after the
  summary was requested is never silently covered by it.

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
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import CallKind, EventType, RecoveryReport, RuntimeEvent, UnfinishedCall
from .protocols import SessionSnapshot
from .types import require_non_empty

__all__ = [
    "CHECKPOINT_SUMMARY_NOTICE",
    "CheckpointConflictError",
    "CompactionAttempt",
    "ContextCheckpoint",
    "RecoveryConflictError",
    "SessionStoreClosedError",
    "SQLiteSessionStore",
]

# The summary is stored as an assistant message because leading system messages
# are dropped when a later request snapshot is projected. The notice keeps the
# message honest: it is generated text, not something the assistant said.
CHECKPOINT_SUMMARY_NOTICE = (
    "[context checkpoint] Earlier conversation was compacted to save context. "
    "This is a model-generated summary of it, not a user message and not an "
    "instruction to execute anything."
)

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
);
CREATE TABLE IF NOT EXISTS session_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    covers_sequence INTEGER NOT NULL,
    summary TEXT NOT NULL,
    retained_messages TEXT NOT NULL,
    selection TEXT NOT NULL,
    usage TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_checkpoints_session
    ON session_checkpoints (session_id, checkpoint_id);
CREATE TABLE IF NOT EXISTS compaction_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    effort TEXT,
    usage TEXT NOT NULL,
    reason TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_compaction_attempts_session
    ON compaction_attempts (session_id, attempt_id)
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


class RecoveryConflictError(RuntimeError):
    """Turn startup could not fold and persist a stable recovery view.

    The session head kept moving through every bounded retry, so no recovery
    snapshot can be returned without risking a stale interruption or a stale
    report. Nothing was written by the failed attempts; the caller must not
    open a new turn and should retry on the next submit.
    """

    def __init__(self, session_id: str, attempts: int) -> None:
        self.session_id = session_id
        self.attempts = attempts
        super().__init__(
            f"recovery conflict for session {session_id!r}: the session head "
            f"moved during {attempts} startup attempts; no recovery event was "
            "written and no turn may start"
        )


class CheckpointConflictError(RuntimeError):
    """A checkpoint cannot be written without covering or rewriting new work.

    Raised when the session head moved after the summary snapshot was taken,
    when the session has an unfinished turn, or when a checkpoint would not
    move past the previous one. Nothing was written; the event log is
    unchanged.
    """

    def __init__(self, session_id: str, reason: str) -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"context checkpoint rejected for session {session_id!r}: {reason}")


@dataclass(frozen=True, slots=True)
class ContextCheckpoint:
    """A stored read projection that replaces an older conversation prefix.

    ``covers_sequence`` is the session head the summary was taken from.
    ``retained_messages`` are the complete recent messages kept verbatim beside
    the summary; they start at a user-message boundary so a tool call and its
    results are never split. ``selection`` records the provider/model/effort
    used for the summary; it is metadata and does not change the session log.
    ``usage`` is a copy of the summary call's token accounting for reference;
    cumulative session totals come from compaction attempts, so the same call
    is not counted twice.

    The raw events stay in ``session_events``; a checkpoint only changes what
    ``read_conversation`` projects.
    """

    session_id: str
    covers_sequence: int
    summary: str
    retained_messages: tuple[Mapping[str, Any], ...] = ()
    selection: Mapping[str, str | None] = field(default_factory=dict)
    usage: Mapping[str, int] = field(default_factory=dict)
    checkpoint_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        require_non_empty("session_id", self.session_id)
        if not isinstance(self.covers_sequence, int) or isinstance(self.covers_sequence, bool):
            raise TypeError("covers_sequence must be an int")
        if self.covers_sequence < 0:
            raise ValueError("covers_sequence must be >= 0")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("summary must be a non-empty string")

        retained: list[Mapping[str, Any]] = []
        for message in self.retained_messages:
            if not isinstance(message, Mapping):
                raise TypeError("retained_messages must contain mappings")
            role = message.get("role")
            if not isinstance(role, str) or not role.strip():
                raise ValueError("retained messages require a non-empty role")
            retained.append(dict(message))
        object.__setattr__(self, "retained_messages", tuple(retained))

        selection: dict[str, str | None] = {}
        for key, value in self.selection.items():
            if value is not None and not isinstance(value, str):
                raise TypeError(f"selection[{key!r}] must be a string or None")
            selection[str(key)] = value
        object.__setattr__(self, "selection", selection)

        usage: dict[str, int] = {}
        for key, value in self.usage.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"usage[{key!r}] must be a non-negative int")
            usage[str(key)] = value
        object.__setattr__(self, "usage", usage)

        if self.checkpoint_id is not None:
            if (
                not isinstance(self.checkpoint_id, int)
                or isinstance(self.checkpoint_id, bool)
                or self.checkpoint_id < 1
            ):
                raise ValueError("checkpoint_id must be a positive int or None")
        if not isinstance(self.created_at, datetime):
            raise TypeError("created_at must be a datetime")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")


_ATTEMPT_STATUSES = frozenset({"started", "completed", "failed", "interrupted"})


@dataclass(frozen=True, slots=True)
class CompactionAttempt:
    """One manual summary call, tracked independently from the checkpoint.

    The row is written before the provider call and is updated once when the
    attempt ends. It records known token usage even when the summary is
    rejected or the checkpoint commit loses the compare-and-swap, so a failed
    attempt never changes the conversation projection but its cost stays
    visible. ``usage`` only contains keys the provider reported; unknown
    output tokens are absent, not zero.

    ``status`` is one of ``started``, ``completed``, ``failed`` or
    ``interrupted``.
    """

    session_id: str
    status: str
    attempt_id: int | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        require_non_empty("session_id", self.session_id)
        if self.status not in _ATTEMPT_STATUSES:
            raise ValueError(f"unknown compaction attempt status: {self.status!r}")
        for name, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("effort", self.effort),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        usage: dict[str, int] = {}
        for key, value in self.usage.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"usage[{key!r}] must be a non-negative int")
            usage[str(key)] = value
        object.__setattr__(self, "usage", usage)
        if self.attempt_id is not None:
            if (
                not isinstance(self.attempt_id, int)
                or isinstance(self.attempt_id, bool)
                or self.attempt_id < 1
            ):
                raise ValueError("attempt_id must be a positive int or None")
        if self.reason is not None:
            if not isinstance(self.reason, str):
                raise TypeError("reason must be a string or None")
            object.__setattr__(self, "reason", self.reason[:500])
        for name, value in (("started_at", self.started_at), ("finished_at", self.finished_at)):
            if value is None:
                continue
            if not isinstance(value, datetime):
                raise TypeError(f"{name} must be a datetime")
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")


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


def _encode_usage_json(usage: Mapping[str, int] | None) -> str:
    clean: dict[str, int] = {}
    if usage is not None:
        if not isinstance(usage, Mapping):
            raise TypeError("usage must be a mapping or None")
        for key, value in usage.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"usage[{key!r}] must be a non-negative int")
            clean[str(key)] = value
    return json.dumps(clean, ensure_ascii=False)


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


def _insert_event_row(
    conn: sqlite3.Connection,
    event: RuntimeEvent,
    sequence: int,
    payload_json: str,
    recorded_at: str,
) -> None:
    """Insert one already-encoded row inside the caller's transaction."""
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


def _call_payload(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _unfinished_state(
    session_id: str, events: Sequence[RuntimeEvent]
) -> tuple[tuple[str, ...], dict[tuple[CallKind, str, str, str], UnfinishedCall]]:
    """Fold the log into unfinished turns and unfinished calls.

    The fold is order-sensitive by ``sequence``: a start event opens the call,
    a terminal event of the matching kind closes it. Calls still open at the
    end were interrupted by whatever stopped the writer.

    A call is keyed by kind plus its full scope (turn, step, call id).
    Provider call ids are only unique within one response, so two turns of the
    same session can legitimately reuse the same id without being the same
    call.
    """
    open_turns: dict[str, None] = {}
    open_calls: dict[tuple[CallKind, str, str, str], UnfinishedCall] = {}

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
            if call_id is None or turn_id is None or event.step_id is None:
                continue
            key = (CallKind.MODEL, turn_id, event.step_id, call_id)
            if event_type is EventType.MODEL_ATTEMPT_STARTED:
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
            key = (CallKind.TOOL, turn_id, event.step_id, call_id)
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
            if call_id is not None and turn_id is not None and event.step_id is not None:
                open_calls.pop((CallKind.TOOL, turn_id, event.step_id, call_id), None)

    return tuple(open_turns), open_calls


def _recovery_report(
    session_id: str, events: Sequence[RuntimeEvent]
) -> RecoveryReport:
    """Fold one already-read event view into a recovery report."""
    unfinished_turns, open_calls = _unfinished_state(session_id, events)
    return RecoveryReport(
        session_id=session_id,
        unfinished_turns=unfinished_turns,
        unfinished_calls=tuple(open_calls.values()),
    )


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


def _checkpoint_from_row(row: tuple[Any, ...]) -> ContextCheckpoint:
    (
        checkpoint_id,
        session_id,
        covers_sequence,
        summary,
        retained_json,
        selection_json,
        usage_json,
        created_at,
    ) = row
    retained = json.loads(retained_json)
    selection = json.loads(selection_json)
    usage = json.loads(usage_json)
    if not isinstance(retained, list):
        raise ValueError(f"checkpoint {checkpoint_id!r} retained_messages is not a list")
    if not isinstance(selection, Mapping):
        raise ValueError(f"checkpoint {checkpoint_id!r} selection is not an object")
    if not isinstance(usage, Mapping):
        raise ValueError(f"checkpoint {checkpoint_id!r} usage is not an object")
    return ContextCheckpoint(
        session_id=session_id,
        covers_sequence=int(covers_sequence),
        summary=summary,
        retained_messages=tuple(retained),
        selection=selection,
        usage=usage,
        checkpoint_id=int(checkpoint_id),
        created_at=datetime.fromisoformat(created_at),
    )


def _attempt_from_row(row: tuple[Any, ...]) -> CompactionAttempt:
    (
        attempt_id,
        session_id,
        status,
        provider,
        model,
        effort,
        usage_json,
        reason,
        started_at,
        finished_at,
    ) = row
    usage = json.loads(usage_json)
    if not isinstance(usage, Mapping):
        raise ValueError(f"compaction attempt {attempt_id!r} usage is not an object")
    return CompactionAttempt(
        session_id=session_id,
        status=status,
        attempt_id=int(attempt_id),
        provider=provider,
        model=model,
        effort=effort,
        usage=usage,
        reason=reason,
        started_at=datetime.fromisoformat(started_at),
        finished_at=datetime.fromisoformat(finished_at) if finished_at else None,
    )


def _checkpoint_conversation(
    checkpoint: ContextCheckpoint,
) -> tuple[Mapping[str, Any], ...]:
    """Project a checkpoint as legal messages: summary first, then the kept tail."""
    summary_message: dict[str, Any] = {
        "role": "assistant",
        "content": f"{CHECKPOINT_SUMMARY_NOTICE}\n\n{checkpoint.summary}",
    }
    messages: list[Mapping[str, Any]] = [summary_message]
    messages.extend(copy.deepcopy(dict(message)) for message in checkpoint.retained_messages)
    return tuple(messages)


def _project_with_checkpoint(
    events: Sequence[RuntimeEvent],
    checkpoint: ContextCheckpoint | None,
) -> tuple[Mapping[str, Any], ...]:
    """Project conversation from already-read events and the newest checkpoint.

    This is the same rule ``read_conversation`` applies: a completed turn after
    the checkpoint wins with its own request snapshot; otherwise the checkpoint
    projection is used.
    """
    if checkpoint is None:
        return _conversation_from_events(events)
    tail = tuple(
        event
        for event in events
        if (event.sequence if event.sequence is not None else 0)
        > checkpoint.covers_sequence
    )
    if tail:
        projected = _conversation_from_events(tail)
        if projected:
            return projected
    return _checkpoint_conversation(checkpoint)


def _checkpoint_identity(checkpoint: ContextCheckpoint | None) -> tuple[int | None, int] | None:
    """Identity used to notice a checkpoint change at an unchanged head."""
    if checkpoint is None:
        return None
    return (checkpoint.checkpoint_id, checkpoint.covers_sequence)


_INIT_RETRY_ATTEMPTS = 20
_INIT_RETRY_BASE_SECONDS = 0.005
_INIT_RETRY_MAX_SECONDS = 0.1
_STARTUP_SNAPSHOT_ATTEMPTS = 4
_SQLITE_BUSY_BASE_CODES = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED


def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    """True only for SQLITE_BUSY / SQLITE_LOCKED, not every OperationalError."""
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in _SQLITE_BUSY_BASE_CODES:
        return True
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text or "busy" in text


def _open_sqlite_connection(
    path: str,
    busy_timeout_ms: int,
    schema: str,
) -> sqlite3.Connection:
    """Open a connection and finish WAL/schema setup despite simultaneous first opens.

    ``PRAGMA journal_mode = WAL`` needs a brief exclusive lock. Unlike ordinary
    reads and writes, that switch can fail immediately with SQLITE_BUSY /
    SQLITE_LOCKED even when ``busy_timeout`` is set, so several processes
    opening a brand-new database at once can fail the first one. The whole
    setup is retried with bounded backoff for exactly those two result codes;
    any other ``OperationalError`` is raised on the first attempt. WAL and
    ``synchronous = FULL`` are never downgraded, and after the retry budget the
    last real error is raised.
    """
    is_memory = path == ":memory:" or "mode=memory" in path
    timeout = max(busy_timeout_ms, 0) / 1000.0
    delay = _INIT_RETRY_BASE_SECONDS
    last_error: sqlite3.OperationalError | None = None
    last_mode: object | None = None

    for attempt in range(_INIT_RETRY_ATTEMPTS):
        conn = sqlite3.connect(
            path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {max(busy_timeout_ms, 0)}")
            if not is_memory:
                mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    # The switch can also report the unchanged mode instead of
                    # raising when another connection holds the lock.
                    last_mode = mode
                    last_error = None
                else:
                    conn.execute("PRAGMA synchronous = FULL")
                    conn.executescript(schema)
                    return conn
            else:
                conn.executescript(schema)
                return conn
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy(exc):
                conn.close()
                raise
            last_error = exc
        except BaseException:
            conn.close()
            raise
        conn.close()
        if attempt + 1 < _INIT_RETRY_ATTEMPTS:
            time.sleep(delay)
            delay = min(_INIT_RETRY_MAX_SECONDS, delay * 2)

    if last_mode is not None and last_error is None:
        raise RuntimeError(f"SQLite refused WAL mode for {path}: {last_mode}")
    assert last_error is not None
    raise last_error


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

    def _connect(self) -> sqlite3.Connection:
        return _open_sqlite_connection(self._path, self._busy_timeout_ms, _SCHEMA)

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
        """Return the message history the next turn should start from.

        Read-only projection. Without a checkpoint this is the newest completed
        turn's last ``request.prepared`` messages (leading system messages
        removed) plus the ``turn.completed`` reply. With a checkpoint covering
        sequence ``N``, the newest completed turn after ``N`` wins with its own
        request snapshot; otherwise the projection is the checkpoint summary
        plus its retained messages. Failed, interrupted and unfinished turns
        contribute nothing, and no work is resumed.
        """
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        return await asyncio.to_thread(self._conversation_sync, session_id)

    async def read_startup_snapshot(self, session_id: str) -> SessionSnapshot:
        """Return recovery and the next conversation from one consistent read.

        Recovery is decided against a head sequence and its writes are
        conditional on that head: an external process that completes a call or
        turn between the read and the write makes the conditional append fail,
        and the whole view is read and folded again instead of closing work
        that is already terminal. No stale interruption is ever written.

        Streaming deltas are excluded because neither ``_unfinished_state`` nor
        conversation projection consumes them. A checkpoint change at an
        unchanged head refreshes the conversation; it does not change recovery.
        Across attempts this method keeps no cache.

        Known limit: there is no writer lease. A terminal event committed after
        a successful CAS can still follow the interruption. Work that was
        already terminal before the CAS is never interrupted, and exhaustion
        raises :class:`RecoveryConflictError` instead of returning a snapshot.

        A store subclass that overrides ``recover`` keeps the public
        recover-then-read path here. That preserves hooks such as a test or an
        embedding that gates recovery; the base store is the fast path.
        """
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        if getattr(type(self), "recover", None) is not SQLiteSessionStore.recover:
            report = await self.recover(session_id)
            return SessionSnapshot(report, await self.read_conversation(session_id))

        for _attempt in range(_STARTUP_SNAPSHOT_ATTEMPTS):
            events, checkpoint, head = await asyncio.to_thread(
                self._startup_rows_sync, session_id
            )
            report = _recovery_report(session_id, events)
            pending = report.interruption_events()
            if pending:
                saved = await self.append_recovery(
                    session_id, pending, expected_head=head
                )
                if saved is None:
                    # An external writer moved the head: the fold above no
                    # longer describes the log, so decide again from a new read.
                    continue
                head_sequence = (
                    saved[-1].sequence if saved and saved[-1].sequence is not None else head
                )
                events = (*events, *saved)
                latest = await asyncio.to_thread(
                    self._latest_checkpoint_sync, session_id
                )
                if _checkpoint_identity(latest) != _checkpoint_identity(checkpoint):
                    conversation = await self.read_conversation(session_id)
                else:
                    conversation = _project_with_checkpoint(events, checkpoint)
                return SessionSnapshot(
                    report, conversation, head_sequence=head_sequence
                )

            head_after = await asyncio.to_thread(self._head_sequence_sync, session_id)
            latest = await asyncio.to_thread(
                self._latest_checkpoint_sync, session_id
            )
            if (
                head_after == head
                and _checkpoint_identity(latest) == _checkpoint_identity(checkpoint)
            ):
                return SessionSnapshot(
                    report,
                    _project_with_checkpoint(events, checkpoint),
                    head_sequence=head,
                )
            # Work landed after the read and before this check. Re-read rather
            # than returning a report that may already be stale.

        # A continuously moving writer exhausted the retries. Fail closed: an
        # unwritten recovery is safer than a stale interruption, but returning
        # an executable snapshot here would let the runtime start a new turn
        # while this one is still unfinished.
        raise RecoveryConflictError(session_id, _STARTUP_SNAPSHOT_ATTEMPTS)

    async def append_recovery(
        self,
        session_id: str,
        events: Sequence[RuntimeEvent],
        *,
        expected_head: int,
    ) -> tuple[RuntimeEvent, ...] | None:
        """Append recovery events only if the session head is still expected.

        This is one SQLite write transaction: the head check and every insert
        either all commit or none do. ``None`` means another connection moved
        the head, so the caller must fold again; no recovery event was written.
        Subclasses may override this public hook to observe recovery writes
        without changing the atomicity requirement.
        """
        require_non_empty("session_id", session_id)
        if not isinstance(expected_head, int) or isinstance(expected_head, bool):
            raise TypeError("expected_head must be an int")
        if expected_head < 0:
            raise ValueError("expected_head must be >= 0")
        for event in events:
            if not isinstance(event, RuntimeEvent):
                raise TypeError("recovery events must be RuntimeEvent instances")
            if event.session_id != session_id:
                raise ValueError("recovery event belongs to a different session")
        self._raise_if_closed()
        return await asyncio.to_thread(
            self._append_recovery_sync, session_id, tuple(events), expected_head
        )

    async def read_head_sequence(self, session_id: str) -> int:
        """Return the highest stored sequence for ``session_id`` (0 when empty)."""
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        return await asyncio.to_thread(self._head_sequence_sync, session_id)

    async def read_checkpoint(self, session_id: str) -> ContextCheckpoint | None:
        """Return the newest context checkpoint, or ``None`` when none exists."""
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        return await asyncio.to_thread(self._latest_checkpoint_sync, session_id)

    async def get_compaction_usage(self, session_id: str) -> dict[str, int]:
        """Return cumulative summary-call usage for one session.

        Only compaction attempts are summed, so a successful checkpoint's
        copied usage is not counted twice. Unknown token kinds stay absent.
        """
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        return await asyncio.to_thread(self._attempt_usage_sync, session_id)

    async def list_compaction_attempts(
        self, session_id: str
    ) -> Sequence[CompactionAttempt]:
        """Return this session's attempts in start order (durable cost trail)."""
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        return await asyncio.to_thread(self._list_attempts_sync, session_id)

    async def begin_compaction_attempt(
        self,
        session_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> CompactionAttempt:
        """Record a summary attempt before the provider call.

        A row left in ``started`` means the process stopped before the outcome
        was recorded; readers must not assume either success or failure.
        """
        require_non_empty("session_id", session_id)
        self._raise_if_closed()
        return await asyncio.to_thread(
            self._begin_attempt_sync, session_id, provider, model, effort
        )

    async def finish_compaction_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        usage: Mapping[str, int] | None = None,
        reason: str | None = None,
    ) -> CompactionAttempt:
        """Close a started attempt with its known usage and a short reason."""
        if not isinstance(attempt_id, int) or isinstance(attempt_id, bool) or attempt_id < 1:
            raise ValueError("attempt_id must be a positive int")
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"terminal compaction status required, got {status!r}")
        self._raise_if_closed()
        return await asyncio.to_thread(
            self._finish_attempt_sync, attempt_id, status, usage, reason
        )

    async def commit_checkpoint(
        self, checkpoint: ContextCheckpoint, *, expected_head_sequence: int
    ) -> ContextCheckpoint:
        """Store a projection checkpoint under a head compare-and-swap.

        ``expected_head_sequence`` must be the head observed *before* the
        summary input was read. The commit is rejected when the head moved,
        when the session has an unfinished turn, or when the checkpoint would
        not move past the previous one. A rejected commit writes nothing, so
        the original history and the previous projection stay intact.
        """
        if not isinstance(checkpoint, ContextCheckpoint):
            raise TypeError("checkpoint must be a ContextCheckpoint")
        if (
            not isinstance(expected_head_sequence, int)
            or isinstance(expected_head_sequence, bool)
        ):
            raise TypeError("expected_head_sequence must be an int")
        if expected_head_sequence < 0:
            raise ValueError("expected_head_sequence must be >= 0")
        if checkpoint.covers_sequence != expected_head_sequence:
            raise ValueError("checkpoint.covers_sequence must equal expected_head_sequence")
        self._raise_if_closed()
        return await asyncio.to_thread(
            self._commit_checkpoint_sync, checkpoint, expected_head_sequence
        )

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
                    _insert_event_row(conn, event, sequence, payload_json, recorded_at)
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

    def _startup_rows_sync(
        self, session_id: str
    ) -> tuple[tuple[RuntimeEvent, ...], ContextCheckpoint | None, int]:
        """Read recovery-relevant events, the checkpoint, and the head together.

        One deferred read transaction gives the three values the same snapshot.
        ``model.response.delta`` rows are left out: recovery folds them as
        no-ops and conversation projection never reads them, so decoding their
        payloads only adds work.
        """
        self._raise_if_closed()
        with self._session_lock(session_id):
            with self._conn_lock:
                self._raise_if_closed()
                conn = self._conn
                conn.execute("BEGIN")
                try:
                    rows = conn.execute(
                        "SELECT turn_id, step_id, event_type, payload, recorded_at,"
                        " sequence FROM session_events"
                        " WHERE session_id = ? AND event_type != ?"
                        " ORDER BY sequence ASC",
                        (session_id, EventType.MODEL_RESPONSE_DELTA.value),
                    ).fetchall()
                    checkpoint_row = conn.execute(
                        "SELECT checkpoint_id, session_id, covers_sequence, summary,"
                        " retained_messages, selection, usage, created_at"
                        " FROM session_checkpoints WHERE session_id = ?"
                        " ORDER BY checkpoint_id DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    head_row = conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM session_events"
                        " WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        events = tuple(_row_to_event(session_id, row) for row in rows)
        checkpoint = None if checkpoint_row is None else _checkpoint_from_row(checkpoint_row)
        return events, checkpoint, int(head_row[0])

    def _append_recovery_sync(
        self,
        session_id: str,
        events: tuple[RuntimeEvent, ...],
        expected_head: int,
    ) -> tuple[RuntimeEvent, ...] | None:
        """All-or-nothing recovery append under a head compare-and-swap."""
        self._raise_if_closed()
        encoded = [
            (
                event,
                _encode_payload(event.payload),
                event.recorded_at.astimezone(timezone.utc).isoformat(),
            )
            for event in events
        ]
        with self._session_lock(session_id):
            with self._conn_lock:
                self._raise_if_closed()
                conn = self._conn
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM session_events"
                        " WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    if int(row[0]) != expected_head:
                        conn.execute("ROLLBACK")
                        return None
                    saved: list[RuntimeEvent] = []
                    sequence = expected_head
                    for event, payload_json, recorded_at in encoded:
                        sequence += 1
                        _insert_event_row(
                            conn, event, sequence, payload_json, recorded_at
                        )
                        saved.append(replace(event, sequence=sequence))
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
        return tuple(saved)

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
        checkpoint = self._latest_checkpoint_sync(session_id)
        if checkpoint is None:
            return _conversation_from_events(self._select_sync(session_id, None, 0))
        tail = self._select_sync(session_id, None, checkpoint.covers_sequence)
        if tail:
            # A turn completed after the checkpoint carries the exact history
            # the model was given, which already starts with the checkpoint
            # projection. Use it instead of rebuilding the old log.
            projected = _conversation_from_events(tail)
            if projected:
                return projected
        return _checkpoint_conversation(checkpoint)

    def _head_sequence_sync(self, session_id: str) -> int:
        with self._conn_lock:
            self._raise_if_closed()
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM session_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0])

    def _latest_checkpoint_sync(self, session_id: str) -> ContextCheckpoint | None:
        with self._conn_lock:
            self._raise_if_closed()
            row = self._conn.execute(
                "SELECT checkpoint_id, session_id, covers_sequence, summary,"
                " retained_messages, selection, usage, created_at"
                " FROM session_checkpoints WHERE session_id = ?"
                " ORDER BY checkpoint_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return None if row is None else _checkpoint_from_row(row)

    def _attempt_usage_sync(self, session_id: str) -> dict[str, int]:
        with self._conn_lock:
            self._raise_if_closed()
            rows = self._conn.execute(
                "SELECT usage FROM compaction_attempts WHERE session_id = ?"
                " ORDER BY attempt_id ASC",
                (session_id,),
            ).fetchall()
        totals: dict[str, int] = {}
        for (usage_json,) in rows:
            usage = json.loads(usage_json)
            if not isinstance(usage, Mapping):
                raise ValueError("compaction attempt usage is not an object")
            for key, value in usage.items():
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    totals[str(key)] = totals.get(str(key), 0) + value
        return totals

    def _list_attempts_sync(self, session_id: str) -> tuple[CompactionAttempt, ...]:
        with self._conn_lock:
            self._raise_if_closed()
            rows = self._conn.execute(
                "SELECT attempt_id, session_id, status, provider, model, effort,"
                " usage, reason, started_at, finished_at"
                " FROM compaction_attempts WHERE session_id = ?"
                " ORDER BY attempt_id ASC",
                (session_id,),
            ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def _begin_attempt_sync(
        self,
        session_id: str,
        provider: str | None,
        model: str | None,
        effort: str | None,
    ) -> CompactionAttempt:
        candidate = CompactionAttempt(
            session_id=session_id,
            status="started",
            provider=provider,
            model=model,
            effort=effort,
        )
        with self._conn_lock:
            self._raise_if_closed()
            now = datetime.now(timezone.utc)
            cursor = self._conn.execute(
                "INSERT INTO compaction_attempts"
                " (session_id, status, provider, model, effort, usage, reason,"
                "  started_at, finished_at)"
                " VALUES (?, 'started', ?, ?, ?, '{}', NULL, ?, NULL)",
                (session_id, provider, model, effort, now.isoformat()),
            )
            attempt_id = int(cursor.lastrowid)
        return replace(candidate, attempt_id=attempt_id, started_at=now)

    def _finish_attempt_sync(
        self,
        attempt_id: int,
        status: str,
        usage: Mapping[str, int] | None,
        reason: str | None,
    ) -> CompactionAttempt:
        usage_json = _encode_usage_json(usage)
        if reason is not None and not isinstance(reason, str):
            raise TypeError("reason must be a string or None")
        with self._conn_lock:
            self._raise_if_closed()
            now = datetime.now(timezone.utc)
            cursor = self._conn.execute(
                "UPDATE compaction_attempts SET status = ?, usage = ?, reason = ?,"
                " finished_at = ? WHERE attempt_id = ? AND status = 'started'",
                (
                    status,
                    usage_json,
                    reason[:500] if reason else None,
                    now.isoformat(),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"compaction attempt {attempt_id} is missing or already finished"
                )
            row = self._conn.execute(
                "SELECT attempt_id, session_id, status, provider, model, effort,"
                " usage, reason, started_at, finished_at"
                " FROM compaction_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return _attempt_from_row(row)

    def _commit_checkpoint_sync(
        self, checkpoint: ContextCheckpoint, expected_head_sequence: int
    ) -> ContextCheckpoint:
        self._raise_if_closed()
        # Encode before the transaction: an unserializable retained message or
        # selection must fail without changing the database.
        retained_json = json.dumps(
            _plain([dict(message) for message in checkpoint.retained_messages]),
            ensure_ascii=False,
            allow_nan=False,
        )
        selection_json = json.dumps(_plain(dict(checkpoint.selection)), ensure_ascii=False)
        usage_json = json.dumps(_plain(dict(checkpoint.usage)), ensure_ascii=False)
        created_at = checkpoint.created_at.astimezone(timezone.utc).isoformat()
        session_id = checkpoint.session_id

        with self._session_lock(session_id):
            with self._conn_lock:
                self._raise_if_closed()
                conn = self._conn
                conn.execute("BEGIN IMMEDIATE")
                try:
                    head = int(
                        conn.execute(
                            "SELECT COALESCE(MAX(sequence), 0) FROM session_events"
                            " WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()[0]
                    )
                    if head != expected_head_sequence:
                        raise CheckpointConflictError(
                            session_id,
                            f"session head moved from {expected_head_sequence} to {head}",
                        )
                    unfinished_turns, open_calls = _unfinished_state(
                        session_id, self._select_sync(session_id, None, 0)
                    )
                    if unfinished_turns or open_calls:
                        raise CheckpointConflictError(
                            session_id,
                            "session has unfinished work; compact only between turns",
                        )
                    previous = conn.execute(
                        "SELECT covers_sequence FROM session_checkpoints"
                        " WHERE session_id = ? ORDER BY checkpoint_id DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    if previous is not None and int(previous[0]) >= checkpoint.covers_sequence:
                        raise CheckpointConflictError(
                            session_id,
                            "checkpoint does not move past the previous coverage",
                        )
                    cursor = conn.execute(
                        "INSERT INTO session_checkpoints"
                        " (session_id, covers_sequence, summary, retained_messages,"
                        "  selection, usage, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            checkpoint.covers_sequence,
                            checkpoint.summary,
                            retained_json,
                            selection_json,
                            usage_json,
                            created_at,
                        ),
                    )
                    checkpoint_id = int(cursor.lastrowid)
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
        return replace(
            checkpoint,
            checkpoint_id=checkpoint_id,
            created_at=datetime.fromisoformat(created_at),
        )

    def _close_sync(self) -> None:
        with self._conn_lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

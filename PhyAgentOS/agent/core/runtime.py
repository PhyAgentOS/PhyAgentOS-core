"""Concrete :class:`~PhyAgentOS.agent.core.protocols.AgentRuntime`.

The runtime is the process-level owner of turn lifecycle. It sits between an
entry point and an injected :class:`LoopDriver`, and it is the only writer of
``input.accepted``, ``turn.started`` and terminal ``turn.*`` events. The loop
driver owns ``request.prepared``, ``model.attempt.*`` and ``context.compacted``;
the tool executor owns ``tool.*``.

Invariants this module enforces:

* One session has at most one active turn. A second ``submit`` for the same
  session waits for the active turn; different sessions run concurrently.
  Because of that, ``cancel(session_id)`` has exactly one target.
* Every ``submit`` first recovers its session. A store that implements
  ``read_startup_snapshot`` returns the recovery report and the conversation
  from one consistent read; other stores keep the explicit ``recover`` then
  ``read_conversation`` pair. Recovery always completes before the new turn's
  ``input.accepted`` event is written. Unfinished turns that the store only
  reports are closed by the runtime with ``turn.interrupted``, because the
  runtime owns terminal ``turn.*`` events. When the snapshot reports the head
  it was folded at, that close is conditional on the head: a concurrent
  terminal event makes the runtime fold a new snapshot instead of closing an
  already-completed turn.
* After recovery and before ``input.accepted``, ``submit`` loads the
  conversation projection into ``TurnStart.history``. The store owns that
  projection; the runtime never trims, merges, or generates messages. A
  failed read is a storage failure: no turn is opened and the driver is not
  called.
* Each turn gets a freshly minted ``turn_id``, :class:`CancellationToken`,
  :class:`Budget` and :class:`TurnStart`. No turn state is reused across turns.
* A turn is only recorded as ``turn.completed`` when the driver returned a
  matching result. A requested cancellation is recorded as
  ``turn.interrupted``, and a driver exception as ``turn.failed`` with the
  error text. A failed storage write is never reported as success: the runtime
  raises :class:`RuntimeStorageError` and does not call the driver after a
  start event failed to persist.
* ``stop`` rejects new submissions, cancels active turns, and waits for every
  accepted submit (queued ones included) while leaving the store open.
  ``aclose`` reuses ``stop`` and then closes the store exactly once.
* ``cancel`` only requests local cancellation and waits for the turn to settle.
  If the session is registered but no turn is published yet, it records
  ``cancel_pending`` for that accepted submit instead of losing the request.
  It never calls a capability or Forge, never claims remote work stopped, and
  never cancels an asyncio task (so a ``submit`` that is waiting in the same
  session is not cancelled).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .events import EventType, RecoveryReport, RuntimeEvent
from .protocols import LoopDriver, SessionSnapshot, SessionStore
from .types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    OperationCancelledError,
    TurnRequest,
    TurnResult,
    TurnStart,
    TurnStatus,
    require_non_empty,
)

__all__ = ["AgentRuntime", "AgentRuntimeImpl", "RuntimeStorageError"]

CANCEL_STEP_ID = "runtime/cancel"
_STARTUP_ATTEMPTS = 4
"""Scope name used by the context returned from :meth:`AgentRuntime.cancel`.

It is intentionally not shaped like a model step id: the context only exists
so the host can ask capabilities to stop, and it must never be recorded as a
model or tool step of the turn.
"""


class RuntimeStorageError(RuntimeError):
    """A runtime-owned event could not be durably stored.

    The turn cannot be reported as successful in this case: the caller must
    treat the turn as failed at the process level, even if the driver had
    finished its work. The original store error is attached as ``__cause__``.
    """

    def __init__(self, event_type: EventType | None, detail: str) -> None:
        self.event_type = event_type
        self.detail = detail
        target = "runtime event" if event_type is None else event_type.value
        super().__init__(f"runtime storage failure ({target}): {detail}")


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


@dataclass(slots=True)
class _ActiveTurn:
    """The single turn a session is currently running."""

    turn_id: str
    start: TurnStart
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: TurnStatus | None = None

    @property
    def cancellation(self) -> CancellationToken:
        return self.start.cancellation


@dataclass(slots=True)
class _SessionState:
    """Per-session serialization plus the current turn, if any.

    ``users`` counts coroutines that currently hold a reference to this state
    (submitters and cancellers). The state is removed from the registry once
    it has no users and no active turn, so locks do not accumulate for ended
    sessions and a freshly created lock can never coexist with a live waiter
    of the old one.

    ``cancel_pending`` records a cancellation that arrived after ``submit``
    registered the session but before it published ``active``. The next turn
    of this state consumes the flag exactly once; it is never inherited by a
    turn that has not been accepted yet.
    """

    session_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: _ActiveTurn | None = None
    users: int = 0
    cancel_pending: bool = False


class AgentRuntime:
    """Turn lifecycle, per-session serialization, and local cancellation.

    Construction injects the two collaborators; nothing is built or read from
    config here::

        runtime = AgentRuntime(driver=driver, store=store, max_steps=8)

    ``max_steps`` is the step ceiling of every turn's :class:`Budget`.
    ``deadline_seconds``, when given, becomes a per-turn absolute
    :func:`time.monotonic` deadline measured from the moment the turn starts.
    Token billing is deliberately absent.
    """

    def __init__(
        self,
        driver: LoopDriver,
        store: SessionStore,
        max_steps: int = 16,
        deadline_seconds: float | None = None,
    ) -> None:
        if not isinstance(max_steps, int) or isinstance(max_steps, bool):
            raise TypeError("max_steps must be an int")
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if deadline_seconds is not None:
            if not isinstance(deadline_seconds, (int, float)) or isinstance(
                deadline_seconds, bool
            ):
                raise TypeError("deadline_seconds must be a number or None")
            if deadline_seconds < 0:
                raise ValueError("deadline_seconds must be >= 0")
        self._driver = driver
        self._store = store
        self._max_steps = max_steps
        self._deadline_seconds = (
            None if deadline_seconds is None else float(deadline_seconds)
        )

        self._registry_lock = asyncio.Lock()
        self._sessions: dict[str, _SessionState] = {}
        self._inflight_submits = 0
        self._idle = asyncio.Event()
        self._idle.set()

        self._stopped = False
        self._stop_started = False
        self._stop_done = asyncio.Event()
        self._close_started = False
        self._close_done = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def submit(self, request: TurnRequest) -> TurnResult:
        """Run one turn and persist its complete event sequence.

        The durable order is ``input.accepted`` -> ``turn.started`` ->
        ``driver.run`` -> exactly one terminal ``turn.*`` event. Recovery for
        the session, including closing unfinished turns, runs before the stored
        conversation is read; ``read_conversation`` runs before any new input
        event is written and its result is handed through ``TurnStart.history``
        unchanged. A second submit for the same session waits for the current
        turn; other sessions are independent.

        Returns a :class:`TurnResult` whose status matches the persisted
        terminal event. A driver exception becomes ``failed`` with the error
        text. A requested cancellation becomes ``interrupted``. Storage
        failures raise :class:`RuntimeStorageError`; after a failed start write
        the driver is not called, and a failed terminal write is never
        reported as ``completed``.
        """
        if not isinstance(request, TurnRequest):
            raise TypeError("request must be a TurnRequest")

        state = await self._acquire_state(request.session_id, create=True, submit=True)
        active: _ActiveTurn | None = None
        try:
            async with state.lock:
                if self._stopped:
                    raise RuntimeError("agent runtime is stopped")

                history = await self._startup(request.session_id)
                if self._stopped:
                    raise RuntimeError("agent runtime is stopped")

                turn_id = f"turn-{uuid.uuid4().hex}"
                token = CancellationToken()
                budget = Budget(
                    max_steps=self._max_steps,
                    deadline=(
                        None
                        if self._deadline_seconds is None
                        else time.monotonic() + self._deadline_seconds
                    ),
                )
                # Consuming a pending cancel and publishing ``active`` happen
                # under the registry lock, which ``cancel`` and ``stop`` also
                # take. A cancel either records intent before this block (and
                # this turn consumes it) or observes ``active`` after it; it
                # can never fall between the two and be lost.
                async with self._registry_lock:
                    if self._stopped:
                        raise RuntimeError("agent runtime is stopped")
                    if state.cancel_pending:
                        state.cancel_pending = False
                        token.request("cancellation requested by runtime")
                    start = TurnStart(
                        request=request,
                        turn_id=turn_id,
                        cancellation=token,
                        budget=budget,
                        history=history,
                    )
                    active = _ActiveTurn(turn_id=turn_id, start=start)
                    state.active = active

                try:
                    await self._append_start_events(start)
                    result, event_type, payload = await self._execute(start)
                    active.status = result.status
                    await self._append_terminal(start, event_type, payload)
                    return result
                finally:
                    state.active = None
                    active.done.set()
        finally:
            await self._release_state(state, submit=True)

    async def cancel(self, session_id: str) -> ExecutionContext | None:
        """Request local cancellation for the session's active turn.

        The cancellation token is requested first. ``cancel`` then waits until
        the turn has settled *and* its terminal event has been persisted, and
        returns the runtime-scoped :class:`ExecutionContext` the host passes to
        ``capability.cancel``. It returns ``None`` when no turn was active or
        when the turn ended without local cancellation.

        If the session is registered but its accepted ``submit`` has not
        published a turn yet (it is still recovering or reading history),
        ``cancel`` records ``cancel_pending`` instead of dropping the request.
        That submit consumes the flag once, before publishing its turn, so the
        turn is recorded as interrupted without calling the driver. The flag
        never survives into a later, unrelated submit.

        The returned context uses :data:`CANCEL_STEP_ID` as ``step_id``; it is
        not a model step and must not be recorded as one. This method never
        calls a capability or Forge, never cancels an asyncio task, and does not
        assert that remote work stopped.
        """
        require_non_empty("session_id", session_id)
        state = await self._acquire_state(session_id, create=False, submit=False)
        if state is None:
            return None
        try:
            async with self._registry_lock:
                active = state.active
                if active is None:
                    state.cancel_pending = True
                    return None
            active.cancellation.request("cancellation requested by runtime")
            await active.done.wait()
            if active.status is TurnStatus.INTERRUPTED:
                return self._cancel_context(active.start)
            return None
        finally:
            await self._release_state(state, submit=False)

    async def stop(self) -> None:
        """Reject new submits, cancel active turns, wait for accepted ones.

        Submission is rejected immediately (``RuntimeError``). Every active
        turn is asked to cancel; ``stop`` then waits until no submit is in
        flight, including turns queued behind an active one, so a queued turn
        can never open after ``stop`` returns. The store stays open for
        capability calls and event reads.

        Idempotent and concurrency-safe: the first caller performs the work,
        later and concurrent callers wait for the same completion.
        """
        async with self._lifecycle_lock:
            initiator = not self._stop_started
            if initiator:
                self._stop_started = True
                self._stopped = True

        if not initiator:
            await self._stop_done.wait()
            return

        try:
            while True:
                async with self._registry_lock:
                    active_turns = [
                        state.active
                        for state in self._sessions.values()
                        if state.active is not None
                    ]
                for turn in active_turns:
                    turn.cancellation.request("runtime is stopping")
                for turn in active_turns:
                    await turn.done.wait()

                async with self._registry_lock:
                    quiet = self._inflight_submits == 0 and not any(
                        state.active is not None for state in self._sessions.values()
                    )
                if quiet:
                    break
                await self._idle.wait()
        finally:
            self._stop_done.set()

    async def aclose(self) -> None:
        """Stop the runtime, then close the store exactly once.

        ``aclose`` reuses :meth:`stop` so there is one shutdown order: active
        turns settle and queued submits finish before the store closes. The
        first caller closes the store; concurrent and later callers wait for
        that separate close step instead of returning after ``stop`` alone.
        """
        await self.stop()

        async with self._lifecycle_lock:
            initiator = not self._close_started
            if initiator:
                self._close_started = True

        if not initiator:
            await self._close_done.wait()
            return

        try:
            await self._store.aclose()
        finally:
            self._close_done.set()

    # ------------------------------------------------------------------
    # turn execution
    # ------------------------------------------------------------------
    async def _startup(self, session_id: str) -> tuple[Mapping[str, Any], ...]:
        """Recover the session and return the next turn's stored history.

        A store exposing ``read_startup_snapshot`` is asked for both values.
        Unfinished turns are closed against the head sequence the report was
        folded at; if another process changed the log first, the snapshot is
        read and folded again instead of closing an already-terminal turn.
        Any other store keeps the explicit recover-then-read path.
        """
        snapshot_reader = getattr(self._store, "read_startup_snapshot", None)
        if callable(snapshot_reader):
            for _attempt in range(_STARTUP_ATTEMPTS):
                try:
                    snapshot = await snapshot_reader(session_id)
                except Exception as exc:  # noqa: BLE001 - re-raised as a storage error
                    raise RuntimeStorageError(
                        None,
                        f"startup snapshot failed for session {session_id!r}: "
                        f"{_describe(exc)}",
                    ) from exc
                if not isinstance(snapshot, SessionSnapshot):
                    raise RuntimeStorageError(
                        None,
                        "read_startup_snapshot returned "
                        f"{type(snapshot).__name__}, expected SessionSnapshot",
                    )
                closed = await self._close_unfinished_turns(
                    session_id, snapshot.recovery, snapshot.head_sequence
                )
                if closed:
                    return tuple(snapshot.conversation)
            # A live writer kept moving the head. Fail closed: no new turn may
            # start while the report has not been persisted stably. Nothing
            # was written for this startup; the next submit retries.
            raise RuntimeStorageError(
                None,
                f"recovery conflict for session {session_id!r}: the session "
                f"head kept moving during {_STARTUP_ATTEMPTS} startup attempts; "
                "no turn was opened",
            )

        report = await self._store_recovery(session_id)
        await self._close_unfinished_turns(session_id, report)
        return await self._read_history(session_id)

    async def _store_recovery(self, session_id: str) -> RecoveryReport:
        """Ask a plain store for its recovery report (closing calls only)."""
        try:
            return await self._store.recover(session_id)
        except Exception as exc:  # noqa: BLE001 - re-raised as a storage error
            raise RuntimeStorageError(
                None,
                f"recover failed for session {session_id!r}: {_describe(exc)}",
            ) from exc

    async def _close_unfinished_turns(
        self,
        session_id: str,
        report: RecoveryReport,
        head_sequence: int | None = None,
    ) -> bool:
        """Close turns the store only reported; the runtime owns ``turn.*``.

        This keeps the reading rule intact: every stored ``turn.started`` gets
        one terminal event, and recovery never claims a crashed turn was
        completed. When the store exposes ``append_recovery`` and the snapshot
        reports its head, the close is conditional on that head: ``False``
        means another process changed the log first, so the caller must fold a
        fresh snapshot instead of closing a turn that may already be terminal.
        """
        events = [
            RuntimeEvent(
                type=EventType.TURN_INTERRUPTED,
                session_id=session_id,
                turn_id=turn_id,
                payload={
                    "recovered": True,
                    "reason": "turn was unfinished when the session reopened",
                },
            )
            for turn_id in report.unfinished_turns
        ]
        if not events:
            return True
        batch = getattr(self._store, "append_recovery", None)
        if callable(batch) and head_sequence is not None:
            try:
                saved = await batch(
                    session_id, events, expected_head=head_sequence
                )
            except Exception as exc:  # noqa: BLE001 - re-raised as a storage error
                raise RuntimeStorageError(
                    EventType.TURN_INTERRUPTED,
                    f"recovered turn in session {session_id!r}: {_describe(exc)}",
                ) from exc
            return saved is not None
        for event in events:
            try:
                await self._store.append(event)
            except Exception as exc:  # noqa: BLE001 - re-raised as a storage error
                raise RuntimeStorageError(
                    EventType.TURN_INTERRUPTED,
                    f"recovered turn {event.turn_id!r} in session "
                    f"{session_id!r}: {_describe(exc)}",
                ) from exc
        return True

    async def _read_history(
        self, session_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        """Load the stored conversation for the next turn.

        The store owns the projection; the runtime only snapshots it into
        ``TurnStart.history`` and never trims, merges, or generates messages
        itself. A failed read is a storage failure: the turn is not opened, no
        event is written, and the driver is not called.
        """
        try:
            conversation: Sequence[Mapping[str, Any]] = (
                await self._store.read_conversation(session_id)
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a storage error
            raise RuntimeStorageError(
                None,
                f"read_conversation failed for session {session_id!r}: "
                f"{_describe(exc)}",
            ) from exc
        return tuple(conversation)

    async def _append_start_events(self, start: TurnStart) -> None:
        accepted = RuntimeEvent(
            type=EventType.INPUT_ACCEPTED,
            session_id=start.request.session_id,
            payload={"characters": len(start.request.text)},
        )
        try:
            await self._store.append(accepted)
        except Exception as exc:  # noqa: BLE001 - re-raised as a storage error
            raise RuntimeStorageError(
                EventType.INPUT_ACCEPTED,
                f"session {start.request.session_id!r}: {_describe(exc)}",
            ) from exc

        started = RuntimeEvent(
            type=EventType.TURN_STARTED,
            session_id=start.request.session_id,
            turn_id=start.turn_id,
            payload={"max_steps": start.budget.max_steps},
        )
        try:
            await self._store.append(started)
        except Exception as exc:  # noqa: BLE001 - re-raised as a storage error
            raise RuntimeStorageError(
                EventType.TURN_STARTED,
                f"turn {start.turn_id!r}: {_describe(exc)}",
            ) from exc

    async def _execute(
        self, start: TurnStart
    ) -> tuple[TurnResult, EventType, dict[str, Any]]:
        """Call the driver and map its outcome to one terminal event.

        The runtime never trusts a driver result for a different turn, never
        calls the driver after a cancellation was requested, and records a
        requested cancellation as interrupted regardless of what the driver
        returned.
        """
        if start.cancellation.cancelled:
            return self._interrupted(start)

        try:
            outcome = await self._driver.run(start)
        except OperationCancelledError as exc:
            if start.cancellation.cancelled:
                return self._interrupted(start)
            return self._failed(start, _describe(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded as a failed turn
            if start.cancellation.cancelled:
                return self._interrupted(start)
            return self._failed(start, _describe(exc))

        if not isinstance(outcome, TurnResult):
            return self._failed(
                start,
                f"driver returned {type(outcome).__name__} instead of TurnResult",
            )
        if (
            outcome.session_id != start.request.session_id
            or outcome.turn_id != start.turn_id
        ):
            return self._failed(
                start,
                "driver returned a result for a different turn: "
                f"{outcome.session_id!r}/{outcome.turn_id!r}",
            )
        if start.cancellation.cancelled:
            return self._interrupted(start)

        if outcome.status is TurnStatus.COMPLETED:
            return self._completed(start, outcome.reply)
        if outcome.status is TurnStatus.INTERRUPTED:
            return self._interrupted(start)
        return self._failed(
            start, outcome.error or "driver reported a failure without an error"
        )

    def _completed(
        self, start: TurnStart, reply: str | None
    ) -> tuple[TurnResult, EventType, dict[str, Any]]:
        result = TurnResult(
            session_id=start.request.session_id,
            turn_id=start.turn_id,
            status=TurnStatus.COMPLETED,
            reply=reply,
        )
        payload: dict[str, Any] = {} if reply is None else {"reply": reply}
        return result, EventType.TURN_COMPLETED, payload

    def _interrupted(
        self, start: TurnStart
    ) -> tuple[TurnResult, EventType, dict[str, Any]]:
        result = TurnResult(
            session_id=start.request.session_id,
            turn_id=start.turn_id,
            status=TurnStatus.INTERRUPTED,
        )
        reason = start.cancellation.reason
        payload: dict[str, Any] = {} if reason is None else {"reason": reason}
        return result, EventType.TURN_INTERRUPTED, payload

    def _failed(
        self, start: TurnStart, error: str
    ) -> tuple[TurnResult, EventType, dict[str, Any]]:
        message = error or "turn failed"
        result = TurnResult(
            session_id=start.request.session_id,
            turn_id=start.turn_id,
            status=TurnStatus.FAILED,
            error=message,
        )
        return result, EventType.TURN_FAILED, {"error": message}

    async def _append_terminal(
        self, start: TurnStart, event_type: EventType, payload: dict[str, Any]
    ) -> None:
        event = RuntimeEvent(
            type=event_type,
            session_id=start.request.session_id,
            turn_id=start.turn_id,
            payload=payload,
        )
        try:
            await self._store.append(event)
        except Exception as exc:  # noqa: BLE001 - re-raised as a storage error
            if event_type is not EventType.TURN_FAILED:
                await self._best_effort_failure(start, exc)
            raise RuntimeStorageError(
                event_type, f"turn {start.turn_id!r}: {_describe(exc)}"
            ) from exc

    async def _best_effort_failure(
        self, start: TurnStart, cause: BaseException
    ) -> None:
        """Try to leave a failure record when the intended terminal write failed.

        This never fabricates success and never masks the original error: any
        failure here is swallowed because the caller already raises the
        storage error for the original terminal event.
        """
        try:
            await self._store.append(
                RuntimeEvent(
                    type=EventType.TURN_FAILED,
                    session_id=start.request.session_id,
                    turn_id=start.turn_id,
                    payload={
                        "error": f"terminal event could not be stored: {_describe(cause)}"
                    },
                )
            )
        except Exception:  # noqa: BLE001 - best effort only
            return

    def _cancel_context(self, start: TurnStart) -> ExecutionContext:
        return ExecutionContext(
            session_id=start.request.session_id,
            turn_id=start.turn_id,
            step_id=CANCEL_STEP_ID,
            cancellation=start.cancellation,
            budget=start.budget,
            policy=start.policy,
            reply_target=start.request.reply_target,
            parent_session_id=start.request.parent_session_id,
            domain_refs=start.domain_refs,
        )

    # ------------------------------------------------------------------
    # session registry
    # ------------------------------------------------------------------
    async def _acquire_state(
        self, session_id: str, *, create: bool, submit: bool
    ) -> _SessionState | None:
        async with self._registry_lock:
            if submit and self._stopped:
                raise RuntimeError("agent runtime is stopped")
            state = self._sessions.get(session_id)
            if state is None:
                if not create:
                    return None
                state = _SessionState(session_id=session_id)
                self._sessions[session_id] = state
            state.users += 1
            if submit:
                self._inflight_submits += 1
                self._idle.clear()
            return state

    async def _release_state(self, state: _SessionState, *, submit: bool) -> None:
        async with self._registry_lock:
            state.users -= 1
            if submit:
                self._inflight_submits -= 1
                if self._inflight_submits == 0:
                    self._idle.set()
            if state.users == 0 and state.active is None:
                if self._sessions.get(state.session_id) is state:
                    del self._sessions[state.session_id]


# The protocols module uses this name in its assembly sketch; keep both
# spellings so integrators can follow either document.
AgentRuntimeImpl = AgentRuntime

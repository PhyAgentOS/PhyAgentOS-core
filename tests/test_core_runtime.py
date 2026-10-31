"""Tests for the concrete agent runtime.

The fakes here implement the published Protocols only (``LoopDriver``,
``SessionStore``), so these tests exercise the runtime's lifecycle rules
without a provider, Forge, or a real database.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

import pytest

import PhyAgentOS.agent.core.protocols as core_protocols
from PhyAgentOS.agent.core import (
    CallKind,
    EventType,
    LoopDriver,
    RecoveryReport,
    RuntimeEvent,
    SessionStore,
    TurnRequest,
    TurnResult,
    TurnStart,
    TurnStatus,
    UnfinishedCall,
)
from PhyAgentOS.agent.core.runtime import (
    CANCEL_STEP_ID,
    AgentRuntime,
    RuntimeStorageError,
)


def _request(session: str = "session-1", text: str = "hello") -> TurnRequest:
    return TurnRequest(session_id=session, text=text)


def _result(
    turn: TurnStart,
    *,
    status: TurnStatus = TurnStatus.COMPLETED,
    reply: str | None = None,
    error: str | None = None,
) -> TurnResult:
    return TurnResult(
        session_id=turn.request.session_id,
        turn_id=turn.turn_id,
        status=status,
        reply=reply,
        error=error,
    )


def _types(store: FakeStore) -> list[EventType]:
    return [event.type for event in store.events]


class FakeStore:
    """In-memory SessionStore with failure injection and an operation log."""

    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []
        self.log: list[tuple[str, object]] = []
        self.fail_types: dict[EventType, int] = {}
        self.recover_reports: dict[str, RecoveryReport] = {}
        self.recover_calls: list[str] = []
        self.conversations: dict[str, list[dict[str, Any]]] = {}
        self.read_conversation_calls: list[str] = []
        self.fail_read_conversation = 0
        self.recover_entered: asyncio.Event | None = None
        self.recover_gate: asyncio.Event | None = None
        self.read_entered: asyncio.Event | None = None
        self.read_gate: asyncio.Event | None = None
        self.close_entered: asyncio.Event | None = None
        self.close_gate: asyncio.Event | None = None
        self.closed = 0

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        self.log.append(("append", event.type))
        remaining = self.fail_types.get(event.type, 0)
        if remaining > 0:
            self.fail_types[event.type] = remaining - 1
            raise OSError(f"disk unavailable while storing {event.type.value}")
        stored = replace(event, sequence=len(self.events))
        self.events.append(stored)
        return stored

    async def read_turn(self, session_id: str, turn_id: str) -> list[RuntimeEvent]:
        return [
            event
            for event in self.events
            if event.session_id == session_id and event.turn_id == turn_id
        ]

    async def read_session(
        self, session_id: str, *, after_sequence: int = 0
    ) -> list[RuntimeEvent]:
        return [
            event
            for event in self.events
            if event.session_id == session_id
            and event.sequence is not None
            and event.sequence >= after_sequence
        ]

    async def recover(self, session_id: str) -> RecoveryReport:
        self.log.append(("recover", session_id))
        self.recover_calls.append(session_id)
        if self.recover_entered is not None:
            self.recover_entered.set()
        if self.recover_gate is not None:
            await self.recover_gate.wait()
        report = self.recover_reports.pop(
            session_id, RecoveryReport(session_id=session_id)
        )
        for event in report.interruption_events():
            await self.append(event)
        return report

    async def read_conversation(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        self.log.append(("read_conversation", session_id))
        self.read_conversation_calls.append(session_id)
        if self.read_entered is not None:
            self.read_entered.set()
        if self.read_gate is not None:
            await self.read_gate.wait()
        if self.fail_read_conversation > 0:
            self.fail_read_conversation -= 1
            raise OSError("conversation projection unavailable")
        if session_id in self.conversations:
            return [dict(message) for message in self.conversations[session_id]]
        return self._project_conversation(session_id)

    def _project_conversation(self, session_id: str) -> list[dict[str, Any]]:
        """Minimal projection of the newest completed turn, like the real store."""
        completed = [
            event
            for event in self.events
            if event.session_id == session_id
            and event.type is EventType.TURN_COMPLETED
        ]
        if not completed:
            return []
        last = completed[-1]
        prepared = [
            event
            for event in self.events
            if event.session_id == session_id
            and event.turn_id == last.turn_id
            and event.type is EventType.REQUEST_PREPARED
        ]
        if not prepared:
            return []
        messages = [
            dict(message) for message in prepared[-1].payload.get("messages", ())
        ]
        while messages and messages[0].get("role") == "system":
            messages.pop(0)
        reply = last.payload.get("reply")
        if reply:
            messages.append({"role": "assistant", "content": reply})
        return messages

    async def aclose(self) -> None:
        if self.close_entered is not None:
            self.close_entered.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed += 1


class StaticDriver:
    def __init__(
        self,
        *,
        reply: str | None = "done",
        status: TurnStatus = TurnStatus.COMPLETED,
        error: str | None = None,
        delay: float = 0.0,
    ) -> None:
        self.reply = reply
        self.status = status
        self.error = error
        self.delay = delay
        self.turns: list[TurnStart] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.turns.append(turn)
        if self.delay:
            await asyncio.sleep(self.delay)
        return _result(turn, status=self.status, reply=self.reply, error=self.error)


class RaisingDriver:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def run(self, turn: TurnStart) -> TurnResult:
        self.calls += 1
        raise self.exc


class CancellableDriver:
    """Waits for the runtime token and reports interruption."""

    def __init__(
        self, *, fallback: TurnStatus = TurnStatus.INTERRUPTED
    ) -> None:
        self.started = asyncio.Event()
        self.fallback = fallback
        self.turns: list[TurnStart] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.turns.append(turn)
        self.started.set()
        await turn.cancellation.wait()
        return _result(turn, status=self.fallback, reply="late")


class ScriptedDriver:
    """First turn waits for cancellation; later turns complete immediately."""

    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.calls = 0
        self.turns: list[TurnStart] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.calls += 1
        self.turns.append(turn)
        if self.calls == 1:
            self.first_started.set()
            await turn.cancellation.wait()
            return _result(turn, status=TurnStatus.INTERRUPTED)
        return _result(turn, status=TurnStatus.COMPLETED, reply=turn.request.text)


class ConcurrencyDriver:
    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.order: list[tuple[str, str]] = []
        self.turns: list[TurnStart] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.turns.append(turn)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.order.append(("start", turn.turn_id))
        await asyncio.sleep(self.delay)
        self.order.append(("end", turn.turn_id))
        self.active -= 1
        return _result(
            turn, status=TurnStatus.COMPLETED, reply=turn.request.text
        )


class BarrierDriver:
    """Both runs must be in flight at the same time or the test times out."""

    def __init__(self, expected: int = 2, timeout: float = 0.5) -> None:
        self.expected = expected
        self.timeout = timeout
        self.started = 0
        self.ready = asyncio.Event()

    async def run(self, turn: TurnStart) -> TurnResult:
        self.started += 1
        if self.started >= self.expected:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=self.timeout)
        return _result(turn, status=TurnStatus.COMPLETED, reply="parallel")


class EventWritingDriver:
    """Writes a driver-owned event before finishing."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.seen: list[list[EventType]] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.seen.append([event.type for event in self.store.events])
        await self.store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_STARTED,
                session_id=turn.request.session_id,
                turn_id=turn.turn_id,
                step_id="model-step-1",
                payload={"model_call_id": "model-1"},
            )
        )
        return _result(turn, status=TurnStatus.COMPLETED, reply="done")


class ConversationDriver:
    """Writes a driver-owned request snapshot so the store can project history."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.turns: list[TurnStart] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.turns.append(turn)
        messages = [dict(message) for message in turn.history]
        messages.append({"role": "user", "content": turn.request.text})
        await self.store.append(
            RuntimeEvent(
                type=EventType.REQUEST_PREPARED,
                session_id=turn.request.session_id,
                turn_id=turn.turn_id,
                step_id="step-1",
                payload={"messages": messages, "model_call_id": "model-1"},
            )
        )
        return _result(
            turn,
            status=TurnStatus.COMPLETED,
            reply=f"reply:{turn.request.text}",
        )


def _runtime(driver: object, store: FakeStore, **overrides: object) -> AgentRuntime:
    return AgentRuntime(driver=driver, store=store, **overrides)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# lifecycle
# ----------------------------------------------------------------------
async def test_direct_completion_writes_ordered_turn_events() -> None:
    store = FakeStore()
    driver = StaticDriver(reply="the answer")
    runtime = _runtime(driver, store, max_steps=3)

    result = await runtime.submit(_request())

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "the answer"
    assert _types(store) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.TURN_COMPLETED,
    ]
    accepted, started, completed = store.events
    assert accepted.turn_id is None
    assert started.turn_id == result.turn_id
    assert completed.turn_id == result.turn_id
    assert [event.sequence for event in store.events] == [0, 1, 2]
    # Recovery and the conversation read both happen before the new turn opens.
    assert store.log[:3] == [
        ("recover", "session-1"),
        ("read_conversation", "session-1"),
        ("append", EventType.INPUT_ACCEPTED),
    ]

    turn = driver.turns[0]
    assert turn.turn_id != ""
    assert turn.request.session_id == "session-1"
    assert turn.cancellation.cancelled is False
    assert turn.budget.max_steps == 3


async def test_each_turn_gets_fresh_identity_token_and_budget() -> None:
    store = FakeStore()
    driver = StaticDriver()
    runtime = _runtime(driver, store, max_steps=2, deadline_seconds=30)

    await runtime.submit(_request(text="first"))
    await runtime.submit(_request(text="second"))

    first, second = driver.turns
    assert first.turn_id != second.turn_id
    assert first.cancellation is not second.cancellation
    assert first.budget is not second.budget
    assert first.budget.deadline is not None
    assert first.budget.remaining_seconds() > 0
    assert first.budget.deadline <= time.monotonic() + 30


async def test_stored_history_is_passed_to_turn_start_unchanged() -> None:
    store = FakeStore()
    seeded = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    store.conversations["session-1"] = [dict(message) for message in seeded]
    driver = StaticDriver()
    runtime = _runtime(driver, store)

    await runtime.submit(_request(text="new question"))

    turn = driver.turns[0]
    assert [dict(message) for message in turn.history] == seeded
    assert store.read_conversation_calls == ["session-1"]
    # The runtime does not trim system messages or add the current input.
    assert turn.history[0]["role"] == "system"

    seeded[0]["content"] = "mutated after the turn"
    assert turn.history[0]["content"] == "system prompt"


async def test_second_turn_of_the_same_session_carries_first_turn_history() -> None:
    store = FakeStore()
    driver = ConversationDriver(store)
    runtime = _runtime(driver, store)

    await runtime.submit(_request(text="first"))
    await runtime.submit(_request(text="second"))

    first, second = driver.turns
    assert first.history == ()
    assert [dict(message) for message in second.history] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply:first"},
    ]
    assert store.read_conversation_calls == ["session-1", "session-1"]


async def test_conversation_read_failure_opens_no_turn_and_skips_driver() -> None:
    store = FakeStore()
    store.fail_read_conversation = 1
    driver = StaticDriver()
    runtime = _runtime(driver, store)

    with pytest.raises(RuntimeStorageError) as excinfo:
        await runtime.submit(_request())

    assert excinfo.value.event_type is None
    assert "read_conversation" in str(excinfo.value)
    assert driver.turns == []
    assert store.events == []
    assert store.log == [
        ("recover", "session-1"),
        ("read_conversation", "session-1"),
    ]


async def test_driver_exception_becomes_failed_turn_with_error() -> None:
    store = FakeStore()
    driver = RaisingDriver(ValueError("boom"))
    runtime = _runtime(driver, store)

    result = await runtime.submit(_request())

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "boom" in result.error
    assert _types(store) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.TURN_FAILED,
    ]
    assert "boom" in store.events[-1].payload["error"]


async def test_driver_result_for_another_turn_is_not_trusted() -> None:
    store = FakeStore()

    class WrongTurnDriver:
        async def run(self, turn: TurnStart) -> TurnResult:
            return TurnResult(
                session_id="other-session",
                turn_id="other-turn",
                status=TurnStatus.COMPLETED,
                reply="wrong",
            )

    result = await _runtime(WrongTurnDriver(), store).submit(_request())

    assert result.status is TurnStatus.FAILED
    assert store.events[-1].type is EventType.TURN_FAILED


# ----------------------------------------------------------------------
# concurrency
# ----------------------------------------------------------------------
async def test_same_session_turns_run_strictly_serially() -> None:
    store = FakeStore()
    driver = ConcurrencyDriver(delay=0.02)
    runtime = _runtime(driver, store)

    results = await asyncio.gather(
        runtime.submit(_request(text="first")),
        runtime.submit(_request(text="second")),
    )

    assert [result.status for result in results] == [
        TurnStatus.COMPLETED,
        TurnStatus.COMPLETED,
    ]
    assert driver.peak == 1
    first_id, second_id = (turn.turn_id for turn in driver.turns)
    assert driver.order == [
        ("start", first_id),
        ("end", first_id),
        ("start", second_id),
        ("end", second_id),
    ]
    first_events = [
        index
        for index, event in enumerate(store.events)
        if event.turn_id == first_id
    ]
    second_events = [
        index
        for index, event in enumerate(store.events)
        if event.turn_id == second_id
    ]
    assert max(first_events) < min(second_events)


async def test_different_sessions_run_concurrently() -> None:
    store = FakeStore()
    driver = BarrierDriver(expected=2)
    runtime = _runtime(driver, store)

    results = await asyncio.gather(
        runtime.submit(_request(session="session-a")),
        runtime.submit(_request(session="session-b")),
    )

    assert [result.status for result in results] == [
        TurnStatus.COMPLETED,
        TurnStatus.COMPLETED,
    ]
    assert driver.started == 2


# ----------------------------------------------------------------------
# cancellation
# ----------------------------------------------------------------------
async def test_cancel_waits_for_terminal_event_and_returns_cancel_context() -> None:
    store = FakeStore()
    driver = CancellableDriver()
    runtime = _runtime(driver, store)

    submit_task = asyncio.create_task(runtime.submit(_request()))
    await driver.started.wait()

    context = await runtime.cancel("session-1")

    assert context is not None
    assert context.session_id == "session-1"
    assert context.turn_id == driver.turns[0].turn_id
    assert context.step_id == CANCEL_STEP_ID
    assert "runtime/cancel" in context.step_id
    assert context.cancellation.cancelled
    assert context.budget is driver.turns[0].budget
    # The terminal event was already persisted when cancel returned.
    assert store.events[-1].type is EventType.TURN_INTERRUPTED
    assert store.events[-1].turn_id == context.turn_id

    result = await submit_task
    assert result.status is TurnStatus.INTERRUPTED
    assert not submit_task.cancelled()


async def test_cancel_wins_even_if_driver_returns_completed() -> None:
    store = FakeStore()
    driver = CancellableDriver(fallback=TurnStatus.COMPLETED)
    runtime = _runtime(driver, store)

    submit_task = asyncio.create_task(runtime.submit(_request()))
    await driver.started.wait()
    context = await runtime.cancel("session-1")
    result = await submit_task

    assert context is not None
    assert result.status is TurnStatus.INTERRUPTED
    assert store.events[-1].type is EventType.TURN_INTERRUPTED
    assert all(event.type is not EventType.TURN_COMPLETED for event in store.events)


async def test_cancel_returns_none_without_an_active_turn() -> None:
    store = FakeStore()
    driver = StaticDriver()
    runtime = _runtime(driver, store)

    assert await runtime.cancel("unknown-session") is None

    await runtime.submit(_request())
    assert await runtime.cancel("session-1") is None

    # A cancel that found no active turn must not leak into the next submit.
    result = await runtime.submit(_request(text="next"))
    assert result.status is TurnStatus.COMPLETED
    assert len(driver.turns) == 2


@pytest.mark.parametrize("stage", ["recover", "read_conversation"])
async def test_cancel_during_startup_is_recorded_and_skips_driver(
    stage: str,
) -> None:
    store = FakeStore()
    driver = StaticDriver()
    runtime = _runtime(driver, store)

    if stage == "recover":
        store.recover_entered = asyncio.Event()
        store.recover_gate = asyncio.Event()
        entered, gate = store.recover_entered, store.recover_gate
    else:
        store.read_entered = asyncio.Event()
        store.read_gate = asyncio.Event()
        entered, gate = store.read_entered, store.read_gate

    submit_task = asyncio.create_task(runtime.submit(_request()))
    await entered.wait()

    # No turn is published yet, so there is no context to return, but the
    # cancellation must be remembered for the accepted submit.
    assert await runtime.cancel("session-1") is None
    assert not submit_task.done()
    gate.set()

    result = await submit_task

    assert result.status is TurnStatus.INTERRUPTED
    assert driver.turns == []
    assert _types(store) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.TURN_INTERRUPTED,
    ]


async def test_cancel_targets_active_turn_without_cancelling_queued_submit() -> None:
    store = FakeStore()
    driver = ScriptedDriver()
    runtime = _runtime(driver, store)

    first = asyncio.create_task(runtime.submit(_request(text="first")))
    await driver.first_started.wait()
    second = asyncio.create_task(runtime.submit(_request(text="second")))
    await asyncio.sleep(0)  # let the second submit queue on the session lock

    context = await runtime.cancel("session-1")
    first_result = await first
    second_result = await second

    assert context is not None
    assert context.turn_id == driver.turns[0].turn_id
    assert first_result.status is TurnStatus.INTERRUPTED
    assert second_result.status is TurnStatus.COMPLETED
    assert second_result.reply == "second"
    assert not second.cancelled()


# ----------------------------------------------------------------------
# events, recovery, and storage failures
# ----------------------------------------------------------------------
async def test_runtime_writes_only_its_events_in_contract_order() -> None:
    store = FakeStore()
    driver = EventWritingDriver(store)
    runtime = _runtime(driver, store, max_steps=4)

    await runtime.submit(_request())

    assert driver.seen == [[EventType.INPUT_ACCEPTED, EventType.TURN_STARTED]]
    assert _types(store) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.TURN_COMPLETED,
    ]
    # The driver-owned event is present and the runtime neither duplicated it
    # nor replaced it with a turn event of its own.
    assert _types(store).count(EventType.MODEL_ATTEMPT_STARTED) == 1
    assert _types(store).count(EventType.TURN_COMPLETED) == 1


async def test_recovery_runs_before_every_new_turn_and_is_idempotent() -> None:
    store = FakeStore()
    store.recover_reports["session-1"] = RecoveryReport(
        session_id="session-1",
        unfinished_turns=("turn-old",),
        unfinished_calls=(
            UnfinishedCall(
                session_id="session-1",
                turn_id="turn-old",
                step_id="step-1",
                kind=CallKind.MODEL,
                call_id="model-old",
            ),
        ),
    )
    runtime = _runtime(StaticDriver(), store)

    await runtime.submit(_request(text="first"))
    await runtime.submit(_request(text="second"))

    assert store.recover_calls == ["session-1", "session-1"]
    # Recovery closes the reported unfinished turn before the new turn opens.
    recovery_types = [event.type for event in store.events[:3]]
    assert recovery_types == [
        EventType.MODEL_ATTEMPT_INTERRUPTED,
        EventType.TURN_INTERRUPTED,
        EventType.INPUT_ACCEPTED,
    ]
    recovered_turn = store.events[1]
    assert recovered_turn.turn_id == "turn-old"
    assert recovered_turn.payload["recovered"] is True
    # The second submit finds nothing unfinished and appends no recovery events.
    assert _types(store).count(EventType.MODEL_ATTEMPT_INTERRUPTED) == 1
    assert _types(store).count(EventType.TURN_INTERRUPTED) == 1
    assert store.log[:5] == [
        ("recover", "session-1"),
        ("append", EventType.MODEL_ATTEMPT_INTERRUPTED),
        ("append", EventType.TURN_INTERRUPTED),
        ("read_conversation", "session-1"),
        ("append", EventType.INPUT_ACCEPTED),
    ]


@pytest.mark.parametrize(
    "failing_type",
    [EventType.INPUT_ACCEPTED, EventType.TURN_STARTED],
)
async def test_start_event_write_failure_raises_and_skips_driver(
    failing_type: EventType,
) -> None:
    store = FakeStore()
    store.fail_types[failing_type] = 1
    driver = StaticDriver()
    runtime = _runtime(driver, store)

    with pytest.raises(RuntimeStorageError) as excinfo:
        await runtime.submit(_request())

    assert excinfo.value.event_type is failing_type
    assert failing_type.value in str(excinfo.value)
    assert driver.turns == []
    assert EventType.TURN_FAILED not in _types(store)
    assert EventType.TURN_COMPLETED not in _types(store)
    assert EventType.TURN_INTERRUPTED not in _types(store)
    if failing_type is EventType.TURN_STARTED:
        assert _types(store) == [EventType.INPUT_ACCEPTED]


async def test_terminal_write_failure_is_not_reported_as_completed() -> None:
    store = FakeStore()
    store.fail_types[EventType.TURN_COMPLETED] = 1
    driver = StaticDriver(reply="looks fine")
    runtime = _runtime(driver, store)

    with pytest.raises(RuntimeStorageError) as excinfo:
        await runtime.submit(_request())

    assert excinfo.value.event_type is EventType.TURN_COMPLETED
    assert EventType.TURN_COMPLETED not in _types(store)
    assert store.events[-1].type is EventType.TURN_FAILED
    assert "could not be stored" in store.events[-1].payload["error"]


# ----------------------------------------------------------------------
# shutdown and session bookkeeping
# ----------------------------------------------------------------------
async def test_aclose_cancels_active_turn_waits_then_closes_store_once() -> None:
    store = FakeStore()
    driver = CancellableDriver()
    runtime = _runtime(driver, store)

    submit_task = asyncio.create_task(runtime.submit(_request()))
    await driver.started.wait()

    await runtime.aclose()
    result = await submit_task

    assert result.status is TurnStatus.INTERRUPTED
    assert store.events[-1].type is EventType.TURN_INTERRUPTED
    assert store.closed == 1

    await runtime.aclose()
    assert store.closed == 1
    with pytest.raises(RuntimeError):
        await runtime.submit(_request())


async def test_concurrent_aclose_calls_close_the_store_once() -> None:
    store = FakeStore()
    runtime = _runtime(StaticDriver(), store)
    await runtime.submit(_request())

    await asyncio.gather(runtime.aclose(), runtime.aclose(), runtime.aclose())

    assert store.closed == 1
    assert runtime._sessions == {}


async def test_ended_session_state_is_cleaned_up() -> None:
    store = FakeStore()
    runtime = _runtime(StaticDriver(), store)

    for index in range(5):
        await runtime.submit(_request(session=f"session-{index}"))

    assert runtime._sessions == {}
    assert await runtime.cancel("session-0") is None
    assert runtime._sessions == {}


async def test_stop_prevents_queued_submit_from_opening_a_turn() -> None:
    store = FakeStore()
    driver = ScriptedDriver()
    runtime = _runtime(driver, store)

    first = asyncio.create_task(runtime.submit(_request(text="first")))
    await driver.first_started.wait()
    second = asyncio.create_task(runtime.submit(_request(text="second")))
    await asyncio.sleep(0)  # the second submit is queued on the session lock

    await runtime.stop()

    first_result = await first
    with pytest.raises(RuntimeError):
        await second

    assert first_result.status is TurnStatus.INTERRUPTED
    assert driver.calls == 1
    assert _types(store).count(EventType.INPUT_ACCEPTED) == 1
    assert _types(store).count(EventType.TURN_STARTED) == 1
    assert _types(store).count(EventType.TURN_INTERRUPTED) == 1
    # stop keeps the store open so events remain readable.
    assert store.closed == 0
    assert await store.read_session("session-1")

    await runtime.aclose()
    assert store.closed == 1


async def test_stop_is_idempotent_and_aclose_waits_for_the_store_close() -> None:
    store = FakeStore()
    runtime = _runtime(StaticDriver(), store)
    await runtime.submit(_request())

    await asyncio.gather(runtime.stop(), runtime.stop())

    assert store.closed == 0
    assert await store.read_session("session-1")
    with pytest.raises(RuntimeError):
        await runtime.submit(_request(text="late"))
    assert store.closed == 0

    store.close_entered = asyncio.Event()
    store.close_gate = asyncio.Event()
    first_close = asyncio.create_task(runtime.aclose())
    await store.close_entered.wait()
    second_close = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)
    # A concurrent aclose must wait for the store close, not return after stop.
    assert not second_close.done()

    store.close_gate.set()
    await asyncio.gather(first_close, second_close)
    assert store.closed == 1

    await runtime.stop()
    await runtime.aclose()
    assert store.closed == 1


def test_runtime_satisfies_the_published_protocol() -> None:
    runtime = _runtime(StaticDriver(), FakeStore())
    assert isinstance(runtime, core_protocols.AgentRuntime)
    assert isinstance(StaticDriver(), LoopDriver)
    assert isinstance(FakeStore(), SessionStore)


class FailedWithReplyDriver:
    """A failed turn may still return partial progress as data."""

    async def run(self, turn: TurnStart) -> TurnResult:
        return _result(
            turn,
            status=TurnStatus.FAILED,
            reply="partial progress",
            error="budget exhausted",
        )


async def test_failed_driver_reply_is_kept_without_becoming_success() -> None:
    store = FakeStore()
    runtime = _runtime(FailedWithReplyDriver(), store)

    result = await runtime.submit(_request())

    assert result.status is TurnStatus.FAILED
    assert result.reply == "partial progress"
    assert result.error == "budget exhausted"
    assert _types(store)[-1] is EventType.TURN_FAILED
    assert store.events[-1].payload["error"] == "budget exhausted"
    assert store.events[-1].payload["reply"] == "partial progress"
    assert EventType.TURN_COMPLETED not in _types(store)

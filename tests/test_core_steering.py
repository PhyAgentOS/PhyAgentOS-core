"""Tests for bounded step-boundary corrections and their event contract.

These tests use only the core Protocols plus the real ``TurnControl``: a fake
driver drains the same object the runtime opened, so no model, tool, Forge, or
provider is involved.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from PhyAgentOS.agent.core.events import EventType, RecoveryReport, RuntimeEvent
from PhyAgentOS.agent.core.runtime import AgentRuntime
from PhyAgentOS.agent.core.types import (
    TurnRequest,
    TurnResult,
    TurnStart,
    TurnStatus,
)
from PhyAgentOS.agent.services.turn_control import (
    TurnControl,
    TurnControlClosedError,
    TurnControlInactiveError,
    TurnControlLimitError,
)


class FakeStore:
    """Minimal in-memory SessionStore used by the runtime."""

    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []
        self.closed = 0
        self.conversation: dict[str, list[dict[str, Any]]] = {}

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
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
        return [event for event in self.events if event.session_id == session_id]

    async def read_conversation(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        return [dict(message) for message in self.conversation.get(session_id, ())]

    async def recover(self, session_id: str) -> RecoveryReport:
        return RecoveryReport(session_id=session_id)

    async def aclose(self) -> None:
        self.closed += 1


class _WritingDriver:
    """Consume the same control object at a fake step boundary."""

    def __init__(self, control: TurnControl, store: FakeStore) -> None:
        self.steering = control
        self.store = store
        self.started = asyncio.Event()
        self.drain_now = asyncio.Event()
        self.drained: list[tuple[str, ...]] = []
        self.turns: list[TurnStart] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.turns.append(turn)
        step_id = f"{turn.turn_id}:step:1"
        self.started.set()
        await self.drain_now.wait()
        texts = self.steering.drain(turn.request.session_id, turn.turn_id)
        self.drained.append(texts)
        for text in texts:
            await self.store.append(
                RuntimeEvent(
                    type=EventType.INPUT_STEERED,
                    session_id=turn.request.session_id,
                    turn_id=turn.turn_id,
                    step_id=step_id,
                    payload={"text": text},
                )
            )
        return TurnResult(
            session_id=turn.request.session_id,
            turn_id=turn.turn_id,
            status=TurnStatus.COMPLETED,
            reply="ok",
        )


# ----------------------------------------------------------------------
# event contract
# ----------------------------------------------------------------------
def test_input_steered_is_turn_and_step_scoped() -> None:
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.INPUT_STEERED,
            session_id="s",
            turn_id="t1",
            payload={"text": "do this instead"},
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.INPUT_STEERED,
            session_id="s",
            step_id="step-1",
            payload={"text": "do this instead"},
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.INPUT_STEERED,
            session_id="s",
            turn_id="t1",
            step_id="step-1",
            payload={"text": "   "},
        )

    event = RuntimeEvent(
        type=EventType.INPUT_STEERED,
        session_id="s",
        turn_id="t1",
        step_id="step-1",
        payload={"text": "do this instead"},
    )
    assert event.payload["text"] == "do this instead"
    assert event.turn_id == "t1"
    assert event.step_id == "step-1"


# ----------------------------------------------------------------------
# queue behaviour
# ----------------------------------------------------------------------
def test_empty_text_is_refused_before_it_can_reach_a_turn() -> None:
    control = TurnControl()
    control.begin_turn("s", "t1")
    for text in ("", "   ", "\n\t"):
        with pytest.raises(ValueError):
            control.enqueue("s", "t1", text)
    assert control.drain("s", "t1") == ()


def test_only_the_active_turn_accepts_text() -> None:
    control = TurnControl()
    with pytest.raises(TurnControlInactiveError):
        control.enqueue("s", "t1", "text")
    control.begin_turn("s", "t1")
    with pytest.raises(TurnControlInactiveError):
        control.enqueue("s", "t2", "text")
    with pytest.raises(TurnControlInactiveError):
        control.enqueue("other", "t1", "text")
    control.end_turn("s", "t1")
    with pytest.raises(TurnControlInactiveError):
        control.enqueue("s", "t1", "text")


def test_drain_is_fifo_and_isolated_by_session_and_turn() -> None:
    control = TurnControl()
    control.begin_turn("s1", "t1")
    control.begin_turn("s2", "t2")
    control.enqueue("s1", "t1", "first")
    control.enqueue("s1", "t1", "second")
    control.enqueue("s2", "t2", "other")

    assert control.drain("s1", "wrong") == ()
    assert control.pending("s1", "t1") == 2
    assert control.drain("s1", "t1") == ("first", "second")
    assert control.pending("s1", "t1") == 0
    assert control.drain("s2", "t2") == ("other",)
    assert control.drain("s2", "t2") == ()


def test_queue_is_bounded_by_item_count_and_characters() -> None:
    control = TurnControl(max_pending=2, max_text_chars=5, max_total_chars=7)
    control.begin_turn("s", "t1")
    with pytest.raises(TurnControlLimitError):
        control.enqueue("s", "t1", "123456")
    control.enqueue("s", "t1", "1234")
    control.enqueue("s", "t1", "123")
    with pytest.raises(TurnControlLimitError):
        control.enqueue("s", "t1", "1")
    control.end_turn("s", "t1")


def test_end_and_close_discard_undrained_text() -> None:
    control = TurnControl()
    control.begin_turn("s", "t1")
    control.enqueue("s", "t1", "never consumed")
    assert control.unapplied("s", "t1") == ("never consumed",)
    assert control.end_turn("s", "t1") == ("never consumed",)
    assert control.end_turn("s", "t1") == ()
    assert control.active_turn_id("s") is None
    assert control.drain("s", "t1") == ()

    control.begin_turn("s", "t2")
    control.enqueue("s", "t2", "also lost")
    control.close()
    assert control.closed is True
    assert control.drain("s", "t2") == ()
    with pytest.raises(TurnControlClosedError):
        control.enqueue("s", "t2", "late")
    with pytest.raises(TurnControlClosedError):
        control.begin_turn("s", "t3")


# ----------------------------------------------------------------------
# runtime integration
# ----------------------------------------------------------------------
async def test_runtime_steer_reaches_the_drain_at_the_next_step() -> None:
    store = FakeStore()
    control = TurnControl()
    driver = _WritingDriver(control, store)
    runtime = AgentRuntime(driver=driver, store=store, steering=control)

    task = asyncio.create_task(
        runtime.submit(TurnRequest(session_id="s", text="start"))
    )
    await driver.started.wait()
    turn_id = await runtime.steer("s", "turn left instead")
    assert turn_id == driver.turns[0].turn_id
    assert control.active_turn_id("s") == turn_id
    driver.drain_now.set()
    result = await task

    assert result.status is TurnStatus.COMPLETED
    assert driver.drained == [("turn left instead",)]
    steered = [
        event for event in store.events if event.type is EventType.INPUT_STEERED
    ]
    assert len(steered) == 1
    assert steered[0].turn_id == turn_id
    assert steered[0].step_id == f"{turn_id}:step:1"
    assert steered[0].payload == {"text": "turn left instead"}
    assert control.active_turn_id("s") is None


async def test_runtime_uses_driver_steering_when_constructor_is_omitted() -> None:
    store = FakeStore()
    control = TurnControl()
    driver = _WritingDriver(control, store)
    runtime = AgentRuntime(driver=driver, store=store)

    task = asyncio.create_task(
        runtime.submit(TurnRequest(session_id="s", text="start"))
    )
    await driver.started.wait()
    await runtime.steer("s", "correction")
    driver.drain_now.set()
    await task
    assert driver.drained == [("correction",)]


async def test_runtime_steer_refuses_empty_text_and_no_active_turn() -> None:
    store = FakeStore()
    control = TurnControl()
    runtime = AgentRuntime(
        driver=_WritingDriver(control, store), store=store, steering=control
    )

    with pytest.raises(RuntimeError, match="no active turn"):
        await runtime.steer("s", "text")
    with pytest.raises(ValueError):
        await runtime.steer("s", "   ")

    task = asyncio.create_task(
        runtime.submit(TurnRequest(session_id="s", text="start"))
    )
    driver = runtime._driver
    await driver.started.wait()  # type: ignore[attr-defined]
    driver.drain_now.set()  # type: ignore[attr-defined]
    await task
    with pytest.raises(RuntimeError, match="no active turn"):
        await runtime.steer("s", "late")


async def test_runtime_without_steering_reports_a_clear_refusal() -> None:
    store = FakeStore()

    class StaticDriver:
        async def run(self, turn: TurnStart) -> TurnResult:
            return TurnResult(
                session_id=turn.request.session_id,
                turn_id=turn.turn_id,
                status=TurnStatus.COMPLETED,
                reply="ok",
            )

    runtime = AgentRuntime(driver=StaticDriver(), store=store)
    with pytest.raises(RuntimeError, match="not configured"):
        await runtime.steer("s", "text")


async def test_undrained_correction_is_discarded_with_the_turn() -> None:
    store = FakeStore()
    control = TurnControl()

    class NoDrainDriver:
        steering = control

        async def run(self, turn: TurnStart) -> TurnResult:
            await control_ready.wait()
            return TurnResult(
                session_id=turn.request.session_id,
                turn_id=turn.turn_id,
                status=TurnStatus.COMPLETED,
                reply="ok",
            )

    control_ready = asyncio.Event()
    runtime = AgentRuntime(driver=NoDrainDriver(), store=store)
    task = asyncio.create_task(
        runtime.submit(TurnRequest(session_id="s", text="start"))
    )
    await asyncio.sleep(0)
    await runtime.steer("s", "never read")
    control_ready.set()
    await task

    assert control.active_turn_id("s") is None
    assert not any(
        event.type is EventType.INPUT_STEERED for event in store.events
    )


class _DrainThenWaitDriver:
    """Consume one boundary, then finish while another correction is pending."""

    def __init__(self, control: TurnControl, store: FakeStore) -> None:
        self.steering = control
        self.store = store
        self.started = asyncio.Event()
        self.drain_now = asyncio.Event()
        self.drained = asyncio.Event()
        self.finish_now = asyncio.Event()
        self.turns: list[TurnStart] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.turns.append(turn)
        self.started.set()
        await self.drain_now.wait()
        texts = self.steering.drain(turn.request.session_id, turn.turn_id)
        for text in texts:
            await self.store.append(
                RuntimeEvent(
                    type=EventType.INPUT_STEERED,
                    session_id=turn.request.session_id,
                    turn_id=turn.turn_id,
                    step_id=f"{turn.turn_id}:step:1",
                    payload={"text": text},
                )
            )
        self.drained.set()
        await self.finish_now.wait()
        return TurnResult(
            session_id=turn.request.session_id,
            turn_id=turn.turn_id,
            status=TurnStatus.COMPLETED,
            reply="final",
        )


async def test_pending_correction_is_recorded_on_terminal_without_input_steered() -> None:
    store = FakeStore()
    control = TurnControl()
    driver = _DrainThenWaitDriver(control, store)
    runtime = AgentRuntime(driver=driver, store=store)
    task = asyncio.create_task(
        runtime.submit(TurnRequest(session_id="s", text="start"))
    )
    await driver.started.wait()
    turn_id = await runtime.steer("s", "consumed")
    driver.drain_now.set()
    await driver.drained.wait()
    await runtime.steer("s", "too late for a step")
    await runtime.steer("s", "also too late")
    assert control.pending("s", turn_id) == 2
    driver.finish_now.set()
    result = await task

    assert result.status is TurnStatus.COMPLETED
    terminal = store.events[-1]
    assert terminal.type is EventType.TURN_COMPLETED
    assert terminal.payload["unapplied_corrections"] == [
        "too late for a step",
        "also too late",
    ]
    steered = [
        event.payload["text"]
        for event in store.events
        if event.type is EventType.INPUT_STEERED
    ]
    assert steered == ["consumed"]
    assert runtime.unapplied_corrections("s", turn_id) == (
        "too late for a step",
        "also too late",
    )
    assert control.active_turn_id("s") is None
    with pytest.raises(RuntimeError, match="no active turn"):
        await runtime.steer("s", "next turn must not inherit")


async def test_closed_steering_fails_submit_without_leaving_an_active_turn() -> None:
    store = FakeStore()
    control = TurnControl()
    control.close()
    runtime = AgentRuntime(
        driver=_WritingDriver(control, store), store=store, steering=control
    )

    with pytest.raises(TurnControlClosedError):
        await runtime.submit(TurnRequest(session_id="s", text="start"))

    assert store.events == []
    assert runtime._sessions == {}
    # A leaked active turn would make either call wait forever.
    assert await asyncio.wait_for(runtime.cancel("s"), 0.5) is None
    await asyncio.wait_for(runtime.stop(), 0.5)
    assert runtime._sessions == {}

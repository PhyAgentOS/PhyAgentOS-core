"""Admission control and shutdown for the gateway's shared background runtime.

These tests use a fake Core session on purpose: they are about the gate that
sits *before* Core (same-session order, cross-session bound, per-session queue
limit, reply-marker handoff, closing behavior). The tests that prove a turn
really goes through a Core request live in ``test_core_background_capabilities``
and ``test_core_scheduler_gateway``.
"""

from __future__ import annotations

import asyncio

import pytest

from PhyAgentOS.agent.core import TurnRequest, TurnResult, TurnStatus
from PhyAgentOS.agent.services.background_runtime import (
    BackgroundClosedError,
    BackgroundCoreRuntime,
    TurnBinding,
    TurnRejectedError,
    TurnRoutes,
)

_BINDING = TurnBinding(channel="telegram", chat_id="42")


class FakeSession:
    """A Core-shaped session whose turns are released explicitly."""

    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []
        self._gates: list[asyncio.Event] = []
        self.active = 0
        self.max_active = 0
        self.closed = False
        self._closed_event = asyncio.Event()

    async def submit(self, request: TurnRequest) -> TurnResult:
        assert not self.closed, "submit must never reach a closed session"
        self.requests.append(request)
        gate = asyncio.Event()
        self._gates.append(gate)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await gate.wait()
            return TurnResult(
                session_id=request.session_id,
                turn_id=f"turn-{len(self.requests)}",
                status=TurnStatus.COMPLETED,
                reply=f"reply to {request.text}",
            )
        finally:
            self.active -= 1

    def release(self, index: int = 0) -> None:
        self._gates[index].set()

    async def aclose(self) -> None:
        self.closed = True
        self._closed_event.set()
        for gate in self._gates:
            gate.set()


def _runtime(session: FakeSession, *, concurrent: int = 4, per_session: int = 8, routes=None):
    return BackgroundCoreRuntime(
        session,
        routes=routes,
        max_concurrent_turns=concurrent,
        per_session_queue_limit=per_session,
    )


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async def poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout)


async def _run(runtime, session_id="telegram:42", text="hi", binding=_BINDING):
    return await runtime.run_turn(
        session_id=session_id, text=text, binding=binding, source="test"
    )


async def test_same_session_runs_in_registration_order() -> None:
    session = FakeSession()
    runtime = _runtime(session)

    first = asyncio.create_task(_run(runtime, text="one"))
    await _wait_until(lambda: len(session.requests) == 1)
    second = asyncio.create_task(_run(runtime, text="two"))
    await asyncio.sleep(0)
    assert len(session.requests) == 1  # queued behind the running turn

    session.release(0)
    await _wait_until(lambda: len(session.requests) == 2)
    session.release(1)
    results = await asyncio.gather(first, second)

    assert [r.result.reply for r in results] == ["reply to one", "reply to two"]
    assert [r.session_id for r in session.requests] == ["telegram:42", "telegram:42"]


async def test_different_sessions_run_concurrently_up_to_the_limit() -> None:
    session = FakeSession()
    runtime = _runtime(session, concurrent=2)

    tasks = [
        asyncio.create_task(_run(runtime, session_id=f"telegram:{index}", text="x"))
        for index in range(3)
    ]
    await _wait_until(lambda: len(session.requests) == 2)
    await asyncio.sleep(0.01)
    assert len(session.requests) == 2  # third waits for a cross-session slot

    session.release(0)
    await _wait_until(lambda: len(session.requests) == 3)
    session.release(1)
    session.release(2)
    await asyncio.gather(*tasks)
    assert session.max_active == 2


async def test_per_session_queue_limit_rejects_instead_of_growing() -> None:
    session = FakeSession()
    runtime = _runtime(session, concurrent=4, per_session=1)

    running = asyncio.create_task(_run(runtime, text="one"))
    await _wait_until(lambda: len(session.requests) == 1)
    queued = asyncio.create_task(_run(runtime, text="two"))
    await asyncio.sleep(0)

    with pytest.raises(TurnRejectedError) as excinfo:
        await _run(runtime, text="three")
    assert excinfo.value.session_id == "telegram:42"
    assert "already has 1 queued task" in excinfo.value.reason

    session.release(0)
    await _wait_until(lambda: len(session.requests) == 2)
    session.release(1)
    await asyncio.gather(running, queued)
    assert runtime.unsettled == 0


async def test_global_pending_cap_bounds_cross_session_tasks() -> None:
    session = FakeSession()
    runtime = _runtime(session, concurrent=1, per_session=0)
    # Derived cap: max_concurrent_turns * (per_session_queue_limit + 1) == 1.
    holding = asyncio.create_task(_run(runtime, session_id="telegram:a", text="one"))
    await _wait_until(lambda: len(session.requests) == 1)

    with pytest.raises(TurnRejectedError) as excinfo:
        await _run(runtime, session_id="telegram:b", text="two")
    assert "maximum number of unsettled turns" in excinfo.value.reason

    session.release(0)
    await holding
    assert runtime.unsettled == 0


async def test_reply_marker_is_consumed_before_the_next_turn_binds() -> None:
    """The next queued turn must not clear the previous turn's sent marker.

    Turn one is answered by the ``message`` tool while it runs. Turn two is
    already queued on the same session. The marker has to be read before the
    ordering gate opens, otherwise turn two's ``bind`` would erase it and the
    entry point would deliver turn one's reply a second time.
    """
    session = FakeSession()
    runtime = _runtime(session)

    first = asyncio.create_task(_run(runtime, text="one"))
    await _wait_until(lambda: len(session.requests) == 1)
    runtime.routes.mark_reply_sent("telegram:42", "turn-1")

    second = asyncio.create_task(_run(runtime, text="two"))
    await asyncio.sleep(0)
    session.release(0)
    outcome = await asyncio.wait_for(first, 2)
    # Turn two is now allowed to bind, and it must not affect turn one.
    await _wait_until(lambda: len(session.requests) == 2)
    assert outcome.reply_sent is True
    assert runtime.routes.consume_reply_sent("telegram:42", "turn-1") is False
    assert runtime.routes.get("telegram:42").chat_id == "42"

    session.release(1)
    second_outcome = await asyncio.wait_for(second, 2)
    assert second_outcome.reply_sent is False


async def test_marker_is_dropped_when_a_turn_fails() -> None:
    session = FakeSession()
    runtime = _runtime(session)

    class Boom(FakeSession):
        async def submit(self, request):
            runtime.routes.mark_reply_sent(request.session_id, "turn-1")
            raise RuntimeError("storage failed")

    runtime.session = Boom()
    with pytest.raises(RuntimeError):
        await _run(runtime)
    assert runtime.routes.consume_reply_sent("telegram:42", "turn-1") is False
    assert runtime.unsettled == 0


async def test_binding_is_visible_during_the_turn_and_removed_after() -> None:
    routes = TurnRoutes()
    seen: list[TurnBinding | None] = []

    class Inspect(FakeSession):
        async def submit(self, request):
            seen.append(routes.get(request.session_id))
            return await super().submit(request)

    session = Inspect()
    runtime = _runtime(session, routes=routes)
    task = asyncio.create_task(_run(runtime))
    await _wait_until(lambda: len(session.requests) == 1)
    session.release(0)
    await task

    assert seen == [_BINDING]
    assert routes.get("telegram:42") is None


async def test_close_refuses_queued_turns_and_leaves_no_pending_tasks() -> None:
    session = FakeSession()
    runtime = _runtime(session)

    running = asyncio.create_task(_run(runtime, text="one"))
    await _wait_until(lambda: len(session.requests) == 1)
    queued = asyncio.create_task(_run(runtime, text="two"))
    await asyncio.sleep(0)
    assert runtime.unsettled == 2

    await runtime.aclose()

    with pytest.raises(BackgroundClosedError):
        await asyncio.wait_for(queued, 2)
    await asyncio.wait_for(running, 2)
    assert runtime.unsettled == 0
    assert runtime.closed is True
    assert len(session.requests) == 1  # the queued turn never reached Core

    with pytest.raises(BackgroundClosedError):
        await _run(runtime, text="three")
    assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]


async def test_two_bindings_for_one_session_are_refused() -> None:
    routes = TurnRoutes()
    routes.bind("telegram:42", _BINDING)
    with pytest.raises(RuntimeError):
        routes.bind("telegram:42", TurnBinding(channel="telegram", chat_id="43"))
    routes.unbind("telegram:42")
    routes.bind("telegram:42", TurnBinding(channel="telegram", chat_id="43"))
    assert routes.get("telegram:42").chat_id == "43"

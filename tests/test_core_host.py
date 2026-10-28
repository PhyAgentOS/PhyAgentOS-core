"""Tests for the entry-facing agent host.

The fakes implement the public Protocols on purpose: the host must coordinate
only through ``AgentRuntime`` and ``Capability`` behaviour, so the tests never
reach into its private bookkeeping.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from PhyAgentOS.agent.core import (
    AgentHost,
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolRegistration,
    TurnRequest,
    TurnResult,
    TurnStatus,
)
from PhyAgentOS.agent.core.host import AgentHostCloseError, AgentHostImpl


def _context(session_id: str = "session-1") -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="turn-1",
        step_id="step-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=2),
    )


@dataclass
class _Gate:
    """One blocked submit: signal entry, then wait for release."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class _FakeRuntime:
    """Protocol fake with a controllable submit gate and an event log."""

    def __init__(self) -> None:
        self.log: list[tuple[object, ...]] = []
        self.submitted: list[TurnRequest] = []
        self.cancelled: list[str] = []
        self.contexts: dict[str, ExecutionContext | None] = {}
        self.stop_error: Exception | None = None
        self.stopped = False
        self.closed = False
        self._planned: dict[str, list[_Gate]] = {}
        self._inflight: dict[str, list[_Gate]] = {}

    def gate(self, session_id: str) -> _Gate:
        gate = _Gate()
        self._planned.setdefault(session_id, []).append(gate)
        return gate

    async def submit(self, request: TurnRequest) -> TurnResult:
        self.log.append(("runtime.submit.enter", request.session_id))
        self.submitted.append(request)
        planned = self._planned.get(request.session_id)
        gate = planned.pop(0) if planned else None
        if gate is not None:
            self._inflight.setdefault(request.session_id, []).append(gate)
            gate.started.set()
            await gate.release.wait()
            self._inflight[request.session_id].remove(gate)
        self.log.append(("runtime.submit.returned", request.session_id))
        if gate is not None:
            gate.done.set()
        return TurnResult(
            session_id=request.session_id,
            turn_id=f"turn-{len(self.submitted)}",
            status=TurnStatus.COMPLETED,
            reply=request.text,
        )

    async def cancel(self, session_id: str) -> ExecutionContext | None:
        self.cancelled.append(session_id)
        self.log.append(("runtime.cancel", session_id))
        # The contract promises that the turn is terminal before cancel
        # returns, so wait for every blocked submit of this session.
        for gate in list(self._inflight.get(session_id, ())):
            gate.release.set()
            await gate.done.wait()
        self.log.append(("runtime.cancel.returned", session_id))
        return self.contexts.get(session_id)

    async def stop(self) -> None:
        self.log.append(("runtime.stop",))
        self.stopped = True
        # stop waits for every accepted submit, including a blocked one.
        for session_id in list(self._inflight):
            for gate in list(self._inflight[session_id]):
                gate.release.set()
                await gate.done.wait()
        self.log.append(("runtime.stop.returned",))
        if self.stop_error is not None:
            raise self.stop_error

    async def aclose(self) -> None:
        self.log.append(("runtime.aclose",))
        self.closed = True


class _RecordingCapability:
    def __init__(
        self,
        name: str,
        log: list[tuple[object, ...]],
        *,
        cancel_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.name = name
        self._log = log
        self._cancel_error = cancel_error
        self._close_error = close_error
        self.cancel_calls: list[tuple[str, ExecutionContext | None]] = []

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return ()

    def prompt_context(self, context: ExecutionContext) -> str | None:
        return None

    async def cancel(self, session_id: str, context: ExecutionContext | None) -> None:
        self._log.append(("capability.cancel", self.name, session_id))
        self.cancel_calls.append((session_id, context))
        if self._cancel_error is not None:
            raise self._cancel_error

    async def aclose(self) -> None:
        self._log.append(("capability.aclose", self.name))
        if self._close_error is not None:
            raise self._close_error


async def test_submit_delegates_exactly_once_and_returns_the_result() -> None:
    runtime = _FakeRuntime()
    capability = _RecordingCapability("cap", runtime.log)
    host = AgentHostImpl(runtime=runtime, capabilities=[capability])
    assert isinstance(host, AgentHost)

    request = TurnRequest(session_id="session-1", text="hello")
    result = await host.submit(request)

    assert runtime.submitted == [request]
    assert result.reply == "hello"
    assert result.status is TurnStatus.COMPLETED
    assert runtime.log == [
        ("runtime.submit.enter", "session-1"),
        ("runtime.submit.returned", "session-1"),
    ]
    assert capability.cancel_calls == []


async def test_cancel_passes_the_runtime_context_to_capabilities_in_order() -> None:
    runtime = _FakeRuntime()
    first = _RecordingCapability("first", runtime.log)
    second = _RecordingCapability("second", runtime.log)
    context = _context()
    runtime.contexts["session-1"] = context
    host = AgentHostImpl(runtime, [first, second])

    assert await host.cancel("session-1") is None

    assert runtime.cancelled == ["session-1"]
    assert first.cancel_calls == [("session-1", context)]
    assert second.cancel_calls == [("session-1", context)]
    assert runtime.log == [
        ("runtime.cancel", "session-1"),
        ("runtime.cancel.returned", "session-1"),
        ("capability.cancel", "first", "session-1"),
        ("capability.cancel", "second", "session-1"),
    ]


async def test_cancel_without_an_active_turn_still_notifies_with_none() -> None:
    runtime = _FakeRuntime()
    capability = _RecordingCapability("cap", runtime.log)
    host = AgentHostImpl(runtime, [capability])

    # No submit happened and the runtime reports no active context. A
    # completed session may still own remote work, so the stop request is sent.
    await host.cancel("session-1")

    assert runtime.cancelled == ["session-1"]
    assert capability.cancel_calls == [("session-1", None)]


async def test_aclose_order_rejects_new_submit_and_closes_everything_once() -> None:
    runtime = _FakeRuntime()
    first = _RecordingCapability("first", runtime.log)
    second = _RecordingCapability("second", runtime.log)
    runtime.contexts["session-1"] = _context()
    host = AgentHostImpl(runtime, [first, second])

    gate = runtime.gate("session-1")
    submit_task = asyncio.create_task(host.submit(TurnRequest(session_id="session-1", text="hi")))
    await asyncio.wait_for(gate.started.wait(), timeout=1)

    await host.aclose()
    result = await asyncio.wait_for(submit_task, timeout=1)

    assert result.session_id == "session-1"
    # aclose uses the runtime-wide stop, never a per-session cancel.
    assert runtime.cancelled == []
    assert runtime.stopped
    assert runtime.log == [
        ("runtime.submit.enter", "session-1"),
        ("runtime.stop",),
        # stop returns only after the local turn is terminal.
        ("runtime.submit.returned", "session-1"),
        ("runtime.stop.returned",),
        ("capability.cancel", "first", "session-1"),
        ("capability.cancel", "second", "session-1"),
        ("capability.aclose", "first"),
        ("capability.aclose", "second"),
        ("runtime.aclose",),
    ]
    # No context is forwarded: stop already settled every local turn.
    assert first.cancel_calls == [("session-1", None)]
    assert second.cancel_calls == [("session-1", None)]
    assert runtime.closed

    with pytest.raises(RuntimeError):
        await host.submit(TurnRequest(session_id="session-2", text="late"))


async def test_aclose_notifies_capabilities_for_a_completed_session() -> None:
    runtime = _FakeRuntime()
    capability = _RecordingCapability("cap", runtime.log)
    host = AgentHostImpl(runtime, [capability])
    await host.submit(TurnRequest(session_id="session-1", text="done"))
    assert capability.cancel_calls == []

    await host.aclose()

    assert runtime.cancelled == []
    assert runtime.stopped
    assert capability.cancel_calls == [("session-1", None)]
    assert ("capability.aclose", "cap") in runtime.log
    assert runtime.log[-1] == ("runtime.aclose",)


async def test_aclose_is_idempotent() -> None:
    runtime = _FakeRuntime()
    capability = _RecordingCapability("cap", runtime.log)
    host = AgentHostImpl(runtime, [capability])
    gate = runtime.gate("session-1")
    submit_task = asyncio.create_task(host.submit(TurnRequest(session_id="session-1", text="hi")))
    await asyncio.wait_for(gate.started.wait(), timeout=1)

    await host.aclose()
    await asyncio.wait_for(submit_task, timeout=1)
    runtime.log.clear()

    await host.aclose()

    assert runtime.log == []
    assert runtime.stopped
    assert runtime.cancelled == []
    assert capability.cancel_calls == [("session-1", None)]
    assert runtime.closed


async def test_concurrent_submits_keep_every_accepted_session_once() -> None:
    runtime = _FakeRuntime()
    capability = _RecordingCapability("cap", runtime.log)
    host = AgentHostImpl(runtime, [capability])

    gate_a = runtime.gate("session-a")
    gate_b = runtime.gate("session-b")
    task_a = asyncio.create_task(host.submit(TurnRequest(session_id="session-a", text="a")))
    await asyncio.wait_for(gate_a.started.wait(), timeout=1)
    task_b = asyncio.create_task(host.submit(TurnRequest(session_id="session-b", text="b")))
    await asyncio.wait_for(gate_b.started.wait(), timeout=1)

    gate_b.release.set()
    await asyncio.wait_for(task_b, timeout=1)

    await host.aclose()
    await asyncio.wait_for(task_a, timeout=1)

    # Both accepted sessions get a stop request; stop already settled every
    # local turn, so no old context is forwarded.
    assert runtime.cancelled == []
    assert runtime.stopped
    assert capability.cancel_calls == [
        ("session-a", None),
        ("session-b", None),
    ]


async def test_repeated_submits_for_one_session_are_cancelled_once() -> None:
    runtime = _FakeRuntime()
    capability = _RecordingCapability("cap", runtime.log)
    host = AgentHostImpl(runtime, [capability])

    first_gate = runtime.gate("session-1")
    second_gate = runtime.gate("session-1")
    first = asyncio.create_task(host.submit(TurnRequest(session_id="session-1", text="one")))
    await asyncio.wait_for(first_gate.started.wait(), timeout=1)
    second = asyncio.create_task(host.submit(TurnRequest(session_id="session-1", text="two")))
    await asyncio.wait_for(second_gate.started.wait(), timeout=1)

    first_gate.release.set()
    await asyncio.wait_for(first, timeout=1)

    await host.aclose()
    await asyncio.wait_for(second, timeout=1)

    assert runtime.cancelled == []
    assert runtime.stopped
    assert capability.cancel_calls == [("session-1", None)]


async def test_aclose_reports_capability_cancel_failure_but_keeps_closing() -> None:
    runtime = _FakeRuntime()
    first = _RecordingCapability("first", runtime.log, cancel_error=ValueError("no stop"))
    second = _RecordingCapability("second", runtime.log)
    host = AgentHostImpl(runtime, [first, second])
    gate = runtime.gate("session-1")
    submit_task = asyncio.create_task(host.submit(TurnRequest(session_id="session-1", text="hi")))
    await asyncio.wait_for(gate.started.wait(), timeout=1)
    runtime.log.clear()

    with pytest.raises(AgentHostCloseError) as excinfo:
        await host.aclose()
    await asyncio.wait_for(submit_task, timeout=1)

    error = excinfo.value
    assert error.operations == ("capability[0].cancel",)
    assert isinstance(error.failures[0], ValueError)
    assert isinstance(error.__cause__, ValueError)
    # stop ran before any capability, and the second capability still stopped.
    assert runtime.log[0] == ("runtime.stop",)
    assert ("capability.cancel", "second", "session-1") in runtime.log
    assert ("capability.aclose", "first") in runtime.log
    assert ("capability.aclose", "second") in runtime.log
    assert runtime.closed

    # A repeated call is still harmless after the failed shutdown.
    runtime.log.clear()
    await host.aclose()
    assert runtime.log == []


async def test_aclose_runtime_stop_failure_still_notifies_and_closes() -> None:
    runtime = _FakeRuntime()
    capability = _RecordingCapability("cap", runtime.log)
    host = AgentHostImpl(runtime, [capability])
    await host.submit(TurnRequest(session_id="session-1", text="done"))
    runtime.stop_error = RuntimeError("stop failed")

    with pytest.raises(AgentHostCloseError) as excinfo:
        await host.aclose()

    error = excinfo.value
    assert error.operations == ("runtime.stop",)
    # Every accepted session is still notified, then closed in order.
    assert capability.cancel_calls == [("session-1", None)]
    assert runtime.log == [
        ("runtime.submit.enter", "session-1"),
        ("runtime.submit.returned", "session-1"),
        ("runtime.stop",),
        ("runtime.stop.returned",),
        ("capability.cancel", "cap", "session-1"),
        ("capability.aclose", "cap"),
        ("runtime.aclose",),
    ]
    assert runtime.closed


async def test_aclose_capability_close_failure_does_not_block_later_resources() -> None:
    runtime = _FakeRuntime()
    broken = _RecordingCapability("broken", runtime.log, close_error=RuntimeError("boom"))
    healthy = _RecordingCapability("healthy", runtime.log)
    host = AgentHostImpl(runtime, [broken, healthy])

    with pytest.raises(AgentHostCloseError) as excinfo:
        await host.aclose()

    error = excinfo.value
    assert error.operations == ("capability[0].aclose",)
    assert isinstance(error.failures[0], RuntimeError)
    # stop comes before capability shutdown even with no accepted session.
    assert runtime.log == [
        ("runtime.stop",),
        ("runtime.stop.returned",),
        ("capability.aclose", "broken"),
        ("capability.aclose", "healthy"),
        ("runtime.aclose",),
    ]
    assert runtime.closed

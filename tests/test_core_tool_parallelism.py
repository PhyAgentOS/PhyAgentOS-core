"""Behavior tests for the executor's bounded tool parallelism.

Only calls whose registration declares ``parallel_safe=True`` may overlap.
Everything else is a sequential barrier. Every call still goes through the
single-call path, so the event order and the durability rules are unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from PhyAgentOS.agent.core import (
    Budget,
    CancellationToken,
    EventType,
    ExecutionContext,
    RuntimeEvent,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolPolicy,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.agent.core.tools import ToolExecutorImpl

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class _Store:
    def __init__(self, fail_intent_for: str | None = None) -> None:
        self.events: list[RuntimeEvent] = []
        self.fail_intent_for = fail_intent_for
        self.failed = asyncio.Event()

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        if (
            self.fail_intent_for is not None
            and event.type is EventType.TOOL_INTENT
            and event.payload.get("tool_call_id") == self.fail_intent_for
        ):
            self.failed.set()
            raise OSError("simulated intent disk failure")
        stored = replace(event, sequence=len(self.events))
        self.events.append(stored)
        return stored


def _context(*, token: CancellationToken | None = None) -> ExecutionContext:
    return ExecutionContext(
        session_id="session-1",
        turn_id="turn-1",
        step_id="step-1",
        cancellation=token if token is not None else CancellationToken(),
        budget=Budget(max_steps=4),
        policy=ToolPolicy(),
    )


def _call(call_id: str, name: str = "echo") -> ToolCall:
    return ToolCall(tool_call_id=call_id, name=name, arguments={"text": "a"})


def _registration(
    handler: Any,
    *,
    name: str = "echo",
    parallel_safe: bool,
) -> ToolRegistration:
    return ToolRegistration(
        definition=ToolDefinition(
            name=name,
            description=f"Tool {name}.",
            parameters=_PARAMETERS,
        ),
        handler=handler,
        parallel_safe=parallel_safe,
    )


def _succeeded(call: ToolCall) -> ToolOutcome:
    return ToolOutcome(
        tool_call_id=call.tool_call_id,
        tool_name=call.name,
        status=ToolStatus.SUCCEEDED,
        text=f"ok:{call.tool_call_id}",
    )


async def test_consecutive_safe_calls_overlap_within_the_parallel_bound() -> None:
    active = 0
    peak = 0
    started = 0
    both_started = asyncio.Event()

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal active, peak, started
        active += 1
        started += 1
        peak = max(peak, active)
        if started >= 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active -= 1
        return _succeeded(call)

    executor = ToolExecutorImpl([_registration(handler, parallel_safe=True)], _Store())
    calls = (_call("call-1"), _call("call-2"), _call("call-3"))

    outcomes = await executor.execute_batch(calls, _context(), max_parallel=2)

    assert [outcome.tool_call_id for outcome in outcomes] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert all(outcome.status is ToolStatus.SUCCEEDED for outcome in outcomes)
    assert peak == 2
    assert started == 3


async def test_unsafe_call_is_a_sequential_barrier() -> None:
    log: list[tuple[str, str]] = []

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        log.append(("start", call.tool_call_id))
        await asyncio.sleep(0.01)
        log.append(("end", call.tool_call_id))
        return _succeeded(call)

    executor = ToolExecutorImpl(
        [
            _registration(handler, name="safe", parallel_safe=True),
            _registration(handler, name="unsafe", parallel_safe=False),
        ],
        _Store(),
    )
    calls = (
        _call("a1", "safe"),
        _call("a2", "safe"),
        _call("u", "unsafe"),
        _call("b1", "safe"),
        _call("b2", "safe"),
    )

    outcomes = await executor.execute_batch(calls, _context(), max_parallel=4)

    assert [outcome.status for outcome in outcomes] == [ToolStatus.SUCCEEDED] * 5
    positions = {entry: index for index, entry in enumerate(log)}
    # The safe pair overlaps; the unsafe call runs alone between the pairs.
    assert positions[("start", "a2")] < positions[("end", "a1")]
    assert positions[("end", "a1")] < positions[("start", "u")]
    assert positions[("end", "a2")] < positions[("start", "u")]
    assert positions[("end", "u")] < positions[("start", "b1")]
    assert positions[("start", "b1")] < positions[("start", "b2")]


async def test_max_parallel_one_serialises_safe_calls_in_order() -> None:
    active = 0
    peak = 0
    order: list[str] = []

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        order.append(call.tool_call_id)
        await asyncio.sleep(0)
        active -= 1
        return _succeeded(call)

    executor = ToolExecutorImpl([_registration(handler, parallel_safe=True)], _Store())
    calls = tuple(_call(f"call-{index}") for index in range(1, 5))

    outcomes = await executor.execute_batch(calls, _context(), max_parallel=1)

    assert [outcome.tool_call_id for outcome in outcomes] == [
        call.tool_call_id for call in calls
    ]
    assert order == [call.tool_call_id for call in calls]
    assert peak == 1


async def test_results_keep_call_order_when_completion_order_differs() -> None:
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        if call.tool_call_id == "call-1":
            await asyncio.sleep(0.03)
        else:
            await asyncio.sleep(0)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=call.tool_call_id,
        )

    executor = ToolExecutorImpl([_registration(handler, parallel_safe=True)], _Store())

    outcomes = await executor.execute_batch(
        (_call("call-1"), _call("call-2")), _context(), max_parallel=2
    )

    assert [outcome.tool_call_id for outcome in outcomes] == ["call-1", "call-2"]
    assert [outcome.text for outcome in outcomes] == ["call-1", "call-2"]


async def test_each_batch_call_still_writes_its_own_lifecycle_events() -> None:
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        return _succeeded(call)

    store = _Store()
    executor = ToolExecutorImpl([_registration(handler, parallel_safe=True)], store)
    calls = (_call("call-1"), _call("call-2"))

    outcomes = await executor.execute_batch(calls, _context(), max_parallel=2)

    assert all(outcome.is_success for outcome in outcomes)
    per_call: dict[str, list[EventType]] = {}
    for event in store.events:
        per_call.setdefault(event.payload["tool_call_id"], []).append(event.type)
    assert per_call == {
        "call-1": [
            EventType.TOOL_INTENT,
            EventType.TOOL_DISPATCHED,
            EventType.TOOL_SETTLED,
        ],
        "call-2": [
            EventType.TOOL_INTENT,
            EventType.TOOL_DISPATCHED,
            EventType.TOOL_SETTLED,
        ],
    }


async def test_policy_denial_in_a_batch_is_rejected_without_calling_the_handler() -> None:
    calls_seen: list[str] = []

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        calls_seen.append(call.tool_call_id)
        return _succeeded(call)

    executor = ToolExecutorImpl(
        [
            _registration(handler, name="safe", parallel_safe=True),
            _registration(handler, name="denied", parallel_safe=True),
        ],
        _Store(),
    )
    context = replace(
        _context(), policy=ToolPolicy(allowed_tools=frozenset({"safe"}))
    )

    outcomes = await executor.execute_batch(
        (_call("call-1", "safe"), _call("call-2", "denied")),
        context,
        max_parallel=2,
    )

    assert outcomes[0].is_success
    assert outcomes[1].status is ToolStatus.REJECTED
    assert outcomes[1].error is not None
    assert outcomes[1].error.code == "tool_denied"
    assert calls_seen == ["call-1"]


async def test_persistence_failure_stops_unstarted_calls_and_settles_started_ones() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    handlers: list[str] = []

    async def blocking(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        handlers.append(call.tool_call_id)
        started.set()
        await release.wait()
        return _succeeded(call)

    async def immediate(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        handlers.append(call.tool_call_id)
        return _succeeded(call)

    store = _Store(fail_intent_for="call-2")
    executor = ToolExecutorImpl(
        [
            _registration(blocking, name="safe1", parallel_safe=True),
            _registration(immediate, name="safe2", parallel_safe=True),
            _registration(immediate, name="safe3", parallel_safe=True),
            _registration(immediate, name="unsafe", parallel_safe=False),
        ],
        store,
    )
    calls = (
        _call("call-1", "safe1"),
        _call("call-2", "safe2"),
        _call("call-3", "safe3"),
        _call("call-4", "unsafe"),
    )

    task = asyncio.create_task(
        executor.execute_batch(calls, _context(), max_parallel=2)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(store.failed.wait(), timeout=1)
    release.set()
    outcomes = await asyncio.wait_for(task, timeout=1)

    assert outcomes[0].status is ToolStatus.SUCCEEDED
    assert outcomes[1].status is ToolStatus.FAILED
    assert outcomes[1].error is not None
    assert outcomes[1].error.code == "tool_intent_write_failed"
    for outcome in outcomes[2:]:
        assert outcome.status is ToolStatus.CANCELLED
        assert outcome.error is None
        assert "was not called" in outcome.text
    # Only the started call reached its handler; the never-started ones did not.
    assert handlers == ["call-1"]
    assert {event.payload["tool_call_id"] for event in store.events} == {"call-1"}
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]


async def test_local_cancel_stops_remaining_calls_and_reports_started_unknown() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    handlers: list[str] = []

    async def blocking(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        handlers.append(call.tool_call_id)
        started.set()
        await release.wait()
        return _succeeded(call)

    async def never(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        handlers.append(call.tool_call_id)
        return _succeeded(call)

    token = CancellationToken()
    store = _Store()
    executor = ToolExecutorImpl(
        [
            _registration(blocking, name="safe1", parallel_safe=True),
            _registration(never, name="safe2", parallel_safe=True),
            _registration(never, name="unsafe", parallel_safe=False),
        ],
        store,
    )
    calls = (
        _call("call-1", "safe1"),
        _call("call-2", "safe2"),
        _call("call-3", "unsafe"),
    )

    task = asyncio.create_task(
        executor.execute_batch(calls, _context(token=token), max_parallel=1)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    token.request("user stop")
    outcomes = await asyncio.wait_for(task, timeout=1)

    assert outcomes[0].status is ToolStatus.UNKNOWN
    assert outcomes[1].status is ToolStatus.CANCELLED
    assert outcomes[2].status is ToolStatus.CANCELLED
    assert handlers == ["call-1"]
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[-1].payload["status"] == "unknown"

    release.set()
    while executor.pending_handler_tasks:
        await asyncio.sleep(0)


async def test_unknown_tool_is_a_barrier_and_never_starts_safe_neighbours_early() -> None:
    log: list[str] = []

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        log.append(call.tool_call_id)
        return _succeeded(call)

    executor = ToolExecutorImpl([_registration(handler, parallel_safe=True)], _Store())
    calls = (_call("missing", "ghost"), _call("call-1"), _call("call-2"))

    outcomes = await executor.execute_batch(calls, _context(), max_parallel=4)

    assert outcomes[0].status is ToolStatus.REJECTED
    assert outcomes[0].error is not None
    assert outcomes[0].error.code == "unknown_tool"
    assert outcomes[1].is_success and outcomes[2].is_success
    assert log == ["call-1", "call-2"]


async def test_empty_batch_and_argument_validation() -> None:
    executor = ToolExecutorImpl([], _Store())

    assert await executor.execute_batch((), _context()) == ()

    with pytest.raises(ValueError):
        await executor.execute_batch((), _context(), max_parallel=0)
    with pytest.raises(TypeError):
        await executor.execute_batch((), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await executor.execute_batch((object(),), _context())  # type: ignore[arg-type]


def test_registrations_are_exposed_for_reuse_and_lookup() -> None:
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        return _succeeded(call)

    registration = _registration(handler, parallel_safe=True)
    executor = ToolExecutorImpl([registration], _Store())

    assert executor.registrations == (registration,)
    assert executor.registration("echo") is registration
    assert executor.registration("missing") is None

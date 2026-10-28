"""Behavior tests for the core tool executor.

These pin the rules the loop driver relies on: policy and argument checks run
before any event; ``tool.intent`` and ``tool.dispatched`` are durable before the
handler runs; terminal results are settled honestly; a cancellation after
dispatch is ``unknown`` and never retried automatically.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from PhyAgentOS.agent.core import (
    Budget,
    CancellationToken,
    DomainRefs,
    EventType,
    ExecutionContext,
    OperationCancelledError,
    RuntimeEvent,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolExecutor,
    ToolOutcome,
    ToolPolicy,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.agent.core.tools import ToolExecutorImpl

_ECHO_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class _Store:
    """Minimal append-only store with per-call failure injection."""

    def __init__(self, failures: dict[int, BaseException] | None = None) -> None:
        self.events: list[RuntimeEvent] = []
        self.failures = dict(failures or {})
        self.append_calls = 0

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        self.append_calls += 1
        failure = self.failures.get(self.append_calls)
        if failure is not None:
            raise failure
        stored = replace(event, sequence=len(self.events))
        self.events.append(stored)
        return stored


class _CancellingStore(_Store):
    """Store that requests cancellation while appending the intent event."""

    def __init__(self, token: CancellationToken) -> None:
        super().__init__()
        self._token = token

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        if event.type is EventType.TOOL_INTENT:
            self._token.request("cancelled during intent append")
        return await super().append(event)


def _context(
    *,
    token: CancellationToken | None = None,
    policy: ToolPolicy | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        session_id="session-1",
        turn_id="turn-1",
        step_id="step-1",
        cancellation=token if token is not None else CancellationToken(),
        budget=Budget(max_steps=4),
        policy=policy if policy is not None else ToolPolicy(),
    )


def _registration(
    handler: Any,
    *,
    name: str = "echo",
    parameters: dict[str, Any] | None = None,
) -> ToolRegistration:
    return ToolRegistration(
        definition=ToolDefinition(
            name=name,
            description=f"Tool {name}.",
            parameters=_ECHO_PARAMETERS if parameters is None else parameters,
        ),
        handler=handler,
    )


def _succeeded(
    *,
    call_id: str = "call-1",
    name: str = "echo",
    text: str = "ok",
    domain_refs: DomainRefs | None = None,
) -> ToolOutcome:
    return ToolOutcome(
        tool_call_id=call_id,
        tool_name=name,
        status=ToolStatus.SUCCEEDED,
        text=text,
        domain_refs=DomainRefs() if domain_refs is None else domain_refs,
    )


async def _drain(executor: ToolExecutorImpl, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while executor.pending_handler_tasks:
        if loop.time() > deadline:
            raise AssertionError("handler task did not finish")
        await asyncio.sleep(0)


def test_executor_is_a_tool_executor_and_rejects_duplicate_names() -> None:
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        return _succeeded()

    registration = _registration(handler)
    executor = ToolExecutorImpl([registration], _Store())
    assert isinstance(executor, ToolExecutor)

    with pytest.raises(ValueError):
        ToolExecutorImpl([registration, registration], _Store())


async def test_success_writes_ordered_lifecycle_events() -> None:
    store = _Store()
    seen: dict[str, Any] = {}

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        seen["call"] = call
        seen["arguments"] = call.arguments_dict()
        seen["context"] = context
        seen["arguments"]["text"] = "changed"
        return _succeeded(
            text="hello back",
            domain_refs=DomainRefs(task_id="task-1", revision_id="rev-1"),
        )

    executor = ToolExecutorImpl([_registration(handler)], store)
    call = ToolCall(
        tool_call_id="call-1",
        name="echo",
        arguments={"text": "hi"},
        raw_arguments='{"text": "hi"}',
    )
    context = _context()

    outcome = await executor.execute(call, context)

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.is_success
    assert seen["context"] is context
    assert type(seen["arguments"]) is dict
    assert seen["arguments"] == {"text": "changed"}
    handler_call = seen["call"]
    assert handler_call is not call
    assert handler_call.tool_call_id == "call-1"
    assert handler_call.name == "echo"
    assert handler_call.raw_arguments == '{"text": "hi"}'
    assert handler_call.arguments == {"text": "hi"}
    assert call.arguments == {"text": "hi"}

    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert [event.sequence for event in store.events] == [0, 1, 2]
    for event in store.events:
        assert event.session_id == "session-1"
        assert event.turn_id == "turn-1"
        assert event.step_id == "step-1"
        assert event.payload["tool_call_id"] == "call-1"
        assert event.payload["tool_name"] == "echo"

    # The intent snapshot is taken before the handler mutates its own copy.
    assert store.events[0].payload["arguments"] == {"text": "hi"}
    settled = store.events[-1]
    assert settled.payload["status"] == "succeeded"
    assert settled.payload["text"] == "hello back"
    assert settled.payload["error"] is None
    assert settled.payload["domain_refs"] == {
        "task_id": "task-1",
        "revision_id": "rev-1",
        "invocation_id": None,
        "attempt_id": None,
    }
    json.dumps(dict(settled.payload))


async def test_handler_receives_the_full_call_with_a_dynamic_id() -> None:
    store = _Store()
    seen: dict[str, Any] = {}

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        seen["tool_call_id"] = call.tool_call_id
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text="ok",
        )

    executor = ToolExecutorImpl([_registration(handler)], store)
    call = ToolCall("call-dynamic-42", "echo", {"text": "hi"})

    outcome = await executor.execute(call, _context())

    assert outcome.is_success
    assert seen["tool_call_id"] == "call-dynamic-42"
    assert outcome.tool_call_id == "call-dynamic-42"
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert all(
        event.payload["tool_call_id"] == "call-dynamic-42" for event in store.events
    )
    assert store.events[-1].payload["status"] == "succeeded"


async def test_handler_receives_a_deep_copy_of_arguments() -> None:
    store = _Store()

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        arguments = call.arguments_dict()
        arguments["payload"]["items"].append("mutated")
        return _succeeded(name="nested")

    parameters = {
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "properties": {"items": {"type": "array"}},
                "required": ["items"],
            }
        },
        "required": ["payload"],
    }
    executor = ToolExecutorImpl(
        [_registration(handler, name="nested", parameters=parameters)], store
    )
    original = {"payload": {"items": ["a"]}}
    call = ToolCall(
        tool_call_id="call-1",
        name="nested",
        arguments=original,
        raw_arguments=json.dumps(original),
    )

    outcome = await executor.execute(call, _context())

    assert outcome.is_success
    assert call.arguments == {"payload": {"items": ["a"]}}


async def test_unknown_tool_is_rejected_without_dispatch() -> None:
    store = _Store()
    executor = ToolExecutorImpl([], store)

    outcome = await executor.execute(ToolCall("call-1", "missing", {}), _context())

    assert outcome.status is ToolStatus.REJECTED
    assert outcome.error is not None
    assert outcome.error.code == "unknown_tool"
    assert [event.type for event in store.events] == [EventType.TOOL_SETTLED]
    assert store.events[0].payload["status"] == "rejected"
    assert store.events[0].payload["error"]["code"] == "unknown_tool"


@pytest.mark.parametrize(
    "policy",
    [
        ToolPolicy(allowed_tools=frozenset({"other"})),
        ToolPolicy(denied_tools=frozenset({"echo"})),
    ],
)
async def test_policy_denial_is_rejected_before_dispatch(policy: ToolPolicy) -> None:
    store = _Store()
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded()

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context(policy=policy)
    )

    assert outcome.status is ToolStatus.REJECTED
    assert outcome.error is not None
    assert outcome.error.code == "tool_denied"
    assert calls == 0
    assert [event.type for event in store.events] == [EventType.TOOL_SETTLED]
    assert store.events[0].payload["status"] == "rejected"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"text": 5},
        {"text": "hi", "extra": True},
    ],
)
async def test_invalid_arguments_are_rejected_before_dispatch(
    arguments: dict[str, Any],
) -> None:
    store = _Store()
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded()

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(ToolCall("call-1", "echo", arguments), _context())

    assert outcome.status is ToolStatus.REJECTED
    assert outcome.error is not None
    assert outcome.error.code == "invalid_arguments"
    assert calls == 0
    assert [event.type for event in store.events] == [EventType.TOOL_SETTLED]
    assert store.events[0].payload["status"] == "rejected"


@pytest.mark.parametrize(
    "raw",
    [
        '{"text": "hi"',
        "",
        "[]",
        "null",
    ],
)
async def test_bad_raw_arguments_are_rejected(raw: str) -> None:
    store = _Store()
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded()

    executor = ToolExecutorImpl([_registration(handler)], store)
    call = ToolCall(
        tool_call_id="call-1",
        name="echo",
        arguments={"text": "hi"},
        raw_arguments=raw,
    )

    outcome = await executor.execute(call, _context())

    assert outcome.status is ToolStatus.REJECTED
    assert outcome.error is not None
    assert outcome.error.code == "invalid_arguments"
    assert calls == 0
    assert [event.type for event in store.events] == [EventType.TOOL_SETTLED]


async def test_missing_raw_arguments_are_allowed() -> None:
    store = _Store()

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        return _succeeded()

    executor = ToolExecutorImpl([_registration(handler)], store)
    call = ToolCall("call-1", "echo", {"text": "hi"}, raw_arguments=None)

    outcome = await executor.execute(call, _context())

    assert outcome.is_success
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]


async def test_raw_arguments_override_the_parsed_mapping() -> None:
    store = _Store()
    seen: dict[str, Any] = {}

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        seen["call"] = call
        seen["arguments"] = call.arguments_dict()
        return _succeeded(text=seen["arguments"]["text"])

    executor = ToolExecutorImpl([_registration(handler)], store)
    call = ToolCall(
        tool_call_id="call-1",
        name="echo",
        arguments={"text": "from-mapping"},
        raw_arguments='{"text": "from-raw"}',
    )

    outcome = await executor.execute(call, _context())

    assert outcome.is_success
    assert seen["arguments"] == {"text": "from-raw"}
    assert outcome.text == "from-raw"
    handler_call = seen["call"]
    assert handler_call is not call
    assert handler_call.tool_call_id == "call-1"
    assert handler_call.name == "echo"
    assert handler_call.raw_arguments == '{"text": "from-raw"}'
    assert handler_call.arguments == {"text": "from-raw"}
    assert call.arguments == {"text": "from-mapping"}
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[0].payload["tool_name"] == "echo"
    assert store.events[0].payload["arguments"] == {"text": "from-raw"}
    assert sum(event.type is EventType.TOOL_SETTLED for event in store.events) == 1


async def test_raw_arguments_are_schema_validated_before_execution() -> None:
    store = _Store()
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded()

    executor = ToolExecutorImpl([_registration(handler)], store)
    call = ToolCall(
        tool_call_id="call-1",
        name="echo",
        arguments={"text": "present in the parsed mapping"},
        raw_arguments="{}",
    )

    outcome = await executor.execute(call, _context())

    assert outcome.status is ToolStatus.REJECTED
    assert outcome.error is not None
    assert outcome.error.code == "invalid_arguments"
    assert calls == 0
    assert [event.type for event in store.events] == [EventType.TOOL_SETTLED]


async def test_handler_exception_becomes_failed_without_retry() -> None:
    store = _Store()
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "tool_handler_error"
    assert "boom" in outcome.error.message
    assert calls == 1
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    settled = store.events[-1]
    assert settled.payload["status"] == "failed"
    assert settled.payload["error"]["code"] == "tool_handler_error"
    assert "boom" in settled.payload["error"]["message"]


@pytest.mark.parametrize(
    "result",
    [
        ToolOutcome("other-call", "echo", ToolStatus.SUCCEEDED, "ok"),
        ToolOutcome("call-1", "other", ToolStatus.SUCCEEDED, "ok"),
    ],
)
async def test_handler_outcome_must_match_the_call(result: ToolOutcome) -> None:
    store = _Store()

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        return result

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "invalid_tool_outcome"
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[-1].payload["status"] == "failed"
    assert store.events[-1].payload["error"]["code"] == "invalid_tool_outcome"


async def test_handler_must_return_a_tool_outcome() -> None:
    store = _Store()

    async def handler(call: ToolCall, context: ExecutionContext) -> Any:
        return "not an outcome"

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "invalid_tool_outcome"
    assert store.events[-1].payload["status"] == "failed"


async def test_running_outcome_is_returned_without_settlement() -> None:
    store = _Store()

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        return ToolOutcome("call-1", "echo", ToolStatus.RUNNING, "working")

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.RUNNING
    assert not outcome.is_terminal
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
    ]


async def test_cancelled_before_dispatch_never_calls_the_handler() -> None:
    store = _Store()
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded()

    token = CancellationToken()
    token.request("user stop")
    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context(token=token)
    )

    assert outcome.status is ToolStatus.CANCELLED
    assert outcome.error is None
    assert calls == 0
    assert [event.type for event in store.events] == [EventType.TOOL_SETTLED]
    assert store.events[0].payload["status"] == "cancelled"


async def test_cancellation_during_intent_append_prevents_dispatch() -> None:
    token = CancellationToken()
    store = _CancellingStore(token)
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded()

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context(token=token)
    )

    assert outcome.status is ToolStatus.CANCELLED
    assert calls == 0
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[-1].payload["status"] == "cancelled"


async def test_cancellation_after_dispatch_is_unknown_and_managed() -> None:
    store = _Store()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        finished.set()
        return _succeeded(text="late success that must be ignored")

    executor = ToolExecutorImpl([_registration(handler)], store)
    token = CancellationToken()
    running = asyncio.create_task(
        executor.execute(
            ToolCall("call-1", "echo", {"text": "hi"}),
            _context(token=token),
        )
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    token.request("user stop")
    outcome = await asyncio.wait_for(running, timeout=1)

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.error is None
    assert "unknown" in outcome.text.lower()
    assert "do not retry automatically" in outcome.text.lower()
    assert calls == 1
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[-1].payload["status"] == "unknown"
    assert len(executor.pending_handler_tasks) == 1

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    await _drain(executor)

    assert executor.pending_handler_tasks == ()
    assert calls == 1
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[-1].payload["status"] == "unknown"
    # The late handler completion must not settle the call a second time.
    assert sum(event.type is EventType.TOOL_SETTLED for event in store.events) == 1


async def test_external_task_cancellation_settles_unknown_and_reraises() -> None:
    store = _Store()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        finished.set()
        return _succeeded(text="late success that must be ignored")

    executor = ToolExecutorImpl([_registration(handler)], store)
    running = asyncio.create_task(
        executor.execute(ToolCall("call-1", "echo", {"text": "hi"}), _context())
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert calls == 1
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[-1].payload["status"] == "unknown"
    assert len(executor.pending_handler_tasks) == 1

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    await _drain(executor)

    assert calls == 1
    assert store.events[-1].payload["status"] == "unknown"
    # The late handler completion must not settle the call a second time.
    assert sum(event.type is EventType.TOOL_SETTLED for event in store.events) == 1


@pytest.mark.parametrize("exc_type", [asyncio.CancelledError, OperationCancelledError])
async def test_handler_cancellation_after_dispatch_is_unknown(
    exc_type: type[BaseException],
) -> None:
    store = _Store()

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        raise exc_type("stop")

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.error is None
    assert "unknown" in outcome.text.lower()
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[-1].payload["status"] == "unknown"


async def test_intent_write_failure_prevents_handler_and_dispatch() -> None:
    store = _Store({1: RuntimeError("intent disk")})
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded()

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "tool_intent_write_failed"
    assert calls == 0
    assert store.events == []
    assert store.append_calls == 1


async def test_dispatched_write_failure_prevents_handler() -> None:
    store = _Store({2: RuntimeError("dispatch disk")})
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded()

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "tool_dispatched_write_failed"
    assert calls == 0
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_SETTLED,
    ]
    assert store.events[-1].payload["status"] == "failed"
    assert store.events[-1].payload["error"]["code"] == "tool_dispatched_write_failed"
    assert store.append_calls == 3


async def test_settled_write_failure_is_unknown_with_local_status_kept() -> None:
    store = _Store({3: RuntimeError("settle disk")})
    calls = 0

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        nonlocal calls
        calls += 1
        return _succeeded(text="worked")

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.is_terminal
    assert not outcome.is_success
    assert outcome.error is not None
    assert outcome.error.code == "tool_settled_write_failed"
    assert outcome.content is not None
    assert outcome.content["settled"] is False
    assert outcome.content["local_status"] == "succeeded"
    assert "durable record is unknown" in outcome.text
    assert calls == 1
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
    ]
    assert store.append_calls == 3


async def test_rejection_that_cannot_be_settled_is_reported_as_unknown() -> None:
    store = _Store({1: RuntimeError("settle disk")})
    executor = ToolExecutorImpl([], store)

    outcome = await executor.execute(ToolCall("call-1", "missing", {}), _context())

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.is_terminal
    assert outcome.error is not None
    assert outcome.error.code == "tool_settled_write_failed"
    assert outcome.content is not None
    assert outcome.content["settled"] is False
    assert outcome.content["local_status"] == "rejected"
    assert outcome.content["local_error"]["code"] == "unknown_tool"


async def test_settled_payload_truncates_text_but_keeps_identity_and_error() -> None:
    store = _Store()
    long_text = "x" * 5000

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        return ToolOutcome(
            "call-1",
            "echo",
            ToolStatus.FAILED,
            long_text,
            error=ToolError("boom", "failure detail"),
        )

    executor = ToolExecutorImpl([_registration(handler)], store)

    outcome = await executor.execute(
        ToolCall("call-1", "echo", {"text": "hi"}), _context()
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.text == long_text
    settled = store.events[-1]
    assert settled.payload["tool_call_id"] == "call-1"
    assert settled.payload["tool_name"] == "echo"
    assert settled.payload["status"] == "failed"
    assert settled.payload["error"] == {"code": "boom", "message": "failure detail"}
    assert len(settled.payload["text"]) < len(long_text)
    assert settled.payload["text"].endswith("...[truncated]")

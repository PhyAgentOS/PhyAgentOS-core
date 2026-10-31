"""Tests for the agent core loop driver.

The driver is pinned on three things: the model is never called before its
request snapshot is durable, only the driver-owned events are written, and an
unfinished or truncated step becomes a clear failure instead of a reply.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace

import pytest

from PhyAgentOS.agent.core import AgentRuntimeImpl, SQLiteSessionStore
from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.loop import LoopDriverImpl
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    PromptBundle,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolStatus,
    TurnRequest,
    TurnStart,
    TurnStatus,
)
from PhyAgentOS.agent.services.context_policy import ContextPolicy
from PhyAgentOS.agent.services.turn_budget import TurnBudget

_DRIVER_EVENTS = frozenset(
    {
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.MODEL_ATTEMPT_FAILED,
        EventType.MODEL_ATTEMPT_INTERRUPTED,
    }
)


def _echo_definition() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Echo one string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


def _echo_call(call_id: str = "call-1", **overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "tool_call_id": call_id,
        "name": "echo",
        "arguments": {"text": "a"},
    }
    values.update(overrides)
    return ToolCall(**values)  # type: ignore[arg-type]


def _turn(
    text: str = "hi",
    *,
    max_steps: int = 4,
    deadline: float | None = None,
    history: tuple[dict, ...] = (),
) -> TurnStart:
    return TurnStart(
        request=TurnRequest(session_id="session-1", text=text),
        turn_id="turn-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=max_steps, deadline=deadline),
        history=history,
    )


class _Assembler:
    def __init__(self) -> None:
        self.calls: list[tuple[ExecutionContext, list[dict], list[ToolDefinition]]] = []

    async def assemble(
        self,
        context: ExecutionContext,
        *,
        history: tuple[dict, ...],
        tools: tuple[ToolDefinition, ...],
    ) -> PromptBundle:
        self.calls.append((context, list(history), list(tools)))
        return PromptBundle(
            messages=(*history, {"role": "system", "content": "sys"}),
            tools=tools,
        )


class _Adapter:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, ModelResponse)
        return item


class _PendingAdapter:
    """Adapter that never answers until it is cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ModelResponse(content="late", finish_reason="stop")


class _AnswerThenCancelAdapter:
    """Requests cancellation while answering with a tool call."""

    def __init__(self, turn: TurnStart, call: ToolCall) -> None:
        self._turn = turn
        self._call = call

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._turn.cancellation.request("stop after the answer")
        return ModelResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=(self._call,),
        )


class _Executor:
    def __init__(
        self,
        outcomes: list[ToolOutcome] | None = None,
        *,
        on_execute: Callable[[ToolCall, ExecutionContext], None] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.outcomes = list(outcomes or [])
        self.on_execute = on_execute
        self.error = error
        self.calls: list[tuple[ToolCall, ExecutionContext]] = []

    async def execute(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        self.calls.append((call, context))
        if self.on_execute is not None:
            self.on_execute(call, context)
        if self.error is not None:
            raise self.error
        if self.outcomes:
            return self.outcomes.pop(0)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=f"ok:{call.name}",
        )


class _Store:
    def __init__(self, fail_on: frozenset[EventType] = frozenset()) -> None:
        self.fail_on = set(fail_on)
        self.events: list[RuntimeEvent] = []

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        if event.type in self.fail_on:
            raise OSError("simulated store failure")
        stored = replace(event, sequence=len(self.events))
        self.events.append(stored)
        return stored


def _driver(
    assembler: _Assembler,
    adapter: _Adapter,
    executor: _Executor,
    store: _Store,
    *,
    tools: tuple[ToolDefinition, ...] = (),
    max_parallel_tools: int = 4,
    context_policy: object = None,
    turn_budget: object = None,
    steering: object = None,
) -> LoopDriverImpl:
    return LoopDriverImpl(
        assembler=assembler,
        adapter=adapter,
        executor=executor,
        store=store,
        tools=tools,
        model="fake-model",
        temperature=0.1,
        max_tokens=64,
        reasoning_effort="low",
        max_parallel_tools=max_parallel_tools,
        context_policy=context_policy,
        turn_budget=turn_budget,
        steering=steering,
    )


def _event_types(store: _Store) -> list[EventType]:
    return [event.type for event in store.events]


async def test_text_answer_completes_the_turn() -> None:
    assembler = _Assembler()
    adapter = _Adapter([ModelResponse(content="hello", finish_reason="stop")])
    executor = _Executor()
    store = _Store()

    result = await _driver(assembler, adapter, executor, store).run(_turn("  hi  "))

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "hello"
    assert result.error is None
    assert len(adapter.requests) == 1
    assert assembler.calls[0][1] == [{"role": "user", "content": "  hi  "}]
    assert _event_types(store) == [
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
    ]


async def test_empty_text_answer_is_a_normal_completion() -> None:
    adapter = _Adapter([ModelResponse(content="", finish_reason="stop")])
    result = await _driver(_Assembler(), adapter, _Executor(), _Store()).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == ""


async def test_completed_turn_history_precedes_the_current_user_text() -> None:
    history = (
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier reply"},
    )
    assembler = _Assembler()
    adapter = _Adapter([ModelResponse(content="now done", finish_reason="stop")])
    store = _Store()
    turn = _turn("now", history=history)

    result = await _driver(assembler, adapter, _Executor(), store).run(turn)

    assert result.status is TurnStatus.COMPLETED
    messages = adapter.requests[0].to_messages()
    assert messages[:2] == list(history)
    assert messages[2] == {"role": "user", "content": "now"}
    prepared = [e for e in store.events if e.type is EventType.REQUEST_PREPARED][0]
    assert prepared.payload["messages"] == messages
    assert len(turn.history) == 2


async def test_tool_result_is_returned_to_the_model_before_the_reply() -> None:
    assembler = _Assembler()
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(),),
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    executor = _Executor()
    store = _Store()
    tool = _echo_definition()

    result = await _driver(
        assembler, adapter, executor, store, tools=(tool,)
    ).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "done"
    assert len(adapter.requests) == 2
    assert len(executor.calls) == 1

    second_messages = adapter.requests[1].to_messages()
    assistant = [m for m in second_messages if m["role"] == "assistant"][0]
    tool_message = [m for m in second_messages if m["role"] == "tool"][0]
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert tool_message == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "echo",
        "content": "ok:echo",
    }
    assert assembler.calls[1][2] == [tool]
    assert assembler.calls[0][0].step_id != assembler.calls[1][0].step_id


async def test_request_snapshot_records_messages_tools_and_parameters() -> None:
    assembler = _Assembler()
    adapter = _Adapter([ModelResponse(content="hello", finish_reason="stop")])
    store = _Store()
    tool = _echo_definition()

    driver = _driver(assembler, adapter, store=store, executor=_Executor(), tools=(tool,))
    await driver.run(_turn())

    prepared = [e for e in store.events if e.type is EventType.REQUEST_PREPARED][0]
    sent = adapter.requests[0]

    assert prepared.payload["model_call_id"] == sent.model_call_id
    assert prepared.payload["messages"] == sent.to_messages()
    assert prepared.payload["tools"] == [tool.to_provider_dict()]
    assert prepared.payload["model"] == "fake-model"
    assert prepared.payload["temperature"] == 0.1
    assert prepared.payload["max_tokens"] == 64
    assert prepared.payload["reasoning_effort"] == "low"
    assert "api_key" not in prepared.payload


async def test_provider_tool_call_fields_and_raw_arguments_survive_the_round_trip() -> None:
    call = _echo_call(
        raw_arguments='{"text": "a"}',
        provider_fields={"index": 0},
        function_provider_fields={"signature": "sig"},
    )
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(call,),
                reasoning_content="why",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )

    await _driver(_Assembler(), adapter, _Executor(), _Store()).run(_turn())

    assistant = [
        m for m in adapter.requests[1].to_messages() if m["role"] == "assistant"
    ][0]
    forwarded = assistant["tool_calls"][0]
    assert assistant["content"] is None
    assert assistant["reasoning_content"] == "why"
    assert forwarded["function"]["arguments"] == '{"text": "a"}'
    assert forwarded["provider_specific_fields"] == {"index": 0}
    assert forwarded["function"]["provider_specific_fields"] == {"signature": "sig"}


async def test_thinking_blocks_and_tool_calls_are_kept_in_message_and_event() -> None:
    call = _echo_call(raw_arguments='{"text": "a"}', provider_fields={"index": 0})
    blocks = (
        {"type": "thinking", "thinking": "hmm"},
        {"signature": "sig"},
    )
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(call,),
                reasoning_content="why",
                thinking_blocks=blocks,
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    store = _Store()

    await _driver(_Assembler(), adapter, _Executor(), store).run(_turn())

    assistant = [
        m for m in adapter.requests[1].to_messages() if m["role"] == "assistant"
    ][0]
    assert isinstance(assistant["thinking_blocks"], list)
    assert assistant["thinking_blocks"] == [dict(block) for block in blocks]

    completed = [
        e for e in store.events if e.type is EventType.MODEL_ATTEMPT_COMPLETED
    ][0]
    assert completed.payload["reasoning_content"] == "why"
    assert completed.payload["thinking_blocks"] == [dict(block) for block in blocks]
    assert completed.payload["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "echo", "arguments": '{"text": "a"}'},
            "provider_specific_fields": {"index": 0},
        }
    ]


async def test_tool_messages_follow_the_model_call_order() -> None:
    calls = (_echo_call("call-1"), _echo_call("call-2"))
    adapter = _Adapter(
        [
            ModelResponse(content=None, finish_reason="tool_calls", tool_calls=calls),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    executor = _Executor()

    await _driver(_Assembler(), adapter, executor, _Store()).run(_turn())

    tool_messages = [
        m for m in adapter.requests[1].to_messages() if m["role"] == "tool"
    ]
    assert [m["tool_call_id"] for m in tool_messages] == ["call-1", "call-2"]
    assert [call.tool_call_id for call, _ in executor.calls] == ["call-1", "call-2"]


async def test_truncated_tool_call_fails_without_executing_anything() -> None:
    call = _echo_call(raw_arguments='{"text": "a"')
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="length",
                tool_calls=(call,),
                usage={"total_tokens": 9},
                reasoning_content="thinking",
            )
        ]
    )
    executor = _Executor()
    store = _Store()

    result = await _driver(_Assembler(), adapter, executor, store).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "truncated" in result.error
    assert executor.calls == []
    completed = [
        e for e in store.events if e.type is EventType.MODEL_ATTEMPT_COMPLETED
    ][0]
    assert completed.payload["finish_reason"] == "length"
    assert completed.payload["usage"] == {"total_tokens": 9}
    assert completed.payload["reasoning_content"] == "thinking"


@pytest.mark.parametrize(
    ("fail_on", "expected_types"),
    [
        (frozenset({EventType.REQUEST_PREPARED}), []),
        (
            frozenset({EventType.MODEL_ATTEMPT_STARTED}),
            [EventType.REQUEST_PREPARED],
        ),
    ],
)
async def test_failed_pre_write_prevents_the_model_call(
    fail_on: frozenset[EventType], expected_types: list[EventType]
) -> None:
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])
    store = _Store(fail_on=fail_on)

    result = await _driver(_Assembler(), adapter, _Executor(), store).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert result.reply is None
    assert adapter.requests == []
    assert _event_types(store) == expected_types


async def test_model_error_fails_without_becoming_an_assistant_reply() -> None:
    adapter = _Adapter([ModelCallError("model-x", "provider exploded")])
    store = _Store()

    result = await _driver(_Assembler(), adapter, _Executor(), store).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert result.reply is None
    assert result.error is not None and "provider exploded" in result.error
    assert _event_types(store)[-1] is EventType.MODEL_ATTEMPT_FAILED
    failed = store.events[-1]
    assert failed.payload["error"] == "provider exploded"
    assert EventType.MODEL_ATTEMPT_COMPLETED not in _event_types(store)


async def test_unexpected_adapter_exception_becomes_a_clear_failure() -> None:
    adapter = _Adapter([RuntimeError("socket closed")])
    result = await _driver(_Assembler(), adapter, _Executor(), _Store()).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "socket closed" in result.error


async def test_running_tool_fails_the_turn_and_is_not_shown_to_the_model() -> None:
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(),),
            ),
            ModelResponse(content="should not be requested", finish_reason="stop"),
        ]
    )
    running = ToolOutcome(
        tool_call_id="call-1",
        tool_name="echo",
        status=ToolStatus.RUNNING,
        text="working",
    )
    store = _Store()

    result = await _driver(
        _Assembler(), adapter, _Executor(outcomes=[running]), store
    ).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "running" in result.error
    assert len(adapter.requests) == 1
    assert _event_types(store) == [
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
    ]


@pytest.mark.parametrize(
    "code",
    [
        "tool_intent_write_failed",
        "tool_dispatched_write_failed",
        "tool_settled_write_failed",
    ],
)
async def test_core_persistence_failure_stops_the_turn_immediately(code: str) -> None:
    calls = (_echo_call("call-1"), _echo_call("call-2"))
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=calls,
            ),
            ModelResponse(content="pretend success", finish_reason="stop"),
        ]
    )
    failed_outcome = ToolOutcome(
        tool_call_id="call-1",
        tool_name="echo",
        status=ToolStatus.FAILED,
        text="durability failed",
        error=ToolError(code, "disk write failed"),
    )
    executor = _Executor(outcomes=[failed_outcome])
    store = _Store()

    result = await _driver(_Assembler(), adapter, executor, store).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert result.reply is None
    assert result.error is not None and code in result.error
    assert len(adapter.requests) == 1
    assert len(executor.calls) == 1
    assert _event_types(store) == [
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
    ]


async def test_ordinary_tool_failure_is_still_shown_to_the_model() -> None:
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(),),
            ),
            ModelResponse(content="recovered", finish_reason="stop"),
        ]
    )
    failed_outcome = ToolOutcome(
        tool_call_id="call-1",
        tool_name="echo",
        status=ToolStatus.FAILED,
        text="handler exploded",
        error=ToolError("handler_error", "boom"),
    )

    result = await _driver(
        _Assembler(), adapter, _Executor(outcomes=[failed_outcome]), _Store()
    ).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "recovered"
    tool_message = [
        m for m in adapter.requests[1].to_messages() if m["role"] == "tool"
    ][0]
    assert tool_message["content"] == "Tool failed: handler exploded"


async def test_cancelled_turn_stops_before_any_model_or_tool_call() -> None:
    turn = _turn()
    turn.cancellation.request("user stop")
    assembler = _Assembler()
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])
    executor = _Executor()
    store = _Store()

    result = await _driver(assembler, adapter, executor, store).run(turn)

    assert result.status is TurnStatus.INTERRUPTED
    assert assembler.calls == []
    assert adapter.requests == []
    assert executor.calls == []
    assert store.events == []


async def test_cancel_requested_during_a_tool_blocks_the_next_model_call() -> None:
    def request_stop(call: ToolCall, context: ExecutionContext) -> None:
        context.cancellation.request("stop during tool")

    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(),),
            ),
            ModelResponse(content="must not be requested", finish_reason="stop"),
        ]
    )

    result = await _driver(
        _Assembler(), adapter, _Executor(on_execute=request_stop), _Store()
    ).run(_turn())

    assert result.status is TurnStatus.INTERRUPTED
    assert len(adapter.requests) == 1


async def test_pending_model_call_stops_when_the_token_is_requested() -> None:
    turn = _turn()
    adapter = _PendingAdapter()
    executor = _Executor()
    store = _Store()
    runner = _driver(_Assembler(), adapter, executor, store)
    before = asyncio.all_tasks()

    task = asyncio.create_task(runner.run(turn))
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    turn.cancellation.request("user stop")
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status is TurnStatus.INTERRUPTED
    assert adapter.cancelled
    assert executor.calls == []
    assert _event_types(store) == [
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_INTERRUPTED,
    ]
    assert asyncio.all_tasks() - before == set()


async def test_completed_answer_after_token_request_does_not_run_tools() -> None:
    turn = _turn()
    adapter = _AnswerThenCancelAdapter(turn, _echo_call())
    executor = _Executor()
    store = _Store()

    result = await _driver(_Assembler(), adapter, executor, store).run(turn)

    assert result.status is TurnStatus.INTERRUPTED
    assert executor.calls == []
    types = _event_types(store)
    assert EventType.MODEL_ATTEMPT_COMPLETED in types
    assert EventType.MODEL_ATTEMPT_INTERRUPTED not in types


async def test_external_cancellation_is_not_recorded_as_a_completed_answer() -> None:
    turn = _turn()
    adapter = _PendingAdapter()
    executor = _Executor()
    store = _Store()
    runner = _driver(_Assembler(), adapter, executor, store)
    before = asyncio.all_tasks()

    task = asyncio.create_task(runner.run(turn))
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.cancelled
    assert executor.calls == []
    assert EventType.MODEL_ATTEMPT_COMPLETED not in _event_types(store)
    assert asyncio.all_tasks() - before == set()


async def test_expired_deadline_fails_before_calling_the_model() -> None:
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])
    result = await _driver(_Assembler(), adapter, _Executor(), _Store()).run(
        _turn(deadline=time.monotonic() - 1)
    )

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "deadline" in result.error
    assert adapter.requests == []


async def test_step_budget_exhaustion_fails_instead_of_completing() -> None:
    tool_response = ModelResponse(
        content=None,
        finish_reason="tool_calls",
        tool_calls=(_echo_call(),),
    )
    adapter = _Adapter([tool_response, tool_response])
    executor = _Executor()

    result = await _driver(_Assembler(), adapter, executor, _Store()).run(
        _turn(max_steps=2)
    )

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "2 steps" in result.error
    assert len(adapter.requests) == 2
    assert len(executor.calls) == 2


async def test_driver_writes_only_its_own_events() -> None:
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(),),
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    store = _Store()

    await _driver(_Assembler(), adapter, _Executor(), store).run(_turn())

    assert set(_event_types(store)) <= _DRIVER_EVENTS
    assert all(event.turn_id == "turn-1" for event in store.events)
    step_ids = {event.step_id for event in store.events}
    assert len(step_ids) == 2
    model_call_ids = [
        event.payload["model_call_id"]
        for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_STARTED
    ]
    assert len(set(model_call_ids)) == 2


class _Reduction:
    def __init__(
        self,
        messages: list[dict],
        before_tokens: int,
        after_tokens: int,
        reason: str = "test reduction",
    ) -> None:
        self.messages = tuple(messages)
        self.before_tokens = before_tokens
        self.after_tokens = after_tokens
        self.reason = reason


class _ContextPolicy:
    def __init__(self, handler: Callable[[tuple, bool], object]) -> None:
        self._handler = handler
        self.calls: list[tuple[ExecutionContext, list[dict], tuple, bool]] = []

    async def prepare(
        self,
        context: ExecutionContext,
        history: tuple,
        tools: tuple,
        *,
        force: bool = False,
    ) -> object:
        self.calls.append((context, [dict(message) for message in history], tools, force))
        return self._handler(history, force)


class _Steering:
    def __init__(self, texts: list[tuple[str, ...]]) -> None:
        self.texts = list(texts)
        self.calls: list[tuple[str, str]] = []

    def drain(self, session_id: str, turn_id: str) -> tuple[str, ...]:
        self.calls.append((session_id, turn_id))
        return self.texts.pop(0) if self.texts else ()


class _BrokenSteering:
    def drain(self, session_id: str, turn_id: str) -> tuple[str, ...]:
        raise RuntimeError("steering backend down")


async def test_context_policy_reduction_is_persisted_before_the_model_call() -> None:
    reduction = _Reduction(
        [
            {"role": "system", "content": "summary of earlier work"},
            {"role": "user", "content": "hi"},
        ],
        100,
        40,
        "trim older turns",
    )
    policy = _ContextPolicy(lambda history, force: reduction if not force else None)
    adapter = _Adapter([ModelResponse(content="done", finish_reason="stop")])
    store = _Store()

    result = await _driver(
        _Assembler(), adapter, _Executor(), store, context_policy=policy
    ).run(_turn("hi"))

    assert result.status is TurnStatus.COMPLETED
    types = _event_types(store)
    assert types == [
        EventType.CONTEXT_COMPACTED,
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
    ]
    compacted = store.events[0]
    assert compacted.turn_id == "turn-1"
    assert compacted.step_id is None
    assert compacted.payload["before_tokens"] == 100
    assert compacted.payload["after_tokens"] == 40
    assert compacted.payload["reason"] == "trim older turns"
    assert compacted.payload["messages"] == list(reduction.messages)
    assert adapter.requests[0].to_messages()[:2] == list(reduction.messages)
    assert policy.calls[0][3] is False
    assert policy.calls[0][1] == [{"role": "user", "content": "hi"}]
    assert policy.calls[0][2] == ()


async def test_context_policy_none_leaves_history_and_events_unchanged() -> None:
    policy = _ContextPolicy(lambda history, force: None)
    adapter = _Adapter([ModelResponse(content="done", finish_reason="stop")])
    store = _Store()

    result = await _driver(
        _Assembler(), adapter, _Executor(), store, context_policy=policy
    ).run(_turn("hi"))

    assert result.status is TurnStatus.COMPLETED
    assert EventType.CONTEXT_COMPACTED not in _event_types(store)
    assert adapter.requests[0].to_messages() == [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "sys"},
    ]
    assert len(policy.calls) == 1


async def test_context_policy_invalid_reduction_fails_before_the_model_call() -> None:
    policy = _ContextPolicy(lambda history, force: _Reduction([], -1, 0))
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])
    store = _Store()

    result = await _driver(
        _Assembler(), adapter, _Executor(), store, context_policy=policy
    ).run(_turn("hi"))

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "invalid reduction" in result.error
    assert adapter.requests == []
    assert store.events == []


async def test_context_overflow_retries_once_after_a_forced_reduction() -> None:
    reduction = _Reduction([{"role": "user", "content": "short"}], 1000, 100, "forced")
    policy = _ContextPolicy(
        lambda history, force: reduction if force else None
    )
    adapter = _Adapter(
        [
            ModelCallError("model-x", "too long", code="context_overflow"),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    store = _Store()

    result = await _driver(
        _Assembler(), adapter, _Executor(), store, context_policy=policy
    ).run(_turn("hi"))

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "done"
    assert len(adapter.requests) == 2
    assert adapter.requests[0].model_call_id != adapter.requests[1].model_call_id
    assert [call[3] for call in policy.calls] == [False, True]
    assert adapter.requests[1].to_messages()[0] == {
        "role": "user",
        "content": "short",
    }
    types = _event_types(store)
    assert types == [
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_FAILED,
        EventType.CONTEXT_COMPACTED,
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
    ]
    assert store.events[2].payload["error"] == "too long"


async def test_context_overflow_without_a_real_reduction_is_not_retried() -> None:
    policy = _ContextPolicy(lambda history, force: None)
    adapter = _Adapter(
        [ModelCallError("model-x", "too long", code="context_overflow")]
    )

    result = await _driver(
        _Assembler(), adapter, _Executor(), _Store(), context_policy=policy
    ).run(_turn("hi"))

    assert result.status is TurnStatus.FAILED
    assert len(adapter.requests) == 1
    assert [call[3] for call in policy.calls] == [False, True]


async def test_context_overflow_is_retried_at_most_once() -> None:
    reduction = _Reduction([{"role": "user", "content": "short"}], 1000, 100, "forced")
    policy = _ContextPolicy(lambda history, force: reduction if force else None)
    adapter = _Adapter(
        [
            ModelCallError("model-x", "too long", code="context_overflow"),
            ModelCallError("model-y", "still too long", code="context_overflow"),
        ]
    )

    result = await _driver(
        _Assembler(), adapter, _Executor(), _Store(), context_policy=policy
    ).run(_turn("hi"))

    assert result.status is TurnStatus.FAILED
    assert len(adapter.requests) == 2
    assert [call[3] for call in policy.calls] == [False, True]


async def test_steering_text_is_persisted_then_used_at_the_next_safe_step() -> None:
    steering = _Steering([(), ("correct it",)])
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(),),
            ),
            ModelResponse(content="fixed", finish_reason="stop"),
        ]
    )
    store = _Store()

    result = await _driver(
        _Assembler(), adapter, _Executor(), store, steering=steering
    ).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert steering.calls == [("session-1", "turn-1"), ("session-1", "turn-1")]
    types = _event_types(store)
    assert types.count(EventType.INPUT_STEERED) == 1
    steered = [event for event in store.events if event.type is EventType.INPUT_STEERED][0]
    assert steered.payload["text"] == "correct it"
    assert steered.turn_id == "turn-1"
    assert steered.step_id is not None
    request_messages = adapter.requests[1].to_messages()
    assert request_messages[-2] == {"role": "user", "content": "correct it"}
    assert EventType.INPUT_STEERED in types


async def test_steering_failure_stops_before_the_model_call() -> None:
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])
    store = _Store()

    result = await _driver(
        _Assembler(), adapter, _Executor(), store, steering=_BrokenSteering()
    ).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "steering drain failed" in result.error
    assert adapter.requests == []
    assert store.events == []


async def test_reserved_summary_step_returns_partial_reply_and_stays_failed() -> None:
    config = TurnBudget(reserve_steps=1)
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(),),
            ),
            ModelResponse(content="partial progress summary", finish_reason="stop"),
        ]
    )
    executor = _Executor()
    store = _Store()

    result = await _driver(
        _Assembler(), adapter, executor, store, turn_budget=config
    ).run(_turn(max_steps=2))

    assert result.status is TurnStatus.FAILED
    assert result.reply == "partial progress summary"
    assert result.error is not None and "summary" in result.error
    assert len(adapter.requests) == 2
    assert adapter.requests[1].tools == ()
    assert adapter.requests[1].tool_choice == "none"
    assert len(executor.calls) == 1
    prepared = [
        event for event in store.events if event.type is EventType.REQUEST_PREPARED
    ]
    assert prepared[-1].payload["tools"] == []
    assert prepared[-1].payload["tool_choice"] == "none"


async def test_hard_token_budget_stops_before_dispatching_requested_tools() -> None:
    config = TurnBudget(max_tokens=5)
    adapter = _Adapter(
        [
            ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(),),
                usage={"total_tokens": 5},
            ),
            ModelResponse(content="never", finish_reason="stop"),
        ]
    )
    executor = _Executor()

    result = await _driver(
        _Assembler(), adapter, executor, _Store(), turn_budget=config
    ).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "token budget" in result.error
    assert executor.calls == []
    assert len(adapter.requests) == 1


async def test_turn_budget_config_keeps_independent_state_per_run() -> None:
    config = TurnBudget(max_tokens=5)
    first_adapter = _Adapter(
        [ModelResponse(content="first", finish_reason="stop", usage={"total_tokens": 5})]
    )
    second_adapter = _Adapter(
        [ModelResponse(content="second", finish_reason="stop", usage={"total_tokens": 5})]
    )

    first = await _driver(
        _Assembler(), first_adapter, _Executor(), _Store(), turn_budget=config
    ).run(_turn())
    second = await _driver(
        _Assembler(), second_adapter, _Executor(), _Store(), turn_budget=config
    ).run(_turn())

    assert first.status is TurnStatus.COMPLETED
    assert second.status is TurnStatus.COMPLETED
    assert len(first_adapter.requests) == 1
    assert len(second_adapter.requests) == 1


async def test_identical_no_progress_tool_results_stop_the_turn() -> None:
    config = TurnBudget(repeated_call_limit=3)
    repeated = ToolOutcome(
        tool_call_id="call-1",
        tool_name="echo",
        status=ToolStatus.FAILED,
        text="same failure",
        error=ToolError("same_error", "same failure"),
    )
    tool_response = ModelResponse(
        content=None,
        finish_reason="tool_calls",
        tool_calls=(_echo_call(),),
    )
    adapter = _Adapter([tool_response, tool_response, tool_response, tool_response])
    executor = _Executor(outcomes=[repeated, repeated, repeated])

    result = await _driver(
        _Assembler(), adapter, executor, _Store(), turn_budget=config
    ).run(_turn(max_steps=4))

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "no progress" in result.error
    assert len(executor.calls) == 3
    assert len(adapter.requests) == 3


async def test_one_step_turn_with_reserved_summary_still_gets_a_normal_request() -> None:
    config = TurnBudget(reserve_steps=1)
    adapter = _Adapter([ModelResponse(content="normal answer", finish_reason="stop")])

    result = await _driver(
        _Assembler(), adapter, _Executor(), _Store(), turn_budget=config
    ).run(_turn(max_steps=1))

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "normal answer"
    assert len(adapter.requests) == 1
    assert adapter.requests[0].tool_choice is None


async def test_overflow_retry_is_not_made_after_the_hard_token_cap_is_reached() -> None:
    reduction = _Reduction([{"role": "user", "content": "short"}], 1000, 100, "forced")
    policy = _ContextPolicy(lambda history, force: reduction if force else None)
    config = TurnBudget(max_tokens=5)
    adapter = _Adapter(
        [
            ModelCallError(
                "model-x",
                "too long",
                usage={"total_tokens": 5},
                code="context_overflow",
            ),
            ModelResponse(content="never requested", finish_reason="stop"),
        ]
    )
    store = _Store()

    result = await _driver(
        _Assembler(),
        adapter,
        _Executor(),
        store,
        context_policy=policy,
        turn_budget=config,
    ).run(_turn("hi"))

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "token budget" in result.error
    assert len(adapter.requests) == 1
    # The forced policy call would itself cost another model request.
    assert [call[3] for call in policy.calls] == [False]
    assert EventType.CONTEXT_COMPACTED not in _event_types(store)


class _NarrowingAssembler:
    """Hides one tool and appends a live fragment on every assembly."""

    def __init__(self) -> None:
        self.calls: list[tuple[ExecutionContext, list[dict], tuple]] = []

    async def assemble(
        self,
        context: ExecutionContext,
        *,
        history: tuple,
        tools: tuple[ToolDefinition, ...],
    ) -> PromptBundle:
        self.calls.append(
            (context, [dict(message) for message in history], tuple(tools))
        )
        visible = tuple(tool for tool in tools if tool.name != "hidden")
        return PromptBundle(
            messages=(
                *history,
                {
                    "role": "system",
                    "content": f"live-fragment-{len(self.calls)}",
                },
            ),
            tools=visible,
        )


class _RequestMessagesPolicy:
    """A newer policy that accepts the real assembled request messages."""

    def __init__(self, reduction: object = None, *, force_reduction: object = None) -> None:
        self.reduction = reduction
        self.force_reduction = force_reduction
        self.calls: list[tuple[ExecutionContext, list[dict], tuple, bool]] = []
        self.request_messages: list[list[dict] | None] = []

    async def prepare(
        self,
        context: ExecutionContext,
        history: tuple,
        tools: tuple,
        *,
        force: bool = False,
        request_messages: object = None,
    ) -> object:
        self.calls.append((context, [dict(message) for message in history], tools, force))
        self.request_messages.append(
            None
            if request_messages is None
            else [dict(message) for message in request_messages]
        )
        return self.force_reduction if force else self.reduction


def _hidden_tool() -> ToolDefinition:
    return ToolDefinition(
        name="hidden",
        description="Hidden tool.",
        parameters={"type": "object", "properties": {}},
    )


async def test_context_policy_uses_visible_tools_and_real_bundle_messages() -> None:
    visible = _echo_definition()
    hidden = _hidden_tool()
    assembler = _NarrowingAssembler()
    reduction = _Reduction([{"role": "user", "content": "compressed"}], 100, 40)
    policy = _RequestMessagesPolicy(reduction)
    adapter = _Adapter([ModelResponse(content="done", finish_reason="stop")])
    store = _Store()

    result = await _driver(
        assembler,
        adapter,
        _Executor(),
        store,
        tools=(visible, hidden),
        context_policy=policy,
    ).run(_turn("hi"))

    assert result.status is TurnStatus.COMPLETED
    # Measurement assembly plus one re-assembly after the reduction.
    assert len(assembler.calls) == 2
    assert assembler.calls[0][2] == (visible, hidden)
    assert assembler.calls[1][1] == [{"role": "user", "content": "compressed"}]

    _, policy_history, policy_tools, _ = policy.calls[0]
    assert policy_tools == (visible,)
    assert policy_history == [{"role": "user", "content": "hi"}]
    assert policy.request_messages[0] is not None
    assert policy.request_messages[0][-1] == {
        "role": "system",
        "content": "live-fragment-1",
    }
    # The dynamic fragment never enters the internal history or the checkpoint.
    compacted = [
        event for event in store.events if event.type is EventType.CONTEXT_COMPACTED
    ][0]
    assert compacted.payload["messages"] == [{"role": "user", "content": "compressed"}]
    assert adapter.requests[0].to_messages() == [
        {"role": "user", "content": "compressed"},
        {"role": "system", "content": "live-fragment-2"},
    ]


async def test_context_policy_without_reduction_reuses_the_single_assembly() -> None:
    visible = _echo_definition()
    assembler = _NarrowingAssembler()
    policy = _RequestMessagesPolicy(None)
    adapter = _Adapter([ModelResponse(content="done", finish_reason="stop")])

    result = await _driver(
        assembler,
        adapter,
        _Executor(),
        _Store(),
        tools=(visible, _hidden_tool()),
        context_policy=policy,
    ).run(_turn("hi"))

    assert result.status is TurnStatus.COMPLETED
    assert len(assembler.calls) == 1
    assert policy.request_messages[0] is not None
    assert policy.request_messages[0][-1]["content"] == "live-fragment-1"
    assert adapter.requests[0].to_messages()[-1] == {
        "role": "system",
        "content": "live-fragment-1",
    }


class _UsageDrainingPolicy:
    """Policy that also exposes the summary usage it observed."""

    def __init__(
        self,
        reduction: object = None,
        *,
        force_reduction: object = None,
        batches: tuple = (),
        error: BaseException | None = None,
    ) -> None:
        self.reduction = reduction
        self.force_reduction = force_reduction
        self.batches = list(batches)
        self.error = error
        self.drain_calls = 0

    async def prepare(
        self,
        context: ExecutionContext,
        history: tuple,
        tools: tuple,
        *,
        force: bool = False,
    ) -> object:
        if self.error is not None:
            raise self.error
        return self.force_reduction if force else self.reduction

    def drain_usage(self) -> tuple:
        self.drain_calls += 1
        return self.batches.pop(0) if self.batches else ()


async def test_context_preparation_usage_is_recorded_and_can_stop_the_turn() -> None:
    config = TurnBudget(max_tokens=5)
    policy = _UsageDrainingPolicy(batches=[({"total_tokens": 5},)])
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])

    result = await _driver(
        _Assembler(),
        adapter,
        _Executor(),
        _Store(),
        context_policy=policy,
        turn_budget=config,
    ).run(_turn("hi"))

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "token budget" in result.error
    assert adapter.requests == []
    assert policy.drain_calls == 1


async def test_context_preparation_usage_without_a_total_is_not_zero() -> None:
    config = TurnBudget(max_tokens=5)
    policy = _UsageDrainingPolicy(batches=[({"prompt_tokens": 3},)])
    adapter = _Adapter([ModelResponse(content="done", finish_reason="stop")])

    result = await _driver(
        _Assembler(),
        adapter,
        _Executor(),
        _Store(),
        context_policy=policy,
        turn_budget=config,
    ).run(_turn("hi"))

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "done"
    assert len(adapter.requests) == 1
    assert policy.drain_calls == 1


async def test_policy_usage_is_drained_even_when_prepare_fails() -> None:
    policy = _UsageDrainingPolicy(
        error=RuntimeError("summary backend down"),
        batches=[({"total_tokens": 5},)],
    )

    result = await _driver(
        _Assembler(),
        _Adapter([ModelResponse(content="never", finish_reason="stop")]),
        _Executor(),
        _Store(),
        context_policy=policy,
        turn_budget=TurnBudget(max_tokens=5),
    ).run(_turn("hi"))

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "context policy failed" in result.error
    assert policy.drain_calls == 1


async def test_forced_recovery_summary_usage_blocks_the_retry() -> None:
    config = TurnBudget(max_tokens=5)
    reduction = _Reduction([{"role": "user", "content": "short"}], 1000, 100, "forced")
    policy = _UsageDrainingPolicy(
        force_reduction=reduction,
        batches=[(), ({"total_tokens": 5},)],
    )
    adapter = _Adapter(
        [
            ModelCallError("model-x", "too long", code="context_overflow"),
            ModelResponse(content="never requested", finish_reason="stop"),
        ]
    )

    result = await _driver(
        _Assembler(),
        adapter,
        _Executor(),
        _Store(),
        context_policy=policy,
        turn_budget=config,
    ).run(_turn("hi"))

    assert result.status is TurnStatus.FAILED
    assert result.error is not None and "token budget" in result.error
    assert len(adapter.requests) == 1
    assert policy.drain_calls == 2


class _BlockingPolicy:
    """Context policy whose summary provider call is pending until cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.drain_calls = 0

    async def prepare(
        self,
        context: ExecutionContext,
        history: tuple,
        tools: tuple,
        *,
        force: bool = False,
    ) -> object:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return None

    def drain_usage(self) -> tuple:
        self.drain_calls += 1
        return ({"total_tokens": 2},)


async def test_cancel_during_context_summary_stops_before_any_model_call() -> None:
    policy = _BlockingPolicy()
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])
    turn = _turn()
    driver = _driver(_Assembler(), adapter, _Executor(), _Store(), context_policy=policy)
    before = asyncio.all_tasks()

    running = asyncio.create_task(driver.run(turn))
    await asyncio.wait_for(policy.started.wait(), timeout=1)
    turn.cancellation.request("user stop")
    result = await asyncio.wait_for(running, timeout=1)

    assert result.status is TurnStatus.INTERRUPTED
    assert policy.cancelled
    assert policy.drain_calls == 1
    assert adapter.requests == []
    assert asyncio.all_tasks() - before == set()


async def test_external_cancel_during_context_summary_reaps_and_drains() -> None:
    policy = _BlockingPolicy()
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])
    driver = _driver(_Assembler(), adapter, _Executor(), _Store(), context_policy=policy)
    before = asyncio.all_tasks()

    running = asyncio.create_task(driver.run(_turn()))
    await asyncio.wait_for(policy.started.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert policy.cancelled
    assert policy.drain_calls == 1
    assert adapter.requests == []
    assert asyncio.all_tasks() - before == set()


class _CancelThenReturnPolicy:
    def __init__(self, turn: TurnStart) -> None:
        self._turn = turn
        self.drain_calls = 0

    async def prepare(
        self,
        context: ExecutionContext,
        history: tuple,
        tools: tuple,
        *,
        force: bool = False,
    ) -> object:
        self._turn.cancellation.request("stop before the model call")
        return None

    def drain_usage(self) -> tuple:
        self.drain_calls += 1
        return ()


async def test_token_set_while_prepare_settles_wins_over_the_model_call() -> None:
    turn = _turn()
    policy = _CancelThenReturnPolicy(turn)
    adapter = _Adapter([ModelResponse(content="never", finish_reason="stop")])

    result = await _driver(
        _Assembler(), adapter, _Executor(), _Store(), context_policy=policy
    ).run(turn)

    assert result.status is TurnStatus.INTERRUPTED
    assert adapter.requests == []
    assert policy.drain_calls == 1


class _BlockingSummaryProvider:
    """Real ContextPolicy summary provider: pending until cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def chat_with_retry(self, messages: object, tools: object = None, **kwargs: object) -> object:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError as exc:
            self.cancelled = True
            exc.usage = {
                "total_tokens": 4,
                "prompt_tokens": 3,
                "completion_tokens": 1,
            }
            raise
        raise AssertionError("unreachable")


async def test_runtime_cancel_during_real_context_summary_is_bounded(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "context-cancel.db")
    provider = _BlockingSummaryProvider()
    policy = ContextPolicy(
        store,
        provider,
        model="fake-model",
        capacity=100_000,
        reserve_tokens=1_000,
        threshold=10,
        system_instructions="sys",
        estimator=lambda messages, tools: 1_000,
        provider_name="fake-provider",
    )
    adapter = _Adapter([ModelResponse(content="first", finish_reason="stop")])
    driver = LoopDriverImpl(
        assembler=_Assembler(),
        adapter=adapter,
        executor=_Executor(),
        store=store,
        model="fake-model",
        context_policy=policy,
        turn_budget=TurnBudget(),
    )
    runtime = AgentRuntimeImpl(driver=driver, store=store, max_steps=4)

    first = await runtime.submit(TurnRequest(session_id="s1", text="first"))
    assert first.status is TurnStatus.COMPLETED

    second = asyncio.create_task(
        runtime.submit(TurnRequest(session_id="s1", text="second"))
    )
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    await asyncio.wait_for(runtime.cancel("s1"), timeout=1)
    elapsed = loop.time() - started_at
    result = await asyncio.wait_for(second, timeout=1)

    assert elapsed < 1
    assert result.status is TurnStatus.INTERRUPTED
    assert provider.cancelled
    assert provider.calls == 1
    # The only model request was the completed first turn; the blocked
    # summary never turned into a normal model call.
    assert len(adapter.requests) == 1

    attempts = await store.list_compaction_attempts("s1")
    assert len(attempts) == 1
    assert attempts[0].status == "interrupted"
    assert attempts[0].usage == {
        "total_tokens": 4,
        "prompt_tokens": 3,
        "completion_tokens": 1,
    }
    await runtime.aclose()

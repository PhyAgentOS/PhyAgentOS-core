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

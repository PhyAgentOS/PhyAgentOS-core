"""Streaming display contract for the core loop driver.

These tests pin the parts an entry point depends on: body text reaches the
event log before the model call finishes, the terminal answer is recorded once,
batches do not turn every token into a database write, a provider without a
stream keeps the old path, and a failed stream keeps its partial text and known
usage without being retried or dispatching a malformed tool call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.loop import LoopDriverImpl
from PhyAgentOS.agent.core.model import ModelAdapter
from PhyAgentOS.agent.core.tools import ToolExecutorImpl
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    ModelStreamDelta,
    PromptBundle,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
    TurnRequest,
    TurnStart,
    TurnStatus,
)
from PhyAgentOS.providers.base import LLMResponse, StreamEvent


class _Assembler:
    async def assemble(
        self,
        context: ExecutionContext,
        *,
        history: tuple[dict, ...],
        tools: tuple[ToolDefinition, ...],
    ) -> PromptBundle:
        return PromptBundle(
            messages=(*history, {"role": "system", "content": "sys"}),
            tools=tools,
        )


class _Store:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        stored = replace(event, sequence=len(self.events) + 1)
        self.events.append(stored)
        return stored

    def types(self) -> list[EventType]:
        return [event.type for event in self.events]

    def deltas(self) -> list[str]:
        return [
            event.payload["content"]
            for event in self.events
            if event.type is EventType.MODEL_RESPONSE_DELTA
        ]


class _Executor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        self.calls.append(call)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text="ok",
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


def _turn() -> TurnStart:
    return TurnStart(
        request=TurnRequest(session_id="session-1", text="hi"),
        turn_id="turn-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _driver(adapter: object, executor: object, store: _Store, *,
            tools: tuple[ToolDefinition, ...] = ()) -> LoopDriverImpl:
    return LoopDriverImpl(
        assembler=_Assembler(),
        adapter=adapter,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        tools=tools,
        model="fake-model",
    )


class _BlockingStreamAdapter:
    """Yields one delta, then waits for the test before the terminal response."""

    def __init__(self, content: str = "Hello world", usage: dict | None = None) -> None:
        self.content = content
        self.usage = usage or {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.stream_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        return ModelResponse(content=self.content, finish_reason="stop")

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamDelta | ModelResponse]:
        return self._events()

    async def _events(self) -> AsyncIterator[ModelStreamDelta | ModelResponse]:
        self.stream_calls += 1
        try:
            yield ModelStreamDelta("Hello ")
            self.started.set()
            await self.release.wait()
            yield ModelStreamDelta("world")
            yield ModelResponse(
                content=self.content,
                finish_reason="stop",
                usage=self.usage,
            )
        finally:
            self.closed = True


class _CompleteOnlyAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content="blocking answer",
            finish_reason="stop",
            usage={
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
                "cached_tokens": 3,
                "reasoning_tokens": 1,
            },
        )


class _FailingStreamAdapter:
    def __init__(self, usage: dict[str, int]) -> None:
        self.usage = usage
        self.stream_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise AssertionError("complete must not be called when a stream exists")

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamDelta | ModelResponse]:
        return self._events()

    async def _events(self) -> AsyncIterator[ModelStreamDelta | ModelResponse]:
        self.stream_calls += 1
        yield ModelStreamDelta("first ")
        yield ModelStreamDelta("second")
        raise ModelCallError("step:model", "connection reset", usage=self.usage)


class _SlowStreamAdapter:
    """Yields a short first delta, pauses, then yields more and finishes."""

    def __init__(self) -> None:
        self.first = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise AssertionError("stream must be used")

    def stream(self, request: ModelRequest):
        return self._events()

    async def _events(self):
        yield ModelStreamDelta("好")
        self.first.set()
        await asyncio.sleep(0.3)
        yield ModelStreamDelta("世界")
        await asyncio.sleep(0.3)
        yield ModelResponse(content="好世界", finish_reason="stop")


async def test_delta_is_durable_before_the_terminal_answer() -> None:
    adapter = _BlockingStreamAdapter()
    executor = _Executor()
    store = _Store()
    task = asyncio.create_task(_driver(adapter, executor, store).run(_turn()))

    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    # The provider has not produced a terminal response yet, but the first
    # batch is already in the append-only log.
    assert store.deltas() == ["Hello "]
    assert EventType.MODEL_ATTEMPT_COMPLETED not in store.types()

    adapter.release.set()
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "Hello world"
    assert "".join(store.deltas()) == "Hello world"
    completed = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].payload["content"] == "Hello world"
    assert completed[0].payload["usage"]["total_tokens"] == 5
    # One terminal answer, not one assistant message per delta.
    assert store.types().count(EventType.MODEL_ATTEMPT_COMPLETED) == 1


async def test_text_is_batched_instead_of_one_write_per_delta() -> None:
    class _ManyDeltas:
        async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
            return ModelResponse(content="", finish_reason="stop")

        def stream(self, request: ModelRequest):
            return self._events()

        async def _events(self):
            for _ in range(100):
                yield ModelStreamDelta("a")
            yield ModelResponse(content="a" * 100, finish_reason="stop")

    store = _Store()
    result = await _driver(_ManyDeltas(), _Executor(), store).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert "".join(store.deltas()) == "a" * 100
    delta_writes = store.types().count(EventType.MODEL_RESPONSE_DELTA)
    assert 1 < delta_writes < 100


async def test_adapter_without_stream_keeps_the_blocking_path() -> None:
    adapter = _CompleteOnlyAdapter()
    store = _Store()
    result = await _driver(adapter, _Executor(), store).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert adapter.calls == 1
    assert EventType.MODEL_RESPONSE_DELTA not in store.types()
    completed = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_COMPLETED
    ][0]
    assert completed.payload["streaming"] == "blocking"
    assert completed.payload["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "total_tokens": 6,
        "cached_tokens": 3,
        "reasoning_tokens": 1,
    }


class _CompleteOnlyProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="fallback answer", finish_reason="stop")


async def test_stream_adapter_fallback_is_observable_in_completed_event() -> None:
    provider = _CompleteOnlyProvider()
    adapter = ModelAdapter(provider)
    store = _Store()

    result = await _driver(adapter, _Executor(), store).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "fallback answer"
    assert provider.calls == 1
    assert EventType.MODEL_RESPONSE_DELTA not in store.types()
    completed = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_COMPLETED
    ][0]
    assert completed.payload["streaming"] == "fallback"


class _NativeStreamProvider:
    async def chat_with_retry(self, messages, tools=None, **kwargs):  # pragma: no cover
        raise AssertionError("native stream must be used")

    async def chat_stream(self, messages, tools=None, **kwargs):
        yield StreamEvent(type="delta", content="na")
        yield StreamEvent(type="delta", content="tive")
        yield StreamEvent(
            type="done",
            finish_reason="stop",
            usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        )


async def test_native_stream_completes_with_native_marker_and_full_text() -> None:
    adapter = ModelAdapter(_NativeStreamProvider())
    store = _Store()

    result = await _driver(adapter, _Executor(), store).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "native"
    assert "".join(store.deltas()) == "native"
    completed = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_COMPLETED
    ][0]
    assert completed.payload["streaming"] == "native"
    assert completed.payload["content"] == "native"
    assert completed.payload["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
    }


async def test_error_event_usage_is_merged_before_raising() -> None:
    class _UsageThenError:
        async def chat_with_retry(self, messages, tools=None, **kwargs):  # pragma: no cover
            raise AssertionError("stream must be used")

        async def chat_stream(self, messages, tools=None, **kwargs):
            yield StreamEvent(type="delta", content="partial")
            yield StreamEvent(
                type="error",
                content="connection reset",
                finish_reason="error",
                usage={"prompt_tokens": 3, "cached_tokens": 2},
            )

    adapter = ModelAdapter(_UsageThenError())
    request = ModelRequest(
        model_call_id="step:model", messages=[{"role": "user", "content": "hi"}]
    )

    seen: list[str] = []
    with pytest.raises(ModelCallError) as excinfo:
        async for item in adapter.stream(request):
            if isinstance(item, ModelStreamDelta):
                seen.append(item.content)

    assert seen == ["partial"]
    assert dict(excinfo.value.usage) == {"prompt_tokens": 3, "cached_tokens": 2}


async def test_failed_stream_keeps_partial_text_usage_and_is_not_retried() -> None:
    adapter = _FailingStreamAdapter({"prompt_tokens": 7, "completion_tokens": 1})
    executor = _Executor()
    store = _Store()

    result = await _driver(adapter, executor, store).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert adapter.stream_calls == 1
    assert executor.calls == []
    # Both the immediately flushed first delta and the short, still-buffered
    # second delta are durable even though the model never finished.
    assert store.deltas() == ["first ", "second"]
    assert EventType.MODEL_ATTEMPT_COMPLETED not in store.types()
    failed = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_FAILED
    ]
    assert len(failed) == 1
    assert failed[0].payload["error"]
    assert failed[0].payload["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 1,
    }
    assert "total_tokens" not in failed[0].payload["usage"]


async def test_slow_stream_is_flushed_by_idle_time_before_terminal() -> None:
    adapter = _SlowStreamAdapter()
    store = _Store()
    task = asyncio.create_task(_driver(adapter, _Executor(), store).run(_turn()))

    await asyncio.wait_for(adapter.first.wait(), timeout=1)
    # The provider has yielded a second short delta and is sleeping again; the
    # terminal response is still pending. The one-character tail cannot reach
    # the character threshold, so only the idle timer can have flushed it.
    for _ in range(100):
        if store.deltas() == ["好", "世界"]:
            break
        await asyncio.sleep(0.01)

    assert store.deltas() == ["好", "世界"]
    assert EventType.MODEL_ATTEMPT_COMPLETED not in store.types()

    result = await asyncio.wait_for(task, timeout=2)

    assert result.status is TurnStatus.COMPLETED
    assert "".join(store.deltas()) == "好世界"
    completed = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_COMPLETED
    ][0]
    assert completed.payload["content"] == "好世界"


async def test_cancelled_native_stream_keeps_usage_seen_before_the_stop() -> None:
    class _UsageThenBlock:
        async def chat_with_retry(self, messages, tools=None, **kwargs):  # pragma: no cover
            raise AssertionError("stream must be used")

        async def chat_stream(self, messages, tools=None, **kwargs):
            yield StreamEvent(type="delta", content="a")
            yield StreamEvent(
                type="usage",
                usage={"prompt_tokens": 11, "cached_tokens": 4},
            )
            await asyncio.Event().wait()

    adapter = ModelAdapter(_UsageThenBlock())
    store = _Store()
    turn = _turn()
    task = asyncio.create_task(_driver(adapter, _Executor(), store).run(turn))

    for _ in range(100):
        if store.deltas():
            break
        await asyncio.sleep(0.01)
    assert store.deltas() == ["a"]

    turn.cancellation.request("stop")
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status is TurnStatus.INTERRUPTED
    interrupted = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_INTERRUPTED
    ]
    assert len(interrupted) == 1
    assert interrupted[0].payload["usage"] == {
        "prompt_tokens": 11,
        "cached_tokens": 4,
    }
    assert EventType.MODEL_ATTEMPT_COMPLETED not in store.types()


async def test_truncated_tool_payload_is_rejected_before_dispatch(tmp_path) -> None:
    malformed = ToolCall(
        tool_call_id="call-1",
        name="echo",
        arguments={"text": "a"},
        raw_arguments='{"text": "a"',  # closes improperly: truncated
    )

    class _MalformedThenFinal:
        def __init__(self) -> None:
            self.calls = 0
            self.requests: list[ModelRequest] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
            raise AssertionError("streaming adapter must use stream")

        def stream(self, request: ModelRequest):
            self.requests.append(request)
            return self._events()

        async def _events(self):
            self.calls += 1
            if self.calls == 1:
                yield ModelResponse(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=(malformed,),
                )
                return
            yield ModelResponse(content="done", finish_reason="stop")

    handler_calls: list[ToolCall] = []

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        handler_calls.append(call)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text="ran",
        )

    store = _Store()
    executor = ToolExecutorImpl(
        [ToolRegistration(definition=_echo_definition(), handler=handler)],
        store,  # type: ignore[arg-type]
    )
    adapter = _MalformedThenFinal()
    result = await _driver(
        adapter, executor, store, tools=(_echo_definition(),)
    ).run(_turn())

    assert result.status is TurnStatus.COMPLETED
    assert handler_calls == []
    # The next request sees one complete assistant message and the settlement,
    # not one message per streamed delta.
    second_messages = adapter.requests[1].to_messages()
    assistant_messages = [
        message for message in second_messages if message["role"] == "assistant"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["tool_calls"][0]["id"] == "call-1"
    settled = [
        event for event in store.events if event.type is EventType.TOOL_SETTLED
    ]
    assert len(settled) == 1
    assert settled[0].payload["status"] == ToolStatus.REJECTED.value
    assert settled[0].payload["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("bad_yield", [object(), 5, "delta"])
async def test_unexpected_stream_item_fails_without_dispatch(bad_yield: object) -> None:
    class _BadStream:
        async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
            return ModelResponse(content="", finish_reason="stop")

        def stream(self, request: ModelRequest):
            return self._events()

        async def _events(self):
            yield bad_yield

    executor = _Executor()
    store = _Store()
    result = await _driver(_BadStream(), executor, store).run(_turn())

    assert result.status is TurnStatus.FAILED
    assert executor.calls == []
    failed = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_FAILED
    ]
    assert len(failed) == 1
    assert "yielded" in failed[0].payload["error"]

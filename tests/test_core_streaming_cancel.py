"""Cancellation contract for streamed model calls.

A local stop must stop waiting immediately, close the provider stream, leave a
traceable ``model.attempt.interrupted`` record, and never complete the attempt
or dispatch a tool. The same cleanup must happen when the caller cancels the
driver task externally.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.loop import LoopDriverImpl
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ModelRequest,
    ModelResponse,
    ModelStreamDelta,
    PromptBundle,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolStatus,
    TurnRequest,
    TurnStart,
    TurnStatus,
)


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
        self.first_delta = asyncio.Event()

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        stored = replace(event, sequence=len(self.events) + 1)
        self.events.append(stored)
        if event.type is EventType.MODEL_RESPONSE_DELTA:
            self.first_delta.set()
        return stored

    def types(self) -> list[EventType]:
        return [event.type for event in self.events]


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


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Echo one string.",
        parameters={"type": "object", "properties": {}},
    )


def _turn() -> TurnStart:
    return TurnStart(
        request=TurnRequest(session_id="session-1", text="hi"),
        turn_id="turn-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


class _StalledStreamAdapter:
    """Yields one delta and then waits forever until the stream is closed."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise AssertionError("stream must be used")

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamDelta | ModelResponse]:
        return self._events()

    async def _events(self) -> AsyncIterator[ModelStreamDelta | ModelResponse]:
        try:
            yield ModelStreamDelta("partial")
            self.started.set()
            await asyncio.Event().wait()
        finally:
            self.closed = True


def _driver(adapter: object, executor: object, store: _Store) -> LoopDriverImpl:
    return LoopDriverImpl(
        assembler=_Assembler(),
        adapter=adapter,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        tools=(_definition(),),
        model="fake-model",
    )


class _FakeStream:
    """Adapter stream double exposing the usage the loop reads on cancel."""

    def __init__(self, events, usage: dict[str, int]) -> None:
        self._events = events
        self.usage = usage
        self.native = True

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self):
        return await self._events.__anext__()

    async def aclose(self) -> None:
        await self._events.aclose()


class _TwoDeltasThenStallAdapter:
    def __init__(self, usage: dict[str, int]) -> None:
        self.usage = usage
        self.started = asyncio.Event()
        self.closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise AssertionError("stream must be used")

    def stream(self, request: ModelRequest):
        return self._events()

    def _events(self):
        async def events():
            try:
                yield ModelStreamDelta("first ")
                yield ModelStreamDelta("second")
                self.started.set()
                await asyncio.Event().wait()
            finally:
                self.closed = True

        return _FakeStream(events(), self.usage)


async def test_token_cancel_keeps_both_partial_batches_and_known_usage() -> None:
    adapter = _TwoDeltasThenStallAdapter(
        {"prompt_tokens": 5, "completion_tokens": 2, "cached_tokens": 3}
    )
    executor = _Executor()
    store = _Store()
    turn = _turn()
    task = asyncio.create_task(_driver(adapter, executor, store).run(turn))

    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    await asyncio.wait_for(store.first_delta.wait(), timeout=1)
    turn.cancellation.request("user stop")
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status is TurnStatus.INTERRUPTED
    assert adapter.closed
    assert executor.calls == []
    deltas = [
        event.payload["content"]
        for event in store.events
        if event.type is EventType.MODEL_RESPONSE_DELTA
    ]
    assert deltas == ["first ", "second"]
    assert all("usage" not in event.payload for event in store.events
               if event.type is EventType.MODEL_RESPONSE_DELTA)
    interrupted = [
        event for event in store.events
        if event.type is EventType.MODEL_ATTEMPT_INTERRUPTED
    ]
    assert len(interrupted) == 1
    assert interrupted[0].payload["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "cached_tokens": 3,
    }
    assert EventType.MODEL_ATTEMPT_COMPLETED not in store.types()


async def test_token_cancel_closes_stream_and_does_not_complete() -> None:
    adapter = _StalledStreamAdapter()
    executor = _Executor()
    store = _Store()
    turn = _turn()
    driver = _driver(adapter, executor, store)
    task = asyncio.create_task(driver.run(turn))

    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    turn.cancellation.request("user stop")
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status is TurnStatus.INTERRUPTED
    assert adapter.closed
    assert executor.calls == []
    assert EventType.MODEL_ATTEMPT_INTERRUPTED in store.types()
    assert EventType.MODEL_ATTEMPT_COMPLETED not in store.types()
    assert store.types().count(EventType.MODEL_RESPONSE_DELTA) == 1


async def test_external_task_cancel_closes_stream_and_keeps_no_success() -> None:
    adapter = _StalledStreamAdapter()
    executor = _Executor()
    store = _Store()
    task = asyncio.create_task(_driver(adapter, executor, store).run(_turn()))

    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.closed
    assert executor.calls == []
    assert EventType.MODEL_ATTEMPT_COMPLETED not in store.types()


class _BeforeFirstTokenAdapter:
    """Blocks before the first delta so cancellation has no text to keep."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise AssertionError("stream must be used")

    def stream(self, request: ModelRequest):
        return self._events()

    async def _events(self):
        try:
            self.started.set()
            await asyncio.Event().wait()
            yield ModelStreamDelta("never")
        finally:
            self.closed = True


async def test_cancel_before_first_delta_writes_no_completion() -> None:
    adapter = _BeforeFirstTokenAdapter()
    turn = _turn()
    store = _Store()
    task = asyncio.create_task(_driver(adapter, _Executor(), store).run(turn))

    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    turn.cancellation.request("stop before first token")
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status is TurnStatus.INTERRUPTED
    assert adapter.closed
    assert EventType.MODEL_ATTEMPT_COMPLETED not in store.types()
    assert EventType.MODEL_RESPONSE_DELTA not in store.types()

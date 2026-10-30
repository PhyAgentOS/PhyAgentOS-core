"""Provider usage normalization: cache and reasoning details.

The entry point reports tokens from ``usage``. These tests pin that the
normalizer keeps the existing three keys, adds cached/reasoning detail only
when the provider actually reported it, and never double-counts cached input or
reasoning output in the totals.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
from openai import AsyncOpenAI

from PhyAgentOS.agent.core.events import EventType
from PhyAgentOS.agent.core.host import AgentHostImpl
from PhyAgentOS.agent.core.loop import LoopDriverImpl
from PhyAgentOS.agent.core.model import ModelAdapter
from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl
from PhyAgentOS.agent.core.runtime import AgentRuntimeImpl
from PhyAgentOS.agent.core.session import SQLiteSessionStore
from PhyAgentOS.agent.core.tools import ToolExecutorImpl
from PhyAgentOS.agent.core.types import ModelRequest, ModelResponse, TurnRequest, TurnStatus
from PhyAgentOS.providers.azure_openai_provider import AzureOpenAIProvider
from PhyAgentOS.providers.base import LLMProvider, LLMResponse
from PhyAgentOS.providers.custom_provider import CustomProvider
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.openai_responses_provider import OpenAIResponsesProvider


def _openai_usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        prompt_tokens_details=SimpleNamespace(cached_tokens=40),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=20),
    )


def _responses_usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        input_tokens_details=SimpleNamespace(cached_tokens=30),
        output_tokens_details=SimpleNamespace(reasoning_tokens=10),
    )


def test_openai_chat_usage_keeps_totals_and_adds_details() -> None:
    assert LLMProvider._usage_to_dict(_openai_usage()) == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 40,
        "reasoning_tokens": 20,
    }


def test_responses_usage_maps_input_output_and_details() -> None:
    assert LLMProvider._usage_to_dict(_responses_usage()) == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 30,
        "reasoning_tokens": 10,
    }


def test_nested_dict_usage_is_supported() -> None:
    raw = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 25},
        "completion_tokens_details": {"reasoning_tokens": 5},
    }
    assert LLMProvider._usage_to_dict(raw) == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 25,
        "reasoning_tokens": 5,
    }


def test_unknown_detail_is_absent_not_zero() -> None:
    raw = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    usage = LLMProvider._usage_to_dict(raw)

    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }
    assert "cached_tokens" not in usage
    assert "reasoning_tokens" not in usage


def test_provider_native_cache_fields_are_read() -> None:
    deepseek = {"prompt_tokens": 8, "completion_tokens": 2, "prompt_cache_hit_tokens": 6}
    anthropic = {"prompt_tokens": 8, "completion_tokens": 2, "cache_read_input_tokens": 4}

    assert LLMProvider._usage_to_dict(deepseek) == {
        "prompt_tokens": 8,
        "completion_tokens": 2,
        "cached_tokens": 6,
    }
    assert LLMProvider._usage_to_dict(anthropic) == {
        "prompt_tokens": 8,
        "completion_tokens": 2,
        "cached_tokens": 4,
    }


def test_cache_and_reasoning_are_not_added_to_totals() -> None:
    usage = LLMProvider._usage_to_dict(_openai_usage())

    # cached_tokens is a subset of prompt_tokens; reasoning_tokens is a subset
    # of completion_tokens. Neither is added a second time.
    assert usage["total_tokens"] == 150
    assert usage["prompt_tokens"] + usage["completion_tokens"] == 150


def test_custom_provider_parse_keeps_usage_details() -> None:
    provider = CustomProvider(api_key="k", api_base="http://localhost:1/v1")
    message = SimpleNamespace(content="done", tool_calls=None, reasoning_content=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=_openai_usage(),
    )

    parsed = provider._parse(response)

    assert parsed.usage == LLMProvider._usage_to_dict(_openai_usage())


def test_litellm_provider_parse_keeps_usage_details() -> None:
    provider = LiteLLMProvider(
        api_key="k", api_base="http://localhost:1/v1", default_model="test-model"
    )
    message = SimpleNamespace(
        content="done", tool_calls=None, reasoning_content=None, thinking_blocks=None
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=_openai_usage(),
    )

    parsed = provider._parse_response(response)

    assert parsed.usage["cached_tokens"] == 40
    assert parsed.usage["reasoning_tokens"] == 20
    assert parsed.usage["prompt_tokens"] == 100


def test_azure_parse_keeps_nested_usage_details() -> None:
    provider = AzureOpenAIProvider(api_key="k", api_base="https://example.openai.azure.com")
    response = {
        "choices": [
            {"message": {"content": "done", "tool_calls": None}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 25},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }

    parsed = provider._parse_response(response)

    assert parsed.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 25,
        "reasoning_tokens": 5,
    }


def test_responses_parse_keeps_usage_details() -> None:
    provider = OpenAIResponsesProvider(api_key="k", api_base="http://localhost:1/v1")
    response = SimpleNamespace(
        output=[],
        output_text="done",
        status="completed",
        usage=_responses_usage(),
    )

    parsed = provider._parse(response)

    assert parsed.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 30,
        "reasoning_tokens": 10,
    }


class _AsyncStream:
    def __init__(self, items: list[Any]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        return None


def _chunk(*, content: str | None = None, finish_reason: str | None = None, usage: Any = None):
    delta = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        thinking_blocks=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


async def _collect(stream):
    return [event async for event in stream]


async def test_stream_done_reports_usage_details() -> None:
    provider = CustomProvider(api_key="k", api_base="http://localhost:1/v1")
    stream = _AsyncStream(
        [
            _chunk(content="ok"),
            _chunk(finish_reason="stop", usage=_openai_usage()),
        ]
    )
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_returning(stream)))
    )

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["delta", "usage", "done"]
    assert events[1].usage == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 40,
        "reasoning_tokens": 20,
    }
    assert events[-1].type == "done"
    assert events[-1].usage == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 40,
        "reasoning_tokens": 20,
    }


async def test_stream_error_keeps_known_usage() -> None:
    provider = CustomProvider(api_key="k", api_base="http://localhost:1/v1")
    usage = SimpleNamespace(
        prompt_tokens=9,
        completion_tokens=2,
        total_tokens=11,
        prompt_tokens_details=SimpleNamespace(cached_tokens=4),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=1),
    )
    stream = _AsyncStream([SimpleNamespace(choices=[], usage=usage)])
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_returning(stream)))
    )

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["usage", "error"]
    assert events[0].usage == {
        "prompt_tokens": 9,
        "completion_tokens": 2,
        "total_tokens": 11,
        "cached_tokens": 4,
        "reasoning_tokens": 1,
    }
    assert events[1].usage == {
        "prompt_tokens": 9,
        "completion_tokens": 2,
        "total_tokens": 11,
        "cached_tokens": 4,
        "reasoning_tokens": 1,
    }


async def test_inherited_chat_stream_is_treated_as_non_streaming_fallback() -> None:
    class _ChatOnlyProvider(LLMProvider):
        def get_default_model(self) -> str:
            return "chat-only"

        async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                       temperature=0.7, reasoning_effort=None, tool_choice=None):
            return LLMResponse(content="answer", finish_reason="stop")

    provider = _ChatOnlyProvider()
    assert provider.supports_native_stream is False

    stream = ModelAdapter(provider).stream(
        ModelRequest(
            model_call_id="step:model",
            messages=[{"role": "user", "content": "hi"}],
        )
    )
    items = [item async for item in stream]

    assert stream.native is False
    assert len(items) == 1
    assert isinstance(items[0], ModelResponse)
    assert items[0].content == "answer"


async def test_early_usage_is_observed_and_prevents_a_retry() -> None:
    usage = SimpleNamespace(
        prompt_tokens=80, completion_tokens=3, total_tokens=83
    )
    stream = _AsyncStream(
        [
            SimpleNamespace(choices=[], usage=usage),
            ConnectionError("connection reset by peer"),
        ]
    )
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return stream

    provider = CustomProvider(api_key="k", api_base="http://localhost:1/v1")
    provider._CHAT_RETRY_DELAYS = (0,)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert len(calls) == 1
    assert [event.type for event in events] == ["usage", "error"]
    assert events[0].usage == {
        "prompt_tokens": 80,
        "completion_tokens": 3,
        "total_tokens": 83,
    }
    assert events[1].usage == {
        "prompt_tokens": 80,
        "completion_tokens": 3,
        "total_tokens": 83,
    }


class _GatedSSE(httpx.AsyncByteStream):
    """Two real SSE chunks, then a gate the test releases by cancelling."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.gate = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        await self.gate.wait()

    async def aclose(self) -> None:
        self.closed = True
        self.gate.set()


async def test_openai_sdk_usage_chunk_survives_cancel_before_terminal() -> None:
    """End-to-end through the real AsyncOpenAI client and an SSE MockTransport.

    The SDK reports usage before the answer finishes. A local cancel while the
    socket is still open must keep that usage on ``model.attempt.interrupted``,
    close the response stream, and leave exactly one POST (no replay).
    """
    usage_chunk = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fake-model",
        "choices": [],
        "usage": {"prompt_tokens": 80, "completion_tokens": 3, "total_tokens": 83},
    }
    delta_chunk = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fake-model",
        "choices": [
            {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
        ],
    }
    stream = _GatedSSE([
        f"data: {_json(usage_chunk)}\n\n".encode(),
        f"data: {_json(delta_chunk)}\n\n".encode(),
    ])
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    client = AsyncOpenAI(
        api_key="test-key",
        base_url="http://test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = CustomProvider(api_key="test-key", api_base="http://test/v1")
    provider._client = client
    store = SQLiteSessionStore(":memory:")
    driver = LoopDriverImpl(
        assembler=PromptAssemblerImpl("system"),
        adapter=ModelAdapter(provider),
        executor=ToolExecutorImpl((), store),
        store=store,
        model="fake-model",
    )
    runtime = AgentRuntimeImpl(driver=driver, store=store, max_steps=2)
    host = AgentHostImpl(runtime=runtime, capabilities=())
    try:
        task = asyncio.create_task(
            host.submit(TurnRequest(session_id="sdk:test", text="hi"))
        )
        for _ in range(300):
            events = await store.read_session("sdk:test")
            if any(
                event.type is EventType.MODEL_RESPONSE_DELTA for event in events
            ):
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - a hang here is the failure
            raise AssertionError("no display delta arrived")

        await host.cancel("sdk:test")
        result = await asyncio.wait_for(task, timeout=3)

        assert result.status is TurnStatus.INTERRUPTED
        events = await store.read_session("sdk:test")
        deltas = [
            event.payload["content"]
            for event in events
            if event.type is EventType.MODEL_RESPONSE_DELTA
        ]
        assert deltas == ["partial"]
        interrupted = [
            event for event in events
            if event.type is EventType.MODEL_ATTEMPT_INTERRUPTED
        ]
        assert len(interrupted) == 1
        assert interrupted[0].payload["usage"] == {
            "prompt_tokens": 80,
            "completion_tokens": 3,
            "total_tokens": 83,
        }
        assert EventType.MODEL_ATTEMPT_COMPLETED not in {
            event.type for event in events
        }
        assert len(posts) == 1
        assert stream.closed
    finally:
        await host.aclose()
        await client.close()


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def _returning(value: Any):
    async def create(**kwargs: Any) -> Any:
        return value

    return create

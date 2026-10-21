"""Tests for provider streaming and the structured agent event model."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from PhyAgentOS.bus import AgentEvent, AgentEventType, OutboundMessage
from PhyAgentOS.providers.base import (
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    StreamEvent,
    ToolCallRequest,
)
from PhyAgentOS.providers.custom_provider import CustomProvider
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.openai_codex_provider import OpenAICodexProvider
from PhyAgentOS.providers.openai_responses_provider import OpenAIResponsesProvider

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tool_fragment(
    index: int | None,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
    provider_specific_fields: dict[str, Any] | None = None,
    function_provider_specific_fields: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        id=call_id,
        provider_specific_fields=provider_specific_fields,
        function=SimpleNamespace(
            name=name,
            arguments=arguments,
            provider_specific_fields=function_provider_specific_fields,
        ),
    )


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
    usage: Any = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
    role: str | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
        thinking_blocks=thinking_blocks,
        role=role,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _usage_chunk(usage: Any) -> SimpleNamespace:
    return SimpleNamespace(choices=[], usage=usage)


class _AsyncStream:
    def __init__(self, items: list[Any]) -> None:
        self._items = iter(items)
        self.closed = 0

    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        try:
            item = next(self._items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed += 1


class _BlockingStream:
    """Yields one chunk, then blocks so the caller can cancel the stream."""

    def __init__(self, first: Any) -> None:
        self._first: Any = first
        self.closed = 0

    def __aiter__(self) -> "_BlockingStream":
        return self

    async def __anext__(self) -> Any:
        if self._first is not None:
            item, self._first = self._first, None
            return item
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed += 1


async def _collect(stream: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in stream]


def _litellm_provider() -> LiteLLMProvider:
    return LiteLLMProvider(
        api_key="test-key",
        api_base="http://localhost:9/v1",
        default_model="test-model",
    )


def _install_litellm(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(responses)

    async def fake_acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(
        "PhyAgentOS.providers.litellm_provider.acompletion", fake_acompletion
    )
    return calls


class _CompletionsStub:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _OpenAIClientStub:
    def __init__(self, responses: list[Any]) -> None:
        self.completions = _CompletionsStub(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def _custom_provider(responses: list[Any]) -> tuple[CustomProvider, _OpenAIClientStub]:
    provider = CustomProvider(
        api_key="test-key",
        api_base="http://localhost:9/v1",
        default_model="test-model",
    )
    stub = _OpenAIClientStub(responses)
    provider._client = stub
    return provider, stub


# ---------------------------------------------------------------------------
# A5: agent event model
# ---------------------------------------------------------------------------


def test_outbound_message_event_field_is_optional_and_backwards_compatible() -> None:
    legacy = OutboundMessage(channel="cli", chat_id="1", content="hi")
    assert legacy.event is None
    assert legacy.reply_to is None
    assert legacy.media == []
    assert legacy.metadata == {}

    event = AgentEvent(
        type=AgentEventType.TEXT_DELTA,
        content="hi",
        turn_id="turn-1",
    )
    enriched = OutboundMessage(channel="cli", chat_id="1", content="", event=event)
    assert enriched.event is event


def test_agent_event_type_values_and_defaults() -> None:
    assert AgentEventType.TEXT_DELTA == "text_delta"
    assert AgentEventType.TOOL_PROGRESS == "tool_progress"
    assert AgentEventType.TURN_STARTED == "turn_started"
    assert AgentEventType.TURN_DONE == "turn_done"
    assert AgentEventType.ERROR == "error"
    assert AgentEventType.CANCELLED == "cancelled"

    event = AgentEvent(type=AgentEventType.TURN_DONE)
    assert event.content is None
    assert event.metadata == {}
    assert event.turn_id is None


# ---------------------------------------------------------------------------
# A6: LiteLLM streaming
# ---------------------------------------------------------------------------


async def test_litellm_stream_text_deltas_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_litellm(
        monkeypatch,
        [
            _AsyncStream([
                _chunk(content="Hel"),
                _chunk(content="lo"),
                _chunk(finish_reason="stop"),
            ])
        ],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "hi"}]))

    assert [event.type for event in events] == ["delta", "delta", "done"]
    assert [event.content for event in events if event.type == "delta"] == ["Hel", "lo"]
    assert "".join(event.content or "" for event in events if event.type == "delta") == "Hello"
    assert events[-1].finish_reason == "stop"
    assert calls[0]["stream"] is True


async def test_litellm_stream_assembles_interleaved_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_litellm(
        monkeypatch,
        [
            _AsyncStream([
                _chunk(tool_calls=[
                    _tool_fragment(0, call_id="call_a", name="add", arguments='{"a": '),
                    _tool_fragment(1, call_id="call_b", name="mul", arguments='{"x": '),
                ]),
                _chunk(tool_calls=[
                    _tool_fragment(0, arguments='2, "b": '),
                    _tool_fragment(1, arguments='3}'),
                ]),
                _chunk(tool_calls=[
                    _tool_fragment(0, arguments='3}'),
                ], finish_reason="tool_calls"),
            ])
        ],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    tool_events = [event for event in events if event.type == "tool_call"]
    assert len(tool_events) == 2
    assert [event.tool_call.id for event in tool_events] == ["call_a", "call_b"]
    assert [event.tool_call.name for event in tool_events] == ["add", "mul"]
    assert [event.tool_call.arguments for event in tool_events] == [
        {"a": 2, "b": 3},
        {"x": 3},
    ]
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "tool_calls"


async def test_litellm_stream_does_not_emit_incomplete_tool_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_litellm(
        monkeypatch,
        [
            _AsyncStream([
                _chunk(tool_calls=[
                    _tool_fragment(
                        0,
                        call_id="call_a",
                        name="read_file",
                        arguments='{"path": "REA',
                    ),
                ]),
                _chunk(finish_reason="tool_calls"),
            ])
        ],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert not [event for event in events if event.type == "tool_call"]
    assert [event.type for event in events] == ["error"]
    assert "Incomplete tool call arguments" in (events[0].content or "")


async def test_litellm_stream_emits_one_tool_call_after_fragments_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_litellm(
        monkeypatch,
        [
            _AsyncStream([
                _chunk(tool_calls=[
                    _tool_fragment(
                        0,
                        call_id="call_a",
                        name="read_file",
                        arguments='{"path": "REA',
                    ),
                ]),
                _chunk(tool_calls=[
                    _tool_fragment(0, arguments='DME.md"}'),
                ], finish_reason="tool_calls"),
            ])
        ],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    tool_events = [event for event in events if event.type == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_call.name == "read_file"
    assert tool_events[0].tool_call.arguments == {"path": "README.md"}
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "tool_calls"


async def test_litellm_stream_reports_usage_and_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_litellm(
        monkeypatch,
        [
            _AsyncStream([
                _chunk(content="ok"),
                _chunk(finish_reason="stop"),
                _usage_chunk(SimpleNamespace(
                    prompt_tokens=3, completion_tokens=5, total_tokens=8
                )),
            ])
        ],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert events[-1].type == "done"
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage == {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }


async def test_litellm_stream_retries_before_first_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_litellm(
        monkeypatch,
        [
            ConnectionError("connection reset by peer"),
            _AsyncStream([_chunk(content="ok"), _chunk(finish_reason="stop")]),
        ],
    )
    provider = _litellm_provider()
    provider._CHAT_RETRY_DELAYS = (0,)

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert len(calls) == 2
    assert [event.content for event in events if event.type == "delta"] == ["ok"]
    assert events[-1].type == "done"


async def test_litellm_stream_does_not_retry_after_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_litellm(
        monkeypatch,
        [
            _AsyncStream([
                _chunk(content="partial"),
                ConnectionError("connection reset by peer"),
            ]),
        ],
    )
    provider = _litellm_provider()
    provider._CHAT_RETRY_DELAYS = (0,)

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert len(calls) == 1
    assert [event.type for event in events] == ["delta", "error"]
    assert events[0].content == "partial"
    assert events[-1].finish_reason == "error"


async def test_litellm_stream_errors_without_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_litellm(
        monkeypatch,
        [_AsyncStream([_chunk(content="done")])],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["delta", "error"]
    assert events[-1].content == "stream ended without finish_reason"
    assert events[-1].finish_reason == "error"


async def test_litellm_stream_explicit_finish_chunk_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_litellm(
        monkeypatch,
        [_AsyncStream([_chunk(content="done"), _chunk(finish_reason="stop")])],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["delta", "done"]
    assert events[-1].finish_reason == "stop"


async def test_litellm_stream_accumulates_reasoning_and_tool_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_litellm(
        monkeypatch,
        [
            _AsyncStream([
                _chunk(
                    reasoning_content="think ",
                    thinking_blocks=[{"type": "thinking", "thinking": "a"}],
                    tool_calls=[
                        _tool_fragment(
                            0,
                            call_id="call_a",
                            name="add",
                            arguments='{"a": 1}',
                            provider_specific_fields={"cache": "x"},
                            function_provider_specific_fields={"kind": "fn"},
                        ),
                    ],
                ),
                _chunk(
                    reasoning_content="more",
                    thinking_blocks=[{"type": "thinking", "thinking": "b"}],
                    finish_reason="tool_calls",
                ),
            ])
        ],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    done = events[-1]
    assert done.type == "done"
    assert done.reasoning_content == "think more"
    assert done.thinking_blocks == [
        {"type": "thinking", "thinking": "a"},
        {"type": "thinking", "thinking": "b"},
    ]
    tool_call = next(event.tool_call for event in events if event.type == "tool_call")
    assert tool_call.provider_specific_fields == {"cache": "x"}
    assert tool_call.function_provider_specific_fields == {"kind": "fn"}


async def test_litellm_stream_reuses_last_index_when_fragments_omit_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_litellm(
        monkeypatch,
        [
            _AsyncStream([
                _chunk(tool_calls=[
                    _tool_fragment(None, call_id="call_a", name="add", arguments='{"a":'),
                ]),
                _chunk(tool_calls=[
                    _tool_fragment(None, arguments=" 1}"),
                ], finish_reason="tool_calls"),
            ])
        ],
    )
    provider = _litellm_provider()

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    tool_events = [event for event in events if event.type == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_call.arguments == {"a": 1}


async def test_litellm_stream_uses_generation_defaults_and_passes_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_litellm(
        monkeypatch,
        [_AsyncStream([_chunk(finish_reason="stop")])],
    )
    provider = _litellm_provider()
    provider.generation = GenerationSettings(
        temperature=0.2, max_tokens=123, reasoning_effort="none"
    )
    tools = [{"type": "function", "function": {"name": "ping"}}]

    await _collect(provider.chat_stream(
        [{"role": "user", "content": "go"}],
        tools=tools,
        tool_choice="required",
    ))

    assert calls[0]["max_tokens"] == 123
    assert calls[0]["temperature"] == 0.2
    assert calls[0]["tools"] == tools
    assert calls[0]["tool_choice"] == "required"


async def test_litellm_stream_closes_transport_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _BlockingStream(_chunk(content="partial"))
    _install_litellm(monkeypatch, [stream])
    provider = _litellm_provider()
    events = provider.chat_stream([{"role": "user", "content": "go"}])

    first = await events.__anext__()
    assert first.type == "delta"
    await events.aclose()

    assert stream.closed == 1


# ---------------------------------------------------------------------------
# A6: Custom provider streaming
# ---------------------------------------------------------------------------


async def test_custom_stream_text_and_usage() -> None:
    provider, stub = _custom_provider([
        _AsyncStream([
            _chunk(content="a"),
            _chunk(content="b"),
            _chunk(
                finish_reason="stop",
                usage=SimpleNamespace(
                    prompt_tokens=1, completion_tokens=2, total_tokens=3
                ),
            ),
        ])
    ])

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.content for event in events if event.type == "delta"] == ["a", "b"]
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
    assert stub.completions.calls[0]["stream"] is True


async def test_custom_stream_assembles_tool_call_fragments() -> None:
    provider, _ = _custom_provider([
        _AsyncStream([
            _chunk(tool_calls=[
                _tool_fragment(0, call_id="call_1", name="search", arguments='{"q": '),
            ]),
            _chunk(tool_calls=[
                _tool_fragment(0, arguments='"cats"}'),
            ], finish_reason="tool_calls"),
        ])
    ])

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    tool_events = [event for event in events if event.type == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_call.id == "call_1"
    assert tool_events[0].tool_call.name == "search"
    assert tool_events[0].tool_call.arguments == {"q": "cats"}


async def test_custom_stream_retries_before_first_token() -> None:
    provider, stub = _custom_provider([
        ConnectionError("connection reset"),
        _AsyncStream([_chunk(content="ok"), _chunk(finish_reason="stop")]),
    ])
    provider._CHAT_RETRY_DELAYS = (0,)

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert len(stub.completions.calls) == 2
    assert events[-1].type == "done"


async def test_custom_stream_does_not_retry_after_output() -> None:
    provider, stub = _custom_provider([
        _AsyncStream([
            _chunk(content="partial"),
            ConnectionError("connection reset"),
        ]),
    ])
    provider._CHAT_RETRY_DELAYS = (0,)

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert len(stub.completions.calls) == 1
    assert [event.type for event in events] == ["delta", "error"]


async def test_custom_stream_errors_without_finish_reason() -> None:
    provider, _ = _custom_provider([
        _AsyncStream([_chunk(content="done")]),
    ])

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["delta", "error"]
    assert events[-1].content == "stream ended without finish_reason"


async def test_custom_stream_does_not_emit_incomplete_tool_json() -> None:
    provider, _ = _custom_provider([
        _AsyncStream([
            _chunk(tool_calls=[
                _tool_fragment(0, call_id="call_1", name="search", arguments='{"q": '),
            ]),
            _chunk(tool_calls=[
                _tool_fragment(0, arguments='"cats"'),
            ], finish_reason="tool_calls"),
        ])
    ])

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["error"]
    assert "Incomplete tool call arguments" in (events[0].content or "")


async def test_custom_stream_rejects_non_object_tool_json() -> None:
    provider, _ = _custom_provider([
        _AsyncStream([
            _chunk(tool_calls=[
                _tool_fragment(0, call_id="call_1", name="search", arguments="[1, 2]"),
            ], finish_reason="tool_calls"),
        ])
    ])

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["error"]
    assert "not a JSON object" in (events[0].content or "")


async def test_custom_stream_ignores_empty_and_role_only_chunks() -> None:
    provider, _ = _custom_provider([
        _AsyncStream([
            _chunk(content="", role="assistant"),
            _chunk(),
            _usage_chunk(SimpleNamespace(
                prompt_tokens=1, completion_tokens=0, total_tokens=1
            )),
            _chunk(finish_reason="stop"),
        ])
    ])

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["done"]
    assert events[-1].usage == {
        "prompt_tokens": 1,
        "completion_tokens": 0,
        "total_tokens": 1,
    }


async def test_custom_stream_retries_without_stream_options() -> None:
    provider, stub = _custom_provider([
        RuntimeError(
            "Error code: 400 - Unsupported parameter: 'stream_options' is not supported"
        ),
        _AsyncStream([_chunk(content="ok"), _chunk(finish_reason="stop")]),
    ])

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["delta", "done"]
    assert stub.completions.calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in stub.completions.calls[1]


async def test_custom_stream_accumulates_reasoning_and_tool_metadata() -> None:
    provider, _ = _custom_provider([
        _AsyncStream([
            _chunk(
                reasoning_content="think ",
                thinking_blocks=[{"type": "thinking", "thinking": "a"}],
                tool_calls=[
                    _tool_fragment(
                        0,
                        call_id="call_1",
                        name="search",
                        arguments='{"q": "cats"}',
                        provider_specific_fields={"cache": "x"},
                        function_provider_specific_fields={"kind": "fn"},
                    ),
                ],
            ),
            _chunk(
                reasoning_content="more",
                thinking_blocks=[{"type": "thinking", "thinking": "b"}],
                finish_reason="tool_calls",
            ),
        ])
    ])

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    done = events[-1]
    assert done.reasoning_content == "think more"
    assert done.thinking_blocks == [
        {"type": "thinking", "thinking": "a"},
        {"type": "thinking", "thinking": "b"},
    ]
    tool_call = next(event.tool_call for event in events if event.type == "tool_call")
    assert tool_call.provider_specific_fields == {"cache": "x"}
    assert tool_call.function_provider_specific_fields == {"kind": "fn"}


async def test_custom_stream_uses_generation_defaults_and_reasoning_tokens() -> None:
    provider, stub = _custom_provider([
        _AsyncStream([_chunk(finish_reason="stop")]),
    ])
    provider.generation = GenerationSettings(temperature=0.3, max_tokens=77)
    tools = [{"type": "function", "function": {"name": "ping"}}]

    await _collect(provider.chat_stream(
        [{"role": "user", "content": "go"}],
        tools=tools,
        tool_choice="required",
        model="gpt-5",
    ))

    call = stub.completions.calls[0]
    assert "temperature" not in call
    assert call["max_completion_tokens"] == 77
    assert call["tools"] == tools
    assert call["tool_choice"] == "required"


async def test_custom_stream_falls_back_to_max_completion_tokens() -> None:
    provider, stub = _custom_provider([
        RuntimeError("Error code: 400: max_completion_tokens is required"),
        _AsyncStream([_chunk(finish_reason="stop")]),
    ])

    events = await _collect(provider.chat_stream(
        [{"role": "user", "content": "go"}], max_tokens=10
    ))

    assert events[-1].type == "done"
    assert stub.completions.calls[0]["max_tokens"] == 10
    assert stub.completions.calls[1]["max_completion_tokens"] == 10
    assert provider._max_tokens_param == "max_completion_tokens"


async def test_custom_stream_closes_transport_on_cancel() -> None:
    stream = _BlockingStream(_chunk(content="partial"))
    provider, _ = _custom_provider([stream])
    events = provider.chat_stream([{"role": "user", "content": "go"}])

    first = await events.__anext__()
    assert first.type == "delta"
    await events.aclose()

    assert stream.closed == 1


# ---------------------------------------------------------------------------
# A6: default fallback
# ---------------------------------------------------------------------------


class _NonStreamingProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        super().__init__()
        self.response = response
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return self.response

    def get_default_model(self) -> str:
        return "stub"


async def test_default_stream_fallback_matches_chat() -> None:
    response = LLMResponse(
        content="fallback",
        tool_calls=[ToolCallRequest(id="call_1", name="ping", arguments={"x": 1})],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6},
        reasoning_content="considering",
        thinking_blocks=[{"type": "thinking", "thinking": "step"}],
    )
    provider = _NonStreamingProvider(response)

    direct = await provider.chat([{"role": "user", "content": "go"}])
    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert provider.calls == 2
    assert "".join(event.content or "" for event in events if event.type == "delta") == direct.content
    tool_events = [event for event in events if event.type == "tool_call"]
    assert [event.tool_call for event in tool_events] == direct.tool_calls
    assert events[-1].type == "done"
    assert events[-1].finish_reason == direct.finish_reason
    assert events[-1].usage == direct.usage
    assert events[-1].reasoning_content == "considering"
    assert events[-1].thinking_blocks == [{"type": "thinking", "thinking": "step"}]


async def test_default_stream_fallback_reports_chat_errors() -> None:
    provider = _NonStreamingProvider(
        LLMResponse(content="Provider request failed", finish_reason="error")
    )

    events = await _collect(provider.chat_stream([{"role": "user", "content": "go"}]))

    assert [event.type for event in events] == ["error"]
    assert events[0].content == "Provider request failed"


def test_codex_and_responses_keep_default_stream_fallback() -> None:
    assert OpenAICodexProvider.chat_stream is LLMProvider.chat_stream
    assert OpenAIResponsesProvider.chat_stream is LLMProvider.chat_stream

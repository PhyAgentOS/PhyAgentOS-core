"""Tests for the provider-backed core :class:`ModelAdapter`.

The fake provider records exactly what the adapter forwarded, so these tests
pin parameter pass-through, response mapping, failure handling, and the rule
that the immutable request snapshot is never mutated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from PhyAgentOS.agent.core import ModelAdapter as ModelAdapterProtocol
from PhyAgentOS.agent.core.model import ModelAdapter
from PhyAgentOS.agent.core.types import (
    ModelCallError,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)
from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest


class FakeProvider:
    """Minimal provider double exposing only ``chat_with_retry``."""

    def __init__(self, response: object = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> object:
        self.calls.append({"messages": messages, "tools": tools, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


def _echo_tool() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Echo one string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


def _request(**overrides: Any) -> ModelRequest:
    values: dict[str, Any] = {
        "model_call_id": "call-1",
        "messages": [{"role": "user", "content": "hi"}],
    }
    values.update(overrides)
    return ModelRequest(**values)


def _response(**overrides: Any) -> LLMResponse:
    values: dict[str, Any] = {"content": "hello"}
    values.update(overrides)
    return LLMResponse(**values)


async def test_forwards_messages_tools_and_all_generation_params() -> None:
    provider = FakeProvider(response=_response())
    adapter = ModelAdapter(provider)
    request = _request(
        messages=[{"role": "user", "content": "hi"}],
        tools=[_echo_tool()],
        model="gpt-test",
        temperature=0.3,
        max_tokens=128,
        reasoning_effort="high",
        tool_choice="required",
    )

    result = await adapter.complete(request)

    assert isinstance(result, ModelResponse)
    assert result.content == "hello"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    assert call["tools"] == [_echo_tool().to_provider_dict()]
    assert call["model"] == "gpt-test"
    assert call["temperature"] == 0.3
    assert call["max_tokens"] == 128
    assert call["reasoning_effort"] == "high"
    assert call["tool_choice"] == "required"


async def test_omits_none_params_so_provider_defaults_apply() -> None:
    provider = FakeProvider(response=_response())
    adapter = ModelAdapter(provider)

    await adapter.complete(_request())

    call = provider.calls[0]
    for key in ("model", "temperature", "max_tokens", "reasoning_effort", "tool_choice"):
        assert key not in call
    assert call["tools"] is None


async def test_maps_response_usage_reasoning_and_tool_call_fields() -> None:
    provider = FakeProvider(
        response=_response(
            content="thinking done",
            finish_reason="tool_calls",
            usage={"prompt_tokens": 3, "completion_tokens": 5, "ignored": -1},
            reasoning_content="because",
            thinking_blocks=[{"type": "thinking", "thinking": "secret"}],
            tool_calls=[
                ToolCallRequest(
                    id="tool-call-1",
                    name="echo",
                    arguments={"text": "hi"},
                    provider_specific_fields={"vendor": "x"},
                    function_provider_specific_fields={"cache": True},
                    raw_arguments='{"text": "hi"}',
                )
            ],
        )
    )

    result = await ModelAdapter(provider).complete(_request())

    assert result.content == "thinking done"
    assert result.finish_reason == "tool_calls"
    assert result.reasoning_content == "because"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 5}
    assert result.thinking_blocks is not None
    assert len(result.thinking_blocks) == 1
    assert dict(result.thinking_blocks[0]) == {"type": "thinking", "thinking": "secret"}
    with pytest.raises(TypeError):
        result.thinking_blocks[0]["thinking"] = "changed"  # type: ignore[index]
    assert result.has_tool_calls
    call = result.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.tool_call_id == "tool-call-1"
    assert call.name == "echo"
    assert dict(call.arguments) == {"text": "hi"}
    assert call.raw_arguments == '{"text": "hi"}'
    assert dict(call.provider_fields or {}) == {"vendor": "x"}
    assert dict(call.function_provider_fields or {}) == {"cache": True}


async def test_thinking_blocks_are_snapshotted_not_aliased() -> None:
    blocks = [{"type": "thinking", "thinking": "original"}]
    provider = FakeProvider(response=_response(thinking_blocks=blocks))

    result = await ModelAdapter(provider).complete(_request())

    blocks[0]["thinking"] = "changed"
    assert result.thinking_blocks is not None
    assert dict(result.thinking_blocks[0])["thinking"] == "original"


@pytest.mark.parametrize(
    "bad_blocks",
    ["thinking", {"type": "thinking"}, ["not-a-mapping"]],
)
async def test_malformed_thinking_blocks_raise_model_call_error(bad_blocks: object) -> None:
    provider = FakeProvider(response=_response(thinking_blocks=bad_blocks))

    with pytest.raises(ModelCallError) as excinfo:
        await ModelAdapter(provider).complete(_request())

    assert excinfo.value.model_call_id == "call-1"


@pytest.mark.parametrize("bad_calls", [5, {"id": "tool-call-1"}, "tool-call-1"])
async def test_malformed_tool_calls_raise_model_call_error(bad_calls: object) -> None:
    provider = FakeProvider(response=_response(tool_calls=bad_calls))

    with pytest.raises(ModelCallError) as excinfo:
        await ModelAdapter(provider).complete(_request())

    assert not isinstance(excinfo.value, TypeError)
    assert excinfo.value.model_call_id == "call-1"


async def test_error_finish_reason_raises_model_call_error() -> None:
    provider = FakeProvider(
        response=_response(content="bad api_key=supersecretvalue", finish_reason="error")
    )

    with pytest.raises(ModelCallError) as excinfo:
        await ModelAdapter(provider).complete(_request())

    assert excinfo.value.model_call_id == "call-1"
    assert "supersecretvalue" not in excinfo.value.detail
    assert excinfo.value.detail


async def test_provider_exception_is_wrapped_and_cause_kept() -> None:
    cause = RuntimeError("boom token=sk-abcdefghijklmnop")
    provider = FakeProvider(error=cause)

    with pytest.raises(ModelCallError) as excinfo:
        await ModelAdapter(provider).complete(_request())

    assert excinfo.value.model_call_id == "call-1"
    assert excinfo.value.__cause__ is cause
    assert "sk-abcdefghijklmnop" not in excinfo.value.detail
    assert "RuntimeError" in excinfo.value.detail


@pytest.mark.parametrize(
    "response",
    [None, object(), _response(content="ignored", finish_reason="")],
)
async def test_unusable_response_raises_model_call_error(response: object) -> None:
    with pytest.raises(ModelCallError) as excinfo:
        await ModelAdapter(FakeProvider(response=response)).complete(_request())
    assert excinfo.value.model_call_id == "call-1"


async def test_empty_answer_without_tool_calls_raises() -> None:
    provider = FakeProvider(response=_response(content=None, finish_reason="stop"))

    with pytest.raises(ModelCallError):
        await ModelAdapter(provider).complete(_request())


async def test_request_snapshot_is_not_mutated() -> None:
    class MutatingProvider(FakeProvider):
        async def chat_with_retry(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ) -> object:
            messages[0]["content"] = "changed"
            messages[0]["meta"]["nested"] = 99
            messages.append({"role": "user", "content": "extra"})
            assert tools is not None
            tools[0]["function"]["parameters"]["properties"]["text"]["type"] = "number"
            return _response()

    request = _request(
        messages=[{"role": "user", "content": "hi", "meta": {"nested": 1}}],
        tools=[_echo_tool()],
    )
    before_messages = request.to_messages()
    before_tools = request.to_provider_tools()

    await ModelAdapter(MutatingProvider()).complete(request)

    assert request.to_messages() == before_messages
    assert request.to_provider_tools() == before_tools


async def test_satisfies_core_protocol() -> None:
    assert isinstance(ModelAdapter(FakeProvider(response=_response())), ModelAdapterProtocol)


def test_module_has_no_provider_sdk_or_legacy_agent_imports() -> None:
    import PhyAgentOS.agent.core.model as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "litellm" not in source
    assert "PhyAgentOS.providers" not in source
    assert "agent.loop" not in source

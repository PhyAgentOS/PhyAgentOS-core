"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import json_repair

from PhyAgentOS.providers._openai_client import build_async_openai_client
from PhyAgentOS.providers.base import LLMProvider, LLMResponse, StreamEvent, ToolCallRequest
from PhyAgentOS.providers.effort import is_openai_reasoning_model
from PhyAgentOS.providers.errors import describe_provider_error


class CustomProvider(LLMProvider):

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str = "http://localhost:8000/v1",
        default_model: str = "default",
        timeout_s: float | None = None,
        max_retries: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self._client = build_async_openai_client(
            api_key, api_base, timeout_s, max_retries, extra_headers=extra_headers
        )
        # Remember the parameter supported by this endpoint after a rejection.
        self._max_tokens_param = "max_tokens"

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                   reasoning_effort: str | None = None,
                   tool_choice: str | dict[str, Any] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            self._max_tokens_param: max(1, max_tokens),
            "temperature": temperature,
        }
        if reasoning_effort and reasoning_effort != "none":
            kwargs["reasoning_effort"] = reasoning_effort
        # Clearing effort restores the model default, not legacy request parameters.
        if "reasoning_effort" in kwargs or is_openai_reasoning_model(kwargs["model"]):
            kwargs.pop("temperature", None)
            kwargs["max_completion_tokens"] = kwargs.pop(self._max_tokens_param)
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")
        try:
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            # Reasoning requests already use max_completion_tokens. Only adapt
            # a request that actually sent the legacy parameter; otherwise a
            # server rejection would be replaced by a local KeyError.
            if "max_tokens" in kwargs and "max_completion_tokens" in str(e):
                self._max_tokens_param = "max_completion_tokens"
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                try:
                    return self._parse(await self._client.chat.completions.create(**kwargs))
                except Exception as e2:
                    return LLMResponse(content=describe_provider_error(e2), finish_reason="error")
            return LLMResponse(content=describe_provider_error(e), finish_reason="error")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a completion from an OpenAI-compatible endpoint."""
        max_tokens, temperature, reasoning_effort = self._resolve_generation(
            max_tokens, temperature, reasoning_effort
        )
        kwargs = self._stream_kwargs(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )
        async with aclosing(
            self._chat_stream_with_retry(lambda: self._stream_once(kwargs))
        ) as events:
            async for event in events:
                yield event

    def _stream_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the request kwargs mirroring ``chat`` plus streaming flags."""
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            self._max_tokens_param: max(1, max_tokens),
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if reasoning_effort and reasoning_effort != "none":
            kwargs["reasoning_effort"] = reasoning_effort
        if "reasoning_effort" in kwargs or is_openai_reasoning_model(kwargs["model"]):
            kwargs.pop("temperature", None)
            kwargs["max_completion_tokens"] = kwargs.pop(self._max_tokens_param)
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")
        return kwargs

    @staticmethod
    def _rejects_stream_options(error: Exception) -> bool:
        """Detect an endpoint that rejects ``stream_options`` as a bad request."""
        text = str(error).lower()
        if "stream_options" not in text:
            return False
        return (
            getattr(error, "status_code", None) == 400
            or "400" in text
            or "bad request" in text
        )

    async def _open_stream(self, kwargs: dict[str, Any]) -> Any:
        """Open the stream, retrying once without unsupported request parameters."""
        for attempt in range(2):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except Exception as e:
                if (
                    attempt == 0
                    and "stream_options" in kwargs
                    and self._rejects_stream_options(e)
                ):
                    kwargs.pop("stream_options")
                    continue
                if "max_tokens" in kwargs and "max_completion_tokens" in str(e):
                    self._max_tokens_param = "max_completion_tokens"
                    kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                    return await self._client.chat.completions.create(**kwargs)
                raise
        raise RuntimeError("unreachable")

    async def _stream_once(self, kwargs: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        """Consume one OpenAI SDK stream, assembling tool calls first."""
        stream = await self._open_stream(kwargs)
        pending: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] = {}
        finish_reason: str | None = None
        reasoning_parts: list[str] = []
        thinking_blocks: list[dict] = []

        try:
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = self._usage_to_dict(chunk_usage)

                for choice in getattr(chunk, "choices", None) or []:
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        content = getattr(delta, "content", None)
                        if isinstance(content, str) and content:
                            yield StreamEvent(type="delta", content=content)
                        self._accumulate_reasoning(
                            reasoning_parts, thinking_blocks, delta
                        )
                        for fragment in getattr(delta, "tool_calls", None) or []:
                            self._accumulate_tool_fragment(pending, fragment)
                    reason = getattr(choice, "finish_reason", None)
                    if reason:
                        finish_reason = reason
        finally:
            await self._close_stream(stream)

        if finish_reason is None:
            yield StreamEvent(
                type="error",
                content="stream ended without finish_reason",
                finish_reason="error",
            )
            return

        tool_calls = self._finalize_tool_calls(
            pending, lambda index: f"call_{index}"
        )
        if isinstance(tool_calls, str):
            yield StreamEvent(type="error", content=tool_calls, finish_reason="error")
            return
        for tool_call in tool_calls:
            yield StreamEvent(type="tool_call", tool_call=tool_call)
        if usage:
            yield StreamEvent(type="usage", usage=dict(usage))
        yield StreamEvent(
            type="done",
            finish_reason=finish_reason,
            usage=dict(usage),
            reasoning_content="".join(reasoning_parts) or None,
            thinking_blocks=thinking_blocks or None,
        )

    def _parse(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCallRequest(id=tc.id, name=tc.function.name,
                            arguments=json_repair.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        u = response.usage
        return LLMResponse(
            content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason or "stop",
            usage={"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens} if u else {},
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    def get_default_model(self) -> str:
        return self.default_model

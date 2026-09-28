"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

import json_repair

from PhyAgentOS.providers._openai_client import build_async_openai_client
from PhyAgentOS.providers.base import LLMProvider, LLMResponse, ToolCallRequest
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

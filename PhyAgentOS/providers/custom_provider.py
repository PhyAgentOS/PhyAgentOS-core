"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

import json_repair

from PhyAgentOS.providers._openai_client import build_async_openai_client
from PhyAgentOS.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class CustomProvider(LLMProvider):

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str = "http://localhost:8000/v1",
        default_model: str = "default",
        timeout_s: float | None = None,
        max_retries: int | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        # Timeout / retry reasoning lives with the shared client builder
        # (`providers/_openai_client.py`); configure as
        # `providers.<name>.timeoutS` / `providers.<name>.maxRetries`.
        self._client = build_async_openai_client(api_key, api_base, timeout_s, max_retries)
        # Reasoning-model endpoints (GPT-5/6, o-series) reject legacy max_tokens;
        # adopted from the server error and remembered for later calls.
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
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")
        try:
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            if self._max_tokens_param == "max_tokens" and "max_completion_tokens" in str(e):
                self._max_tokens_param = "max_completion_tokens"
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                try:
                    return self._parse(await self._client.chat.completions.create(**kwargs))
                except Exception as e2:
                    return LLMResponse(content=f"Error: {e2}", finish_reason="error")
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

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

"""Direct OpenAI Responses API provider (/v1/responses) — bypasses LiteLLM.

The Responses endpoint allows function tools together with reasoning effort,
which /v1/chat/completions rejects on reasoning models (GPT-5/6, o-series).
The provider is stateless: it translates the chat-style message history kept
by the Agent loop into Responses input items on every call and parses the
output items back, so no server-side response state is used.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import json_repair
from openai import AsyncOpenAI

from PhyAgentOS.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class OpenAIResponsesProvider(LLMProvider):

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
        # One HTTP attempt's read timeout. The 180s default assumes a model that answers in
        # seconds; a reasoning model on a long turn can legitimately need minutes, and a call
        # that exceeds this is retried by the SDK before it ever returns, so a ceiling below the
        # model's real latency converts every slow turn into several failed attempts (measured
        # 2026-09-24: qwen3.8-max-0902, 3 x 180s = 540s, matching the run's recurring 541-543s
        # gaps). Configure it per provider as `providers.<name>.timeoutS`.
        self._timeout_s = float(timeout_s) if timeout_s else 180.0
        # SDK-level retries per chat call. 0 leaves retrying to the framework's own
        # chat_with_retry, so the worst case for one logical call is a known multiple of the
        # timeout instead of the SDK's own multiplication on top of it.
        self._max_retries = 2 if max_retries is None else int(max_retries)
        # Reasoning models (GPT-5/6, o-series) reject sampling temperature;
        # dropped from requests once the server says so and remembered after.
        self._temperature_supported = True
        # Use httpx client with trust_env=False to avoid picking up system SOCKS proxy
        # that uses the unsupported 'socks://' scheme (httpx only supports socks5://).
        http_client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(self._timeout_s, connect=15.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            default_headers={"x-session-affinity": uuid.uuid4().hex},
            max_retries=self._max_retries,
            http_client=http_client,
        )

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                   reasoning_effort: str | None = None,
                   tool_choice: str | dict[str, Any] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "input": self._to_input(self._sanitize_empty_content(messages)),
            "max_output_tokens": max(1, max_tokens),
        }
        if self._temperature_supported:
            kwargs["temperature"] = temperature
        if reasoning_effort and reasoning_effort != "none":
            # 'none' is a chat-completions-only value; the Responses API takes
            # low/medium/high inside a reasoning object and has no 'none'.
            kwargs["reasoning"] = {"effort": reasoning_effort}
        if tools:
            kwargs["tools"] = [self._to_tool(t) for t in tools]
            kwargs["tool_choice"] = self._to_tool_choice(tool_choice)
        try:
            return self._parse(await self._client.responses.create(**kwargs))
        except Exception as e:
            if self._temperature_supported and "'temperature' is not supported" in str(e):
                self._temperature_supported = False
                kwargs.pop("temperature", None)
                try:
                    return self._parse(await self._client.responses.create(**kwargs))
                except Exception as e2:
                    return LLMResponse(content=f"Error: {e2}", finish_reason="error")
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    # -- request translation -------------------------------------------------

    @staticmethod
    def _to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Chat-style messages -> Responses input items."""
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": message.get("content") or "",
                })
                continue
            if role == "assistant" and message.get("tool_calls"):
                # Preserve order: optional assistant text first, then calls.
                content = message.get("content")
                if content:
                    items.append({"role": "assistant", "content": content})
                for call in message["tool_calls"]:
                    function = call.get("function", {})
                    items.append({
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments") or "{}",
                    })
                continue
            content = message.get("content")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part.get("type") == "image_url":
                        parts.append({
                            "type": "input_image",
                            "image_url": (part.get("image_url") or {}).get("url", ""),
                        })
                    else:
                        parts.append({
                            "type": "input_text",
                            "text": part.get("text") or "",
                        })
                items.append({"role": role, "content": parts})
            else:
                items.append({"role": role, "content": content or ""})
        return items

    @staticmethod
    def _to_tool(tool: dict[str, Any]) -> dict[str, Any]:
        """Nested chat tool def -> flattened Responses function tool."""
        if "function" not in tool:
            return tool
        function = tool["function"]
        return {
            "type": "function",
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object", "properties": {}}),
        }

    @staticmethod
    def _to_tool_choice(tool_choice: str | dict[str, Any] | None) -> str | dict[str, Any]:
        if isinstance(tool_choice, dict) and "function" in tool_choice:
            name = tool_choice["function"].get("name", "")
            return {"type": "function", "name": name}
        return tool_choice or "auto"

    # -- response parsing ----------------------------------------------------

    def _parse(self, response: Any) -> LLMResponse:
        tool_calls = []
        reasoning_parts = []
        for item in getattr(response, "output", None) or []:
            item_type = getattr(item, "type", "")
            if item_type == "function_call":
                arguments = getattr(item, "arguments", "{}")
                tool_calls.append(ToolCallRequest(
                    id=getattr(item, "call_id", "") or getattr(item, "id", ""),
                    name=getattr(item, "name", ""),
                    arguments=json_repair.loads(arguments) if isinstance(arguments, str) else arguments,
                ))
            elif item_type == "reasoning":
                for summary in getattr(item, "summary", None) or []:
                    text = getattr(summary, "text", None)
                    if text:
                        reasoning_parts.append(text)
        status = getattr(response, "status", "completed")
        if status == "completed":
            finish_reason = "stop"
        elif status == "incomplete":
            finish_reason = "length"
        else:
            finish_reason = status or "stop"
        u = getattr(response, "usage", None)
        return LLMResponse(
            content=getattr(response, "output_text", "") or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage={
                "prompt_tokens": u.input_tokens,
                "completion_tokens": u.output_tokens,
                "total_tokens": u.total_tokens,
            } if u else {},
            reasoning_content="\n".join(reasoning_parts) or None,
        )

    def get_default_model(self) -> str:
        return self.default_model

"""Stateless model-backed semantic verification."""

from __future__ import annotations

import asyncio
import json
from typing import Any

_VERIFIER_INITIAL_OUTPUT_TOKENS = 16384
_VERIFIER_MAX_OUTPUT_TOKENS = 32768
_VERIFIER_INITIAL_EFFORT = "low"


class VerificationEngine:
    def __init__(
        self,
        *,
        provider: Any,
        model: str,
        timeout_s: float = 180.0,
        reasoning_effort: str | None = _VERIFIER_INITIAL_EFFORT,
        max_output_tokens: int = _VERIFIER_INITIAL_OUTPUT_TOKENS,
    ) -> None:
        self.provider = provider
        self.model = model
        self.timeout_s = float(timeout_s)
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max(256, int(max_output_tokens))

    async def complete(self, *, system_prompt: str, content: list[dict[str, Any]]) -> dict:
        budget = self.max_output_tokens
        effort = self.reasoning_effort
        while True:
            response = await asyncio.wait_for(
                self.provider.chat_with_retry(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": content},
                    ],
                    model=self.model,
                    tools=None,
                    temperature=0.0,
                    max_tokens=budget,
                    reasoning_effort=effort,
                ),
                timeout=self.timeout_s,
            )
            text = (response.content or "").strip()
            if response.finish_reason != "error" and text:
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError("verifier response must be a JSON object")
                return value
            if response.finish_reason == "error" and effort and _effort_rejected(text):
                # Endpoints that do not implement reasoning_effort must still verify.
                effort = None
                continue
            if response.finish_reason == "length" and budget < _VERIFIER_MAX_OUTPUT_TOKENS:
                # Reasoning models can spend the whole budget before emitting JSON.
                budget = _VERIFIER_MAX_OUTPUT_TOKENS
                continue
            detail = f" (finish_reason={response.finish_reason}, max_tokens={budget})"
            raise RuntimeError((text or "verifier model returned no content") + detail)


def _effort_rejected(message: str) -> bool:
    lowered = message.lower()
    return "reasoning_effort" in lowered or "reasoning effort" in lowered

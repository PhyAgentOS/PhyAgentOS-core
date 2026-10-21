from __future__ import annotations

import json
from typing import Any

import pytest

from PhyAgentOS.providers.base import LLMResponse
from PhyAgentOS.verification.engine import VerificationEngine


class RecordingProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


async def test_length_truncation_retries_with_larger_budget() -> None:
    provider = RecordingProvider(
        [
            LLMResponse(content="", finish_reason="length"),
            LLMResponse(content=json.dumps({"verdict": "success"}), finish_reason="stop"),
        ]
    )
    engine = VerificationEngine(provider=provider, model="reasoning-model")
    verdict = await engine.complete(system_prompt="judge", content=[])
    assert verdict == {"verdict": "success"}
    assert [call["max_tokens"] for call in provider.calls] == [16384, 32768]
    assert all(call["reasoning_effort"] == "low" for call in provider.calls)


async def test_unsupported_reasoning_effort_falls_back_to_default() -> None:
    provider = RecordingProvider(
        [
            LLMResponse(
                content="Error: Unsupported parameter: reasoning_effort",
                finish_reason="error",
            ),
            LLMResponse(content=json.dumps({"verdict": "failure"}), finish_reason="stop"),
        ]
    )
    engine = VerificationEngine(provider=provider, model="plain-model")
    verdict = await engine.complete(system_prompt="judge", content=[])
    assert verdict == {"verdict": "failure"}
    assert provider.calls[0]["reasoning_effort"] == "low"
    assert provider.calls[1]["reasoning_effort"] is None


async def test_empty_content_without_truncation_reports_the_reason() -> None:
    provider = RecordingProvider([LLMResponse(content="", finish_reason="stop")])
    engine = VerificationEngine(provider=provider, model="quiet-model")
    with pytest.raises(RuntimeError, match="no content"):
        await engine.complete(system_prompt="judge", content=[])

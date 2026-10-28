"""Compatibility tests between core :class:`ModelAdapter` and ProvidersManager.

The fake provider is a minimal :class:`LLMProvider`: the real
``ProvidersManager`` routes to it, the inherited ``chat_with_retry`` owns
retries, and ``chat`` records the resolved generation parameters.
"""

from __future__ import annotations

from typing import Any

import pytest

from PhyAgentOS.agent.core.model import ModelAdapter
from PhyAgentOS.agent.core.types import ModelRequest, ToolDefinition
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.base import LLMProvider, LLMResponse
from PhyAgentOS.providers.providers_manager import ProvidersManager


class RecordingProvider(LLMProvider):
    """Minimal downstream provider that records what it received."""

    def __init__(self, model: str) -> None:
        super().__init__("test-secret", "https://example.test/v1")
        self._model = model
        self.requests: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return self._model

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        # Record the manager -> provider hop, then use the real retry path.
        self.requests.append(dict(kwargs))
        return await super().chat_with_retry(**kwargs)

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.chat_calls.append(dict(kwargs))
        return LLMResponse(content="ok")


def _manager(
    default_mode: str = "main",
) -> tuple[ProvidersManager, RecordingProvider, RecordingProvider]:
    main = RecordingProvider("fake-main")
    coding = RecordingProvider("fake-coding")
    manager = ProvidersManager(
        Config(),
        {
            "main": {"provider": main, "describe": "main mode"},
            "coding": {"provider": coding, "describe": "coding mode"},
        },
        default_mode=default_mode,
    )
    return manager, main, coding


def _echo_tool() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Echo one string.",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )


async def test_tool_choice_and_generation_params_reach_provider_and_chat() -> None:
    manager, main, _ = _manager()

    await manager.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        tools=[_echo_tool().to_provider_dict()],
        mode="main",
        tool_choice="required",
        max_tokens=77,
        temperature=0.2,
        reasoning_effort="high",
    )

    forwarded = main.requests[0]
    assert forwarded["tool_choice"] == "required"
    assert forwarded["max_tokens"] == 77
    assert forwarded["temperature"] == 0.2
    assert forwarded["reasoning_effort"] == "high"
    assert forwarded["model"] == "fake-main"
    assert forwarded["messages"] == [{"role": "user", "content": "hi"}]

    chat = main.chat_calls[0]
    assert chat["tool_choice"] == "required"
    assert chat["max_tokens"] == 77
    assert chat["temperature"] == 0.2
    assert chat["reasoning_effort"] == "high"


async def test_dict_tool_choice_routes_with_mode() -> None:
    manager, main, coding = _manager()
    choice = {"type": "function", "function": {"name": "echo"}}

    await manager.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        tools=[_echo_tool().to_provider_dict()],
        mode="coding",
        tool_choice=choice,
    )

    assert main.requests == []
    assert coding.requests[0]["tool_choice"] == choice
    assert coding.chat_calls[0]["tool_choice"] == choice
    assert coding.chat_calls[0]["model"] == "fake-coding"


@pytest.mark.parametrize("extra", [{}, {"tool_choice": None}])
async def test_unspecified_tool_choice_keeps_existing_defaults(extra: dict[str, Any]) -> None:
    manager, main, _ = _manager(default_mode="main")

    await manager.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        mode="",
        **extra,
    )

    # The manager does not add a tool_choice override when nothing was asked.
    assert main.requests[0].get("tool_choice") is None
    chat = main.chat_calls[0]
    assert chat["tool_choice"] is None
    assert chat["model"] == "fake-main"
    assert chat["max_tokens"] == 4096
    assert chat["temperature"] == 0.7
    assert chat["reasoning_effort"] is None


async def test_core_adapter_against_real_manager_forwards_tool_choice() -> None:
    manager, main, _ = _manager()
    request = ModelRequest(
        model_call_id="call-1",
        messages=[{"role": "user", "content": "hi"}],
        tools=[_echo_tool()],
        tool_choice="required",
        max_tokens=99,
    )

    result = await ModelAdapter(manager).complete(request)

    assert result.content == "ok"
    forwarded = main.requests[0]
    assert forwarded["tool_choice"] == "required"
    assert forwarded["max_tokens"] == 99
    assert forwarded["model"] == "fake-main"
    # The adapter sent no temperature, so the manager's existing default applies.
    assert main.chat_calls[0]["temperature"] == 0.7

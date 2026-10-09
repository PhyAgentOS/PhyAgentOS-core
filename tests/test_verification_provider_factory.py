"""The Forge verifier subprocess must build the same direct providers the
agent uses — an ``openai_responses`` agent must not silently fall back to
LiteLLM chat-completions, which is exactly the path the provider bypasses
(reasoning models reject tools+reasoning there).
"""

from __future__ import annotations

from PhyAgentOS.providers.custom_provider import CustomProvider
from PhyAgentOS.providers.openai_responses_provider import OpenAIResponsesProvider
from PhyAgentOS.verification.service import _provider


def test_provider_factory_builds_openai_responses_directly() -> None:
    provider = _provider(
        {
            "provider_name": "openai_responses",
            "model": "gpt-6-astra-phyagentos",
            "api_key": "k",
            "api_base": "http://localhost:9/v1",
        },
        timeout_s=30.0,
    )

    assert isinstance(provider, OpenAIResponsesProvider)


def test_provider_factory_still_builds_custom_directly() -> None:
    provider = _provider(
        {"provider_name": "custom", "model": "m", "api_key": "k"}, timeout_s=30.0
    )

    assert isinstance(provider, CustomProvider)

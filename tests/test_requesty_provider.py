"""Tests for the Requesty gateway provider registration."""

from __future__ import annotations

import litellm
import pytest

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.registry import find_by_name, find_gateway


def _config(provider: str) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": {"model": "openai/gpt-4o-mini", "provider": provider}},
            "providers": {"requesty": {"apiKey": "rqsty-test"}},
        }
    )


def test_requesty_is_a_gateway_with_default_base() -> None:
    spec = find_by_name("requesty")

    assert spec is not None
    assert spec.is_gateway
    assert spec.env_key == "REQUESTY_API_KEY"
    assert spec.default_api_base == "https://router.requesty.ai/v1"
    assert find_gateway(api_base="https://router.eu.requesty.ai/v1") is spec


def test_requesty_config_resolves_provider_and_base() -> None:
    for provider in ("requesty", "auto"):
        config = _config(provider)

        assert config.get_provider_name() == "requesty"
        assert config.get_api_key() == "rqsty-test"
        assert config.get_api_base() == "https://router.requesty.ai/v1"


def test_requesty_keeps_vendor_prefix_in_model_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTY_API_KEY", "")
    monkeypatch.setattr(litellm, "api_base", None)
    provider = LiteLLMProvider(
        api_key="rqsty-test",
        api_base="https://router.requesty.ai/v1",
        default_model="openai/gpt-4o-mini",
        provider_name="requesty",
    )

    assert provider._resolve_model("openai/gpt-4o-mini") == "custom_openai/openai/gpt-4o-mini"
    assert (
        provider._resolve_model("anthropic/claude-sonnet-4-5")
        == "custom_openai/anthropic/claude-sonnet-4-5"
    )
    assert provider._resolve_model("gpt-5.4-mini") == "custom_openai/gpt-5.4-mini"

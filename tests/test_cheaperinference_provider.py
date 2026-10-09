"""Tests for the Cheaper Inference gateway provider registration."""

from __future__ import annotations

import litellm
import pytest

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.registry import find_by_name, find_gateway


def _config(provider: str) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": {"model": "gpt-5.4-mini", "provider": provider}},
            "providers": {"cheaperinference": {"apiKey": "ci_live_test"}},
        }
    )


def test_cheaperinference_is_a_gateway_with_default_base() -> None:
    spec = find_by_name("cheaperinference")

    assert spec is not None
    assert spec.is_gateway
    assert spec.env_key == "CHEAPER_INFERENCE_API_KEY"
    assert spec.default_api_base == "https://api.cheaperinference.com/v1"
    assert find_gateway(api_base="https://api.cheaperinference.com/v1") is spec


def test_cheaperinference_config_resolves_provider_and_base() -> None:
    for provider in ("cheaperinference", "auto"):
        config = _config(provider)

        assert config.get_provider_name() == "cheaperinference"
        assert config.get_api_key() == "ci_live_test"
        assert config.get_api_base() == "https://api.cheaperinference.com/v1"


def test_cheaperinference_sends_bare_model_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEAPER_INFERENCE_API_KEY", "")
    monkeypatch.setattr(litellm, "api_base", None)
    provider = LiteLLMProvider(
        api_key="ci_live_test",
        api_base="https://api.cheaperinference.com/v1",
        default_model="gpt-5.4-mini",
        provider_name="cheaperinference",
    )

    assert provider._resolve_model("gpt-5.4-mini") == "custom_openai/gpt-5.4-mini"
    assert provider._resolve_model("claude-sonnet-5") == "custom_openai/claude-sonnet-5"
    assert provider._resolve_model("cheaperinference/gpt-5.4-mini") == "custom_openai/gpt-5.4-mini"

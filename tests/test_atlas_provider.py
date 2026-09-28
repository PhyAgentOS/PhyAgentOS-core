"""Tests for the optional Atlas Cloud provider configuration."""

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.registry import find_by_name


def test_atlas_provider_uses_catalog_model_ids_with_default_api_base() -> None:
    model = "Qwen/Qwen3-235B-A22B-Instruct-2507"
    config = Config.model_validate(
        {
            "agents": {"defaults": {"model": model, "provider": "atlas"}},
            "providers": {"atlas": {"apiKey": "test-key"}},
        }
    )

    assert config.get_provider_name() == "atlas"
    assert config.get_api_key() == "test-key"
    assert config.get_api_base() == "https://api.atlascloud.ai/v1"


def test_atlas_provider_routes_through_litellm_openai_compatibility() -> None:
    provider = LiteLLMProvider(provider_name="atlas")

    assert provider._resolve_model("Qwen/Qwen3-235B-A22B-Instruct-2507") == (
        "openai/Qwen/Qwen3-235B-A22B-Instruct-2507"
    )


def test_atlas_provider_is_detected_from_its_api_base() -> None:
    spec = find_by_name("atlas")

    assert spec is not None
    assert spec.is_gateway
    assert spec.default_api_base == "https://api.atlascloud.ai/v1"

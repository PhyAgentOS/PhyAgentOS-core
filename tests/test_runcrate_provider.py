"""Runcrate gateway selection and OpenAI-compatible request routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import litellm
import pytest

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.registry import find_by_name, find_gateway
from PhyAgentOS.providers.service import ProviderService


@pytest.mark.parametrize("provider_name", ["runcrate", "auto"])
def test_config_selects_gateway_when_only_gateway_is_configured(provider_name: str) -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {"model": "deepseek-ai/DeepSeek-V4.1-Flash", "provider": provider_name}
            },
            "providers": {"runcrate": {"apiKey": "test-runcrate-key"}},
        }
    )
    assert config.get_provider_name() == "runcrate"
    assert config.get_api_key() == "test-runcrate-key"
    assert config.get_api_base() == "https://api.runcrate.ai/v1"
    assert find_gateway(api_base=config.get_api_base()) is find_by_name("runcrate")


@pytest.mark.parametrize(
    ("provider_name", "expected_provider"), [("auto", "anthropic"), ("runcrate", "runcrate")]
)
def test_config_respects_provider_selection_with_native_credentials(
    provider_name: str, expected_provider: str
) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"model": "claude-sonnet-4-6", "provider": provider_name}},
            "providers": {
                "anthropic": {"apiKey": "test-anthropic-key"},
                "runcrate": {"apiKey": "test-runcrate-key"},
            },
        }
    )
    assert config.get_provider_name() == expected_provider
    assert config.get_api_key() == getattr(config.providers, expected_provider).api_key


def test_runtime_reads_provider_key_without_persisting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNCRATE_API_KEY", "test-runcrate-key")
    monkeypatch.delenv("PAOS_RUNCRATE_API_KEY", raising=False)
    config = Config()
    service = ProviderService(config)
    assert service.config.providers.runcrate.api_key == "test-runcrate-key"
    assert config.providers.runcrate.api_key == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model", ["deepseek-ai/DeepSeek-V4.1-Flash", "zai-org/GLM-5.3", "runcrate/Qwen/Qwen3.8-Flash"]
)
async def test_request_uses_gateway_and_preserves_model_id(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    completion = AsyncMock(
        return_value=litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "ok"}}]
        )
    )
    monkeypatch.setattr("PhyAgentOS.providers.litellm_provider.acompletion", completion)
    provider = LiteLLMProvider(api_key="test-runcrate-key", provider_name="runcrate")
    response = await provider.chat(messages=[{"role": "user", "content": "hello"}], model=model)
    assert response.content == "ok"
    kwargs = completion.call_args.kwargs
    assert kwargs["model"] == f"custom_openai/{model.removeprefix('runcrate/')}"
    assert kwargs["api_key"] == "test-runcrate-key"
    assert kwargs["api_base"] == "https://api.runcrate.ai/v1"

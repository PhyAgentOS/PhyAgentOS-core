"""Opper gateway selection and OpenAI-compatible request routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import litellm
import pytest

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.registry import find_by_name, find_gateway
from PhyAgentOS.providers.service import ProviderService


@pytest.mark.parametrize("provider_name", ["opper", "auto"])
def test_config_selects_gateway_when_only_gateway_is_configured(provider_name: str) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"model": "claude-sonnet-4-6", "provider": provider_name}},
            "providers": {"opper": {"apiKey": "test-opper-key"}},
        }
    )
    assert config.get_provider_name() == "opper"
    assert config.get_api_key() == "test-opper-key"
    assert config.get_api_base() == "https://api.opper.ai/v3/compat"
    assert find_gateway(api_base=config.get_api_base()) is find_by_name("opper")


@pytest.mark.parametrize(
    ("provider_name", "expected_provider"), [("auto", "anthropic"), ("opper", "opper")]
)
def test_config_respects_provider_selection_with_native_credentials(
    provider_name: str, expected_provider: str
) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"model": "claude-sonnet-4-6", "provider": provider_name}},
            "providers": {
                "anthropic": {"apiKey": "test-anthropic-key"},
                "opper": {"apiKey": "test-opper-key"},
            },
        }
    )
    assert config.get_provider_name() == expected_provider
    assert config.get_api_key() == getattr(config.providers, expected_provider).api_key


def test_runtime_reads_provider_key_without_persisting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPPER_API_KEY", "test-opper-key")
    monkeypatch.delenv("PAOS_OPPER_API_KEY", raising=False)
    config = Config()
    service = ProviderService(config)
    assert service.config.providers.opper.api_key == "test-opper-key"
    assert config.providers.opper.api_key == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model", ["claude-sonnet-4-6", "anthropic/claude-sonnet-4-6", "opper/gpt-5.4-mini"]
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
    provider = LiteLLMProvider(api_key="test-opper-key", provider_name="opper")
    response = await provider.chat(messages=[{"role": "user", "content": "hello"}], model=model)
    assert response.content == "ok"
    kwargs = completion.call_args.kwargs
    assert kwargs["model"] == f"custom_openai/{model.removeprefix('opper/')}"
    assert kwargs["api_key"] == "test-opper-key"
    assert kwargs["api_base"] == "https://api.opper.ai/v3/compat"

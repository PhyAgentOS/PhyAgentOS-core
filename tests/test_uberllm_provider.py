"""UberLLM gateway selection and OpenAI-compatible request routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import litellm
import pytest

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.registry import find_by_name, find_gateway
from PhyAgentOS.providers.service import ProviderService


@pytest.mark.parametrize("provider_name", ["uberllm", "auto"])
def test_config_selects_gateway_when_only_gateway_is_configured(provider_name: str) -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {"model": "deepseek/deepseek-v4-flash", "provider": provider_name}
            },
            "providers": {"uberllm": {"apiKey": "test-uberllm-key"}},
        }
    )
    assert config.get_provider_name() == "uberllm"
    assert config.get_api_key() == "test-uberllm-key"
    assert config.get_api_base() == "https://api.uberllm.dev/v1"
    assert find_gateway(api_base=config.get_api_base()) is find_by_name("uberllm")


@pytest.mark.parametrize(
    ("provider_name", "expected_provider"), [("auto", "deepseek"), ("uberllm", "uberllm")]
)
def test_config_respects_provider_selection_with_native_credentials(
    provider_name: str, expected_provider: str
) -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {"model": "deepseek/deepseek-v4-flash", "provider": provider_name}
            },
            "providers": {
                "deepseek": {"apiKey": "test-deepseek-key"},
                "uberllm": {"apiKey": "test-uberllm-key"},
            },
        }
    )
    assert config.get_provider_name() == expected_provider
    assert config.get_api_key() == getattr(config.providers, expected_provider).api_key


def test_runtime_reads_provider_key_without_persisting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UBERLLM_API_KEY", "test-uberllm-key")
    monkeypatch.delenv("PAOS_UBERLLM_API_KEY", raising=False)
    config = Config()
    service = ProviderService(config)
    assert service.config.providers.uberllm.api_key == "test-uberllm-key"
    assert config.providers.uberllm.api_key == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "deepseek/deepseek-v4-flash",
        "anthropic/claude-sonnet-4.6",
        "uberllm/openai/gpt-5.4-mini",
    ],
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
    provider = LiteLLMProvider(api_key="test-uberllm-key", provider_name="uberllm")
    response = await provider.chat(messages=[{"role": "user", "content": "hello"}], model=model)
    assert response.content == "ok"
    kwargs = completion.call_args.kwargs
    assert kwargs["model"] == f"custom_openai/{model.removeprefix('uberllm/')}"
    assert kwargs["api_key"] == "test-uberllm-key"
    assert kwargs["api_base"] == "https://api.uberllm.dev/v1"

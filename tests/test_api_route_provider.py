"""API Route gateway selection and OpenAI-compatible request routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import litellm
import pytest

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.registry import find_by_name, find_gateway
from PhyAgentOS.providers.service import ProviderService


@pytest.mark.parametrize("provider_name", ["api_route", "auto"])
def test_config_selects_gateway_before_native_model_provider(provider_name: str) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"model": "claude-fable-5-1", "provider": provider_name}},
            "providers": {"api_route": {"apiKey": "test-route-key"}},
        }
    )
    assert config.get_provider_name() == "api_route"
    assert config.get_api_key() == "test-route-key"
    assert config.get_api_base() == "https://global.api-route.com/v1"
    assert find_gateway(api_base=config.get_api_base()) is find_by_name("api_route")


def test_runtime_reads_provider_key_without_persisting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_ROUTE_API_KEY", "test-route-key")
    monkeypatch.delenv("PAOS_API_ROUTE_API_KEY", raising=False)
    config = Config()
    service = ProviderService(config)
    assert service.config.providers.api_route.api_key == "test-route-key"
    assert config.providers.api_route.api_key == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6.1-sol", "claude-fable-5-1", "api_route/gpt-6.1-sol"])
async def test_request_uses_gateway_and_preserves_model_id(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    completion = AsyncMock(
        return_value=litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "ok"}}]
        )
    )
    monkeypatch.setattr("PhyAgentOS.providers.litellm_provider.acompletion", completion)
    provider = LiteLLMProvider(api_key="test-route-key", provider_name="api_route")
    response = await provider.chat(messages=[{"role": "user", "content": "hello"}], model=model)
    assert response.content == "ok"
    kwargs = completion.call_args.kwargs
    assert kwargs["model"] == f"custom_openai/{model.removeprefix('api_route/')}"
    assert kwargs["api_key"] == "test-route-key"
    assert kwargs["api_base"] == "https://global.api-route.com/v1"

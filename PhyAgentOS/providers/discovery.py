"""Live model discovery using each provider's model-list API."""

from __future__ import annotations

import asyncio

import httpx

from PhyAgentOS.config.schema import ProviderConfig
from PhyAgentOS.providers.errors import describe_provider_error
from PhyAgentOS.providers.registry import ProviderSpec
from PhyAgentOS.providers.service import ProviderError

# Discovery defaults only: native chat clients retain their own endpoint handling.
_DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://api.z.ai/api/paas/v4",
}


class ModelDiscoveryUnavailableError(ProviderError):
    """No usable model-list API; the caller may offer manual configuration."""


async def fetch_models(spec: ProviderSpec, cfg: ProviderConfig) -> list[str]:
    if spec.is_oauth or spec.name == "azure_openai":
        raise ModelDiscoveryUnavailableError(
            "Model discovery is unavailable for this provider; select a saved model or enter "
            "a model ID (Azure requires a deployment name)."
        )
    base = cfg.api_base or spec.default_api_base or _DEFAULT_BASES.get(spec.name)
    if not base:
        raise ModelDiscoveryUnavailableError("Set API Base to discover models, or enter a model ID manually.")
    base = base.rstrip("/")
    headers = httpx.Headers()
    params: dict[str, str] = {}
    collection, identity = "data", "id"
    if spec.name == "anthropic":
        headers.update({"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"})
        if not base.endswith("/v1"):
            base += "/v1"
    elif spec.name == "gemini":
        headers["x-goog-api-key"] = cfg.api_key
        collection, identity = "models", "name"
    elif cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    headers.update(cfg.extra_headers or {})
    url = base + "/models"
    if spec.name == "ollama":
        url = base.removesuffix("/v1") + "/api/tags"
        collection, identity = "models", "name"

    values: list[str] = []
    seen_pages: set[str] = set()
    try:
        async with asyncio.timeout(30), httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            while True:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code in {404, 405, 501}:
                    raise ModelDiscoveryUnavailableError(
                        "This endpoint does not support model discovery. Check API Base or enter a model ID manually."
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get(collection), list):
                    raise ModelDiscoveryUnavailableError("The endpoint returned no model list; enter a model ID manually.")
                for item in payload[collection]:
                    if not isinstance(item, dict):
                        continue
                    model = item.get(identity)
                    if not isinstance(model, str) or not model or any(c.isspace() for c in model):
                        continue
                    if spec.name == "gemini":
                        if "generateContent" not in item.get("supportedGenerationMethods", []):
                            continue
                        model = model.removeprefix("models/")
                    values.append(model)
                token = None
                if spec.name == "anthropic" and payload.get("has_more"):
                    token = payload.get("last_id")
                    parameter = "after_id"
                    if not token:
                        raise ProviderError("Model-list pagination failed; no configuration was saved.")
                elif spec.name == "gemini":
                    token = payload.get("nextPageToken")
                    parameter = "pageToken"
                if not token:
                    break
                if not isinstance(token, str) or token in seen_pages:
                    raise ProviderError("Model-list pagination failed; no configuration was saved.")
                seen_pages.add(token)
                params[parameter] = token
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(describe_provider_error(exc)) from None
    return list(dict.fromkeys(values))

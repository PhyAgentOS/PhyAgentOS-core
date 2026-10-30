"""Automatic context-window resolution and its status label.

No provider, endpoint or Gateway is contacted: capacity resolution is a local
metadata/no-metadata decision.
"""

from __future__ import annotations

import pytest

from PhyAgentOS.agent.services.usage import (
    ContextSnapshot,
    estimate_context,
    estimate_context_from_conversation,
)
from PhyAgentOS.config.schema import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    AgentDefaults,
    Config,
)
from PhyAgentOS.providers.base import LLMProvider, LLMResponse
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.service import (
    ProviderService,
    RuntimeSelection,
    SessionRuntimes,
    compose_context_capacity,
    context_window_source_label,
)


class _FakeProvider(LLMProvider):
    def __init__(self, window: int | None = None) -> None:
        super().__init__("key", "https://example.test/v1")
        self.window = window

    async def chat(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - not used
        raise AssertionError("capacity resolution must not call the provider")

    def get_default_model(self) -> str:
        return "deepseek-flash"

    def get_context_window(self, model: str | None = None) -> int | None:
        return self.window


def _custom_config() -> Config:
    config = Config()
    config.agents.defaults.provider = "custom"
    config.agents.defaults.model = "deepseek-flash"
    config.providers.custom.api_base = "https://example.test/v1"
    return config


def _resolve(config: Config, monkeypatch, window: int | None) -> RuntimeSelection:
    provider = _FakeProvider(window)
    monkeypatch.setattr(
        ProviderService, "create_provider", staticmethod(lambda *args, **kwargs: provider)
    )
    return ProviderService(config).resolve()


def test_default_is_automatic_not_a_silent_65536_cap():
    assert AgentDefaults().context_window_tokens is None
    config = Config()
    assert config.agents.defaults.context_window_tokens is None


def test_explicit_value_is_kept_and_acts_as_a_cap():
    defaults = AgentDefaults(contextWindowTokens=65_536)
    assert defaults.context_window_tokens == 65_536
    assert "context_window_tokens" in defaults.model_fields_set
    assert compose_context_capacity(reported=200_000, configured=65_536) == (
        65_536,
        "configured",
    )


def test_known_model_window_is_used_without_config():
    assert compose_context_capacity(reported=200_000, configured=None) == (200_000, "model")


def test_smaller_model_window_wins_over_a_larger_cap():
    assert compose_context_capacity(reported=128_000, configured=1_000_000) == (
        128_000,
        "model",
    )


def test_unknown_window_uses_the_documented_fallback():
    assert compose_context_capacity(reported=None, configured=None) == (
        DEFAULT_CONTEXT_WINDOW_TOKENS,
        "fallback",
    )


def test_implausible_model_value_is_treated_as_unknown():
    assert compose_context_capacity(reported=10**12, configured=None) == (
        DEFAULT_CONTEXT_WINDOW_TOKENS,
        "fallback",
    )


def test_resolve_reports_effective_window_and_source(monkeypatch):
    config = _custom_config()
    selection = _resolve(config, monkeypatch, 131_072)
    assert selection.context_window_tokens == 131_072
    assert selection.context_window_source == "model"
    assert selection.model_context_window_tokens == 131_072


def test_resolve_without_metadata_uses_fallback_and_explicit_cap(monkeypatch):
    config = _custom_config()
    selection = _resolve(config, monkeypatch, None)
    assert selection.context_window_tokens == DEFAULT_CONTEXT_WINDOW_TOKENS
    assert selection.context_window_source == "fallback"

    explicit = _custom_config()
    explicit.agents.defaults.context_window_tokens = 32_768
    selection = _resolve(explicit, monkeypatch, None)
    assert selection.context_window_tokens == 32_768
    assert selection.context_window_source == "configured"


def test_custom_endpoint_never_infers_capacity_from_a_similar_catalog_name(monkeypatch):
    """`custom` + `deepseek-flash` must stay unknown despite any catalog entry."""
    import litellm

    def _no_fuzzy_lookup(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("fuzzy model metadata lookup is not allowed")

    monkeypatch.setattr(litellm, "get_model_info", _no_fuzzy_lookup)
    monkeypatch.setattr(
        litellm,
        "model_cost",
        {
            "deepseek-flash": {
                "litellm_provider": "deepseek",
                "max_input_tokens": 1_000_000,
            }
        },
    )
    provider = LiteLLMProvider(api_base="https://example.test/v1", provider_name="custom")
    assert provider.get_context_window("deepseek-flash") is None


def _catalog() -> dict[str, dict]:
    """Controlled local metadata: no remote catalog and no network."""
    return {
        "claude-opus-4-5": {
            "litellm_provider": "anthropic",
            "max_input_tokens": 200_000,
        },
        "gpt-4o": {"litellm_provider": "openai", "max_input_tokens": 128_000},
        "deepseek-chat": {
            "litellm_provider": "deepseek",
            "max_input_tokens": 131_072,
        },
        "deepseek-flash": {
            "litellm_provider": "deepseek",
            "max_input_tokens": 1_000_000,
        },
        "claude-opus-4-5-20251101": {
            "litellm_provider": "anthropic",
            "max_input_tokens": 200_000,
        },
        "output-only": {"litellm_provider": "anthropic", "max_tokens": 8_192},
    }


def test_own_prefixed_model_uses_the_exact_bare_catalog_entry(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", _catalog())
    provider = LiteLLMProvider(provider_name="anthropic")
    assert provider.get_context_window("anthropic/claude-opus-4-5") == 200_000
    assert provider.get_context_window("claude-opus-4-5") == 200_000
    # The prefix is stripped only once, and the default model goes through the
    # same exact lookup.
    defaulted = LiteLLMProvider(
        provider_name="anthropic", default_model="anthropic/claude-opus-4-5"
    )
    assert defaulted.get_context_window() == 200_000


def test_own_prefix_strips_once_for_vendor_namespaced_models(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", _catalog())
    provider = LiteLLMProvider(provider_name="deepseek")
    assert provider.get_context_window("deepseek/deepseek-chat") == 131_072
    assert provider.get_context_window("deepseek-chat") == 131_072
    # A doubly prefixed name is not peeled twice.
    assert provider.get_context_window("deepseek/deepseek/deepseek-chat") is None


def test_foreign_prefix_is_not_stripped(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", _catalog())
    provider = LiteLLMProvider(provider_name="anthropic")
    assert provider.get_context_window("openai/gpt-4o") is None
    assert provider.get_context_window("openrouter/anthropic/claude-opus-4-5") is None


def test_catalog_provider_identity_must_match(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", _catalog())
    provider = LiteLLMProvider(provider_name="anthropic")
    # Exact bare key, but the entry belongs to another provider.
    assert provider.get_context_window("gpt-4o") is None


def test_gateway_and_direct_providers_never_read_the_catalog(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", _catalog())
    gateway = LiteLLMProvider(provider_name="openrouter")
    assert gateway.get_context_window("anthropic/claude-opus-4-5") is None
    custom = LiteLLMProvider(provider_name="custom")
    assert custom.get_context_window("deepseek-flash") is None


def test_suffix_and_partial_names_are_not_matched(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", _catalog())
    provider = LiteLLMProvider(provider_name="anthropic")
    assert provider.get_context_window("claude-opus-4") is None
    assert provider.get_context_window("opus-4-5") is None


def test_output_limit_is_not_used_as_an_input_window(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", _catalog())
    provider = LiteLLMProvider(provider_name="anthropic")
    assert provider.get_context_window("output-only") is None


def test_direct_custom_provider_has_no_metadata():
    # ProviderService builds CustomProvider for `custom`; its base contract
    # reports no window and no endpoint probe is made.
    from PhyAgentOS.providers.registry import find_by_name

    config = _custom_config()
    service = ProviderService(config)
    spec = find_by_name("custom")
    assert spec is not None
    provider = service.create_provider(
        spec,
        config.providers.custom,
        "deepseek-flash",
        "https://example.test/v1",
    )
    assert provider.get_context_window("deepseek-flash") is None


def test_status_command_shows_capacity_and_source():
    selection = RuntimeSelection(
        "custom",
        "deepseek-flash",
        None,
        None,
        _FakeProvider(),
        DEFAULT_CONTEXT_WINDOW_TOKENS,
        "fallback",
    )
    text = SessionRuntimes(ProviderService(_custom_config()), selection).command("s", "/status")
    assert "Context window: 65536" in text
    assert context_window_source_label("fallback") in text
    assert "model reports" not in text


def test_snapshot_label_shows_used_effective_percent_and_assumed_source():
    snapshot = ContextSnapshot(
        tokens=12_345,
        capacity=65_536,
        capacity_source="fallback",
    )
    assert snapshot.label() == "context 12.3k/65.5k ≈19% (assumed)"

    exact = ContextSnapshot(
        tokens=12_345,
        capacity=200_000,
        exact=True,
        capacity_source="model",
        tokens_source="provider_usage",
    )
    assert exact.label() == "context 12.3k/200k 6%"


def test_capacity_source_changes_the_ratio_but_not_the_token_count():
    conversation = [{"role": "user", "content": "hello"}]
    small = estimate_context_from_conversation(conversation, None, capacity=1_000)
    large = estimate_context_from_conversation(conversation, None, capacity=10_000)
    assert small.tokens == large.tokens
    assert small.percent is not None and large.percent is not None
    assert small.percent > large.percent


def test_failed_estimate_stays_missing_instead_of_zero(monkeypatch):
    import PhyAgentOS.agent.services.usage as usage

    monkeypatch.setattr(usage, "estimate_prompt_tokens_chain", lambda *a, **k: (0, "none"))
    snapshot = estimate_context_from_conversation(
        [{"role": "user", "content": "hello"}],
        None,
        capacity=1_000,
    )
    assert snapshot.tokens is None
    assert snapshot.label() == "context --"
    assert snapshot.percent is None


def test_usage_without_capacity_keeps_double_dash(monkeypatch):
    import PhyAgentOS.agent.services.usage as usage

    monkeypatch.setattr(usage, "estimate_prompt_tokens_chain", lambda *a, **k: (42, "tiktoken"))
    snapshot = estimate_context_from_conversation(
        [{"role": "user", "content": "hello"}], None, capacity=None
    )
    assert snapshot.label() == "context 42/--"


@pytest.mark.parametrize(
    ("reported", "configured", "expected"),
    [
        (None, None, (DEFAULT_CONTEXT_WINDOW_TOKENS, "fallback")),
        (0, None, (DEFAULT_CONTEXT_WINDOW_TOKENS, "fallback")),
        (-1, None, (DEFAULT_CONTEXT_WINDOW_TOKENS, "fallback")),
        (True, None, (DEFAULT_CONTEXT_WINDOW_TOKENS, "fallback")),
    ],
)
def test_invalid_reported_values_cannot_become_capacity(reported, configured, expected):
    assert compose_context_capacity(reported=reported, configured=configured) == expected


def test_long_history_estimate_is_not_created_from_nothing():
    assert estimate_context([], capacity=None).tokens is None

"""Effort must affect native request parameters, or fail without changing state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from litellm.utils import get_optional_params

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from PhyAgentOS.providers.effort import apply_litellm_effort, supported_efforts, supports_effort
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    SessionRuntimes,
    provider_spec,
)


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-5"])
def test_reasoning_requires_adapter_mapping(monkeypatch, model):
    monkeypatch.setattr("litellm.supports_reasoning", lambda **kw: True)
    monkeypatch.setattr("litellm.get_supported_openai_params", lambda **kw: ["temperature"])
    assert not supports_effort(provider_spec("openai"), model)


def test_toggle_only_model_does_not_claim_effort_support():
    assert not supports_effort(provider_spec("deepseek"), "deepseek-reasoner")


@pytest.mark.parametrize("provider,model,expected", [
    ("anthropic", "claude-opus-4-6", ("low", "medium", "high", "max")),
    ("anthropic", "anthropic/claude-sonnet-4-5", ("low", "medium", "high")),
    ("openai", "gpt-5.2", ("minimal", "low", "medium", "high", "xhigh")),
    ("openai", "gpt-5.5-pro", ("medium", "high", "xhigh")),
    ("openai_codex", "openai-codex/gpt-5.2", ("minimal", "low", "medium", "high", "xhigh")),
    ("custom", "gpt-5.2", ("minimal", "low", "medium", "high", "xhigh")),
    ("azure_openai", "gpt-5.2", ("minimal", "low", "medium", "high", "xhigh")),
    ("openai", "gpt-4o", ()),
    ("deepseek", "deepseek-reasoner", ()),
])
def test_effort_choices_follow_model_and_route(provider, model, expected):
    assert supported_efforts(provider_spec(provider), model) == expected


def test_unknown_direct_model_does_not_gain_extra_levels():
    assert supported_efforts(provider_spec("custom"), "gpt-5-unknown-deployment") == ("low", "medium", "high")


def test_gateway_does_not_borrow_native_extra_levels(monkeypatch):
    import litellm

    monkeypatch.setitem(litellm.model_cost, "openrouter/openai/gpt-5.2", {"supports_reasoning": True})
    assert "xhigh" in supported_efforts(provider_spec("openai"), "gpt-5.2")
    assert "xhigh" not in supported_efforts(provider_spec("openrouter"), "openai/gpt-5.2")


def test_explicit_catalog_levels_and_adapter_rejection(monkeypatch):
    import litellm

    info = dict(litellm.model_cost["gpt-5.2"], reasoning_effort_levels=["high", "xhigh"])
    monkeypatch.setitem(litellm.model_cost, "gpt-5.2", info)
    assert supported_efforts(provider_spec("openai"), "gpt-5.2") == ("high", "xhigh")

    def map_params(**kwargs):
        if kwargs["reasoning_effort"] == "xhigh":
            raise ValueError("adapter cannot map this level")
        return {"reasoning_effort": kwargs["reasoning_effort"]}

    monkeypatch.setattr("litellm.utils.get_optional_params", map_params)
    assert supported_efforts(provider_spec("openai"), "gpt-5.2") == ("high",)
    monkeypatch.setattr("litellm.utils.get_optional_params", lambda **kw: {"extra_body": {}})
    assert supported_efforts(provider_spec("openai"), "gpt-5.2") == ()


def test_claude_max_is_native_output_effort():
    kwargs = {"model": "anthropic/claude-opus-4-6", "max_tokens": 4096}
    apply_litellm_effort(kwargs, "max")
    native = get_optional_params(
        model="claude-opus-4-6", custom_llm_provider="anthropic",
        reasoning_effort=kwargs["reasoning_effort"], drop_params=kwargs["drop_params"],
    )
    assert native["output_config"] == {"effort": "max"}
    assert native["thinking"]["type"] == "adaptive"
    assert "budget_tokens" not in native["thinking"]
    assert kwargs["max_tokens"] == 4096


def test_openai_xhigh_reaches_native_payload():
    kwargs = {"model": "openai/gpt-5.2", "max_tokens": 4096}
    apply_litellm_effort(kwargs, "xhigh")
    native = get_optional_params(
        model="gpt-5.2", custom_llm_provider="openai",
        reasoning_effort=kwargs["reasoning_effort"], drop_params=False,
    )
    assert native["reasoning_effort"] == "xhigh"


def test_new_effort_is_shared_by_startup_commands_and_session_listing():
    from PhyAgentOS.cli.commands import _apply_startup_overrides

    config = Config()
    config.agents.defaults.provider = "openai"
    config.agents.defaults.model = "gpt-5.2"
    config.providers.openai.api_key = "test"
    changed = _apply_startup_overrides(config, None, None, "xhigh")
    assert changed.agents.defaults.reasoning_effort == "xhigh"
    service = ProviderService(changed)
    sessions = SessionRuntimes(service, service.resolve())
    assert "xhigh" in sessions.command("alice", "/effort list")
    original = sessions.get("alice")
    with pytest.raises(ProviderError, match="Available options"):
        sessions.command("alice", "/model gpt-5")
    assert sessions.get("alice") is original
    sessions.command("alice", "/effort none")
    sessions.command("alice", "/model gpt-5")
    assert "xhigh" not in sessions.command("alice", "/effort list")
    assert sessions.get("bob") is original


@pytest.mark.parametrize("model", ["claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"])
@pytest.mark.parametrize("effort,budget", [("low", 1024), ("medium", 2048), ("high", 4096)])
def test_claude_native_budget_and_output_allowance(effort, budget, model):
    kwargs = {"model": model, "max_tokens": 4096, "temperature": 0.7}
    apply_litellm_effort(kwargs, effort)
    native = get_optional_params(
        model="claude-sonnet-4-5", custom_llm_provider="anthropic",
        reasoning_effort=kwargs["reasoning_effort"], max_tokens=kwargs["max_tokens"],
        drop_params=kwargs["drop_params"],
    )
    assert native["thinking"] == {"type": "enabled", "budget_tokens": budget}
    assert native["max_tokens"] == budget + 4096
    assert "temperature" not in kwargs
    assert kwargs["drop_params"] is False


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_openai_native_effort(effort):
    kwargs = {"model": "openai/gpt-5", "max_tokens": 4096}
    apply_litellm_effort(kwargs, effort)
    native = get_optional_params(
        model="gpt-5", custom_llm_provider="openai", reasoning_effort=kwargs["reasoning_effort"],
        max_tokens=kwargs["max_tokens"], drop_params=False,
    )
    assert native["reasoning_effort"] == effort


def test_gemini_native_budget_changes_with_effort():
    budgets = []
    for effort in ("low", "medium", "high"):
        kwargs = {"model": "gemini/gemini-2.5-pro", "max_tokens": 4096}
        apply_litellm_effort(kwargs, effort)
        native = get_optional_params(
            model="gemini-2.5-pro", custom_llm_provider="gemini",
            reasoning_effort=kwargs["reasoning_effort"], drop_params=False,
        )
        budgets.append(native["thinkingConfig"]["thinkingBudget"])
    assert budgets[0] < budgets[1] < budgets[2]


def test_adaptive_claude_maps_output_effort():
    kwargs = {"model": "anthropic/claude-sonnet-4-6", "max_tokens": 4096}
    apply_litellm_effort(kwargs, "high")
    native = get_optional_params(
        model="claude-sonnet-4-6", custom_llm_provider="anthropic",
        reasoning_effort=kwargs["reasoning_effort"], drop_params=False,
    )
    assert native["thinking"]["type"] == "adaptive"
    assert native["output_config"] == {"effort": "high"}
    assert kwargs["max_tokens"] == 4096


@pytest.mark.parametrize("effort", ["high", "xhigh", "none", None])
async def test_custom_openai_effort_payload(monkeypatch, effort):
    from PhyAgentOS.providers.custom_provider import CustomProvider

    provider = CustomProvider(api_key="test", default_model="gpt-5.2")
    transport = AsyncMock()
    monkeypatch.setattr(provider._client.chat.completions, "create", transport)
    monkeypatch.setattr(provider, "_parse", lambda response: LLMResponse("ok"))
    try:
        await provider.chat([], reasoning_effort=effort)
        assert transport.call_args.kwargs.get("reasoning_effort") == (effort if effort in {"high", "xhigh"} else None)
        assert transport.call_args.kwargs["max_completion_tokens"] == 4096
        assert "max_tokens" not in transport.call_args.kwargs
        assert "temperature" not in transport.call_args.kwargs
        await provider.chat([], reasoning_effort="none")
        assert "reasoning_effort" not in transport.call_args.kwargs
    finally:
        await provider._client.close()


def test_remote_effort_rejection_is_actionable_and_redacted():
    from PhyAgentOS.providers.errors import describe_provider_error

    message = describe_provider_error(ValueError("model rejected reasoning_effort: secret-token"))
    assert "/effort none" in message
    assert "secret-token" not in message


def test_reasoning_token_budget_is_not_an_http_status():
    from PhyAgentOS.providers.errors import describe_provider_error

    message = describe_provider_error(ValueError("400: budget_tokens 5000 exceeds max_tokens 4096"))
    assert "/effort none" in message
    assert not LiteLLMProvider._is_transient_error(message)


async def test_redacted_transient_error_still_retries(monkeypatch):
    from PhyAgentOS.providers.custom_provider import CustomProvider

    provider = CustomProvider(api_key="test", default_model="gpt-4o")
    transport = AsyncMock(side_effect=[RuntimeError("Service temporarily unavailable: secret"), object()])
    monkeypatch.setattr(provider._client.chat.completions, "create", transport)
    monkeypatch.setattr(provider, "_parse", lambda response: LLMResponse("ok"))
    monkeypatch.setattr(provider, "_CHAT_RETRY_DELAYS", (0,))
    try:
        assert (await provider.chat_with_retry([])).content == "ok"
        assert transport.await_count == 2
    finally:
        await provider._client.close()


def test_azure_none_uses_model_default():
    from PhyAgentOS.providers.azure_openai_provider import AzureOpenAIProvider

    provider = AzureOpenAIProvider(api_key="test", api_base="https://azure.example.test")
    payload = provider._prepare_request_payload("gpt-4o", [], reasoning_effort="none")
    assert "reasoning_effort" not in payload
    assert payload["temperature"] == 0.7


@pytest.mark.parametrize("effort", ["none", "xhigh"])
async def test_codex_effort_payload(monkeypatch, effort):
    from PhyAgentOS.providers.openai_codex_provider import OpenAICodexProvider

    monkeypatch.setattr(
        "PhyAgentOS.providers.openai_codex_provider.asyncio.to_thread",
        AsyncMock(return_value=SimpleNamespace(account_id="test", access="test")),
    )
    transport = AsyncMock(return_value=("ok", [], "stop"))
    monkeypatch.setattr("PhyAgentOS.providers.openai_codex_provider._request_codex", transport)
    result = await OpenAICodexProvider(default_model="gpt-5.2").chat([], reasoning_effort=effort)
    assert result.content == "ok"
    body = transport.call_args.args[2]
    if effort == "none":
        assert "reasoning" not in body
    else:
        assert body["reasoning"] == {"effort": "xhigh"}


async def test_memory_consolidation_does_not_combine_thinking_and_forced_tools(tmp_path, monkeypatch):
    from PhyAgentOS.agent.memory import MemoryStore

    provider = LiteLLMProvider(api_key="test", default_model="claude-sonnet-4-5", provider_name="anthropic")
    provider.generation = GenerationSettings(reasoning_effort="high")
    requests = []

    async def complete(**kwargs):
        requests.append(get_optional_params(
            model=kwargs["model"], custom_llm_provider="anthropic",
            reasoning_effort=kwargs.get("reasoning_effort"),
            tool_choice=kwargs["tool_choice"], max_tokens=kwargs["max_tokens"],
            drop_params=kwargs["drop_params"],
        ))
        return object()

    monkeypatch.setattr("PhyAgentOS.providers.litellm_provider.acompletion", complete)
    monkeypatch.setattr(provider, "_parse_response", lambda response: LLMResponse(
        None, tool_calls=[ToolCallRequest("save", "save_memory", {
            "history_entry": "Stored test history", "memory_update": "Stored test memory",
        })],
    ))
    memory = MemoryStore(tmp_path)
    assert await memory.consolidate([{"role": "user", "content": "remember this"}], provider, provider.default_model)
    assert "thinking" not in requests[0]
    assert requests[0]["tool_choice"] == {"type": "any"}
    assert memory.read_long_term() == "Stored test memory"
    assert provider.generation.reasoning_effort == "high"


async def test_unsupported_request_never_reaches_transport(monkeypatch):
    transport = AsyncMock()
    monkeypatch.setattr("PhyAgentOS.providers.litellm_provider.acompletion", transport)
    provider = LiteLLMProvider(api_key="test", default_model="gpt-4o", provider_name="openai")
    result = await provider.chat([], reasoning_effort="high")
    assert result.finish_reason == "error"
    assert "not supported" in result.content
    transport.assert_not_awaited()


@pytest.mark.parametrize("provider_name,model,effort", [
    ("openai", "gpt-5", "xhigh"),
    ("openai", "gpt-5.2", "max"),
    ("anthropic", "claude-sonnet-4-5", "max"),
])
async def test_unsupported_extra_level_never_reaches_transport(monkeypatch, provider_name, model, effort):
    transport = AsyncMock()
    monkeypatch.setattr("PhyAgentOS.providers.litellm_provider.acompletion", transport)
    provider = LiteLLMProvider(api_key="test", default_model=model, provider_name=provider_name)
    response = await provider.chat([], reasoning_effort=effort)
    assert response.finish_reason == "error"
    assert "Available options" in response.content
    transport.assert_not_awaited()


async def test_supported_request_does_not_silently_drop_effort(monkeypatch):
    transport = AsyncMock()
    monkeypatch.setattr("PhyAgentOS.providers.litellm_provider.acompletion", transport)
    provider = LiteLLMProvider(api_key="test", default_model="gpt-5", provider_name="openai")
    monkeypatch.setattr(provider, "_parse_response", lambda response: LLMResponse("ok"))
    await provider.chat([], reasoning_effort="high")
    assert transport.call_args.kwargs["reasoning_effort"] == "high"
    assert transport.call_args.kwargs["drop_params"] is False


@pytest.mark.parametrize("model,command,result", [
    ("gpt-4o", "/effort high", "unsupported"),
    ("gpt-4o", "/effort", "unsupported"),
    ("gpt-5", "/effort", "cancel"),
    ("gpt-5", "/effort", "high"),
    ("gpt-5", "/effort none", "none"),
    ("gpt-5.2", "/effort", "xhigh"),
    ("gpt-5", "/effort xhigh", "unsupported"),
])
async def test_effort_picker_is_transactional(monkeypatch, model, command, result):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from PhyAgentOS.cli.providers import _interactive_model_command

    config = Config()
    config.agents.defaults.provider = "openai"
    config.agents.defaults.model = model
    config.providers.openai.api_key = "test"
    service = ProviderService(config)
    startup = service.resolve()
    sessions = SessionRuntimes(service, startup)
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            if result == "cancel":
                pipe.send_text("\x1b")
            elif command == "/effort" and result in {"high", "xhigh"}:
                # Start at none; traverse the actual model's available levels.
                levels = ["none", *supported_efforts(provider_spec("openai"), model)]
                pipe.send_text("\x1b[B" * levels.index(result) + "\r")
            response = await _interactive_model_command(command, sessions, "cli:alice")
    if result in {"unsupported", "cancel"}:
        assert sessions.get("cli:alice") is startup
    else:
        assert sessions.get("cli:alice").effort == (None if result == "none" else result)
    if result == "unsupported":
        assert "not supported" in response
    assert sessions.get("cli:bob") is startup
    assert config.agents.defaults.reasoning_effort is None

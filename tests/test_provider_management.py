"""Provider configuration, CLI transactions and concurrent runtime isolation."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock

import httpx
import pytest
from typer.testing import CliRunner

from PhyAgentOS.cli.commands import (
    _apply_startup_overrides,
    _make_evolution_provider,
    _make_forge_verifier,
    app,
)
from PhyAgentOS.config.loader import load_config, save_config
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    SessionRuntimes,
    mask_secret,
    provider_spec,
)


class RecordingProvider(LLMProvider):
    def __init__(self, model="gpt-5", *, block=False):
        super().__init__("test-secret", "https://example.test/v1")
        self.model = model
        self.calls = []
        self.block = block
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def get_default_model(self):
        return self.model

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.block and len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
            return LLMResponse(
                None, tool_calls=[ToolCallRequest("call-1", "read_file", {"path": "test"})]
            )
        return LLMResponse("done")


@pytest.fixture
def config(monkeypatch):
    for name in list(os.environ):
        if name.endswith("_API_KEY") or name.lower().startswith("phyagentos_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr("PhyAgentOS.providers.service.oauth_configured", lambda name: False)
    cfg = Config()
    cfg.agents.defaults.provider = "openai"
    cfg.agents.defaults.model = "gpt-5"
    cfg.providers.openai.api_key = "sk-test-secret-123456"
    cfg.providers.anthropic.api_key = "sk-anthropic-secret-123456"
    cfg.providers.anthropic.default_model = "claude-sonnet-4"
    cfg.providers.custom.api_base = "https://custom.example.test/v1"
    cfg.providers.custom.default_model = "gpt-5.2"
    return cfg


@pytest.fixture
def config_path(tmp_path, config):
    path = tmp_path / "config.json"
    save_config(config, path)
    return path


def test_startup_overrides_are_atomic_and_do_not_write(config, config_path):
    before = config_path.read_bytes()
    changed = _apply_startup_overrides(config, "custom", None, "high")
    assert changed.agents.defaults.provider == "custom"
    assert changed.agents.defaults.model == "gpt-5.2"
    assert changed.agents.defaults.reasoning_effort == "high"
    assert config.agents.defaults.provider == "openai"
    assert config.agents.defaults.reasoning_effort is None
    assert config_path.read_bytes() == before


def test_startup_snapshot_includes_environment_credentials_for_background_jobs(config, monkeypatch):
    monkeypatch.setenv("PAOS_DEEPSEEK_API_KEY", "background-secret")
    changed = _apply_startup_overrides(config, "deepseek", "deepseek-chat", None)
    assert changed.providers.deepseek.api_key == "background-secret"
    assert not config.providers.deepseek.api_key


@pytest.mark.parametrize("explicit_override", [False, True])
def test_startup_provider_override_does_not_retarget_model_based_background_provider(
    config, explicit_override, monkeypatch,
):
    from PhyAgentOS.verification.service import _provider

    config.agents.defaults.provider = "auto"
    config.agents.defaults.model = "deepseek-chat"
    config.providers.deepseek.api_key = "sk-deepseek-secret-123456"
    config.providers.deepseek.api_base = "https://main.example.test/v1"
    config.agents.evolution.enabled = True
    config.agents.evolution.model = "claude-sonnet-4"
    config.agents.verification.service_enabled = True
    config.agents.verification.model = "claude-sonnet-4"
    config.providers.anthropic.extra_headers = {"X-Background": "test"}
    changed = _apply_startup_overrides(
        config, "deepseek" if explicit_override else None, None, None,
    )

    main = ProviderService(changed).resolve()
    evolution, model = _make_evolution_provider(changed, main.provider)
    verifier = _make_forge_verifier(changed, main.provider)
    child_spec = verifier.service.provider_spec
    child = _provider(child_spec, 30)

    assert changed.agents.defaults.provider == "deepseek"
    assert main.name == "deepseek"
    assert evolution._spec.name == "anthropic"
    assert model == "claude-sonnet-4"
    assert child_spec["provider_name"] == "anthropic"
    assert child_spec["api_base"] is None
    assert child_spec["extra_headers"] == {"X-Background": "test"}
    requests = []

    async def complete(**kwargs):
        requests.append(kwargs)
        raise RuntimeError("offline test")

    monkeypatch.setattr("PhyAgentOS.providers.litellm_provider.acompletion", complete)
    asyncio.run(evolution.chat(messages=[], model=model))
    asyncio.run(child.chat(messages=[], model=child_spec["model"]))
    assert len(requests) == 2
    for request in requests:
        assert request["model"] == "claude-sonnet-4"
        assert request["api_key"] == config.providers.anthropic.api_key
        assert "api_base" not in request


@pytest.mark.parametrize("background_provider", [None, "openrouter"])
def test_background_provider_inheritance_and_explicit_override(config, background_provider):
    config.providers.openrouter.api_key = "sk-or-test"
    config.agents.evolution.enabled = True
    config.agents.evolution.provider = background_provider
    config.agents.verification.service_enabled = True
    config.agents.verification.provider = background_provider
    changed = _apply_startup_overrides(config, "custom", "gpt-5", None)
    main = RecordingProvider()

    evolution, model = _make_evolution_provider(changed, main)
    verifier = _make_forge_verifier(changed, main)
    child_spec = verifier.service.provider_spec
    assert model == "gpt-5"
    if background_provider:
        assert evolution is not main
        assert evolution._spec.name == "openrouter"
        assert child_spec["provider_name"] == "openrouter"
        assert child_spec["api_base"] == "https://openrouter.ai/api/v1"
    else:
        assert evolution is main
        assert child_spec["provider_name"] == "custom"
        assert child_spec["api_base"] == config.providers.custom.api_base


def test_evolution_does_not_reuse_forced_provider_when_auto_names_match(config):
    config.agents.evolution.enabled = True
    config.agents.evolution.model = "gpt-4o"
    changed = _apply_startup_overrides(config, "custom", "gpt-5", None)
    main = RecordingProvider()

    evolution, model = _make_evolution_provider(changed, main)

    assert evolution is not main
    assert evolution._spec.name == "openai"
    assert model == "gpt-4o"


@pytest.mark.parametrize("background_name,background_model", [
    ("openai", "gpt-4o"),
    ("deepseek", "deepseek-chat"),
])
def test_independent_background_model_does_not_inherit_main_effort(
    config, background_name, background_model,
):
    config.providers.deepseek.api_key = "test"
    config.agents.defaults.reasoning_effort = "high"
    config.agents.evolution.enabled = True
    config.agents.evolution.provider = background_name
    config.agents.evolution.model = background_model
    config.agents.verification.service_enabled = True
    config.agents.verification.provider = background_name
    config.agents.verification.model = background_model
    main = ProviderService(config).resolve().provider

    evolution, model = _make_evolution_provider(config, main)
    verifier = _make_forge_verifier(config, main)

    assert model == background_model
    assert evolution is not main
    assert evolution._spec.name == background_name
    assert evolution.generation.reasoning_effort is None
    assert verifier.service.provider_spec["reasoning_effort"] is None
    assert main.generation.reasoning_effort == "high"


def test_background_same_target_keeps_startup_effort(config):
    config.agents.defaults.reasoning_effort = "high"
    config.agents.evolution.enabled = True
    config.agents.verification.service_enabled = True
    main = ProviderService(config).resolve().provider

    evolution, model = _make_evolution_provider(config, main)
    verifier = _make_forge_verifier(config, main)

    assert evolution is main
    assert model == config.agents.defaults.model
    assert verifier.service.provider_spec["reasoning_effort"] == "high"


def test_explicit_auto_override_infers_a_different_provider(config):
    selected = ProviderService(config).selection("auto", "claude-sonnet-4")
    assert selected.name == "anthropic"


@pytest.mark.parametrize(
    "name,model,effort,diagnostic",
    [
        ("unknown", "gpt-5", None, "Unknown provider"),
        ("openai", "claude-sonnet-4", None, "cannot be routed"),
        ("openai", "openai/claude-sonnet-4", None, "cannot be routed"),
        ("deepseek", "deepseek-chat", None, "not configured"),
        ("openai", "gpt-4o", "high", "not supported"),
        ("openai", "gpt-5", "invalid", "must be"),
    ],
)
def test_invalid_runtime_selections_are_actionable(config, name, model, effort, diagnostic):
    with pytest.raises(ProviderError, match=diagnostic):
        ProviderService(config).selection(name, model, effort)


def test_environment_keys_are_scoped_and_not_exported(config, monkeypatch, config_path):
    monkeypatch.setenv("PAOS_DEEPSEEK_API_KEY", "environment-secret")
    before = dict(os.environ)
    service = ProviderService(config)
    assert service.config.providers.deepseek.api_key == "environment-secret"
    assert not config.providers.deepseek.api_key
    ProviderService.use(config_path, "custom", "gpt-5")
    assert not load_config(config_path).providers.deepseek.api_key
    assert dict(os.environ) == before


def test_litellm_instances_do_not_change_environment_or_global_endpoint(monkeypatch):
    import litellm

    from PhyAgentOS.providers.litellm_provider import LiteLLMProvider

    before_env = dict(os.environ)
    before_globals = (litellm.api_base, litellm.drop_params, litellm.suppress_debug_info)
    first = LiteLLMProvider(
        "first-key", "https://first.example.test/v1", "gpt-5", provider_name="openrouter"
    )
    second = LiteLLMProvider(
        "second-key", "https://second.example.test/v1", "gpt-5", provider_name="openai"
    )
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("401 api key: first-key second-key")

    monkeypatch.setattr("PhyAgentOS.providers.litellm_provider.acompletion", complete)
    result = asyncio.run(first.chat(messages=[]))
    asyncio.run(second.chat(messages=[]))
    assert calls[0]["api_base"] == "https://first.example.test/v1"
    assert calls[0]["api_key"] == "first-key"
    assert calls[0]["model"] == "openrouter/gpt-5"
    assert calls[1]["api_base"] == "https://second.example.test/v1"
    assert calls[1]["api_key"] == "second-key"
    assert "first-key" not in result.content and "second-key" not in result.content
    assert dict(os.environ) == before_env
    assert (litellm.api_base, litellm.drop_params, litellm.suppress_debug_info) == before_globals


def test_list_show_and_status_never_expose_secrets(config, config_path):
    config.providers.openai.extra_headers = {"Authorization": "Bearer header-secret"}
    config.providers.openai.api_base = "https://api.example.test/private-token?key=url-secret"
    save_config(config, config_path)
    runner = CliRunner()
    listing = runner.invoke(app, ["provider", "list", "--json", "-c", str(config_path)])
    assert listing.exit_code == 0, listing.output
    rows = json.loads(listing.stdout)
    assert next(row for row in rows if row["name"] == "openai")["default"]
    shown = runner.invoke(app, ["provider", "show", "openai", "-c", str(config_path)])
    assert shown.exit_code == 0
    for secret in (config.providers.openai.api_key, "header-secret", "url-secret", "private-token"):
        assert secret not in shown.output + listing.output
    assert mask_secret("tiny") == "****"
    assert mask_secret("sk-test-secret-123456") == "sk-t************3456"
    assert "sk-t************3456" in shown.output


def test_cli_configure_stdin_use_and_remove(config_path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "provider",
            "configure",
            "deepseek",
            "--api-key-stdin",
            "--model",
            "deepseek-chat",
            "--config",
            str(config_path),
        ],
        input="secret-from-stdin\n",
    )
    assert result.exit_code == 0, result.output
    assert "secret-from-stdin" not in result.output
    assert load_config(config_path).providers.deepseek.api_key == "secret-from-stdin"
    result = runner.invoke(app, ["provider", "use", "deepseek", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert load_config(config_path).agents.defaults.model == "deepseek-chat"
    result = runner.invoke(app, ["provider", "remove", "deepseek", "-c", str(config_path)])
    assert result.exit_code == 0
    assert load_config(config_path).providers.deepseek.api_key == ""
    assert provider_spec("deepseek") is not None
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_invalid_json_cannot_be_overwritten_by_management_commands(config_path):
    config_path.write_text('{"api_key": "do-not-echo", invalid json')
    before = config_path.read_bytes()
    result = CliRunner().invoke(app, ["provider", "remove", "openai", "-c", str(config_path)])
    assert result.exit_code == 1
    assert "do-not-echo" not in result.output
    assert config_path.read_bytes() == before


def test_failed_wizard_connection_test_preserves_original_config(config_path, monkeypatch):
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    answers = iter(["new-secret", "https://new.example.test/v1", "{}", "Y"])
    monkeypatch.setattr("PhyAgentOS.cli.providers._prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(
        ProviderService, "discover_models", AsyncMock(side_effect=ProviderError("Authentication failed"))
    )
    before = config_path.read_bytes()
    result = CliRunner().invoke(app, ["provider", "configure", "openai", "-c", str(config_path)])
    assert result.exit_code == 1
    assert config_path.read_bytes() == before
    assert "new-secret" not in result.output


@pytest.mark.parametrize("source", ["environment", "file"])
def test_cli_secret_sources(source, monkeypatch, config_path, tmp_path):
    if source == "environment":
        monkeypatch.setenv("DEPLOYMENT_KEY", "secret-value")
        arguments = ["--api-key-env", "DEPLOYMENT_KEY"]
    else:
        secret_path = tmp_path / "api-key"
        secret_path.write_text("secret-value\n")
        arguments = ["--api-key-file", str(secret_path)]
    result = CliRunner().invoke(
        app,
        [
            "provider",
            "configure",
            "deepseek",
            "--model",
            "deepseek-chat",
            "-c",
            str(config_path),
            *arguments,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "secret-value" not in result.output


@pytest.mark.parametrize("arguments", [[], ["deepseek"], ["deepseek", "--api-key-stdin"]])
def test_non_tty_missing_parameters_never_prompt(arguments, config_path, monkeypatch):
    monkeypatch.setattr(
        "PhyAgentOS.cli.providers._select_provider", lambda *_: pytest.fail("selector opened")
    )
    monkeypatch.setattr(
        "PhyAgentOS.cli.providers._prompt", lambda *_args, **_kwargs: pytest.fail("prompt opened")
    )
    before = config_path.read_bytes()
    result = CliRunner().invoke(app, ["provider", "configure", *arguments, "-c", str(config_path)])
    assert result.exit_code == 1
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("cancel_at", [0, 1, 2, 3, 4])
def test_wizard_cancellation_never_writes_partial_settings(cancel_at, config_path, monkeypatch):
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    monkeypatch.setattr("PhyAgentOS.cli.providers._select_provider", lambda _: "openai")
    answers = iter(["new-hidden-secret", "https://new.example.test/v1", "{}", "n", "gpt-5"])
    count = 0

    def prompt(*args, **kwargs):
        nonlocal count
        if count == cancel_at:
            raise KeyboardInterrupt
        count += 1
        return next(answers)

    monkeypatch.setattr("PhyAgentOS.cli.providers._prompt", prompt)
    before = config_path.read_bytes()
    result = CliRunner().invoke(app, ["provider", "configure", "-c", str(config_path)])
    assert result.exit_code == 1
    assert "Cancelled" in result.output
    assert "new-hidden-secret" not in result.output
    assert config_path.read_bytes() == before


def test_selector_is_inline_keyboard_only_and_can_cancel(config, monkeypatch):
    from PhyAgentOS.cli.providers import _select_provider

    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr("prompt_toolkit.application.Application", FakeApplication)
    with pytest.raises(KeyboardInterrupt):
        _select_provider(ProviderService(config))
    assert captured["full_screen"] is False
    assert captured["mouse_support"] is False
    keys = {
        tuple(str(key) for key in binding.keys) for binding in captured["key_bindings"].bindings
    }
    assert ("Keys.Escape",) in keys
    assert ("Keys.ControlC",) in keys


@pytest.mark.parametrize("keypress", ["\x1b", "\x03"])
def test_selector_real_keyboard_cancel(config, keypress):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from PhyAgentOS.cli.providers import _select_provider

    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keypress)
            with pytest.raises(KeyboardInterrupt):
                _select_provider(ProviderService(config))


def test_selector_real_keyboard_selection(config):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from PhyAgentOS.cli.providers import _select_provider

    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text("\x1b[B\r")
            assert _select_provider(ProviderService(config)) == "openai_codex"


def test_oauth_detection_uses_provider_token_path_without_refreshing(tmp_path, monkeypatch):
    from PhyAgentOS.providers.service import oauth_configured

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("OAUTH_CLI_KIT_TOKEN_PATH", raising=False)
    token_path = tmp_path / "oauth-cli-kit/auth/codex.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(
        json.dumps(
            {
                "access": "local-access",
                "refresh": "local-refresh",
                "expires": 0,
                "account_id": "account",
            }
        )
    )
    before = token_path.read_bytes()
    monkeypatch.setattr("oauth_cli_kit.get_token", lambda: pytest.fail("token refresh attempted"))
    assert oauth_configured("openai_codex")
    assert token_path.read_bytes() == before


def test_explicit_provider_wins_over_endpoint_detection():
    from PhyAgentOS.providers.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(
        "sk-or-example",
        "https://openrouter.example.test/v1",
        "gpt-5",
        provider_name="openai",
    )
    assert provider._resolve_model("gpt-5") == "gpt-5"
    local = LiteLLMProvider(default_model="ollama/qwen3", provider_name="ollama")
    assert local._resolve_model("ollama/qwen3") == "ollama_chat/qwen3"


@pytest.mark.parametrize("prefix", ["", "groq/"])
def test_catalogued_provider_models_with_organization_prefix_are_routable(
    config, config_path, monkeypatch, prefix,
):
    import litellm

    model = "moonshotai/kimi-k2-instruct-0905"
    monkeypatch.setattr(litellm, "model_cost", {"groq/" + model: {"mode": "chat"}})
    config.providers.groq.api_key = "groq-test-key"
    save_config(config, config_path)
    result = CliRunner().invoke(
        app, ["provider", "use", "groq", "--model", prefix + model, "-c", str(config_path)],
    )
    assert result.exit_code == 0, result.output
    selected = load_config(config_path)
    assert selected.agents.defaults.provider == "groq"
    assert selected.agents.defaults.model == prefix + model
    startup = _apply_startup_overrides(selected, None, None, None)
    runtime = ProviderService(startup).resolve()
    assert runtime.provider._resolve_model(runtime.model) == "groq/" + model

    service = ProviderService(config)
    sessions = SessionRuntimes(service, service.resolve())
    sessions.command("alice", "/model " + prefix + model)
    assert sessions.get("alice").name == "groq"
    assert sessions.get("bob").name == "openai"


def test_model_catalog_matching_is_provider_scoped_and_chat_only(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", {
        "github_copilot/mai-code-1-flash": {"mode": "chat"},
        "groq/vendor/embed-v1": {"mode": "embedding"},
        "claude-sonnet-4": {"mode": "chat"},
        "anthropic/claude-sonnet-4": {"mode": "chat"},
    })
    assert ProviderService.routes(provider_spec("github_copilot"), "mai-code-1-flash")
    assert not ProviderService.routes(provider_spec("openai"), "mai-code-1-flash")
    assert not ProviderService.routes(provider_spec("groq"), "vendor/embed-v1")
    assert not ProviderService.routes(provider_spec("groq"), "unknown/model")
    assert not ProviderService.routes(provider_spec("openai"), "claude-sonnet-4")
    assert not ProviderService.routes(provider_spec("openai"), "openai/claude-sonnet-4")
    assert not ProviderService.routes(provider_spec("groq"), "openai/gpt-5")
    assert ProviderService.routes(provider_spec("groq"), "openai/gpt-oss-120b")


@pytest.mark.parametrize(
    "error,expected",
    [
        ("401 api key SECRET", "Authentication"),
        ("connection timeout SECRET", "Network"),
        ("404 unknown model SECRET", "Model"),
        ("404 wrong endpoint SECRET", "Endpoint"),
    ],
)
def test_connection_test_classifies_errors_without_echoing_secrets(
    config, monkeypatch, error, expected
):
    provider = RecordingProvider()
    provider.chat = AsyncMock(return_value=LLMResponse(error, finish_reason="error"))
    monkeypatch.setattr(ProviderService, "create_provider", staticmethod(lambda *_: provider))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ProviderService(config).test("openai"))
    assert expected in str(caught.value)
    assert "SECRET" not in str(caught.value)


def test_session_switch_is_atomic_and_infers_provider(config, monkeypatch):
    monkeypatch.setattr(
        ProviderService,
        "create_provider",
        staticmethod(lambda _s, _c, model, _e: RecordingProvider(model)),
    )
    service = ProviderService(config)
    original = service.resolve()
    sessions = SessionRuntimes(service, original)
    sessions.command("alice", "/effort high")
    selected = sessions.get("alice")
    assert selected.effort == "high"
    with pytest.raises(ProviderError):
        sessions.command("alice", "/model unknown-model")
    assert sessions.get("alice") is selected
    sessions.command("alice", "/effort none")
    sessions.command("alice", "/model claude-sonnet-4")
    assert sessions.get("alice").name == "anthropic"
    assert sessions.get("bob") is original
    assert original.provider.generation.reasoning_effort is None
    sessions.command("alice", "/provider reset")
    assert sessions.get("alice") is original


def test_switch_during_tool_turn_keeps_running_requests_and_background_jobs(
    config, tmp_path, monkeypatch
):
    from PhyAgentOS.agent.loop import AgentLoop
    from PhyAgentOS.bus.events import InboundMessage
    from PhyAgentOS.bus.queue import MessageBus

    created = []

    def create(_spec, _cfg, model, _endpoint):
        provider = RecordingProvider(model)
        created.append(provider)
        return provider

    monkeypatch.setattr(ProviderService, "create_provider", staticmethod(create))

    async def scenario():
        original = RecordingProvider(block=True)
        bus = MessageBus()
        agent = AgentLoop(bus, original, tmp_path, provider_config=config)
        agent.memory_consolidator.maybe_consolidate_by_tokens = AsyncMock()
        agent.tools.execute = AsyncMock(return_value="tool result")
        loop_task = asyncio.create_task(agent.run())

        def message(text, user="alice"):
            return InboundMessage(channel="test", chat_id=user, sender_id=user, content=text)

        async def response():
            while True:
                value = await asyncio.wait_for(bus.consume_outbound(), 2)
                if not value.metadata.get("_progress"):
                    return value.content

        try:
            await bus.publish_inbound(message("start a tool turn"))
            await asyncio.wait_for(original.entered.wait(), 2)
            await bus.publish_inbound(message("/effort high"))
            assert "effort=high" in await response()
            await bus.publish_inbound(message("/provider custom"))
            assert "provider=custom" in await response()
            assert not original.release.is_set()
            assert agent.provider is original
            assert agent.subagents.provider is original
            assert agent.memory_consolidator.provider is original
            assert agent.model == "gpt-5"
            assert agent.session_runtimes.get("test:bob").provider is original
            original.release.set()
            assert await response() == "done"
            assert len(original.calls) == 2
            assert all(call["model"] == "gpt-5" for call in original.calls)
            assert all(call["reasoning_effort"] is None for call in original.calls)
            await bus.publish_inbound(message("next turn"))
            assert await response() == "done"
            assert created[-1].calls[0]["model"] == "gpt-5.2"
            assert created[-1].calls[0]["reasoning_effort"] == "high"
            await bus.publish_inbound(message("another session", "bob"))
            assert await response() == "done"
            assert len(original.calls) == 3
            status = await agent.process_direct("/status", "test:alice")
            assert "gpt-5.2" in status and "test-secret" not in status
            help_text = await agent.process_direct("/help", "test:alice")
            for command in (
                "/new",
                "/stop",
                "/restart",
                "/provider",
                "/model",
                "/effort",
                "/status",
            ):
                assert command in help_text
        finally:
            agent.stop()
            original.release.set()
            await loop_task
            await agent.close_mcp()

    asyncio.run(scenario())


@pytest.fixture
def discovery_transport(monkeypatch):
    """Use real HTTP request construction against an in-memory provider endpoint."""
    client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
        )

    return install


@pytest.mark.parametrize(
    "name,base,path,auth,payload,expected",
    [
        ("openai", None, "/v1/models", "authorization",
         {"data": [{"id": "gpt-5"}, {"id": "gpt-5"}, {"id": "gpt-4o"}, {"id": "invalid model"}]},
         ["gpt-5", "gpt-4o"]),
        ("custom", "https://custom.example.test/proxy/v1/", "/proxy/v1/models", "authorization",
         {"data": [{"id": "vendor/my-model"}]}, ["vendor/my-model"]),
        ("anthropic", "https://anthropic.example.test", "/v1/models", "x-api-key",
         {"data": [{"id": "claude-sonnet-4"}]}, ["claude-sonnet-4"]),
        ("gemini", None, "/v1beta/models", "x-goog-api-key",
         {"models": [
             {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
             {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
         ]}, ["gemini-2.5-pro"]),
        ("ollama", "http://localhost:11434/v1", "/api/tags", "authorization",
         {"models": [{"name": "qwen3:8b"}]}, ["qwen3:8b"]),
        ("deepseek", None, "/v1/models", "authorization",
         {"data": [{"id": "deepseek-chat"}]}, ["deepseek-chat"]),
    ],
)
def test_discovery_uses_provider_api_without_a_model(
    config, discovery_transport, name, base, path, auth, payload, expected,
):
    cfg = getattr(config.providers, name)
    cfg.api_key = "discovery-secret"
    cfg.api_base = base
    cfg.default_model = None
    cfg.extra_headers = {"X-Account": "account-header"}
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == path
        assert request.headers[auth].endswith("discovery-secret")
        assert request.headers["X-Account"] == "account-header"
        return httpx.Response(200, json=payload)

    discovery_transport(respond)
    assert asyncio.run(ProviderService(config).discover_models(name)) == expected
    assert len(requests) == 1
    assert cfg.models == []  # Fetching alone never adds a model.


@pytest.mark.parametrize("name", ["anthropic", "gemini"])
def test_discovery_reads_all_pages(config, discovery_transport, name):
    cfg = getattr(config.providers, name)
    cfg.api_key = "pagination-secret"
    requests = []

    def respond(request):
        requests.append(request)
        if name == "anthropic":
            if len(requests) == 1:
                return httpx.Response(200, json={
                    "data": [{"id": "claude-first"}], "has_more": True, "last_id": "cursor",
                })
            assert request.url.params["after_id"] == "cursor"
            return httpx.Response(200, json={"data": [{"id": "claude-second"}], "has_more": False})
        if len(requests) == 1:
            return httpx.Response(200, json={
                "models": [{"name": "models/gemini-first", "supportedGenerationMethods": ["generateContent"]}],
                "nextPageToken": "cursor",
            })
        assert request.url.params["pageToken"] == "cursor"
        return httpx.Response(200, json={
            "models": [{"name": "models/gemini-second", "supportedGenerationMethods": ["generateContent"]}],
        })

    discovery_transport(respond)
    prefix = "claude" if name == "anthropic" else "gemini"
    assert asyncio.run(ProviderService(config).discover_models(name)) == [prefix + "-first", prefix + "-second"]
    assert len(requests) == 2


@pytest.mark.parametrize("status,diagnostic", [(401, "Authentication"), (429, "rate limit"), (503, "server error")])
def test_discovery_errors_do_not_expose_response_or_credentials(
    config, discovery_transport, status, diagnostic,
):
    config.providers.openai.api_base = "https://example.test/private-secret?key=url-secret"
    discovery_transport(lambda request: httpx.Response(status, text="remote-secret api key sk-secret"))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ProviderService(config).discover_models("openai"))
    assert diagnostic in str(caught.value)
    assert "secret" not in str(caught.value)


def test_discovery_timeout_is_safe(config, discovery_transport):
    def respond(request):
        raise httpx.ReadTimeout("private-secret", request=request)

    discovery_transport(respond)
    with pytest.raises(ProviderError, match="Network") as caught:
        asyncio.run(ProviderService(config).discover_models("openai"))
    assert "private-secret" not in str(caught.value)


def test_wizard_discovers_adds_models_and_reuses_them(config_path, monkeypatch, discovery_transport):
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    answers = iter(["new-secret", "https://new.example.test/v1", "{}", "Y"])
    monkeypatch.setattr("PhyAgentOS.cli.providers._prompt", lambda *args, **kwargs: next(answers))
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == "https://new.example.test/v1/models"
        assert request.headers["authorization"] == "Bearer new-secret"
        return httpx.Response(200, json={"data": [{"id": m} for m in ["gpt-5", "gpt-4o", "gpt-4o-mini"]]})

    discovery_transport(respond)
    selections = []

    def select(models, *, selected=None, multiple=False):
        selections.append(models)
        if multiple:
            assert models == ["gpt-5", "gpt-4o", "gpt-4o-mini"]
            return ["gpt-5", "gpt-4o"]
        assert models == ["gpt-5", "gpt-4o"]
        return ["gpt-4o"]

    monkeypatch.setattr("PhyAgentOS.cli.providers._select_models", select)
    runner = CliRunner()
    result = runner.invoke(app, ["provider", "configure", "openai", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    cfg = load_config(config_path)
    assert cfg.providers.openai.models == ["gpt-5", "gpt-4o"]
    assert cfg.providers.openai.default_model == "gpt-4o"
    assert "new-secret" not in result.output
    result = runner.invoke(app, ["provider", "use", "openai", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert load_config(config_path).agents.defaults.model == "gpt-4o"
    assert len(selections) == 3
    assert len(requests) == 1  # Switching uses the saved list offline.


@pytest.mark.parametrize("discover", [False, True])
def test_wizard_manual_fallback(config_path, monkeypatch, discovery_transport, discover):
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    answers = iter(["new-secret", "https://new.example.test/v1", "{}", "Y" if discover else "n", "gpt-4o"])
    monkeypatch.setattr("PhyAgentOS.cli.providers._prompt", lambda *args, **kwargs: next(answers))

    def respond(request):
        assert discover, "Skipping the test must not make network requests"
        return httpx.Response(404, text="secret remote response")

    discovery_transport(respond)
    result = CliRunner().invoke(app, ["provider", "configure", "openai", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert load_config(config_path).providers.openai.models == ["gpt-4o"]
    assert "secret" not in result.output


def test_cancelling_model_picker_keeps_existing_file(config_path, monkeypatch):
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    answers = iter(["new-secret", "https://new.example.test/v1", "{}", "Y"])
    monkeypatch.setattr("PhyAgentOS.cli.providers._prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(ProviderService, "discover_models", AsyncMock(return_value=["gpt-5"]))

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("PhyAgentOS.cli.providers._select_models", cancel)
    before = config_path.read_bytes()
    result = CliRunner().invoke(app, ["provider", "configure", "openai", "-c", str(config_path)])
    assert result.exit_code == 1
    assert "Cancelled" in result.output
    assert config_path.read_bytes() == before


def test_saved_model_numbers_switch_only_the_current_session(config, monkeypatch):
    config.providers.openai.models = ["gpt-5", "gpt-4o"]
    monkeypatch.setattr(
        ProviderService, "create_provider", staticmethod(lambda _s, _c, model, _e: RecordingProvider(model)),
    )
    service = ProviderService(config)
    sessions = SessionRuntimes(service, service.resolve())
    listing = sessions.command("alice", "/model")
    assert "1. gpt-5" in listing and "2. gpt-4o" in listing
    assert "catalog" not in listing
    sessions.command("alice", "/model 2")
    assert sessions.get("alice").model == "gpt-4o"
    assert sessions.get("bob").model == "gpt-5"
    for value in ["0", "3"]:
        with pytest.raises(ProviderError, match="Invalid model number"):
            sessions.command("alice", "/model " + value)
        assert sessions.get("alice").model == "gpt-4o"


@pytest.mark.parametrize(
    "multiple,keys,expected",
    [(True, "\r \x1b[B \r", ["gpt-5", "gpt-4o"]), (False, "\x1b[B\r", ["gpt-4o"])],
)
def test_model_picker_real_keyboard(multiple, keys, expected):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from PhyAgentOS.cli.providers import _select_models

    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            assert _select_models(["gpt-5", "gpt-4o"], multiple=multiple) == expected


@pytest.mark.parametrize("keys", ["\x1b", "\x03"])
def test_model_picker_real_keyboard_cancel(keys):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from PhyAgentOS.cli.providers import _select_models

    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            with pytest.raises(KeyboardInterrupt):
                _select_models(["gpt-5"], multiple=True)


@pytest.mark.parametrize("command", ["show", "test", "use", "remove"])
def test_provider_commands_offer_picker_when_name_is_omitted(command, config_path, monkeypatch):
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    selections = []

    def select(service, *, action):
        selections.append(action)
        assert service.config.providers.custom.api_base == "https://custom.example.test/v1"
        return "custom"

    monkeypatch.setattr("PhyAgentOS.cli.providers._select_provider", select)
    monkeypatch.setattr("PhyAgentOS.cli.providers._select_models", lambda models, **kwargs: [models[0]])
    test = AsyncMock(return_value="Connection successful.")
    monkeypatch.setattr(ProviderService, "test", test)
    result = CliRunner().invoke(app, ["provider", command, "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert selections == [command.title()]
    cfg = load_config(config_path)
    if command == "test":
        test.assert_awaited_once_with("custom", None)
    elif command == "use":
        assert cfg.agents.defaults.provider == "custom"
        assert cfg.agents.defaults.model == "gpt-5.2"
    elif command == "remove":
        assert cfg.providers.custom.api_base is None
        assert cfg.providers.openai.api_key
    else:
        assert "https://custom.example.test" in result.output


@pytest.mark.parametrize("command", ["show", "test", "use", "remove", "login"])
def test_provider_commands_without_tty_require_name(command, config_path, monkeypatch):
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: False)
    monkeypatch.setattr(
        "PhyAgentOS.cli.providers._select_provider", lambda *args, **kwargs: pytest.fail("picker opened"),
    )
    before = config_path.read_bytes()
    args = ["provider", command]
    if command != "login":
        args.extend(["-c", str(config_path)])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "required outside a TTY" in result.output
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("command", ["show", "test", "use", "remove"])
def test_provider_command_picker_cancel_has_no_side_effects(command, config_path, monkeypatch):
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("PhyAgentOS.cli.providers._select_provider", cancel)
    test = AsyncMock()
    monkeypatch.setattr(ProviderService, "test", test)
    before = config_path.read_bytes()
    result = CliRunner().invoke(app, ["provider", command, "-c", str(config_path)])
    assert result.exit_code == 1
    assert "Cancelled" in result.output
    assert config_path.read_bytes() == before
    test.assert_not_awaited()


@pytest.mark.parametrize("argument", [None, "OpenAI-Codex", "OPENAI_CODEX"])
def test_login_accepts_picker_and_case_insensitive_names(argument, monkeypatch):
    from unittest.mock import Mock

    from PhyAgentOS.cli.commands import _LOGIN_HANDLERS

    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    selected = Mock(return_value="openai_codex")
    monkeypatch.setattr("PhyAgentOS.cli.providers._select_provider", selected)
    handler = Mock()
    monkeypatch.setitem(_LOGIN_HANDLERS, "openai_codex", handler)
    result = CliRunner().invoke(app, ["provider", "login", *([argument] if argument else [])])
    assert result.exit_code == 0, result.output
    handler.assert_called_once_with()
    assert selected.call_count == (0 if argument else 1)
    if argument is None:
        assert selected.call_args.kwargs == {"action": "Login"}


@pytest.mark.parametrize("action", ["Test", "Use", "Show", "Remove", "Login"])
def test_provider_picker_filters_rows_for_action(config, monkeypatch, action):
    from PhyAgentOS.cli.providers import _select_provider

    config.providers.deepseek.default_model = "deepseek-chat"  # Stored but missing credentials.
    rendered = []

    class FakeApplication:
        def __init__(self, **kwargs):
            window = kwargs["layout"].container
            rendered.append(window.content.text()[0][1])

        def run(self):
            return "selected"

    monkeypatch.setattr("prompt_toolkit.application.Application", FakeApplication)
    assert _select_provider(ProviderService(config), action=action) == "selected"
    text = rendered[0]
    assert f"Enter {action}" in text
    if action == "Login":
        assert "OpenAI Codex" in text and "Github Copilot" in text
        assert "Custom" not in text and "DeepSeek" not in text
    else:
        assert "Custom" in text and "Anthropic" in text
        assert "OpenAI Codex" not in text
        assert ("DeepSeek" in text) == (action in {"Show", "Remove"})


def test_provider_picker_handles_empty_configuration(monkeypatch):
    from PhyAgentOS.cli.providers import _select_provider

    service = ProviderService(Config())
    monkeypatch.setattr(service, "list", lambda: [])
    with pytest.raises(ProviderError, match="No providers available"):
        _select_provider(service, action="Test")


@pytest.mark.parametrize("keys,cancelled", [("\x1b[B\r", False), ("\x1b", True), ("\x03", True)])
def test_bare_model_opens_async_picker_and_switches_provider(
    config, config_path, monkeypatch, keys, cancelled,
):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from PhyAgentOS.cli.providers import _interactive_model_command

    config.providers.custom.default_model = "gpt-5"  # Same model ID at a different endpoint.
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    monkeypatch.setattr(
        ProviderService, "create_provider", staticmethod(lambda _s, _c, model, _e: RecordingProvider(model)),
    )
    service = ProviderService(config)
    startup = service.resolve()
    sessions = SessionRuntimes(service, startup)
    before = config_path.read_bytes()

    async def scenario():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                pipe.send_text(keys)
                return await _interactive_model_command(" /MODEL ", sessions, "cli:alice")

    response = asyncio.run(scenario())
    if cancelled:
        assert "cancelled" in response
        assert sessions.get("cli:alice") is startup
    else:
        assert "provider=custom" in response
        current = sessions.get("cli:alice")
        assert current.name == "custom" and current.model == "gpt-5"
        assert current.endpoint == "https://custom.example.test/v1"
        assert sessions.get("cli:bob") is startup
    assert config_path.read_bytes() == before


def test_model_picker_failed_selection_preserves_runtime(config, monkeypatch):
    from PhyAgentOS.cli.providers import _interactive_model_command

    config.providers.openai.models = ["gpt-5", "gpt-4o"]
    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    monkeypatch.setattr(
        ProviderService, "create_provider", staticmethod(lambda _s, _c, model, _e: RecordingProvider(model)),
    )
    service = ProviderService(config)
    startup = service.resolve(effort="high")
    sessions = SessionRuntimes(service, startup)

    class Picker:
        async def run_async(self):
            return ["OpenAI / gpt-4o"]

    monkeypatch.setattr("PhyAgentOS.cli.providers._model_picker", lambda *args, **kwargs: Picker())
    response = asyncio.run(_interactive_model_command("/model", sessions, "cli:alice"))
    assert "not supported" in response
    assert sessions.get("cli:alice") is startup


@pytest.mark.parametrize("command,tty", [("/model list", True), ("/model gpt-4o", True), ("/model", False)])
def test_explicit_model_commands_and_non_tty_keep_text_flow(config, monkeypatch, command, tty):
    from PhyAgentOS.cli.providers import _interactive_model_command

    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: tty)
    monkeypatch.setattr(
        "PhyAgentOS.cli.providers._model_picker", lambda *args, **kwargs: pytest.fail("picker opened"),
    )
    service = ProviderService(config)
    sessions = SessionRuntimes(service, service.resolve())
    assert asyncio.run(_interactive_model_command(command, sessions, "cli:alice")) is None


def test_chat_after_model_picker_uses_selected_provider(config, tmp_path, monkeypatch):
    from PhyAgentOS.agent.loop import AgentLoop
    from PhyAgentOS.bus.queue import MessageBus
    from PhyAgentOS.cli.providers import _interactive_model_command

    monkeypatch.setattr("PhyAgentOS.cli.providers._interactive", lambda: True)
    monkeypatch.setattr(
        ProviderService, "create_provider", staticmethod(lambda _s, _c, model, _e: RecordingProvider(model)),
    )

    class Picker:
        async def run_async(self):
            return ["Custom / gpt-5.2"]

    monkeypatch.setattr("PhyAgentOS.cli.providers._model_picker", lambda *args, **kwargs: Picker())

    async def scenario():
        original = RecordingProvider()
        agent = AgentLoop(MessageBus(), original, tmp_path, provider_config=config)
        agent.memory_consolidator.maybe_consolidate_by_tokens = AsyncMock()
        try:
            result = await _interactive_model_command("/model", agent.session_runtimes, "cli:alice")
            assert "provider=custom" in result
            selected = agent.session_runtimes.get("cli:alice")
            assert await agent.process_direct("hello", "cli:alice") == "done"
            assert selected.provider.calls[0]["model"] == "gpt-5.2"
            assert original.calls == []
            assert await agent.process_direct("hello", "cli:bob") == "done"
            assert original.calls[0]["model"] == "gpt-5"
        finally:
            agent.stop()
            await agent.close_mcp()

    asyncio.run(scenario())


@pytest.mark.parametrize("default_provider", ["ollama", "auto"])
def test_remove_ollama_persists_empty_settings_and_clears_default(
    config, config_path, monkeypatch, default_provider,
):
    from PhyAgentOS.cli.providers import _select_provider
    from PhyAgentOS.config.schema import ProviderConfig

    config.agents.defaults.provider = default_provider
    config.agents.defaults.model = "ollama/qwen3:8b"
    config.agents.defaults.reasoning_effort = "high"
    config.providers.ollama = ProviderConfig(
        api_key="old-local-secret", api_base="http://old-local.example.test:11434",
        extra_headers={"X-Account": "old-account"}, default_model="qwen3:8b",
        models=["qwen3:8b", "llama3.2"],
    )
    save_config(config, config_path)
    other = config.providers.openai.model_dump()
    runner = CliRunner()
    result = runner.invoke(app, ["provider", "remove", "ollama", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    saved = json.loads(config_path.read_text())
    assert "ollama" in saved["providers"]
    assert saved["providers"]["ollama"] == ProviderConfig(enabled=False).model_dump(by_alias=True)
    assert saved["agents"]["defaults"]["provider"] == ""
    assert saved["agents"]["defaults"]["model"] == ""
    assert saved["agents"]["defaults"]["reasoningEffort"] is None
    assert load_config(config_path).providers.openai.model_dump() == other
    assert config_path.stat().st_mode & 0o777 == 0o600

    # Independent loads/commands must see removal, even with ambient credentials.
    monkeypatch.setenv("OLLAMA_API_KEY", "ambient-local-secret")
    fresh = ProviderService.load(config_path)
    assert not fresh.configured(provider_spec("ollama"))
    assert fresh.models("ollama") == []
    assert fresh.config.get_provider_name() is None
    listing = runner.invoke(app, ["provider", "list", "--json", "-c", str(config_path)])
    row = next(row for row in json.loads(listing.stdout) if row["name"] == "ollama")
    assert not row["configured"] and not row["default"] and row["supported"]
    shown = runner.invoke(app, ["provider", "show", "ollama", "-c", str(config_path)])
    data = json.loads(shown.stdout)
    assert data["api_key"] == "not set" and data["endpoint"] == "not set"
    assert data["model"] is None and data["models"] == [] and data["extra_headers"] == {}

    before = config_path.read_bytes()
    used = runner.invoke(app, ["provider", "use", "ollama", "-c", str(config_path)])
    assert used.exit_code == 1 and "not configured" in used.output
    assert config_path.read_bytes() == before
    with pytest.raises(ProviderError, match="No configured provider"):
        fresh.selection()

    screens = []

    class Application:
        def __init__(self, **kwargs):
            screens.append(kwargs["layout"].container.content.text()[0][1])

        def run(self):
            return "unused"

    monkeypatch.setattr("prompt_toolkit.application.Application", Application)
    _select_provider(fresh, action="Configure")
    assert "Ollama  not configured" in screens[-1]
    for action in ["Use", "Test", "Show", "Remove"]:
        _select_provider(fresh, action=action)
        assert "Ollama" not in screens[-1]

    # Configure again starts clean and explicitly re-enables the provider.
    result = runner.invoke(app, [
        "provider", "configure", "ollama", "--model", "new-model",
        "--api-base", "http://new-local.example.test:11434", "-c", str(config_path),
    ])
    assert result.exit_code == 0, result.output
    restored = load_config(config_path).providers.ollama
    assert restored.enabled and restored.api_key == "" and restored.extra_headers is None
    assert restored.default_model == "new-model" and restored.models == ["new-model"]
    result = runner.invoke(app, ["provider", "use", "ollama", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert load_config(config_path).agents.defaults.model == "new-model"


def test_empty_local_provider_is_not_configured_or_auto_matched(config):
    config.agents.defaults.provider = "auto"
    config.agents.defaults.model = "ollama/qwen3"
    for name in ["ollama", "vllm"]:
        service = ProviderService(config)
        assert not service.configured(provider_spec(name))
        assert config.get_provider_name() != name
    config.providers.ollama.default_model = "qwen3"
    service = ProviderService(config)
    assert service.configured(provider_spec("ollama"))
    assert service.selection("ollama").endpoint == "http://localhost:11434"


@pytest.mark.parametrize("name", ["openai", "moonshot", "openai_codex"])
def test_removed_provider_does_not_return_from_environment_or_oauth(
    config, config_path, monkeypatch, name,
):
    config.agents.defaults.provider = "custom"
    save_config(config, config_path)
    before_defaults = config.agents.defaults.model_dump()
    monkeypatch.setenv("PAOS_OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("MOONSHOT_API_KEY", "ambient-secret")
    monkeypatch.setenv("MOONSHOT_API_BASE", "https://ambient.example.test/v1")
    monkeypatch.setattr("PhyAgentOS.providers.service.oauth_configured", lambda _: True)
    ProviderService.remove(config_path, name)
    fresh = ProviderService.load(config_path)
    assert not fresh.configured(provider_spec(name))
    assert getattr(fresh.config.providers, name).api_key == ""
    assert getattr(fresh.config.providers, name).api_base is None
    assert fresh.config.agents.defaults.model_dump() == before_defaults
    # Auto-routing and explicit configuration lookups also honor removal.
    fresh.config.agents.defaults.provider = name
    assert fresh.config.get_provider() is None
    assert fresh.config.get_api_base() is None
    fresh.config.agents.defaults.provider = "auto"
    assert fresh.config.get_provider_name(name + "/gpt-5") != name
    with pytest.raises(ProviderError, match="not configured"):
        fresh.selection(name, "gpt-5")
    assert os.environ["PAOS_OPENAI_API_KEY"] == "ambient-secret"


def test_remove_save_failure_preserves_provider_and_default(config, config_path, monkeypatch):
    before = config_path.read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("PhyAgentOS.providers.service.save_config", fail)
    result = CliRunner().invoke(app, ["provider", "remove", "openai", "-c", str(config_path)])
    assert result.exit_code == 1
    assert config_path.read_bytes() == before

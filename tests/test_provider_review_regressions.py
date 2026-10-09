"""Regression coverage for persistent selection and AWS credential-chain routing."""

import json

import pytest
from typer.testing import CliRunner

from PhyAgentOS.cli.commands import (
    _apply_startup_overrides,
    _make_evolution_provider,
    _make_forge_verifier,
    _make_provider,
    app,
)
from PhyAgentOS.config.loader import load_config, save_config
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.discovery import ModelDiscoveryUnavailableError
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.service import ProviderError, ProviderService, SessionRuntimes

BEDROCK_MODEL = "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr("PhyAgentOS.providers.service.oauth_configured", lambda _name: False)
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.agents.defaults.provider = "openai"
    cfg.agents.defaults.model = "gpt-5"
    cfg.agents.defaults.reasoning_effort = "high"
    cfg.providers.openai.api_key = "test-openai-key"
    cfg.providers.anthropic.api_key = "test-anthropic-key"
    cfg.providers.deepseek.api_key = "test-deepseek-key"
    cfg.providers.deepseek.default_model = "deepseek-chat"
    return cfg


@pytest.mark.parametrize("provider,model,effort,expected", [
    ("deepseek", "deepseek-chat", "none", None),
    ("openai", "gpt-4o", "none", None),
    ("openai", "gpt-5", "low", "low"),
    ("openai", "gpt-5", None, "high"),
])
def test_provider_use_persists_validated_effort(config, tmp_path, provider, model, effort, expected):
    path = tmp_path / "config.json"
    save_config(config, path)
    args = ["provider", "use", provider, "--model", model, "-c", str(path)]
    if effort is not None:
        args.extend(["--reasoning-effort", effort])
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    saved = load_config(path)
    assert saved.agents.defaults.provider == provider
    assert saved.agents.defaults.model == model
    assert saved.agents.defaults.reasoning_effort == expected
    assert ProviderService(saved).resolve().effort == expected


@pytest.mark.parametrize("effort", [None, "high", "invalid"])
def test_provider_use_invalid_effort_keeps_file_unchanged(config, tmp_path, effort):
    path = tmp_path / "config.json"
    save_config(config, path)
    before = path.read_bytes()
    args = ["provider", "use", "deepseek", "-c", str(path)]
    if effort is not None:
        args.extend(["--reasoning-effort", effort])
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert "Reasoning effort" in result.output
    assert path.read_bytes() == before


@pytest.mark.parametrize("provider", ["auto", "bedrock"])
def test_bedrock_startup_and_background_use_aws_credentials(config, monkeypatch, provider):
    from PhyAgentOS.verification.service import _provider

    config.agents.defaults.provider = provider
    config.agents.defaults.model = BEDROCK_MODEL
    config.agents.defaults.reasoning_effort = None
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-aws-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-aws-secret")
    changed = _apply_startup_overrides(config, None, None, None)
    main = _make_provider(changed)

    assert changed.agents.defaults.provider == "bedrock"
    assert isinstance(main, LiteLLMProvider)
    assert main._spec.name == "bedrock"
    assert main._resolve_model(BEDROCK_MODEL) == BEDROCK_MODEL
    assert main.api_key is None
    assert "test-aws-secret" not in changed.model_dump_json()

    evolution, model = _make_evolution_provider(changed, main)
    assert evolution is main
    assert model == BEDROCK_MODEL
    verifier = _make_forge_verifier(changed, main)
    child_spec = verifier.service.provider_spec
    assert child_spec["provider_name"] == "bedrock"
    assert child_spec["api_key"] == ""
    assert "test-aws-secret" not in json.dumps(child_spec)
    child = _provider(child_spec, 30)
    assert isinstance(child, LiteLLMProvider)
    assert child._resolve_model(child.default_model) == BEDROCK_MODEL
    assert child.api_key is None


def test_bedrock_inference_profile_and_session_switch(config):
    service = ProviderService(config)
    sessions = SessionRuntimes(service, service.resolve())
    sessions.command("alice", "/effort none")
    sessions.command("alice", f"/model {BEDROCK_MODEL}")
    assert sessions.get("alice").name == "bedrock"
    assert sessions.get("bob").name == "openai"

    profile = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/test-profile"
    selected = service.resolve("bedrock", profile, "none")
    assert selected.provider._resolve_model(profile) == f"bedrock/{profile}"
    with pytest.raises(ProviderError, match="No configured provider"):
        sessions.command("bob", "/model unknown-model")


def test_bedrock_can_be_removed_and_reconfigured_without_api_key(config, tmp_path):
    path = tmp_path / "config.json"
    config.agents.defaults.reasoning_effort = None
    save_config(config, path)
    ProviderService.use(path, "bedrock", BEDROCK_MODEL)
    ProviderService.remove(path, "bedrock")
    with pytest.raises(ProviderError, match="not configured"):
        ProviderService.load(path).resolve("bedrock", BEDROCK_MODEL)

    result = CliRunner().invoke(
        app, ["provider", "configure", "bedrock", "--model", BEDROCK_MODEL, "-c", str(path)],
    )
    assert result.exit_code == 0, result.output
    ProviderService.use(path, "bedrock", None)
    assert load_config(path).agents.defaults.model == BEDROCK_MODEL
    assert load_config(path).providers.bedrock.api_key == ""


async def test_bedrock_discovery_falls_back_to_manual_model(config, monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient", lambda **_kwargs: pytest.fail("Bedrock must not use OpenAI model discovery"),
    )
    config.providers.bedrock.api_base = "https://bedrock-runtime.us-east-1.amazonaws.com"
    with pytest.raises(ModelDiscoveryUnavailableError):
        await ProviderService(config).discover_models("bedrock")

"""Command registration and dependency-direction checks for the CLI package."""

import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from PhyAgentOS.cli.command_groups import agent_commands as agent_commands_module
from PhyAgentOS.cli.command_groups.agent_commands import register_agent
from PhyAgentOS.cli.commands import app

REPO_ROOT = Path(__file__).parents[1]


def test_root_help_lists_every_registered_command_group() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("agent", "provider", "gateway", "skill", "forge-node", "channels", "status"):
        assert command in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["agent", "--help"],
        ["provider", "--help"],
        ["skill", "--help"],
        ["forge-node", "--help"],
        ["channels", "--help"],
    ],
)
def test_command_help_does_not_run_provider_or_install_flows(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output


def test_cli_submodules_do_not_import_the_top_level_commands_module() -> None:
    code = "\n".join(
        [
            "import sys",
            "import PhyAgentOS.cli.command_groups.agent_commands",
            "import PhyAgentOS.cli.command_groups.provider_commands",
            "import PhyAgentOS.cli.command_groups.gateway_commands",
            "import PhyAgentOS.cli.command_groups.skill_commands",
            "import PhyAgentOS.cli.command_groups.forge_node_commands",
            "import PhyAgentOS.cli.command_groups.channel_commands",
            "import PhyAgentOS.cli.command_groups.system_commands",
            "import PhyAgentOS.cli.configuration",
            "import PhyAgentOS.cli.interaction.agent_output",
            "import PhyAgentOS.cli.interaction.interactive_session",
            "import PhyAgentOS.cli.interaction.terminal_input",
            "import PhyAgentOS.cli.interaction.model_picker",
            "import PhyAgentOS.cli.legacy.legacy_agent",
            "assert 'PhyAgentOS.cli.commands' not in sys.modules",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _fresh_app(legacy_runner=None):
    fresh = typer.Typer()
    register_agent(fresh, legacy_runner=legacy_runner)
    return fresh


def _recording_core(monkeypatch):
    calls: list[dict] = []

    async def run(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(agent_commands_module, "run_core_agent", run)
    return calls


def _legacy_runner(calls: list[dict]):
    def run(**kwargs):
        calls.append(kwargs)
        return None

    return run


def test_default_and_explicit_core_route_to_core(monkeypatch) -> None:
    core_calls = _recording_core(monkeypatch)
    legacy_calls: list[dict] = []
    runner = CliRunner()

    default = runner.invoke(_fresh_app(_legacy_runner(legacy_calls)), ["-m", "hello"])
    explicit = runner.invoke(
        _fresh_app(_legacy_runner(legacy_calls)),
        ["--runtime", "core", "-m", "hello"],
    )

    assert default.exit_code == 0, default.output
    assert explicit.exit_code == 0, explicit.output
    assert len(core_calls) == 2
    assert legacy_calls == []


def test_real_app_default_routes_to_core(monkeypatch) -> None:
    core_calls = _recording_core(monkeypatch)

    result = CliRunner().invoke(app, ["agent", "-m", "hello"])

    assert result.exit_code == 0, result.output
    assert len(core_calls) == 1


def test_legacy_runtime_uses_the_injected_runner(monkeypatch) -> None:
    core_calls = _recording_core(monkeypatch)
    legacy_calls: list[dict] = []

    result = CliRunner().invoke(
        _fresh_app(_legacy_runner(legacy_calls)),
        ["--runtime", "legacy", "-m", "hello"],
    )

    assert result.exit_code == 0, result.output
    assert len(legacy_calls) == 1
    assert core_calls == []


def test_invalid_runtime_runs_neither_path(monkeypatch) -> None:
    core_calls = _recording_core(monkeypatch)
    legacy_calls: list[dict] = []

    result = CliRunner().invoke(
        _fresh_app(_legacy_runner(legacy_calls)), ["--runtime", "fast"]
    )

    assert result.exit_code != 0
    assert core_calls == []
    assert legacy_calls == []


def test_core_failure_does_not_fall_back_to_legacy(monkeypatch) -> None:
    async def failing(**kwargs):
        del kwargs
        raise ValueError("core failed")

    monkeypatch.setattr(agent_commands_module, "run_core_agent", failing)
    legacy_calls: list[dict] = []

    result = CliRunner().invoke(
        _fresh_app(_legacy_runner(legacy_calls)), ["-m", "hello"]
    )

    assert result.exit_code == 1
    assert "core failed" in result.output
    assert legacy_calls == []


def test_explicit_legacy_resolves_the_runner_lazily(monkeypatch) -> None:
    from PhyAgentOS.cli.legacy import legacy_agent

    calls: list[dict] = []
    monkeypatch.setattr(legacy_agent, "agent", _legacy_runner(calls))

    result = CliRunner().invoke(
        _fresh_app(), ["--runtime", "legacy", "-m", "hello"]
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_agent_help_states_core_is_the_default() -> None:
    result = CliRunner().invoke(app, ["agent", "--help"])

    assert result.exit_code == 0, result.output
    assert "core (default)" in result.output
    assert "or legacy." in result.output


def test_real_core_default_and_explicit_selection_never_import_legacy(tmp_path) -> None:
    probe = REPO_ROOT / "tests" / "cli_core_import_probe.py"
    completed = subprocess.run(
        [sys.executable, str(probe), str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "probe-ok" in completed.stdout


def test_legacy_branch_supplies_the_default_session_id_for_a_recording_runner(
    monkeypatch,
) -> None:
    core_calls = _recording_core(monkeypatch)
    legacy_calls: list[dict] = []
    runner = CliRunner()

    default = runner.invoke(
        _fresh_app(_legacy_runner(legacy_calls)), ["--runtime", "legacy", "-m", "hello"]
    )
    explicit = runner.invoke(
        _fresh_app(_legacy_runner(legacy_calls)),
        ["--runtime", "legacy", "-m", "hello", "--session", "cli:chosen"],
    )
    empty = runner.invoke(
        _fresh_app(_legacy_runner(legacy_calls)),
        ["--runtime", "legacy", "-m", "hello", "--session", ""],
    )

    assert default.exit_code == 0, default.output
    assert explicit.exit_code == 0, explicit.output
    assert empty.exit_code == 0, empty.output
    assert legacy_calls[0]["session_id"] == "cli:direct"
    assert legacy_calls[1]["session_id"] == "cli:chosen"
    # An explicitly empty id is not silently replaced by the default.
    assert legacy_calls[2]["session_id"] == ""
    assert core_calls == []

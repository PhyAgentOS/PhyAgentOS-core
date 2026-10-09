import sys
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[1]))

from PhyAgentOS.cli.commands import app  # noqa: E402


def test_skill_command_exposes_runtime_lifecycle_commands() -> None:
    result = CliRunner().invoke(app, ["skill", "--help"])

    assert result.exit_code == 0
    for command in (
        "list",
        "inspect",
        "start",
        "status",
        "logs",
        "stop",
        "search",
        "install",
        "update",
        "remove",
    ):
        assert command in result.stdout


def test_forge_node_command_exposes_distribution_lifecycle() -> None:
    result = CliRunner().invoke(app, ["forge-node", "--help"])

    assert result.exit_code == 0
    for command in ("install", "verify"):
        assert command in result.stdout


@pytest.mark.parametrize("force_color", [None, "1"])
def test_skill_distribution_commands_accept_static_index(force_color: str | None) -> None:
    runner = CliRunner(env={"FORCE_COLOR": force_color})
    for command in ("search", "install", "update"):
        result = runner.invoke(app, ["skill", command, "--help"])
        assert result.exit_code == 0
        assert "--index" in unstyle(result.stdout)

"""Command registration and dependency-direction checks for the CLI package."""

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

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

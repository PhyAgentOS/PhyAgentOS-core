"""CLI entry point: build the Typer app and register every command group."""

import os
import sys

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from rich.console import Console

from PhyAgentOS import __logo__, __version__

from .command_groups.agent_commands import register_agent
from .command_groups.channel_commands import register_channel_commands
from .command_groups.forge_node_commands import register_forge_node_commands
from .command_groups.gateway_commands import register_gateway_commands
from .command_groups.provider_commands import register_provider_commands
from .command_groups.skill_commands import register_skill_commands
from .command_groups.system_commands import register_system_commands
from .legacy import legacy_agent

app = typer.Typer(
    name="paos",
    help=f"{__logo__} PhyAgentOS - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} PhyAgentOS v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """PhyAgentOS - Personal AI Assistant."""
    pass


register_system_commands(app)
register_gateway_commands(app)
# The legacy runtime is still the CLI default. Importing it here is an explicit
# transitional dependency of that default, not a claim that importing this
# module keeps legacy code out of the process.
register_agent(app, legacy_runner=legacy_agent.agent)
register_skill_commands(app)
register_forge_node_commands(app)
register_channel_commands(app)
register_provider_commands(app)


if __name__ == "__main__":
    app()

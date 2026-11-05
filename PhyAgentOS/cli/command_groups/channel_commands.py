"""Channel management commands."""

import typer
from rich.console import Console
from rich.table import Table

console = Console()

channels_app = typer.Typer(help="Manage channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from PhyAgentOS.channels.registry import discover_channel_names, load_channel_class
    from PhyAgentOS.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")

    for modname in sorted(discover_channel_names()):
        section = getattr(config.channels, modname, None)
        enabled = section and getattr(section, "enabled", False)
        try:
            cls = load_channel_class(modname)
            display = cls.display_name
        except ImportError:
            display = modname.title()
        table.add_row(
            display,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


def register_channel_commands(app) -> None:
    app.add_typer(channels_app, name="channels")

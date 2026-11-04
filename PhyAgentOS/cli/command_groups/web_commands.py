"""Local WebUI command: serve the single settings page.

The page has no login flow, so this command only listens on loopback. There is
no remote mode to configure.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from PhyAgentOS import __logo__

console = Console()

DEFAULT_PORT = 8787


def register_web_commands(app) -> None:
    @app.command()
    def web(
        config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
        host: str = typer.Option(
            "127.0.0.1", "--host", help="Loopback listen address (localhost or 127.0.0.1)"
        ),
        port: int = typer.Option(
            DEFAULT_PORT, "--port", "-p", min=1, max=65535, help="Listen port"
        ),
        allow_skill_actions: bool = typer.Option(
            False,
            "--allow-skill-actions",
            help="Enable in-page Skill install/update/remove (off by default)",
        ),
    ):
        """Start the local settings WebUI."""
        from PhyAgentOS.webui.app import create_app, is_loopback_host

        if not is_loopback_host(host):
            console.print(
                "Refusing to listen on a non-loopback address; "
                "the settings WebUI is local-only.",
                style="red",
                markup=False,
            )
            raise typer.Exit(2)

        config_path = Path(config).expanduser().resolve() if config else None
        import uvicorn

        console.print(f"{__logo__} Settings UI: [cyan]http://{host}:{port}[/cyan]")
        if config_path is not None:
            console.print(f"[dim]Using config: {config_path}[/dim]")
        if allow_skill_actions:
            console.print(
                "[yellow]In-page Skill install/update/remove is ENABLED "
                "(--allow-skill-actions).[/yellow]"
            )
        try:
            uvicorn.run(
                create_app(config_path, allow_skill_actions=allow_skill_actions),
                host=host,
                port=port,
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")

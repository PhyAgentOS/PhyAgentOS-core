"""Onboard and status commands."""

import typer
from rich.console import Console

from PhyAgentOS import __logo__
from PhyAgentOS.config.paths import get_workspace_path
from PhyAgentOS.utils.helpers import sync_workspace_templates

console = Console()


def register_system_commands(app) -> None:
    @app.command()
    def onboard():
        """Initialize PhyAgentOS configuration and workspace."""
        from PhyAgentOS.config.loader import get_config_path, load_config, save_config
        from PhyAgentOS.config.schema import Config

        config_path = get_config_path()

        if config_path.exists():
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
            console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
            if typer.confirm("Overwrite?"):
                config = Config()
                save_config(config)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                config = load_config()
                save_config(config)
                console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
        else:
            config = Config()
            save_config(config)
            console.print(f"[green]✓[/green] Created config at {config_path}")

        console.print("[dim]Config template now uses `maxTokens` + `contextWindowTokens`; `memoryWindow` is no longer a runtime setting.[/dim]")

        from PhyAgentOS.embodiment_registry import EmbodimentRegistry

        registry = EmbodimentRegistry(config)
        workspace = get_workspace_path()
        if registry.is_fleet:
            shared_workspace = registry.resolve_agent_workspace()
            if not shared_workspace.exists():
                shared_workspace.mkdir(parents=True, exist_ok=True)
                console.print(f"[green]✓[/green] Created shared workspace at {shared_workspace}")
            registry.sync_layout()
            for instance in registry.instances(enabled_only=True):
                if instance.workspace.exists():
                    console.print(f"[green]✓[/green] Ready robot workspace {instance.robot_id} at {instance.workspace}")
            workspace = shared_workspace
        else:
            if not workspace.exists():
                workspace.mkdir(parents=True, exist_ok=True)
                console.print(f"[green]✓[/green] Created workspace at {workspace}")
            sync_workspace_templates(workspace)

        console.print(f"\n{__logo__} PhyAgentOS is ready!")
        console.print("\nNext steps:")
        console.print("  1. Configure a provider: [cyan]paos provider configure[/cyan]")
        console.print("     Set the default: [cyan]paos provider use <provider>[/cyan]")
        console.print("     Get one at: https://openrouter.ai/keys")
        console.print("  2. Chat: [cyan]paos agent -m \"Hello!\"[/cyan]")
        console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/PhyAgentOS#-chat-apps[/dim]")

    @app.command()
    def status():
        """Show PhyAgentOS status."""
        from PhyAgentOS.config.loader import get_config_path, load_config

        config_path = get_config_path()
        config = load_config()
        workspace = config.workspace_path

        console.print(f"{__logo__} PhyAgentOS Status\n")

        console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
        console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

        if config_path.exists():
            from PhyAgentOS.providers.registry import PROVIDERS

            console.print(f"Model: {config.agents.defaults.model}")

            # Check API keys from registry
            for spec in PROVIDERS:
                p = getattr(config.providers, spec.name, None)
                if p is None:
                    continue
                if spec.is_oauth:
                    console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
                elif spec.is_local:
                    # Local deployments show api_base instead of api_key
                    if p.api_base:
                        console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                    else:
                        console.print(f"{spec.label}: [dim]not set[/dim]")
                else:
                    has_key = bool(p.api_key)
                    console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")

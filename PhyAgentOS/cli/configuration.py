"""Configuration loading and startup provider selection for CLI processes.

These helpers are shared by the gateway command group and the legacy Agent CLI.
They only apply per-process overrides on a copy; persisted configuration is
never changed here.
"""

from pathlib import Path

import typer
from rich.console import Console

from PhyAgentOS.config.schema import Config

console = Console()


def make_provider(
    config: Config,
    model_override: str | None = None,
    provider_name_override: str | None = None,
):
    """Create providers through the same resolver used by session commands."""
    from PhyAgentOS.providers.service import ProviderError, ProviderService

    try:
        return ProviderService(config).resolve(provider_name_override, model_override).provider
    except ProviderError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from None


def apply_startup_overrides(
    config: Config,
    provider: str | None,
    model: str | None,
    reasoning_effort: str | None,
) -> Config:
    """Validate startup overrides on a copy; never persist process settings."""
    from PhyAgentOS.providers.service import ProviderError, ProviderService

    try:
        service = ProviderService(config)
        runtime = service.selection(provider, model, reasoning_effort)
    except ProviderError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from None
    result = service.config.model_copy(deep=True)
    result.agents.defaults.provider = runtime.name
    result.agents.defaults.model = runtime.model
    result.agents.defaults.reasoning_effort = runtime.effort
    return result


def make_evolution_provider(config: Config, default_provider):
    """Resolve the optional evolution model/provider independently of verifier budget."""
    from PhyAgentOS.providers.service import ProviderService

    settings = config.agents.evolution
    if not settings.enabled:
        return default_provider, config.agents.defaults.model
    verification = config.agents.verification
    background_model = settings.model or verification.model
    model = background_model or config.agents.defaults.model
    service = ProviderService(config)
    provider_name = service.background_provider_name(
        settings.provider or verification.provider, background_model,
    )
    default_name = config.get_provider_name()
    if provider_name == default_name and model == config.agents.defaults.model:
        return default_provider, model
    if not provider_name:
        console.print(
            f"[yellow]Evolution provider could not be resolved for {model!r}; "
            "falling back to the Agent provider.[/yellow]"
        )
        return default_provider, config.agents.defaults.model
    try:
        runtime = service.resolve(
            provider_name, model, service.background_effort(provider_name, model) or "none",
        )
        return runtime.provider, model
    except Exception as exc:
        console.print(
            "[yellow]Evolution provider initialization failed; falling back to the Agent "
            f"provider ({type(exc).__name__}).[/yellow]"
        )
        return default_provider, config.agents.defaults.model


def load_command_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from PhyAgentOS.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    if workspace:
        if loaded.is_fleet_mode:
            loaded.embodiments.shared_workspace = workspace
        else:
            loaded.agents.defaults.workspace = workspace
    return loaded


def print_deprecated_memory_window_notice(config: Config) -> None:
    """Warn when running with old memoryWindow-only config."""
    if config.agents.defaults.should_warn_deprecated_memory_window:
        console.print(
            "[yellow]Hint:[/yellow] Detected deprecated `memoryWindow` without "
            "`contextWindowTokens`. `memoryWindow` is ignored; run "
            "[cyan]paos onboard[/cyan] to refresh your config template."
        )

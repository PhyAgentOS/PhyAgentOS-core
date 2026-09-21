"""CLI commands for PhyAgentOS."""

import asyncio
import os
import select
import signal
import sys
from contextlib import contextmanager
from pathlib import Path

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
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

from PhyAgentOS import __logo__, __version__
from PhyAgentOS.config.paths import get_workspace_path
from PhyAgentOS.config.schema import Config
from PhyAgentOS.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="paos",
    help=f"{__logo__} PhyAgentOS - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}


@contextmanager
def _download_progress():
    tasks: dict[str, int] = {}
    with Progress(
        TextColumn("[cyan]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:

        def report(event, artifact, downloaded: int, total: int | None) -> None:
            label = artifact.name or artifact.version or "artifact"
            task_id = tasks.get(artifact.url)
            if event == "cached":
                progress.console.print(
                    f"[green]✓[/green] Using cached [cyan]{label}[/cyan] "
                    f"({downloaded} bytes)"
                )
                return
            if task_id is None:
                progress.console.print(
                    f"[bold]Downloading[/bold] [cyan]{label}[/cyan]\n"
                    f"[dim]{artifact.url}[/dim]"
                )
                task_id = progress.add_task(label, total=total, completed=downloaded)
                tasks[artifact.url] = task_id
            else:
                progress.update(task_id, total=total)
            if event in {"advance", "complete"}:
                progress.update(task_id, completed=downloaded)

        yield report

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from PhyAgentOS.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} PhyAgentOS[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



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


# ============================================================================
# Onboard / Setup
# ============================================================================


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


def _make_provider(
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


def _apply_startup_overrides(
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


def _make_evolution_provider(config: Config, default_provider):
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


def _make_forge_verifier(config: Config, provider):
    """Create the Forge task verifier and its serializable child provider config."""
    if not config.agents.verification.service_enabled:
        return None
    from PhyAgentOS.agent.session_verifier import ForgeTaskVerifier
    from PhyAgentOS.providers.service import ProviderService, provider_spec

    settings = config.agents.verification
    model = settings.model or config.agents.defaults.model
    service = ProviderService(config)
    provider_name = service.background_provider_name(settings.provider, settings.model)
    if not provider_name:
        raise RuntimeError(f"cannot resolve verification provider for model {model!r}")
    provider_name = provider_name.replace("-", "_")
    provider_config = getattr(service.config.providers, provider_name, None)
    if settings.provider is not None and provider_config is None:
        raise RuntimeError(f"unknown verification provider {settings.provider!r}")
    provider_meta = provider_spec(provider_name)
    child_provider_spec = {
        "provider_name": provider_name,
        "model": model,
        "api_key": provider_config.api_key if provider_config is not None else None,
        "api_base": (
            provider_config.api_base
            if provider_config is not None and provider_config.api_base
            else provider_meta.default_api_base or None
        ),
        "extra_headers": (
            provider_config.extra_headers if provider_config is not None else None
        ),
        "temperature": 0.0,
        "max_tokens": min(4096, config.agents.defaults.max_tokens),
        "reasoning_effort": service.background_effort(provider_name, model),
    }
    return ForgeTaskVerifier(
        workspace=config.workspace_path,
        provider=provider,
        model=model,
        evidence_retention=settings.evidence_retention,
        timeout_s=settings.timeout_s,
        service_host=settings.service_host,
        service_port=settings.service_port,
        service_provider_spec=child_provider_spec,
        max_calls=settings.max_verifier_calls_per_run,
        write_legacy_lessons=not config.agents.evolution.enabled,
    )


def _make_forge_components(config: Config, provider):
    """Build dynamic Agent components from the single managed Skill runtime."""
    from PhyAgentOS.forge.binding import ForgeSkillBindingResolver
    from PhyAgentOS.forge.task import AgentTaskCoordinator
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.integration import (
        ActiveRuntimeRegistry,
        DynamicForgeToolClient,
        DynamicRuntimeSet,
        discover_active_runtime,
    )
    from PhyAgentOS.skill_runtime.manager import RuntimeManager
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    catalog = SkillCatalog()
    state_store = RuntimeStateStore()
    manager = RuntimeManager(catalog=catalog, state_store=state_store)
    active_runtime = discover_active_runtime(
        catalog=catalog,
        state_store=state_store,
        manager=manager,
    )
    runtime_registry = ActiveRuntimeRegistry(
        active_runtime,
        catalog=catalog,
        state_store=state_store,
        auto_refresh=True,
    )
    client = DynamicForgeToolClient(runtime_registry)
    invocation_ids = DynamicRuntimeSet(runtime_registry, "invocation_ids")
    session_ids = DynamicRuntimeSet(runtime_registry, "session_ids")
    task_binding_ids = DynamicRuntimeSet(runtime_registry, "task_binding_ids")
    binding_resolver = ForgeSkillBindingResolver(runtime_registry)

    coordinator = AgentTaskCoordinator(
        workspace=config.workspace_path,
        config=config.forge,
        client=client,
        binding_resolver=binding_resolver,
        runtime_invocation_ids=invocation_ids,
        runtime_session_ids=session_ids,
        runtime_task_binding_ids=task_binding_ids,
        verifier=_make_forge_verifier(config, provider),
        max_replans=config.agents.verification.max_replans_per_episode,
        replan_timeout_s=config.agents.verification.replan_timeout_s,
    )
    return client, invocation_ids, coordinator, runtime_registry.is_available


def _load_command_config(config: str | None = None, workspace: str | None = None) -> Config:
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


def _print_deprecated_memory_window_notice(config: Config) -> None:
    """Warn when running with old memoryWindow-only config."""
    if config.agents.defaults.should_warn_deprecated_memory_window:
        console.print(
            "[yellow]Hint:[/yellow] Detected deprecated `memoryWindow` without "
            "`contextWindowTokens`. `memoryWindow` is ignored; run "
            "[cyan]paos onboard[/cyan] to refresh your config template."
        )


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    provider: str | None = typer.Option(None, "--provider", help="Provider for this process"),
    model: str | None = typer.Option(None, "--model", help="Model for this process"),
    reasoning_effort: str | None = typer.Option(None, "--reasoning-effort", help="Model-supported effort: minimal, low, medium, high, xhigh, max; none uses model default"),
):
    """Start the PhyAgentOS gateway."""
    from PhyAgentOS.agent.loop import AgentLoop
    from PhyAgentOS.bus.queue import MessageBus
    from PhyAgentOS.channels.manager import ChannelManager
    from PhyAgentOS.config.paths import get_cron_dir
    from PhyAgentOS.cron.service import CronService
    from PhyAgentOS.cron.types import CronJob
    from PhyAgentOS.heartbeat.service import HeartbeatService
    from PhyAgentOS.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    from PhyAgentOS.embodiment_registry import EmbodimentRegistry

    config = _apply_startup_overrides(
        _load_command_config(config, workspace), provider, model, reasoning_effort,
    )
    _print_deprecated_memory_window_notice(config)
    port = port if port is not None else config.gateway.port
    registry = EmbodimentRegistry(config)

    console.print(f"{__logo__} Starting PhyAgentOS gateway on port {port}...")
    if registry.is_fleet:
        registry.sync_layout()
    else:
        sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    evolution_provider, evolution_model = _make_evolution_provider(config, provider)
    (
        forge_tool_client,
        forge_tool_invocation_ids,
        forge_task_coordinator,
        runtime_availability_provider,
    ) = _make_forge_components(config, provider)
    session_manager = SessionManager(config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        provider_config=config,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        embodiment_registry=registry,
        forge_tool_client=forge_tool_client,
        forge_tool_invocation_ids=forge_tool_invocation_ids,
        forge_task_coordinator=forge_task_coordinator,
        runtime_availability_provider=runtime_availability_provider,
        evolution_config=config.agents.evolution,
        evolution_provider=evolution_provider,
        evolution_model=evolution_model,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from PhyAgentOS.agent.tools.cron import CronTool
        from PhyAgentOS.agent.tools.message import MessageTool
        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        # Prevent the agent from scheduling new cron jobs during execution
        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            from PhyAgentOS.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response
            ))
        return response
    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from PhyAgentOS.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            services = [
                agent.run(),
                channels.start_all(),
            ]
            await asyncio.gather(*services)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())




# ============================================================================
# Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show PhyAgentOS process logs during chat"),
    provider: str | None = typer.Option(None, "--provider", help="Provider for this process"),
    model: str | None = typer.Option(None, "--model", help="Model for this process"),
    reasoning_effort: str | None = typer.Option(None, "--reasoning-effort", help="Model-supported effort: minimal, low, medium, high, xhigh, max; none uses model default"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from PhyAgentOS.agent.loop import AgentLoop
    from PhyAgentOS.bus.queue import MessageBus
    from PhyAgentOS.config.paths import get_cron_dir
    from PhyAgentOS.cron.service import CronService
    from PhyAgentOS.embodiment_registry import EmbodimentRegistry

    config = _apply_startup_overrides(
        _load_command_config(config, workspace), provider, model, reasoning_effort,
    )
    _print_deprecated_memory_window_notice(config)
    registry = EmbodimentRegistry(config)
    if registry.is_fleet:
        registry.sync_layout()
    else:
        sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)
    evolution_provider, evolution_model = _make_evolution_provider(config, provider)
    (
        forge_tool_client,
        forge_tool_invocation_ids,
        forge_task_coordinator,
        runtime_availability_provider,
    ) = _make_forge_components(config, provider)

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("PhyAgentOS")
    else:
        logger.disable("PhyAgentOS")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        provider_config=config,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        embodiment_registry=registry,
        forge_tool_client=forge_tool_client,
        forge_tool_invocation_ids=forge_tool_invocation_ids,
        forge_task_coordinator=forge_task_coordinator,
        runtime_availability_provider=runtime_availability_provider,
        evolution_config=config.agents.evolution,
        evolution_provider=evolution_provider,
        evolution_model=evolution_model,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext
            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]PhyAgentOS is thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            try:
                with _thinking_ctx():
                    response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
                _print_agent_response(response, render_markdown=markdown)
            finally:
                agent_loop.stop()
                await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from PhyAgentOS.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            from PhyAgentOS.cli.providers import _interactive_model_command

            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        selection_result = await _interactive_model_command(
                            command, agent_loop.session_runtimes, f"{cli_channel}:{cli_chat_id}",
                        )
                        if selection_result is not None:
                            _print_agent_response(selection_result, render_markdown=markdown)
                            continue

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(
                    bus_task,
                    outbound_task,
                    return_exceptions=True,
                )
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Skill Runtime Commands
# ============================================================================


skill_app = typer.Typer(help="Manage installed Skill runtimes")
app.add_typer(skill_app, name="skill")


def _skill_runtime_error(error: Exception) -> None:
    console.print(f"[red]Error: {error}[/red]")
    raise typer.Exit(1)


@skill_app.command("search")
def skill_search(
    query: str = typer.Argument("", help="Skill name or search text"),
    index: str | None = typer.Option(
        None,
        "--index",
        help="Schema-v3 static package index path or URL",
    ),
):
    """Search Skills in a static index or the configured Resource Registry."""
    from PhyAgentOS.skill_runtime.registry import RegistryClient, StaticPackageIndex

    try:
        source = StaticPackageIndex(index) if index else RegistryClient()
        with source:
            items = source.search_skills(query)
    except Exception as error:
        _skill_runtime_error(error)
        return
    table = Table(title="Registry Skills")
    table.add_column("Skill", style="cyan")
    table.add_column("Version")
    table.add_column("Description")
    for item in items:
        table.add_row(
            str(item.get("name", "")),
            str(item.get("version", "")),
            str(item.get("description", "")),
        )
    console.print(table)


def _install_skill_bundle(
    archive: Path,
    *,
    expected_sha256: str,
    expected_version: str | None = None,
    index: str | None = None,
) -> None:
    import tempfile

    from PhyAgentOS.skill_runtime.catalog import SkillCatalog, SkillNotFoundError
    from PhyAgentOS.skill_runtime.installer import NodeInstaller, SkillInstaller
    from PhyAgentOS.skill_runtime.registry import (
        DownloadCache,
        RegistryClient,
        StaticPackageIndex,
    )
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    with _download_progress() as report:
        cache = DownloadCache(progress=report)
        try:
            with tempfile.TemporaryDirectory(prefix="paos-skill-preview-") as directory:
                preview_root = Path(directory)
                preview = SkillInstaller(
                    preview_root / "skills",
                    state_store=RuntimeStateStore(preview_root / "run"),
                ).install(
                    archive,
                    expected_sha256=expected_sha256,
                )
                if expected_version is not None and preview.version != expected_version:
                    raise RuntimeError(
                        f"Registry returned Skill {preview.name!r} version "
                        f"{preview.version!r}; requested version was {expected_version!r}"
                    )
                node_installer = NodeInstaller()
                missing_nodes = [
                    (node_id, lock)
                    for node_id, lock in sorted(preview.artifacts.nodes.items())
                    if not node_installer.satisfies(lock)
                ]
                if missing_nodes and (
                    index is not None or preview.artifacts.resolver == "registry"
                ):
                    source = StaticPackageIndex(index) if index else RegistryClient()
                    with source:
                        for node_id, lock in missing_nodes:
                            node_artifact = source.node(
                                lock.artifact_id,
                                expected_sha256=lock.sha256,
                            )
                            if node_artifact.sha256 != lock.sha256:
                                raise RuntimeError(
                                    f"Registry digest for Node {node_id!r} does not match "
                                    "Skill lock"
                                )
                            node_archive = cache.download(node_artifact)
                            installed = node_installer.install(node_archive, lock)
                            if not node_installer.satisfies(lock):
                                raise RuntimeError(
                                    f"Node executable {installed!s} does not satisfy Skill lock"
                                )
                    missing_nodes = []
                try:
                    local = SkillCatalog().get(preview.name)
                except SkillNotFoundError:
                    local = None
                if local == preview:
                    if missing_nodes:
                        names = ", ".join(node_id for node_id, _ in missing_nodes)
                        console.print(
                            f"[yellow]Skill [cyan]{preview.name}[/cyan] is installed; "
                            f"install locked Node(s) locally before start: {names}[/yellow]"
                        )
                    else:
                        console.print(
                            f"[green]✓[/green] Skill [cyan]{preview.name}[/cyan] "
                            f"{preview.version} is already ready"
                        )
                    return
                manifest = SkillInstaller().install(
                    archive,
                    expected_sha256=expected_sha256,
                )
        finally:
            cache.close()
    console.print(
        f"[green]✓[/green] Installed Skill [cyan]{manifest.name}[/cyan] {manifest.version}"
    )
    if missing_nodes:
        names = ", ".join(node_id for node_id, _ in missing_nodes)
        console.print(
            f"[yellow]Install locked Node(s) locally before start: {names}[/yellow]"
        )


def _confirm_skill_install(name: str, source: str, size: int) -> bool:
    size_mib = size / (1024 * 1024)
    console.print(
        f"[bold]Skill:[/bold] [cyan]{name}[/cyan]\n"
        f"[bold]Source:[/bold] {source}\n"
        f"[bold]Skill Bundle:[/bold] {size_mib:.1f} MiB ({size} bytes)\n"
        "[yellow]Additional locked Forge Node archives may be downloaded after the "
        "Skill Bundle is inspected.[/yellow]"
    )
    return typer.confirm("Continue with installation and downloads?", default=False)


def _install_skill_from_registry(
    name: str,
    *,
    version: str | None,
    index: str | None,
    ask_confirmation: bool,
) -> None:
    from PhyAgentOS.skill_runtime.registry import (
        DownloadCache,
        RegistryClient,
        StaticPackageIndex,
    )

    source = StaticPackageIndex(index) if index else RegistryClient()
    with source:
        artifact = source.skill(name, version)
    assert artifact.sha256 is not None and artifact.size is not None
    if ask_confirmation and not _confirm_skill_install(name, artifact.url, artifact.size):
        console.print("[yellow]Installation cancelled.[/yellow]")
        return
    with _download_progress() as report:
        cache = DownloadCache(progress=report)
        try:
            archive = cache.download(artifact)
        finally:
            cache.close()
    _install_skill_bundle(
        archive,
        expected_sha256=artifact.sha256,
        expected_version=version,
        index=index,
    )


def _resolve_local_bundle_path(name: str) -> Path:
    path = Path(name).expanduser()
    if not path.is_file():
        raise RuntimeError(f"Skill bundle file not found: {name!r}")
    if not str(path).endswith((".tar.gz", ".tgz")):
        raise RuntimeError("local Skill bundle must be a .tar.gz or .tgz archive")
    return path


def _install_skill_from_local_bundle(
    path: Path,
    *,
    index: str | None,
    ask_confirmation: bool,
) -> None:
    from PhyAgentOS.skill_runtime.archive import sha256_file

    size = path.stat().st_size
    if ask_confirmation and not _confirm_skill_install(path.name, str(path.resolve()), size):
        console.print("[yellow]Installation cancelled.[/yellow]")
        return
    _install_skill_bundle(
        path,
        expected_sha256=sha256_file(path),
        index=index,
    )


def _resolve_skill_install_source(name: str) -> str | Path:
    path = Path(name).expanduser()
    if path.exists() or name.endswith((".tar.gz", ".tgz")) or "/" in name or "\\" in name:
        return _resolve_local_bundle_path(name)
    return name


@skill_app.command("install")
def skill_install(
    name: str = typer.Argument(..., help="Registry Skill name or local .tar.gz bundle"),
    # Hidden until the Registry serves more than one version per Skill.
    version: str | None = typer.Option(
        None, "--version", "-v", help="Exact version", hidden=True
    ),
    index: str | None = typer.Option(
        None,
        "--index",
        help="Schema-v3 static package index path or URL",
    ),
    local: bool = typer.Option(False, "--local", help="Treat NAME as a local bundle path"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the install confirmation"),
):
    """Install a verified Skill from the Registry, static index, or local bundle."""
    try:
        source = _resolve_local_bundle_path(name) if local else _resolve_skill_install_source(name)
        if isinstance(source, Path):
            _install_skill_from_local_bundle(
                source, index=index, ask_confirmation=not yes
            )
        else:
            _install_skill_from_registry(
                source,
                version=version,
                index=index,
                ask_confirmation=not yes,
            )
    except Exception as error:
        _skill_runtime_error(error)


@skill_app.command("update")
def skill_update(
    name: str = typer.Argument(..., help="Installed Skill name"),
    # Hidden until the Registry serves more than one version per Skill.
    version: str | None = typer.Option(
        None, "--version", "-v", help="Target version", hidden=True
    ),
    index: str | None = typer.Option(
        None,
        "--index",
        help="Schema-v3 static package index path or URL",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the update confirmation"),
):
    """Update an installed, stopped Skill while retaining a backup."""
    try:
        from PhyAgentOS.skill_runtime.catalog import SkillCatalog

        SkillCatalog().get(name)
        _install_skill_from_registry(
            name,
            version=version,
            index=index,
            ask_confirmation=not yes,
        )
    except Exception as error:
        _skill_runtime_error(error)


@skill_app.command("remove")
def skill_remove(name: str = typer.Argument(..., help="Installed Skill name")):
    """Remove a Skill unless it is running or owns invocations, Sessions, or tasks."""
    from PhyAgentOS.skill_runtime.installer import SkillInstaller

    try:
        SkillInstaller().remove(name)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(f"[green]✓[/green] Removed Skill [cyan]{name}[/cyan]")


@skill_app.command("list")
def skill_list():
    """List locally installed Skill bundles."""
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    catalog = SkillCatalog()
    states = RuntimeStateStore()
    table = Table(title="Installed Skills")
    table.add_column("Skill", style="cyan")
    table.add_column("Version")
    table.add_column("Profiles")
    table.add_column("Runtime")
    try:
        manifests = catalog.list()
    except Exception as error:
        _skill_runtime_error(error)
        return
    for manifest in manifests:
        state = states.load(manifest.name)
        table.add_row(
            manifest.name,
            manifest.version,
            ", ".join(sorted(manifest.profiles)),
            state.status if state is not None else "not started",
        )
    for name, error in catalog.errors().items():
        table.add_row(name, "-", "-", f"[red]invalid: {error}[/red]")
    console.print(table)


@skill_app.command("inspect")
def skill_inspect(skill_name: str = typer.Argument(..., help="Installed Skill name")):
    """Inspect a Skill manifest and its last runtime state."""
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    try:
        manifest = SkillCatalog().get(skill_name)
        state = RuntimeStateStore().load(skill_name)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(f"[bold cyan]{manifest.name}[/bold cyan] {manifest.version}")
    console.print(manifest.description)
    console.print(f"Document: {manifest.skill_document.as_posix()}")
    console.print(f"Gateway: {manifest.gateway_url}")
    console.print(f"Tools: {', '.join(manifest.required_tools)}")
    profile_table = Table(title="Runtime Profiles")
    profile_table.add_column("Profile")
    profile_table.add_column("Dataflow")
    profile_table.add_column("Binaries")
    profile_table.add_column("Assets")
    for name, profile in sorted(manifest.profiles.items()):
        profile_table.add_row(
            name,
            profile.dataflow.as_posix(),
            str(len(profile.required_binaries)),
            str(len(profile.required_assets)),
        )
    console.print(profile_table)
    console.print(f"Runtime: {state.status if state is not None else 'not started'}")


@skill_app.command("start")
def skill_start(
    skill_name: str = typer.Argument(..., help="Installed Skill name"),
    profile: str = typer.Option(..., "--profile", "-p", help="Runtime profile"),
):
    """Start an installed Skill's named Dora dataflow."""
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    try:
        state = RuntimeManager().start(skill_name, profile)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(
        f"[green]✓[/green] Skill [cyan]{state.skill_name}[/cyan] is running "
        f"(profile={state.profile}, flow={state.flow_name})"
    )


@skill_app.command("status")
def skill_status(skill_name: str = typer.Argument(..., help="Installed Skill name")):
    """Reconcile persisted state with Dora and Gateway health."""
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    try:
        report = RuntimeManager().status(skill_name)
    except Exception as error:
        _skill_runtime_error(error)
        return
    if report.state is None:
        console.print(f"Skill [cyan]{skill_name}[/cyan]: not started")
        return
    state = report.state
    console.print(f"Skill: {state.skill_name}")
    console.print(f"State: {state.status}")
    console.print(f"Profile: {state.profile}")
    console.print(f"Dora flow: {state.flow_name} ({'running' if report.flow_running else 'down'})")
    console.print(f"Gateway GET /tools: {'ready' if report.gateway_ready else 'unavailable'}")
    for tool_id, ready in report.tool_contexts.items():
        console.print(f"Tool context {tool_id}: {'ready' if ready else 'not ready'}")
    if state.last_error:
        console.print(f"[red]Last error: {state.last_error}[/red]")


@skill_app.command("switch")
def skill_switch(
    skill_name: str = typer.Argument(..., help="Installed target Skill name"),
    profile: str = typer.Option(..., "--profile", "-p", help="Target Runtime profile"),
):
    """Safely switch the active Runtime when no AgentTask is non-terminal."""
    from PhyAgentOS.config.loader import load_config
    from PhyAgentOS.forge.task import AgentTaskStore
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.integration import (
        ActiveRuntimeRegistry,
        SkillRuntimeController,
        discover_active_runtime,
    )
    from PhyAgentOS.skill_runtime.manager import RuntimeManager
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    try:
        catalog = SkillCatalog()
        states = RuntimeStateStore()
        manager = RuntimeManager(catalog=catalog, state_store=states)
        active = discover_active_runtime(
            catalog=catalog,
            state_store=states,
            manager=manager,
        )
        registry = ActiveRuntimeRegistry(active)
        controller = SkillRuntimeController(
            registry,
            manager=manager,
            catalog=catalog,
            state_store=states,
            task_store=AgentTaskStore(load_config().workspace_path),
        )
        selected = controller.switch(skill_name, profile)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(
        f"[green]✓[/green] Active Forge Runtime is now "
        f"[cyan]{selected.skill_name}[/cyan] "
        f"(profile={selected.profile}, instance={selected.runtime_instance_id})"
    )


@skill_app.command("logs")
def skill_logs(
    skill_name: str = typer.Argument(..., help="Installed Skill name"),
    lines: int = typer.Option(200, "--lines", "-n", min=1, help="Lifecycle log lines"),
):
    """Show recent Skill runtime lifecycle logs."""
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    try:
        content = RuntimeManager().read_logs(skill_name, lines=lines)
    except Exception as error:
        _skill_runtime_error(error)
        return
    if content:
        console.print(content, end="")
    else:
        console.print("[dim]No lifecycle logs recorded.[/dim]")


@skill_app.command("stop")
def skill_stop(
    skill_name: str = typer.Argument(..., help="Installed Skill name"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Force-stop despite active invocations, Sessions, or task bindings",
    ),
):
    """Stop a managed Skill dataflow without shutting down shared Dora services."""
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    try:
        state = RuntimeManager().stop(skill_name, force=force)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(
        f"[green]✓[/green] Skill [cyan]{state.skill_name}[/cyan] is {state.status}"
    )


# ============================================================================
# Forge Node Installation Commands
# ============================================================================


forge_node_app = typer.Typer(help="Install and verify independently versioned Forge nodes")
app.add_typer(forge_node_app, name="forge-node")


@forge_node_app.command("install")
def forge_node_install(
    skill_name: str = typer.Argument(..., help="Installed Skill containing the Node lock"),
    node_id: str = typer.Argument(..., help="Node ID from the Skill lock"),
    archive: Path | None = typer.Option(
        None,
        "--archive",
        help="Independently obtained local Node .tar.gz instead of a Registry download",
    ),
):
    """Download the exact single-executable archive pinned by a Skill lock."""
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.installer import NodeInstaller
    from PhyAgentOS.skill_runtime.registry import DownloadCache, RegistryClient

    with _download_progress() as report:
        cache = DownloadCache(progress=report)
        try:
            manifest = SkillCatalog().get(skill_name)
            lock = manifest.artifacts.nodes.get(node_id)
            if lock is None:
                raise RuntimeError(f"Skill {skill_name!r} does not lock Node {node_id!r}")
            if archive is None:
                with RegistryClient() as registry:
                    artifact = registry.node(
                        lock.artifact_id,
                        expected_sha256=lock.sha256,
                    )
                if artifact.sha256 != lock.sha256:
                    raise RuntimeError("Registry Node sha256 does not match the Skill lock")
                node_archive = cache.download(artifact)
            else:
                node_archive = archive.expanduser().resolve()
                if not node_archive.is_file() or node_archive.is_symlink():
                    raise RuntimeError("local Node archive is not a regular file")
            installed = NodeInstaller().install(node_archive, lock)
        except Exception as error:
            _skill_runtime_error(error)
            return
        finally:
            cache.close()
    console.print(
        f"[green]✓[/green] Installed Forge node "
        f"[cyan]{lock.node_id}[/cyan] {lock.version} ({lock.artifact_id}) at {installed}"
    )


@forge_node_app.command("verify")
def forge_node_verify(
    skill_name: str = typer.Argument(..., help="Installed Skill containing the Node lock"),
    node_id: str = typer.Argument(..., help="Node ID from the Skill lock"),
):
    """Verify an installed executable and receipt against its Skill lock."""
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.installer import NodeInstaller

    try:
        manifest = SkillCatalog().get(skill_name)
        lock = manifest.artifacts.nodes.get(node_id)
        if lock is None:
            raise RuntimeError(f"Skill {skill_name!r} does not lock Node {node_id!r}")
        NodeInstaller().load(lock)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(
        f"[green]✓[/green] Forge node [cyan]{lock.node_id}[/cyan] "
        f"{lock.artifact_id} SHA-256 verified"
    )

# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


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


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    from PhyAgentOS.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 20.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # PhyAgentOS/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall PhyAgentOS")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess

    from PhyAgentOS.config.loader import load_config
    from PhyAgentOS.config.paths import get_data_subdir

    config = load_config()
    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    env = {**os.environ}
    if config.channels.whatsapp.bridge_token:
        env["BRIDGE_TOKEN"] = config.channels.whatsapp.bridge_token
    env["AUTH_DIR"] = str(get_data_subdir("whatsapp-auth"))

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Status Commands
# ============================================================================


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


# ============================================================================
# OAuth Login
# ============================================================================

from PhyAgentOS.cli.providers import provider_app  # noqa: E402

app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str | None = typer.Argument(None, help="OAuth provider; omit to select interactively"),
):
    """Authenticate with an OAuth provider."""
    from PhyAgentOS.cli.providers import _provider_argument, _provider_errors
    from PhyAgentOS.providers.registry import PROVIDERS
    from PhyAgentOS.providers.service import ProviderService

    with _provider_errors():
        key = _provider_argument(provider, ProviderService(Config()), action="Login")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)



if __name__ == "__main__":
    app()

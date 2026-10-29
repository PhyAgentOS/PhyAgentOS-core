"""Gateway process command plus its cron and heartbeat wiring."""

import asyncio

import typer
from rich.console import Console

from PhyAgentOS import __logo__
from PhyAgentOS.agent.bootstrap import build_forge_components
from PhyAgentOS.config.paths import get_cron_dir
from PhyAgentOS.utils.helpers import sync_workspace_templates

from ..configuration import (
    apply_startup_overrides,
    load_command_config,
    make_evolution_provider,
    make_provider,
    print_deprecated_memory_window_notice,
)

console = Console()


async def _deliver_cron_response(job, response: str, bus) -> None:
    """Deliver a cron reply unless the message tool already did (empty reply)."""
    if job.payload.deliver and job.payload.to and response:
        from PhyAgentOS.bus.events import OutboundMessage
        await bus.publish_outbound(OutboundMessage(
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to,
            content=response,
        ))


async def _process_direct_for_scheduler(agent, *, source: str, **kwargs) -> str | None:
    """Run a cron/heartbeat turn; a full session queue means skip, not deliver."""
    from loguru import logger

    from PhyAgentOS.agent_legacy.loop import SessionQueueFullError

    try:
        return await agent.process_direct(**kwargs)
    except SessionQueueFullError as exc:
        logger.warning("{} skipped: {}", source, exc)
        return None


def register_gateway_commands(app) -> None:
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
        from PhyAgentOS.agent_legacy.loop import AgentLoop
        from PhyAgentOS.bus.queue import MessageBus
        from PhyAgentOS.channels.manager import ChannelManager
        from PhyAgentOS.cron.service import CronService
        from PhyAgentOS.cron.types import CronJob
        from PhyAgentOS.heartbeat.service import HeartbeatService
        from PhyAgentOS.session.manager import SessionManager

        if verbose:
            import logging
            logging.basicConfig(level=logging.DEBUG)

        from PhyAgentOS.embodiment_registry import EmbodimentRegistry

        config = apply_startup_overrides(
            load_command_config(config, workspace), provider, model, reasoning_effort,
        )
        print_deprecated_memory_window_notice(config)
        port = port if port is not None else config.gateway.port
        registry = EmbodimentRegistry(config)

        console.print(f"{__logo__} Starting PhyAgentOS gateway on port {port}...")
        if registry.is_fleet:
            registry.sync_layout()
        else:
            sync_workspace_templates(config.workspace_path)
        bus = MessageBus()
        provider = make_provider(config)
        evolution_provider, evolution_model = make_evolution_provider(config, provider)
        (
            forge_tool_client,
            forge_tool_invocation_ids,
            forge_task_coordinator,
            runtime_availability_provider,
        ) = build_forge_components(config, provider)
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
            max_concurrent_turns=config.agents.defaults.max_concurrent_turns,
            per_session_queue_limit=config.agents.defaults.per_session_queue_limit,
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
            from PhyAgentOS.agent_legacy.tools.cron import CronTool
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
                response = await _process_direct_for_scheduler(
                    agent,
                    source=f"cron:{job.id}",
                    content=reminder_note,
                    session_key=f"cron:{job.id}",
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to or "direct",
                )
            finally:
                if isinstance(cron_tool, CronTool) and cron_token is not None:
                    cron_tool.reset_cron_context(cron_token)

            if response is None:
                return None
            # An empty response means the message tool already delivered the
            # reply (or the turn was cancelled); do not deliver it again.
            await _deliver_cron_response(job, response, bus)
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

            return await _process_direct_for_scheduler(
                agent,
                source="heartbeat",
                content=tasks,
                session_key="heartbeat",
                channel=channel,
                chat_id=chat_id,
                on_progress=_silent,
            ) or ""

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

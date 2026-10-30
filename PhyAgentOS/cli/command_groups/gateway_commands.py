"""Gateway process command: Core by default, the old loop only when explicit.

The gateway is the process entry point for the three background sources: chat
channels, cron jobs, and the heartbeat. In the default ``core`` mode all three
run through one shared Core session (see
:mod:`PhyAgentOS.agent.services.background_runtime`): every input becomes a
``TurnRequest`` for the same store, Forge coordinator, Skill activation and
capability set the CLI uses, and the reply is delivered back to the source.

``--runtime legacy`` keeps the previous ``AgentLoop`` wiring for comparison
only. It is imported inside the selected branch, so building the CLI app and
running Core never load the legacy package, and a Core failure is reported
instead of being retried on the old runtime.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console

from PhyAgentOS import __logo__
from PhyAgentOS.agent.capabilities.cron_capability import CronCapability
from PhyAgentOS.agent.capabilities.message_capability import MessageCapability
from PhyAgentOS.agent.core import TurnStatus
from PhyAgentOS.agent.services.background_runtime import (
    BackgroundClosedError,
    BackgroundCoreRuntime,
    ChannelTurnDispatcher,
    TurnBinding,
    TurnRejectedError,
    TurnRoutes,
    open_background_core,
)
from PhyAgentOS.bus.events import OutboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.config.paths import get_cron_dir
from PhyAgentOS.config.schema import Config
from PhyAgentOS.cron.service import CronService
from PhyAgentOS.cron.types import CronJob
from PhyAgentOS.heartbeat.service import HeartbeatService
from PhyAgentOS.providers.service import ProviderService
from PhyAgentOS.utils.helpers import sync_workspace_templates

from ..configuration import (
    apply_startup_overrides,
    load_command_config,
    print_deprecated_memory_window_notice,
)

console = Console()


async def _process_direct_for_scheduler(agent, *, source: str, **kwargs) -> str | None:
    """Legacy scheduler bridge, kept for the explicit legacy comparison only.

    The old concurrency tests drive this helper directly. It is imported inside
    the call so the Core gateway never loads ``agent_legacy`` on its own path;
    only an explicit legacy selection or a legacy comparison test reaches it.
    """
    from ..legacy.legacy_gateway import (
        _process_direct_for_scheduler as legacy_process_direct,
    )

    return await legacy_process_direct(agent, source=source, **kwargs)


async def _deliver_cron_response(job: CronJob, response: str, bus) -> None:
    """Deliver a cron reply unless the message tool already did (empty reply)."""
    if job.payload.deliver and job.payload.to and response:
        await bus.publish_outbound(OutboundMessage(
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to,
            content=response,
        ))


class _RecentChats:
    """Channels that most recently sent input, newest first.

    Core keeps chat history in its own SQLite store and writes no legacy
    ``SessionManager`` JSONL, so the heartbeat target cannot be picked from
    legacy session files. This tracker is the equivalent order for the migrated
    gateway: only a channel that is actually configured and has sent a turn can
    become a heartbeat target. It starts empty on restart, so a heartbeat never
    delivers to a chat that has not talked to this process.
    """

    def __init__(self) -> None:
        self._order: dict[tuple[str, str, str], TurnBinding] = {}

    def record(self, binding: TurnBinding) -> None:
        # A thread is part of the target: a heartbeat answer for a forum topic
        # must go back to that topic, not to the chat's main thread.
        thread = str(binding.metadata.get("message_thread_id") or "")
        key = (binding.channel, binding.chat_id, thread)
        self._order.pop(key, None)
        self._order[key] = binding

    def latest(self) -> TurnBinding | None:
        for binding in reversed(self._order.values()):
            return binding
        return None


class _LastRoute:
    """One-record state for the last external chat target.

    Core keeps no legacy session files to read a "most recent chat" from, and
    the heartbeat must still target the last external conversation after a
    restart. This file holds exactly that one frozen target (channel, chat,
    thread) and nothing else; it is not a session registry. The channel name
    is re-validated on load and before use, so a target whose channel is no
    longer enabled is never used.
    """

    FILE_NAME = ".gateway_route.json"

    def __init__(self, workspace: Path) -> None:
        self.path = workspace / self.FILE_NAME

    def load(self) -> TurnBinding | None:
        from loguru import logger

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("Could not read the last gateway route: {}", exc)
            return None
        if not isinstance(data, dict):
            return None
        channel, chat_id = data.get("channel"), data.get("chat_id")
        if not isinstance(channel, str) or not isinstance(chat_id, str):
            return None
        if not channel or not chat_id:
            return None
        metadata = {}
        thread = data.get("message_thread_id")
        if isinstance(thread, (int, str)) and not isinstance(thread, bool):
            metadata["message_thread_id"] = thread
        try:
            return TurnBinding(channel=channel, chat_id=chat_id, metadata=metadata)
        except ValueError:
            return None

    def save(self, binding: TurnBinding) -> None:
        from loguru import logger

        payload = {
            "channel": binding.channel,
            "chat_id": binding.chat_id,
        }
        thread = binding.metadata.get("message_thread_id")
        if thread is not None:
            payload["message_thread_id"] = thread
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(self.path)
        except OSError as exc:
            # Losing the last-target hint must not fail the user's turn.
            logger.warning("Could not persist the last gateway route: {}", exc)


class CoreGateway:
    """Wire cron, heartbeat and channels onto one shared Core runtime.

    Callbacks are installed on the injected services rather than constructed
    here, so a test can supply a clock-free cron store and a fake channel
    manager. ``serve`` starts scheduling and intake; ``aclose`` stops intake and
    scheduling first, settles accepted turns, then closes Core exactly once.
    """

    def __init__(
        self,
        *,
        runtime: BackgroundCoreRuntime,
        bus: MessageBus,
        channels,
        cron: CronService,
        heartbeat: HeartbeatService,
        workspace: Path | None = None,
    ) -> None:
        self.runtime = runtime
        self.bus = bus
        self.channels = channels
        self.cron = cron
        self.heartbeat = heartbeat
        self.recent_chats = _RecentChats()
        self._last_route = _LastRoute(workspace) if workspace is not None else None
        if self._last_route is not None:
            restored = self._last_route.load()
            if restored is not None:
                self.recent_chats.record(restored)
        self._dispatcher = ChannelTurnDispatcher(
            runtime, bus, on_input=self._remember_chat
        )
        self._heartbeat_binding: TurnBinding | None = None
        self._heartbeat_turn_id: str | None = None
        self._closed = False

        cron.on_job = self._on_cron_job
        heartbeat.on_execute = self._on_heartbeat_execute
        heartbeat.on_notify = self._on_heartbeat_notify

    # -- lifecycle ----------------------------------------------------

    async def serve(self) -> None:
        """Run scheduling and intake until cancelled, then shut down in order."""
        try:
            await self.cron.start()
            await self.heartbeat.start()
            await asyncio.gather(self._dispatcher.run(), self.channels.start_all())
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Stop intake and scheduling, settle turns, then close Core once."""
        if self._closed:
            return
        self._closed = True
        self.cron.stop()
        self.heartbeat.stop()
        await self._dispatcher.stop()
        try:
            # Settles accepted turns (local cancel is not a remote stop claim)
            # and closes capabilities and the store exactly once.
            await self.runtime.aclose()
        finally:
            await self._dispatcher.drain()
            await self.channels.stop_all()

    # -- chat input ---------------------------------------------------

    def _remember_chat(self, msg) -> None:
        if self.channels.get_channel(msg.channel) is None:
            return
        if msg.channel in {"cli", "system"}:
            return
        metadata = {}
        thread = (msg.metadata or {}).get("message_thread_id")
        if thread is not None:
            metadata["message_thread_id"] = thread
        binding = TurnBinding(
            channel=msg.channel, chat_id=msg.chat_id, metadata=metadata
        )
        self.recent_chats.record(binding)
        if self._last_route is not None:
            self._last_route.save(binding)

    def _pick_heartbeat_target(self) -> TurnBinding:
        """Freeze the heartbeat target before the turn starts."""
        latest = self.recent_chats.latest()
        if latest is None or self.channels.get_channel(latest.channel) is None:
            # Nothing external is known or the channel is no longer enabled;
            # the fallback keeps the prior behavior and delivers nothing
            # outside the process.
            return TurnBinding(channel="cli", chat_id="direct")
        return latest

    # -- cron ---------------------------------------------------------

    async def _on_cron_job(self, job: CronJob) -> str | None:
        """Execute one due job as a Core turn and deliver its reply once."""
        from loguru import logger

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )
        session_id = f"cron:{job.id}"
        binding = TurnBinding(
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            scheduled=True,
        )
        try:
            outcome = await self.runtime.run_turn(
                session_id=session_id,
                text=reminder_note,
                binding=binding,
                source=f"cron:{job.id}",
            )
        except TurnRejectedError as exc:
            logger.warning("cron:{} skipped: {}", job.id, exc.reason)
            return None
        except BackgroundClosedError:
            logger.info("cron:{} skipped: gateway is shutting down", job.id)
            return None

        result = outcome.result
        if outcome.reply_sent:
            # The message tool already delivered this turn's answer.
            return None
        if result.status is not TurnStatus.COMPLETED or not result.reply:
            return None
        await _deliver_cron_response(job, result.reply, self.bus)
        return result.reply

    # -- heartbeat ----------------------------------------------------

    async def _on_heartbeat_execute(self, tasks: str) -> str:
        """Run the heartbeat task in Core, with the target frozen once."""
        from loguru import logger

        binding = self._pick_heartbeat_target()
        self._heartbeat_binding = binding
        self._heartbeat_turn_id = None
        try:
            outcome = await self.runtime.run_turn(
                session_id="heartbeat",
                text=tasks,
                binding=binding,
                source="heartbeat",
            )
        except TurnRejectedError as exc:
            logger.warning("heartbeat skipped: {}", exc.reason)
            return ""
        except BackgroundClosedError:
            logger.info("heartbeat skipped: gateway is shutting down")
            return ""
        result = outcome.result
        self._heartbeat_turn_id = result.turn_id
        if outcome.reply_sent:
            return ""
        if result.status is not TurnStatus.COMPLETED or not result.reply:
            return ""
        return result.reply

    async def _on_heartbeat_notify(self, response: str) -> None:
        """Deliver the heartbeat answer to the target frozen for this tick."""
        binding = self._heartbeat_binding
        if binding is None or binding.channel == "cli":
            return  # No external channel target for this tick.
        # Keep the frozen routing (for example a forum topic) together with the
        # terminal-turn markers instead of replacing it with a fresh dict.
        metadata = dict(binding.metadata)
        metadata["_turn_final"] = True
        metadata["turn_id"] = self._heartbeat_turn_id
        await self.bus.publish_outbound(OutboundMessage(
            channel=binding.channel,
            chat_id=binding.chat_id,
            content=response,
            reply_to=binding.reply_to,
            metadata=metadata,
        ))


class _ConsoleOutput:
    """Narrow Core output sink for the gateway process: errors only."""

    def __init__(self, out: Console) -> None:
        self._out = out

    def error(self, text: str) -> None:
        self._out.print(text, style="red", markup=False)

    def event(self, event) -> None:
        return None


async def run_core_gateway(
    *,
    config: Config,
    port: int,
    bus: MessageBus | None = None,
    channel_manager=None,
    heartbeat_factory=None,
    cron: CronService | None = None,
    service: ProviderService | None = None,
    selected=None,
    console_out: Console = console,
) -> None:
    """Run the gateway with the Core runtime; never fall back to legacy.

    ``bus``, ``channel_manager``, ``heartbeat_factory``, ``cron`` and the
    provider ``service``/``selected`` pair are seams: the production call passes
    none and uses the real bus, manager, service and job store. Channel
    construction itself is untouched: only channels enabled in configuration
    are created.
    """
    from PhyAgentOS.embodiment_registry import EmbodimentRegistry

    registry = EmbodimentRegistry(config)
    if registry.is_fleet:
        raise ValueError(
            "Core gateway does not support fleet mode yet; select "
            "--runtime legacy explicitly for that comparison."
        )
    sync_workspace_templates(config.workspace_path)

    console_out.print(f"{__logo__} Starting PhyAgentOS gateway on port {port}...")
    bus = bus if bus is not None else MessageBus()
    service = service if service is not None else ProviderService(config)
    selected = selected if selected is not None else service.resolve()

    if channel_manager is None:
        from PhyAgentOS.channels.manager import ChannelManager

        channel_manager = ChannelManager(config, bus)
    channels = channel_manager
    cron = cron if cron is not None else CronService(get_cron_dir() / "jobs.json")
    routes = TurnRoutes()

    async def send(message: OutboundMessage) -> None:
        if channels.get_channel(message.channel) is None:
            raise ValueError(
                f"unknown channel '{message.channel}'; it is not enabled"
            )
        await bus.publish_outbound(message)

    cron_capability = CronCapability(cron, routes)
    message_capability = MessageCapability(routes, send)

    async with open_background_core(
        config,
        service=service,
        selected=selected,
        capabilities=(cron_capability, message_capability),
        routes=routes,
        output=_ConsoleOutput(console_out),
    ) as runtime:
        hb_cfg = config.gateway.heartbeat
        if heartbeat_factory is not None:
            heartbeat = heartbeat_factory(
                config=config, provider=selected.provider, model=selected.model
            )
        else:
            heartbeat = HeartbeatService(
                workspace=config.workspace_path,
                provider=selected.provider,
                model=selected.model,
                interval_s=hb_cfg.interval_s,
                enabled=hb_cfg.enabled,
            )
        gateway = CoreGateway(
            runtime=runtime,
            bus=bus,
            channels=channels,
            cron=cron,
            heartbeat=heartbeat,
            workspace=config.workspace_path,
        )

        if channels.enabled_channels:
            console_out.print(
                f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}"
            )
        else:
            console_out.print("[yellow]Warning: No channels enabled[/yellow]")

        cron_status = cron.status()
        if cron_status["jobs"] > 0:
            console_out.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")
        console_out.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

        try:
            await gateway.serve()
        except KeyboardInterrupt:
            console_out.print("\nShutting down...")


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
        runtime: str = typer.Option(
            "core",
            "--runtime",
            help="Gateway runtime: core (default) or legacy.",
        ),
    ):
        """Start the PhyAgentOS gateway."""
        if runtime not in {"core", "legacy"}:
            raise typer.BadParameter("choose core or legacy", param_hint="--runtime")
        if runtime == "legacy":
            # Imported only for an explicit legacy selection; the Core default
            # never loads the old loop.
            from ..legacy.legacy_gateway import run_gateway

            return run_gateway(
                port=port,
                workspace=workspace,
                verbose=verbose,
                config=config,
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort,
            )

        if verbose:
            import logging

            logging.basicConfig(level=logging.DEBUG)

        config_obj = apply_startup_overrides(
            load_command_config(config, workspace), provider, model, reasoning_effort,
        )
        print_deprecated_memory_window_notice(config_obj)
        port = port if port is not None else config_obj.gateway.port
        try:
            asyncio.run(run_core_gateway(config=config_obj, port=port))
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except (ValueError, OSError, RuntimeError) as exc:
            console.print(str(exc), style="red", markup=False)
            raise typer.Exit(2) from None

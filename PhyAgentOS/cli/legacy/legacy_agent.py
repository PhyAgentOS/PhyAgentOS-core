"""The legacy direct Agent CLI and its terminal interaction."""

import asyncio
import signal
import sys
from contextlib import suppress

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from PhyAgentOS import __logo__
from PhyAgentOS.agent.bootstrap import build_forge_components
from PhyAgentOS.bus.agent_events import AgentEventType
from PhyAgentOS.utils.helpers import sync_workspace_templates

from ..configuration import (
    apply_startup_overrides,
    load_command_config,
    make_evolution_provider,
    make_provider,
    print_deprecated_memory_window_notice,
)
from .legacy_support import (
    _flush_pending_tty_input,
    _init_prompt_session,
    _read_interactive_input_async,
    _restore_terminal,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} PhyAgentOS[/cyan]")
    console.print(body)
    console.print()


class _ThinkFilter:
    """Hold back <think>...</think> text across streaming chunks."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    @staticmethod
    def _partial_suffix(text: str, tag: str) -> int:
        for size in range(min(len(text), len(tag) - 1), 0, -1):
            if tag.startswith(text[-size:]):
                return size
        return 0

    def feed(self, text: str | None) -> str:
        if text:
            self._buffer += text
        visible: list[str] = []
        while self._buffer:
            if self._in_think:
                end = self._buffer.find(self._CLOSE)
                if end < 0:
                    keep = self._partial_suffix(self._buffer, self._CLOSE)
                    self._buffer = self._buffer[-keep:] if keep else ""
                    return "".join(visible)
                self._buffer = self._buffer[end + len(self._CLOSE):]
                self._in_think = False
                continue

            start = self._buffer.find(self._OPEN)
            if start < 0:
                keep = self._partial_suffix(self._buffer, self._OPEN)
                if keep:
                    visible.append(self._buffer[:-keep])
                    self._buffer = self._buffer[-keep:]
                else:
                    visible.append(self._buffer)
                    self._buffer = ""
                return "".join(visible)
            visible.append(self._buffer[:start])
            self._buffer = self._buffer[start + len(self._OPEN):]
            self._in_think = True
        return "".join(visible)

    def flush(self) -> str:
        """Text still safe to show; an unclosed think block stays hidden."""
        if self._in_think:
            self._buffer = ""
            return ""
        text, self._buffer = self._buffer, ""
        return text


def _visible_text(text: str | None) -> str:
    think = _ThinkFilter()
    return think.feed(text) + think.flush()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


def _is_control_command(text: str) -> bool:
    """Commands the agent loop handles itself instead of starting a turn."""
    lowered = text.strip().lower()
    return (
        lowered == "/stop"
        or lowered.startswith("/stop ")
        or lowered == "/correct"
        or lowered.startswith("/correct ")
    )


class _InteractiveRuntime:
    """Interactive CLI loop that keeps reading input while a turn runs.

    Input typed during a turn never interrupts the model or a tool except for
    ``/stop`` (full cancellation) and ``/correct`` (queued for the next safe
    point). Everything else is appended locally and starts a new turn once the
    current one finishes, in the order it was typed.
    """

    def __init__(
        self,
        agent_loop,
        bus,
        *,
        channel: str,
        chat_id: str,
        markdown: bool,
        thinking_ctx,
        turn_control: dict,
        read_input=None,
    ) -> None:
        self.agent_loop = agent_loop
        self.bus = bus
        self.channel = channel
        self.chat_id = chat_id
        self.markdown = markdown
        self.thinking_ctx = thinking_ctx
        self.turn_control = turn_control
        self._read_input = read_input or _read_interactive_input_async
        self._input_task: asyncio.Task | None = None
        self._pending: list[str] = []
        self._spinner_holder: list = []
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._stream = {
            "open": False,
            "current": "",
            "saw_event": False,
            "think": _ThinkFilter(),
        }

    def _ensure_input_task(self) -> asyncio.Task:
        # A completed reader still holds a line the user typed at the end of
        # the previous turn; keep it instead of starting over and losing it.
        if self._input_task is None:
            _flush_pending_tty_input()
            self._input_task = asyncio.create_task(self._read_input())
        return self._input_task

    async def _take_input(self) -> str:
        task = self._ensure_input_task()
        try:
            return await task
        finally:
            self._input_task = None

    async def _publish(self, content: str) -> None:
        from PhyAgentOS.bus.events import InboundMessage

        await self.bus.publish_inbound(InboundMessage(
            channel=self.channel,
            sender_id="user",
            chat_id=self.chat_id,
            content=content,
        ))

    def _stop_message(self):
        from PhyAgentOS.bus.events import InboundMessage

        return InboundMessage(
            channel=self.channel,
            sender_id="user",
            chat_id=self.chat_id,
            content="/stop",
        )

    def _reset_turn_state(self) -> None:
        self._turn_done.clear()
        self._stream.update(
            open=False,
            current="",
            saw_event=False,
            cancelled=False,
            think=_ThinkFilter(),
        )

    def _stop_spinner(self) -> None:
        if self._spinner_holder:
            self._spinner_holder.pop().stop()

    def _close_stream_line(self) -> None:
        if self._stream["open"]:
            console.print()
            self._stream["open"] = False

    def _show_final(self, content: str | None) -> None:
        visible = _visible_text(content)
        if (
            visible.strip()
            and visible.strip() != self._stream["current"].strip()
        ):
            self._close_stream_line()
            _print_agent_response(visible, render_markdown=self.markdown)
        self._stream["current"] = visible

    async def _handle_running_input(self, text: str) -> None:
        if _is_control_command(text):
            await self._publish(text)
            return
        if _is_exit_command(text):
            self._pending.append(text)
            console.print(
                "  [dim]Exit queued; finishing the current turn first.[/dim]"
            )
            return
        self._pending.append(text)
        console.print(f"  [dim]Queued for after this turn: {text}[/dim]")

    async def _run_turn(self, content: str) -> None:
        self._reset_turn_state()
        self.turn_control["active"] = True
        spinner = self.thinking_ctx()
        if hasattr(spinner, "start"):
            spinner.start()
            self._spinner_holder.append(spinner)
        try:
            await self._publish(content)
            while not self._turn_done.is_set():
                reader = self._ensure_input_task()
                done_waiter = asyncio.create_task(self._turn_done.wait())
                try:
                    done, _ = await asyncio.wait(
                        {reader, done_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    done_waiter.cancel()
                    try:
                        await done_waiter
                    except asyncio.CancelledError:
                        pass
                if reader in done:
                    self._input_task = None
                    try:
                        text = reader.result().strip()
                    except KeyboardInterrupt:
                        # Ctrl+C while a turn runs: cancel it through the same
                        # path as /stop before leaving the loop.
                        with suppress(Exception):
                            await self.agent_loop._handle_stop(
                                self._stop_message(), notify=False,
                            )
                        raise
                    if text:
                        await self._handle_running_input(text)
        finally:
            self._stop_spinner()
            self.turn_control["active"] = False

    async def _consume_outbound(self) -> None:
        while True:
            try:
                msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)
                if (msg.channel, msg.chat_id) != (self.channel, self.chat_id):
                    # Another session's turn (subagents run as system turns)
                    # must not close or answer this CLI turn.
                    continue
                event = getattr(msg, "event", None)
                if event is not None:
                    self._stream["saw_event"] = True
                    if event.type != AgentEventType.TURN_STARTED:
                        self._stop_spinner()
                    if event.type == AgentEventType.TEXT_DELTA:
                        if event.content:
                            visible = self._stream["think"].feed(event.content)
                            if visible:
                                self._stream["current"] += visible
                                self._stream["open"] = True
                                console.print(
                                    visible,
                                    end="",
                                    markup=False,
                                    highlight=False,
                                )
                    elif event.type == AgentEventType.TOOL_PROGRESS:
                        if event.content:
                            self._close_stream_line()
                            self._stream["current"] = ""
                            console.print(f"  ↳ {event.content}", style="dim")
                    elif event.type == AgentEventType.ERROR:
                        # The failure reply is published before this event; it
                        # is already visible by now.
                        self._close_stream_line()
                        self._turn_done.set()
                    elif event.type == AgentEventType.CANCELLED:
                        self._close_stream_line()
                        console.print("\nCancelled.", style="yellow")
                        self._turn_done.set()
                    elif event.type == AgentEventType.TURN_DONE:
                        self._close_stream_line()
                        self._turn_done.set()
                    continue
                if msg.metadata.get("correction_ack"):
                    # A /correct receipt is not the turn's answer.
                    console.print(f"  [dim]{msg.content}[/dim]")
                    if not self.turn_control["active"]:
                        self._turn_done.set()
                    continue
                if msg.metadata.get("_progress"):
                    is_tool_hint = msg.metadata.get("_tool_hint", False)
                    ch = self.agent_loop.channels_config
                    if ch and is_tool_hint and not ch.send_tool_hints:
                        pass
                    elif ch and not is_tool_hint and not ch.send_progress:
                        pass
                    else:
                        console.print(f"  [dim]↳ {msg.content}[/dim]")
                else:
                    self._show_final(msg.content)
                    if not self._stream["saw_event"]:
                        # Legacy path: a plain reply with no events.
                        self._turn_done.set()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def run(self) -> None:
        from ..interaction.model_picker import _interactive_model_command

        bus_task = asyncio.create_task(self.agent_loop.run())
        outbound_task = asyncio.create_task(self._consume_outbound())
        try:
            while True:
                try:
                    if self._pending:
                        text = self._pending.pop(0)
                        console.print(
                            f"  [dim]Sending queued message: {text}[/dim]"
                        )
                    else:
                        text = (await self._take_input()).strip()
                    if not text:
                        continue
                    if _is_exit_command(text):
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    selection = await _interactive_model_command(
                        text,
                        self.agent_loop.session_runtimes,
                        f"{self.channel}:{self.chat_id}",
                    )
                    if selection is not None:
                        _print_agent_response(selection, render_markdown=self.markdown)
                        continue
                    if _is_control_command(text):
                        # No turn to watch: run() answers these directly.
                        self._reset_turn_state()
                        await self._publish(text)
                        await self._turn_done.wait()
                        continue
                    await self._run_turn(text)
                except KeyboardInterrupt:
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break
                except EOFError:
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break
        finally:
            self.agent_loop.stop()
            tasks = [bus_task, outbound_task]
            if self._input_task is not None:
                self._input_task.cancel()
                tasks.append(self._input_task)
            outbound_task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.agent_loop.close_mcp()


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

    from PhyAgentOS.agent_legacy.loop import AgentLoop
    from PhyAgentOS.bus.queue import MessageBus
    from PhyAgentOS.config.paths import get_cron_dir
    from PhyAgentOS.cron.service import CronService
    from PhyAgentOS.embodiment_registry import EmbodimentRegistry

    config = apply_startup_overrides(
        load_command_config(config, workspace), provider, model, reasoning_effort,
    )
    print_deprecated_memory_window_notice(config)
    registry = EmbodimentRegistry(config)
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
        max_concurrent_turns=config.agents.defaults.max_concurrent_turns,
        per_session_queue_limit=config.agents.defaults.per_session_queue_limit,
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

    if message:
        # Single message mode — direct call, no bus needed
        cancel_shown = {"value": False}

        async def run_once():
            line_open = False
            current_text = ""
            cancelled = False
            think = _ThinkFilter()
            spinner = None

            def _stop_spinner():
                nonlocal spinner
                if spinner is not None:
                    spinner.stop()
                    spinner = None

            def _close_stream_line():
                nonlocal line_open
                if line_open:
                    console.print()
                    line_open = False

            async def _cli_event(event) -> None:
                nonlocal current_text, line_open, cancelled
                if event.type != AgentEventType.TURN_STARTED:
                    _stop_spinner()
                if event.type == AgentEventType.TEXT_DELTA and event.content:
                    visible = think.feed(event.content)
                    if visible:
                        current_text += visible
                        line_open = True
                        console.print(
                            visible, end="", markup=False, highlight=False
                        )
                elif event.type == AgentEventType.TOOL_PROGRESS and event.content:
                    _close_stream_line()
                    current_text = ""
                    console.print(f"  ↳ {event.content}", style="dim")
                elif event.type == AgentEventType.ERROR:
                    # The failure reply is printed below as the final answer.
                    _close_stream_line()
                elif event.type == AgentEventType.CANCELLED:
                    _close_stream_line()
                    console.print("\nCancelled.", style="yellow")
                    cancel_shown["value"] = True
                    cancelled = True

            spinner = _thinking_ctx()
            if hasattr(spinner, "start"):
                spinner.start()
            else:
                spinner = None

            try:
                response = await agent_loop.process_direct(
                    message, session_id, on_event=_cli_event,
                )
                trailing = think.flush()
                if trailing:
                    current_text += trailing
                    line_open = True
                    console.print(
                        trailing, end="", markup=False, highlight=False
                    )
                _close_stream_line()
                # Only skip the final render when it is exactly what was
                # already shown; fallback replies (max iterations, errors,
                # empty responses) must still be printed.
                visible_final = _visible_text(response)
                if (
                    not cancelled
                    and visible_final.strip() != current_text.strip()
                ):
                    _print_agent_response(visible_final, render_markdown=markdown)
            finally:
                _stop_spinner()
                agent_loop.stop()
                await agent_loop.close_mcp()

        try:
            asyncio.run(run_once())
        except KeyboardInterrupt:
            if not cancel_shown["value"]:
                console.print("\nCancelled.", style="yellow")
            raise typer.Exit(130) from None
        if cancel_shown["value"]:
            raise typer.Exit(130)
    else:
        # Interactive mode — route through bus like other channels
        from PhyAgentOS.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        turn_control = {
            "active": False,
            "session_key": f"{cli_channel}:{cli_chat_id}",
        }

        def _handle_signal(signum, frame):
            if signum == signal.SIGINT and turn_control["active"]:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    stop_msg = InboundMessage(
                        channel=cli_channel,
                        sender_id="user",
                        chat_id=cli_chat_id,
                        content="/stop",
                        session_key_override=turn_control["session_key"],
                    )
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(
                            agent_loop._handle_stop(stop_msg, notify=False)
                        )
                    )
                    return
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
            runtime = _InteractiveRuntime(
                agent_loop,
                bus,
                channel=cli_channel,
                chat_id=cli_chat_id,
                markdown=markdown,
                thinking_ctx=_thinking_ctx,
                turn_control=turn_control,
            )
            await runtime.run()

        asyncio.run(run_interactive())

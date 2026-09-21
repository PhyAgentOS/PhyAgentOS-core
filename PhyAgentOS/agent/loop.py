"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import MutableSet
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

from PhyAgentOS.agent.context import ContextBuilder
from PhyAgentOS.agent.memory import MemoryConsolidator
from PhyAgentOS.agent.subagent import SubagentManager
from PhyAgentOS.agent.tools.agent import AgentModeTool
from PhyAgentOS.agent.tools.cron import CronTool
from PhyAgentOS.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from PhyAgentOS.agent.tools.image import ImageTool
from PhyAgentOS.agent.tools.message import MessageTool
from PhyAgentOS.agent.tools.registry import ToolRegistry
from PhyAgentOS.agent.tools.scene_graph import SceneGraphQueryTool
from PhyAgentOS.agent.tools.shell import ExecTool
from PhyAgentOS.agent.tools.spawn import SpawnTool
from PhyAgentOS.agent.tools.web import WebFetchTool, WebSearchTool
from PhyAgentOS.bus.events import InboundMessage, OutboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.embodiment_registry import EmbodimentRegistry
from PhyAgentOS.providers.base import LLMProvider
from PhyAgentOS.providers.providers_manager import ProvidersManager
from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    RuntimeSelection,
    SessionRuntimes,
)
from PhyAgentOS.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from PhyAgentOS.config.schema import (
        AgentEvolutionConfig,
        ChannelsConfig,
        Config,
        ExecToolConfig,
    )
    from PhyAgentOS.cron.service import CronService
    from PhyAgentOS.forge.task import AgentTaskCoordinator
    from PhyAgentOS.forge.tool_client import ForgeToolClient


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 16_000

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        embodiment_registry: EmbodimentRegistry | None = None,
        forge_tool_client: ForgeToolClient | None = None,
        forge_tool_invocation_ids: MutableSet[str] | None = None,
        forge_task_coordinator: AgentTaskCoordinator | None = None,
        runtime_availability_provider: Callable[[str], bool] | None = None,
        evolution_config: AgentEvolutionConfig | None = None,
        evolution_provider: LLMProvider | None = None,
        evolution_model: str | None = None,
        provider_config: Config | None = None,
    ):
        from PhyAgentOS.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.session_runtimes: SessionRuntimes | None = None
        if provider_config is not None:
            service = ProviderService(provider_config)
            self.session_runtimes = SessionRuntimes(service, RuntimeSelection(
                name=provider_config.get_provider_name(self.model) or "custom",
                model=self.model,
                effort=provider.generation.reasoning_effort,
                endpoint=provider.api_base,
                provider=provider,
            ))
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.forge_tool_client = forge_tool_client
        self.forge_tool_invocation_ids = forge_tool_invocation_ids
        self.forge_task_coordinator = forge_task_coordinator
        binding_resolver = (
            forge_task_coordinator.binding_resolver
            if forge_task_coordinator is not None
            else None
        )

        self.experience = None
        if evolution_config is not None and evolution_config.enabled:
            try:
                from PhyAgentOS.agent.experience.analyzer import ModelExperienceAnalyzer
                from PhyAgentOS.agent.experience.coordinator import ExperienceCoordinator

                self.experience = ExperienceCoordinator(
                    workspace=workspace,
                    analyzer=ModelExperienceAnalyzer(
                        provider=evolution_provider or provider,
                        model=evolution_model or model or provider.get_default_model(),
                    ),
                    task_coordinator=forge_task_coordinator,
                    runtime_availability_provider=runtime_availability_provider,
                    binding_resolver=binding_resolver,
                    min_successful_episodes=evolution_config.min_successful_episodes,
                    min_lesson_episodes=evolution_config.min_lesson_episodes,
                    max_lessons_per_skill=evolution_config.max_lessons_per_skill,
                    max_calls=evolution_config.max_evolution_calls_per_run,
                )
            except Exception as exc:
                logger.warning(
                    "Experience evolution initialization failed open: error_type={}",
                    type(exc).__name__,
                )

        if self.experience is not None:
            self.skill_activation = self.experience.activation
        else:
            from PhyAgentOS.agent.experience.activation import SkillActivationManager
            from PhyAgentOS.agent.experience.store import ExperienceStore

            self.skill_activation = SkillActivationManager(
                workspace=workspace,
                store=ExperienceStore(workspace),
                runtime_availability_provider=runtime_availability_provider,
                binding_resolver=binding_resolver,
            )

        self.context = ContextBuilder(
            workspace,
            forge_context_provider=(
                forge_task_coordinator.capabilities_summary
                if forge_task_coordinator is not None
                else None
            ),
            evolution_enabled=(
                self.experience is not None or self.forge_task_coordinator is not None
            ),
            runtime_availability_provider=runtime_availability_provider,
        )
        if self.forge_task_coordinator is not None:
            self.forge_task_coordinator.set_experience(self.experience)
            self.forge_task_coordinator.set_activation_manager(self.skill_activation)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.embodiment_registry = embodiment_registry
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._forge_reconciled = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._processing_lock = asyncio.Lock()
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
        )
        self._register_default_tools()
        # Load env variables
        try:
            from dotenv import load_dotenv

            load_dotenv(dotenv_path=self.workspace / ".env")
        except Exception:
            logger.warning("Failed to load .env file, ignore using env variables")

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
        if isinstance(self.provider, ProvidersManager):
            self.tools.register(AgentModeTool(self.provider))
            self.tools.register(ImageTool(self.provider, send_callback=self.bus.publish_outbound))

        self.tools.register(SceneGraphQueryTool(workspace=self.workspace))
        from PhyAgentOS.agent.tools.skill_activation import ActivateSkillTool

        self.tools.register(ActivateSkillTool(self.skill_activation))
        if self.forge_task_coordinator is not None:
            from PhyAgentOS.agent.tools.forge_task import build_forge_task_tools

            for tool in build_forge_task_tools(self.forge_task_coordinator):
                self.tools.register(tool)
        if self.forge_tool_client is not None:
            from PhyAgentOS.agent.tools.forge_tool_api import build_forge_tool_api_tools

            for tool in build_forge_tool_api_tools(
                self.forge_tool_client,
                invocation_ids=self.forge_tool_invocation_ids,
                coordinator=self.forge_task_coordinator,
            ):
                self.tools.register(tool)

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if not self._forge_reconciled and self.forge_task_coordinator is not None:
            await self.forge_task_coordinator.reconcile_nonterminal()
            self._forge_reconciled = True
        if self.experience is not None:
            await self.experience.start()
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from PhyAgentOS.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))
        if tool := self.tools.get("forge_task_create"):
            tool.set_context(session_key or f"{channel}:{chat_id}")
        if tool := self.tools.get("activate_skill"):
            tool.set_context(session_key or f"{channel}:{chat_id}")

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        experience_session_key: str | None = None,
        runtime: RuntimeSelection | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        # Pin the entire turn, including retries and tool iterations, to one
        # selection. Session commands only replace the next turn's selection.
        provider = runtime.provider if runtime else self.provider
        model = runtime.model if runtime else self.model

        while iteration < self.max_iterations:
            iteration += 1

            tool_defs = self.tools.get_definitions()

            response = await provider.chat_with_retry(
                messages=messages,
                tools=tool_defs,
                model=model,
            )

            if response.has_tool_calls:
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    tc.to_openai_tool_call()
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    if experience_session_key is not None:
                        self.skill_activation.record_tool(
                            experience_session_key, tool_call.name, tool_call.arguments
                        )
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            cmd = msg.content.strip().lower()
            if cmd == "/stop":
                await self._handle_stop(msg)
            elif cmd == "/restart":
                await self._handle_restart(msg)
            elif response := self._runtime_command(msg):
                await self.bus.publish_outbound(response)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        forge_cancelled = 0
        if self.forge_task_coordinator is not None:
            record = self.forge_task_coordinator.store.active()
            if record is not None and record.origin_session_key == msg.session_key:
                await self.forge_task_coordinator.cancel_task(
                    record.task_id, reason="user_stop"
                )
                forge_cancelled = 1
        local_stopped = cancelled + sub_cancelled
        if forge_cancelled:
            content = (
                f"Stopped {local_stopped} local task(s); requested cancellation for "
                f"{forge_cancelled} Forge AgentTask(s). Physical Action stop is not proven "
                "until Gateway status/result becomes terminal."
            )
        elif local_stopped:
            content = f"Stopped {local_stopped} task(s)."
        else:
            content = "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        ))

        async def _do_restart():
            await asyncio.sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        asyncio.create_task(_do_restart())

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    def _runtime_command(
        self, msg: InboundMessage, session_key: str | None = None,
    ) -> OutboundMessage | None:
        """Handle session settings without interrupting or waiting for active tasks."""
        if msg.channel == "system" or self.session_runtimes is None:
            return None
        try:
            content = self.session_runtimes.command(session_key or msg.session_key, msg.content)
        except ProviderError as exc:
            content = str(exc)
        if content is None:
            return None
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None
        if self.forge_tool_client is not None:
            await self.forge_tool_client.close()

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        if self.experience is not None:
            self.experience.stop()
        if (
            self.forge_task_coordinator is not None
            and self.forge_task_coordinator.verifier is not None
        ):
            self.forge_task_coordinator.verifier.stop()
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        command_response = self._runtime_command(msg, session_key)
        if command_response is not None:
            return command_response
        key = session_key or msg.session_key
        runtime = (
            self.session_runtimes.get(key)
            if self.session_runtimes is not None and msg.channel != "system"
            else None
        )
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = msg.session_key_override or f"{channel}:{chat_id}"
            self.skill_activation.begin_turn(key, msg.content)
            task_ref = msg.metadata.get("agent_task_id") or msg.metadata.get(
                "forge_session_id"
            )
            if self.experience is not None and task_ref:
                self.experience.schedule_forge_completion(
                    str(task_ref)
                )
            session = self.sessions.get_or_create(key)
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)
            self._set_tool_context(
                channel,
                chat_id,
                msg.metadata.get("message_id"),
                key,
            )
            history = session.get_history(max_messages=0)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            final_content, _, all_msgs = await self._run_agent_loop(
                messages, experience_session_key=key
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        self.skill_activation.begin_turn(key, msg.content)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            try:
                if not await self.memory_consolidator.archive_unconsolidated(session):
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Memory archival failed, session not cleared. Please try again.",
                    )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            lines = [
                "🍞 PhyAgentOS commands:",
                "/new — Start a new conversation",
                "/stop — Stop the current task",
                "/restart — Restart the bot",
                "/provider [list|<provider>] — Show or switch this session's provider",
                "/model [list|<model-id>] — Show or switch this session's model",
                "/effort [list|<level>|none] — Show supported levels or set this session's reasoning effort",
                "/status — Show session, provider, model, effort and endpoint",
                "/provider reset — Clear session overrides and use startup settings",
                "/help — Show available commands",
            ]
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content="\n".join(lines),
            )
        await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        self._set_tool_context(
            msg.channel,
            msg.chat_id,
            msg.metadata.get("message_id"),
            key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            experience_session_key=key,
            runtime=runtime,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)
        await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=msg.metadata or {},
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._MESSAGE_CONTEXT_TAG):
                    # Strip the message-metadata prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._MESSAGE_CONTEXT_TAG):
                            continue  # Strip message metadata from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""

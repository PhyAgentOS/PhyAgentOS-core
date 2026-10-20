"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from collections.abc import AsyncIterator, MutableSet
from contextlib import AsyncExitStack, suppress
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
from PhyAgentOS.bus.agent_events import AgentEvent, AgentEventType
from PhyAgentOS.bus.events import InboundMessage, OutboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.embodiment_registry import EmbodimentRegistry
from PhyAgentOS.providers.base import LLMCallError, LLMProvider, StreamEvent
from PhyAgentOS.providers.providers_manager import ProvidersManager
from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    RuntimeSelection,
    SessionRuntimes,
)
from PhyAgentOS.session.manager import Session, SessionManager
from PhyAgentOS.utils.helpers import estimate_message_tokens, estimate_prompt_tokens_chain

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
        interaction = self.forge_task_coordinator.interaction if self.forge_task_coordinator else None
        self.tools = ToolRegistry(policy=interaction)
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
            tool_policy=interaction,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._forge_reconciled = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._cancel_results: dict[str, tuple[int, int]] = {}
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
        # ImageTool works with any provider: leaf providers ignore the mode
        # hint (single-model deployments serve multimodal with the same
        # model), ProvidersManager routes it to a mode-specific provider.
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
            interaction = self.forge_task_coordinator.interaction
            if interaction.supervisor is not None and interaction.supervisor.notify is None:
                async def notify_interaction(task_id, summary):
                    task = self.forge_task_coordinator.get_task(task_id)
                    origin = task.origin_session_key or "cli:direct"
                    channel, _, chat_id = origin.partition(":")
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=channel, chat_id=chat_id or "direct",
                        content="Interaction task: " + json.dumps(summary, ensure_ascii=False),
                        metadata={"interaction_task_id": task_id}))
                interaction.supervisor.notify = notify_interaction
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
        turn_id: str = "",
    ) -> None:
        """Update context for all tools that need routing info."""
        key = session_key or f"{channel}:{chat_id}"
        if tool := self.tools.get("message"):
            if hasattr(tool, "set_context"):
                tool.set_context(
                    channel, chat_id, message_id, session_key=key, turn_id=turn_id
                )
        if tool := self.tools.get("spawn"):
            if hasattr(tool, "set_context"):
                tool.set_context(channel, chat_id, session_key=key, turn_id=turn_id)
        if tool := self.tools.get("cron"):
            if hasattr(tool, "set_context"):
                tool.set_context(channel, chat_id)
        if tool := self.tools.get("forge_task_create"):
            tool.set_context(key)
        if tool := self.tools.get("activate_skill"):
            tool.set_context(key, turn_id)

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

    def _input_budget_error(
        self,
        provider: LLMProvider,
        model: str,
        messages: list[dict],
        tool_defs: list[dict],
    ) -> str | None:
        """Return a failure message when the prompt cannot fit the context window."""
        window = self.context_window_tokens
        if window <= 0:
            return None
        estimated, source = estimate_prompt_tokens_chain(
            provider, model, messages, tool_defs
        )
        if estimated <= 0:
            estimated = sum(estimate_message_tokens(message) for message in messages)
            source = "message_estimate"
        if estimated <= 0:
            return None
        generation = getattr(provider, "generation", None) or getattr(
            self.provider, "generation", None
        )
        reserve = int(getattr(generation, "max_tokens", 0) or 0) or 4096
        if estimated + reserve <= window:
            return None
        return (
            f"上下文超出预算: estimated prompt tokens {estimated} ({source}) + "
            f"reserved output tokens {reserve} > context window {window}"
        )

    async def _chat_stream_events(
        self,
        provider: LLMProvider,
        messages: list[dict],
        tool_defs: list[dict],
        model: str,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one completion, replaying ``chat_with_retry`` when unsupported."""
        stream = getattr(provider, "chat_stream", None)
        if callable(stream):
            # Match chat_with_retry's generation defaults: native stream
            # implementations take explicit parameters.
            generation = getattr(provider, "generation", None)
            settings = {}
            if generation is not None:
                settings = {
                    "max_tokens": generation.max_tokens,
                    "temperature": generation.temperature,
                    "reasoning_effort": generation.reasoning_effort,
                }
            async for event in stream(
                messages=messages, tools=tool_defs, model=model, **settings
            ):
                yield event
            return

        response = await provider.chat_with_retry(
            messages=messages,
            tools=tool_defs,
            model=model,
        )
        if response.finish_reason == "error":
            yield StreamEvent(
                type="error",
                content=response.content,
                finish_reason="error",
            )
            return
        if response.content:
            yield StreamEvent(type="delta", content=response.content)
        for tool_call in response.tool_calls:
            yield StreamEvent(type="tool_call", tool_call=tool_call)
        yield StreamEvent(
            type="done",
            finish_reason=response.finish_reason,
            usage=dict(response.usage),
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )

    @staticmethod
    def _log_usage(usage: dict[str, int], turn_id: str) -> None:
        """Record provider-reported usage without touching credentials."""
        logger.info(
            "LLM usage turn={} prompt={} completion={} total={}",
            turn_id or "-",
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
        )

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        experience_session_key: str | None = None,
        runtime: RuntimeSelection | None = None,
        on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
        turn_id: str = "",
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop, streaming deltas and tool progress."""
        messages = initial_messages
        iteration = 0
        final_content = None
        finish_reason = "stop"
        tools_used: list[str] = []
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        # Pin the entire turn, including retries and tool iterations, to one
        # selection. Session commands only replace the next turn's selection.
        provider = runtime.provider if runtime else self.provider
        model = runtime.model if runtime else self.model

        async def emit(
            event_type: AgentEventType,
            content: str | None = None,
            **kwargs,
        ) -> None:
            if on_event is None:
                return
            await on_event(AgentEvent(
                type=event_type,
                content=content,
                turn_id=turn_id or None,
                **kwargs,
            ))

        try:
            await emit(AgentEventType.TURN_STARTED)

            while iteration < self.max_iterations:
                iteration += 1

                tool_defs = self.tools.get_definitions()

                budget_error = self._input_budget_error(
                    provider, model, messages, tool_defs
                )
                if budget_error is not None:
                    logger.error("Input budget exceeded: {}", budget_error)
                    await emit(AgentEventType.ERROR, budget_error)
                    raise LLMCallError(budget_error, messages=messages)

                deltas: list[str] = []
                tool_calls = []
                stream_usage: dict[str, int] = {}
                stream_error: str | None = None
                reasoning_content: str | None = None
                thinking_blocks: list[dict] | None = None
                finish_reason = "stop"

                async for event in self._chat_stream_events(
                    provider, messages, tool_defs, model
                ):
                    if event.type == "delta":
                        if event.content:
                            deltas.append(event.content)
                            await emit(AgentEventType.TEXT_DELTA, event.content)
                    elif event.type == "tool_call":
                        if event.tool_call is not None:
                            tool_calls.append(event.tool_call)
                    elif event.type == "usage":
                        if event.usage:
                            stream_usage = dict(event.usage)
                    elif event.type == "error":
                        stream_error = event.content or "provider returned no detail"
                        break
                    elif event.type == "done":
                        finish_reason = event.finish_reason or "stop"
                        if event.usage:
                            stream_usage = dict(event.usage)
                        reasoning_content = event.reasoning_content
                        thinking_blocks = event.thinking_blocks
                        break

                if stream_error is not None:
                    # A failed call is not an answer. Surface it as an error event
                    # and let the caller persist the partial turn before replying.
                    logger.error(
                        "LLM call failed, abandoning turn: {}", stream_error[:200]
                    )
                    await emit(AgentEventType.ERROR, stream_error)
                    raise LLMCallError(stream_error, messages=messages)

                if stream_usage:
                    self._log_usage(stream_usage, turn_id)
                    for key in total_usage:
                        total_usage[key] += int(stream_usage.get(key, 0))

                response_content = "".join(deltas) or None

                if tool_calls:
                    if on_progress:
                        thought = self._strip_think(response_content)
                        if thought:
                            await on_progress(thought)
                        await on_progress(self._tool_hint(tool_calls), tool_hint=True)

                    tool_call_dicts = [tc.to_openai_tool_call() for tc in tool_calls]
                    messages = self.context.add_assistant_message(
                        messages, response_content, tool_call_dicts,
                        reasoning_content=reasoning_content,
                        thinking_blocks=thinking_blocks,
                    )

                    for tool_call in tool_calls:
                        tools_used.append(tool_call.name)
                        if experience_session_key is not None:
                            self.skill_activation.record_tool(
                                experience_session_key, tool_call.name, tool_call.arguments
                            )
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        hint = self._tool_hint([tool_call])
                        logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                        await emit(
                            AgentEventType.TOOL_PROGRESS,
                            hint,
                            tool_name=tool_call.name,
                            metadata={"phase": "start"},
                        )
                        result = await self.tools.execute(
                            tool_call.name, tool_call.arguments
                        )
                        await emit(
                            AgentEventType.TOOL_PROGRESS,
                            f"{hint} (done)",
                            tool_name=tool_call.name,
                            metadata={"phase": "done"},
                        )
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )
                else:
                    clean = self._strip_think(response_content)
                    # Error responses never reach here: they raise above, before
                    # anything is appended or persisted (#1303).
                    messages = self.context.add_assistant_message(
                        messages, clean,
                        reasoning_content=reasoning_content,
                        thinking_blocks=thinking_blocks,
                    )
                    final_content = clean
                    break

            if final_content is None and iteration >= self.max_iterations:
                logger.warning("Max iterations ({}) reached", self.max_iterations)
                finish_reason = "max_iterations"
                final_content = (
                    f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                    "without completing the task. You can try breaking the task into smaller steps."
                )

            await emit(
                AgentEventType.TURN_DONE,
                finish_reason=finish_reason,
                usage=dict(total_usage),
            )
            return final_content, tools_used, messages
        except asyncio.CancelledError:
            with suppress(Exception):
                await emit(AgentEventType.CANCELLED)
            raise

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        if self.forge_task_coordinator is not None:
            self.forge_task_coordinator.interaction.long_lived = True
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
                self._register_active_task(msg.session_key, task)

    async def _cancel_session_side_effects(self, session_key: str) -> tuple[int, int]:
        """Cancel subagents and the Forge AgentTask bound to a session.

        This never touches ``_active_tasks``, so a turn can call it from its
        own ``CancelledError`` handler without cancelling itself recursively.
        """
        sub_cancelled = await self.subagents.cancel_by_session(session_key)
        forge_cancelled = 0
        if self.forge_task_coordinator is not None:
            record = self.forge_task_coordinator.store.active()
            if record is not None and record.origin_session_key == session_key:
                await self.forge_task_coordinator.cancel_task(
                    record.task_id, reason="user_stop"
                )
                forge_cancelled = 1
        self._cancel_results[session_key] = (sub_cancelled, forge_cancelled)
        return sub_cancelled, forge_cancelled

    async def _handle_stop(self, msg: InboundMessage, *, notify: bool = True) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if cancelled:
            sub_cancelled, forge_cancelled = self._cancel_results.pop(
                msg.session_key, (0, 0)
            )
        else:
            sub_cancelled, forge_cancelled = await self._cancel_session_side_effects(
                msg.session_key
            )
        local_stopped = cancelled + sub_cancelled
        if not notify:
            return
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
            await self.close_mcp()
            await asyncio.sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        asyncio.create_task(_do_restart())

    def _register_active_task(self, session_key: str, task: asyncio.Task) -> None:
        """Track a running turn so /stop can find and cancel it."""
        self._active_tasks.setdefault(session_key, []).append(task)

        def _cleanup(done: asyncio.Task) -> None:
            tasks = self._active_tasks.get(session_key)
            if tasks is None:
                return
            if done in tasks:
                tasks.remove(done)
            if not tasks:
                self._active_tasks.pop(session_key, None)

        task.add_done_callback(_cleanup)

    async def _dispatch(
        self,
        msg: InboundMessage,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        publish: bool = True,
        on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message under the global lock.

        ``publish=False`` returns the reply to the caller without putting it on
        the outbound queue; ``process_direct`` uses that so cron/heartbeat do
        not deliver the same reply twice. Streaming events go to ``on_event``
        when given, and to the bus for CLI publishers.
        """
        stream_to_bus = publish and msg.channel == "cli"
        deferred_terminal: list[AgentEvent] = []

        async def _send_event(event: AgentEvent) -> None:
            if on_event is not None:
                await on_event(event)
            if stream_to_bus:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=event.content or "",
                    metadata=dict(msg.metadata or {}, _progress=True),
                    event=event,
                ))

        async def _publish_event(event: AgentEvent) -> None:
            # Terminal events wait until the reply itself is on the queue, so a
            # CLI that ends its turn on turn_done never races the reply.
            if event.type in (
                AgentEventType.TURN_DONE,
                AgentEventType.ERROR,
                AgentEventType.CANCELLED,
            ):
                deferred_terminal.append(event)
            else:
                await _send_event(event)

        async def _flush_terminal() -> None:
            while deferred_terminal:
                await _send_event(deferred_terminal.pop(0))

        event_callback = _publish_event if (on_event is not None or stream_to_bus) else None

        try:
            async with self._processing_lock:
                response = await self._process_message(
                    msg, on_progress=on_progress, on_event=event_callback
                )
                if publish:
                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli" and event_callback is None:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                await _flush_terminal()
                return response
        except asyncio.CancelledError:
            logger.info("Task cancelled for session {}", msg.session_key)
            try:
                await self._cancel_session_side_effects(msg.session_key)
            except Exception as exc:
                logger.warning(
                    "Session side-effect cancellation failed for {}: {}",
                    msg.session_key,
                    type(exc).__name__,
                )
            with suppress(Exception):
                await _flush_terminal()
            raise
        except LLMCallError as exc:
            # Defensive net: _process_message normally absorbs these into a
            # failure reply (persisting the partial turn first). If a future
            # path lets one escape, still answer instead of crashing the
            # dispatcher — callers like cron and heartbeat have no handler.
            logger.error("LLM call failed for session {}: {}", msg.session_key, exc.detail)
            reply = OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"LLM call failed: {exc.detail}",
                metadata=dict(msg.metadata or {}),
            )
            if publish:
                await self.bus.publish_outbound(reply)
            with suppress(Exception):
                await _flush_terminal()
            return reply
        except Exception:
            logger.exception("Error processing message for session {}", msg.session_key)
            reply = OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Sorry, I encountered an error.",
            )
            if publish:
                await self.bus.publish_outbound(reply)
            with suppress(Exception):
                await _flush_terminal()
            return reply

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
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
            metadata=dict(msg.metadata or {}),
        )

    async def close_mcp(self) -> None:
        """Persist interaction shutdown before closing its transport."""
        if self.forge_task_coordinator is not None:
            supervisor = self.forge_task_coordinator.interaction.supervisor
            if supervisor is not None:
                await supervisor.shutdown()
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
        if self.forge_task_coordinator is not None:
            self.forge_task_coordinator.interaction.closing = True
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
        on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        if not msg.turn_id:
            msg.turn_id = uuid.uuid4().hex
        turn_id = msg.turn_id
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
            key = session_key or msg.session_key_override or f"{channel}:{chat_id}"
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
                turn_id,
            )
            if message_tool := self.tools.get("message"):
                if isinstance(message_tool, MessageTool):
                    message_tool.start_turn()
            history = session.get_history(max_messages=0)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            try:
                final_content, _, all_msgs = await self._run_agent_loop(
                    messages,
                    experience_session_key=key,
                    on_event=on_event,
                    turn_id=turn_id,
                )
            except LLMCallError as exc:
                return self._abort_turn(
                    session, history, exc, channel, chat_id, metadata=msg.metadata
                )
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)
            if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
                return None
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
            turn_id,
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

        try:
            # Streaming events supersede the legacy progress callbacks; keep
            # the old path for callers that only pass on_progress.
            progress_callback = on_progress or (
                None if on_event is not None else _bus_progress
            )
            final_content, _, all_msgs = await self._run_agent_loop(
                initial_messages,
                on_progress=progress_callback,
                experience_session_key=key,
                runtime=runtime,
                on_event=on_event,
                turn_id=turn_id,
            )
        except LLMCallError as exc:
            return self._abort_turn(
                session, history, exc, msg.channel, msg.chat_id, metadata=msg.metadata
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

    def _abort_turn(
        self,
        session: Session,
        history: list[dict],
        exc: LLMCallError,
        channel: str,
        chat_id: str,
        *,
        metadata: dict | None = None,
    ) -> OutboundMessage:
        """Persist the partial turn and reply with the failure.

        Used when the agent loop cannot obtain a completion. The user's
        message and any completed tool exchanges still belong in the session
        (a retry must not start from scratch), while the failure itself is
        kept out of the history. The reply names the failure so a caller
        that only sees the outbound message (the CLI, a bench harness
        reading the session log) can tell a provider error apart from the
        agent having answered.
        """
        logger.error("LLM call failed for session {}: {}", session.key, exc.detail[:200])
        self._save_turn(session, exc.messages, 1 + len(history))
        self.sessions.save(session)
        return OutboundMessage(
            channel=channel, chat_id=chat_id, content=f"LLM call failed: {exc.detail}",
            metadata=dict(metadata or {}),
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
        channel: str | None = None,
        chat_id: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI, cron or heartbeat usage).

        Runs through ``_dispatch`` so the turn takes the processing lock and is
        tracked in ``_active_tasks`` (and therefore cancellable by ``/stop``).
        The reply is returned instead of published, because these callers
        deliver it themselves.
        """
        if self.forge_task_coordinator is not None:
            self.forge_task_coordinator.interaction.long_lived = self._running
        await self._connect_mcp()
        if channel is None or chat_id is None:
            derived_channel, _, derived_chat = session_key.partition(":")
            if not derived_chat:
                derived_channel, derived_chat = "cli", "direct"
            channel = channel if channel is not None else derived_channel
            chat_id = chat_id if chat_id is not None else derived_chat
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            session_key_override=session_key,
        )
        task = asyncio.create_task(
            self._dispatch(
                msg,
                on_progress=on_progress,
                publish=False,
                on_event=on_event,
            )
        )
        self._register_active_task(msg.session_key, task)
        try:
            response = await task
        except asyncio.CancelledError:
            # Cancelling this outer task does not cancel the dispatch task it
            # started. Cancel it explicitly so the turn runs its CancelledError
            # branch (side effects, cancelled event) before we leave.
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            logger.info("Direct turn cancelled for session {}", msg.session_key)
            return ""
        return response.content if response else ""

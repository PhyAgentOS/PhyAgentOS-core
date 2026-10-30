"""The direct Agent command: dependency construction and process ownership.

Terminal interaction and rendering are separate injected collaborators. The
command has no dependency on commands.py or AgentLoop; the transitional runner
is passed in at registration and imported only when explicitly selected.

The Core session rebuilds the model-facing assembly for every turn from the
selection in force when that turn starts. The durable request snapshot and the
model call therefore always name the same provider model and reasoning effort.
The store, Forge coordinator/client, and Skill activation scope stay shared
across assemblies; only the loop drivers, their provider adapters, and the
runtimes that own them are replaced. Same-session submits run in registration
order because the session owns that ordering; the core runtime keeps its own
per-session serialization, which then sees only one accepted turn at a time.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console

from PhyAgentOS.agent.bootstrap import build_forge_components
from PhyAgentOS.agent.capabilities.agent_mode_capability import AgentModeCapability
from PhyAgentOS.agent.capabilities.cron_capability import CronCapability
from PhyAgentOS.agent.capabilities.forge_capability import ForgeCapability
from PhyAgentOS.agent.capabilities.general_tools_capability import GeneralToolsCapability
from PhyAgentOS.agent.capabilities.image_capability import ImageCapability
from PhyAgentOS.agent.capabilities.mcp_capability import MCPCapability
from PhyAgentOS.agent.capabilities.skill_access_capability import SkillAccessCapability
from PhyAgentOS.agent.capabilities.spawn_capability import SpawnCapability
from PhyAgentOS.agent.composition import AgentAssembly, compose_agent
from PhyAgentOS.agent.core import SQLiteSessionStore, TurnRequest, TurnStatus
from PhyAgentOS.agent.core.events import EventType, reverted_turn_ids
from PhyAgentOS.agent.core.host import AgentHostCloseError
from PhyAgentOS.agent.core.prompt import (
    project_forge_tool_results,
    project_skill_documents,
)
from PhyAgentOS.agent.core.session import CHECKPOINT_SUMMARY_NOTICE
from PhyAgentOS.agent.services.context_compaction import compact_session
from PhyAgentOS.agent.services.session_catalog import (
    SessionCatalog,
    SessionInfo,
    SessionSelection,
)
from PhyAgentOS.agent.services.skill_documents import SkillDocumentLoader
from PhyAgentOS.agent.services.turn_activation import TurnSkillActivationManager
from PhyAgentOS.agent.services.usage import (
    ContextSnapshot,
    estimate_context,
    estimate_context_from_conversation,
)
from PhyAgentOS.bus.events import OutboundMessage
from PhyAgentOS.config.loader import load_config, set_config_path
from PhyAgentOS.config.paths import get_cron_dir
from PhyAgentOS.cron.service import CronService
from PhyAgentOS.providers.base import StreamEvent
from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    RuntimeSelection,
    SessionRuntimes,
)
from PhyAgentOS.utils.helpers import estimate_prompt_tokens_chain, warm_token_encoder


def load_agent_config(config_path: str | None, workspace: str | None):
    path = Path(config_path).expanduser().resolve() if config_path else None
    if path is not None:
        if not path.is_file():
            raise ValueError(f"Config file not found: {path}")
        set_config_path(path)
    config = load_config(path)
    if workspace:
        if config.is_fleet_mode:
            config.embodiments.shared_workspace = workspace
        else:
            config.agents.defaults.workspace = workspace
    return config


def _instructions(config) -> str:
    parts = [
        "You are PhyAgentOS. Report only observed results; never invent ids, state or "
        "success. Physical execution goes only through the Forge Tool API: activate a "
        "primary Skill in the current turn, then create an AgentTask with its "
        "activation_id. Ordinary conversation needs no task. Accepted/running is not "
        "success; poll an admitted action to a terminal result before finalizing. Unknown "
        "remote state is never resent automatically, and local cancellation is not "
        "evidence that remote work stopped.",
        "Query Forge context, ToolSpecs and status instead of guessing. An unresolved "
        "remote blocker is authoritative across sessions: keep its task/invocation "
        "identity (None when unconfirmed), do not resend, and do not cancel another "
        "session's task to bypass it. Omit timeout_ms when the Gateway limit is unknown. "
        "Respect the user's verification requirement and explain missing evidence; "
        "execution facts alone do not prove the user's goal.",
    ]
    # Workspace instructions are data for the model; they cannot affect runtime
    # authorization or move the Forge execution path out of registered tools.
    for name in ("AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "TOOLS.md", "SKILLS.md"):
        path = config.workspace_path / name
        if path.is_file():
            parts.append(f"## Workspace {name}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


class _DisplayStore:
    """Persist first, then notify the terminal.

    The store owns the durable truth. Rendering is a view over an event that
    is already saved, so a display failure must never be reported as a storage
    failure or fail the turn: the exception is swallowed here and reported once
    per session, without its message (which may carry untrusted event text).
    """

    def __init__(self, store, output):
        self.store, self.output = store, output
        self._display_failure_reported = False

    async def append(self, event):
        saved = await self.store.append(event)
        try:
            self.output.event(saved)
        except Exception:
            if not self._display_failure_reported:
                self._display_failure_reported = True
                try:
                    self.output.error(
                        "A persisted event could not be shown in the terminal; "
                        "the session log is unchanged."
                    )
                except Exception:
                    pass
        return saved

    def __getattr__(self, name):
        return getattr(self.store, name)


def _effective_window(config, selection) -> tuple[int | None, str]:
    """One window definition for guard, status and compaction.

    The provider service composes model metadata with the configured cap. When
    that answer and its source are present, trust them. Older selections and
    external embeddings may lack the source (``source=None``): then an explicit
    configured cap and a model capacity are combined with ``min`` (the old
    guard behaviour) and the source names the winner; a lone capacity is
    treated as model metadata.
    """
    capacity = getattr(selection, "context_window_tokens", None)
    source = getattr(selection, "context_window_source", None)
    if capacity:
        capacity = int(capacity)
        if source in {"model", "configured", "fallback"}:
            return capacity, source
        defaults = getattr(getattr(config, "agents", None), "defaults", None)
        configured = getattr(defaults, "context_window_tokens", None)
        if configured:
            configured = int(configured)
            if configured <= capacity:
                return configured, "configured"
            return capacity, "model"
        return capacity, "model"
    defaults = getattr(getattr(config, "agents", None), "defaults", None)
    configured = getattr(defaults, "context_window_tokens", None)
    if configured:
        return int(configured), "configured"
    model_window = getattr(selection, "model_context_window_tokens", None)
    if model_window:
        return int(model_window), "model"
    return None, "unknown"


class _SelectedProvider:
    """Forward a turn to its frozen selection; leave SDK retries to the provider.

    The adapter builds the generation arguments from the model request. This
    wrapper never substitutes the selection for them: if the request says a
    different model or effort, that is the call that is made, so the persisted
    ``request.prepared`` snapshot cannot disagree with the actual request.

    Both the blocking and streaming paths use the same prompt budget guard.
    A provider without ``chat_stream`` is called once through
    ``chat_with_retry``; the terminal says so once instead of retrying a
    connection that never produced a stream.
    """

    def __init__(self, selection, context_window, max_tokens, *, notice=None, window_source="configured"):
        self.selection = selection
        self.context_window = context_window
        self.window_source = window_source
        self.max_tokens = max_tokens
        self._notice = notice
        self._fallback_notified = False
        # A wrapper must not turn a non-native provider into a native one.
        # A provider that only has the inherited ``chat_stream`` fallback is
        # non-native; an older wrapper without the flag and with a real
        # ``chat_stream`` is treated as native for compatibility.
        underlying = selection.provider
        self.supports_native_stream = bool(
            callable(getattr(underlying, "chat_stream", None))
            and bool(getattr(underlying, "supports_native_stream", True))
        )

    def _check_budget(self, *, messages, tools, model) -> None:
        if not self.context_window:
            return
        estimated, _ = estimate_prompt_tokens_chain(
            self.selection.provider, model, messages, tools
        )
        if estimated and estimated + self.max_tokens > self.context_window:
            raise ProviderError(
                f"Context limit reached (estimated prompt {estimated}, reply budget "
                f"{self.max_tokens}, limit {self.context_window}). Use /compact to "
                "summarize older history, or /new to start a new session. No further "
                "model call was sent."
            )

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        model = kwargs.get("model") or self.selection.model
        self._check_budget(messages=messages, tools=tools, model=model)
        return await self.selection.provider.chat_with_retry(
            messages=messages, tools=tools, **kwargs
        )

    async def chat_stream(self, *, messages, tools=None, **kwargs):
        """Forward one stream with the same budget guard as the blocking path."""
        selected = self.selection
        model = kwargs.get("model") or selected.model
        self._check_budget(messages=messages, tools=tools, model=model)
        stream = getattr(selected.provider, "chat_stream", None)
        if self.supports_native_stream and callable(stream):
            events = stream(messages=messages, tools=tools, **kwargs)
            try:
                async for event in events:
                    yield event
            finally:
                # An outer cancellation can stop ``async for`` without
                # exhausting the supplier; close it explicitly.
                close = getattr(events, "aclose", None)
                if close is None:
                    close = getattr(events, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
            return

        # Called directly (the Core adapter chooses its own fallback path when
        # supports_native_stream is False). This is a single completed answer,
        # never a replayed fake stream, so it emits no delta.
        if self._notice is not None and not self._fallback_notified:
            self._fallback_notified = True
            try:
                self._notice(
                    "This provider has no native streaming; the answer is "
                    "shown once it is complete."
                )
            except Exception:
                pass
        response = await selected.provider.chat_with_retry(
            messages=messages, tools=tools, **kwargs
        )
        finish_reason = getattr(response, "finish_reason", None)
        content = getattr(response, "content", None)
        if finish_reason == "error" or not finish_reason:
            yield StreamEvent(
                type="error",
                content=content if isinstance(content, str) else "provider error",
                finish_reason="error",
            )
            return
        for call in getattr(response, "tool_calls", None) or ():
            yield StreamEvent(type="tool_call", tool_call=call)
        yield StreamEvent(
            type="done",
            finish_reason=finish_reason,
            usage=dict(getattr(response, "usage", {}) or {}),
            reasoning_content=getattr(response, "reasoning_content", None),
            thinking_blocks=getattr(response, "thinking_blocks", None),
        )


@dataclass(frozen=True, slots=True)
class _AssemblyState:
    selection: RuntimeSelection
    assembly: AgentAssembly


@dataclass(slots=True)
class _SessionSlot:
    """Same-session ordering plus the assembly registered for its last turn.

    ``tail`` is the completion marker of the submit that entered registration
    last. A submit waits for it before it may build and run its own turn, so
    same-session turns run in the order they entered registration. ``abort`` is
    set once by :meth:`CoreAgentSession.aclose`: a submit still waiting for its
    turn then leaves the queue instead of starting a turn during shutdown.
    ``current`` is the assembly registered for the session's running (or most
    recent) turn; it is the only target of :meth:`CoreAgentSession.cancel`.
    """

    tail: "asyncio.Future[None] | None" = None
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    current: _AssemblyState | None = None
    # The summary task registered for a manual compaction. It is the only
    # target of ``cancel``/``aclose`` while no model turn is running.
    compact: "asyncio.Task | None" = None


class CoreAgentSession:
    """Run Core turns over shared state, one assembly per turn, in session order.

    ``SessionRuntimes`` is read when a turn starts, not when its submit was
    queued, so a selection changed between turns applies from the next turn on
    and a queued turn never picks up an older selection. Each turn builds its
    own assembly and retires its runtime when the turn ends, so the durable
    request snapshot and the model call always name the same provider model and
    reasoning effort. The store, Forge coordinator/client, and Skill activation
    manager stay shared: consecutive turns see the same history, tasks, and
    turn-scoped activations, and building a turn reuses those shared owners
    instead of creating a second Forge or Skill capability.

    Same-session submits are serialized by registration order: a submit that
    enters while another turn is running waits for that turn to settle and only
    then builds and submits its own turn. Sessions are independent, so turns of
    different sessions still run concurrently.

    ``cancel`` never takes the ordering gate. It reads the assembly registered
    for the session under a short lock and asks that assembly's host to cancel,
    so it waits for that turn's local settlement (bounded by the token
    cancelling the model call) and never for a queued submit or for the next
    turn. Only the running turn is affected: the queued submit starts a fresh
    runtime that never saw this cancel, so a cancellation can never be
    inherited by the next turn. When the cancel arrives between turns there is
    no running turn to interrupt; the request goes to the last registered
    assembly (or the seed before the first turn) so capabilities still receive
    it.

    Shutdown rejects new submits, tells every queued submit to leave before it
    starts a turn, stops the runtimes that may still own a turn, waits for the
    submits that were already inside this session to return, notifies each
    accepted session once, closes the canonical capabilities once, then closes
    the store. Per-turn capability objects are views over the same shared
    owners, so they are not closed a second time. A turn whose build or submit
    raises reaches only its own caller: the queue continues with the next
    submit and that turn's runtime is retired like any other.
    """

    def __init__(
        self,
        seed: AgentAssembly,
        selection: RuntimeSelection,
        *,
        build,
        store,
        sessions: SessionRuntimes,
        output,
        raw_store=None,
        compact=None,
    ):
        self._seed = seed
        self._seed_selection = selection
        self._build = build
        self._store = store
        self.raw_store = raw_store if raw_store is not None else getattr(store, "store", None)
        self.sessions = sessions
        self.output = output
        self._compact = compact
        # Attached by the composition root; other entries may leave them unset.
        self.catalog: SessionCatalog | None = None
        self.instructions: str = ""
        self.config = None
        self._canonical_capabilities = seed.capabilities
        self._slots: dict[str, _SessionSlot] = {}
        self._accepted_sessions: dict[str, None] = {}
        self._registry_lock = asyncio.Lock()
        self._pending = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def submit(self, request: TurnRequest):
        session_id = request.session_id
        async with self._registry_lock:
            if self._closed:
                raise RuntimeError("agent session is closed")
            slot = self._slots.setdefault(session_id, _SessionSlot())
            previous = slot.tail
            marker: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            slot.tail = marker
            self._pending += 1
            self._idle.clear()
        try:
            # Registration order is execution order: a submit only starts after
            # every submit that entered before it finished its whole turn.
            await self._wait_turn(slot, previous)
            if self._closed:
                raise RuntimeError("agent session is closed")
            # Read the selection when the turn really starts. A model or effort
            # change made while this submit was queued therefore applies to it.
            selection = self.sessions.get(session_id)
            assembly = self._build(selection)
            try:
                state = _AssemblyState(selection, assembly)
                async with self._registry_lock:
                    if self._closed:
                        raise RuntimeError("agent session is closed")
                    # Register as current before ``host.submit`` can suspend, so
                    # a concurrent cancel targets this turn and never the seed
                    # or a retired assembly.
                    slot.current = state
                    self._accepted_sessions.setdefault(session_id, None)
                return await assembly.host.submit(request)
            finally:
                # One assembly serves exactly one turn. Retiring its runtime
                # keeps the next turn independent and leaves the shared store
                # and capabilities open.
                with suppress(Exception):
                    await assembly.runtime.stop()
        finally:
            # Leaving the queue must not itself need an await: a cancelled
            # submit still has to free its slot and wake a waiting shutdown.
            if not marker.done():
                marker.set_result(None)
            self._pending -= 1
            if self._pending == 0:
                self._idle.set()

    @staticmethod
    async def _wait_turn(slot: _SessionSlot, previous) -> None:
        """Wait for the previous same-session submit, or for shutdown to begin."""
        if previous is None or previous.done():
            return
        aborted = asyncio.ensure_future(slot.abort.wait())
        try:
            await asyncio.wait({previous, aborted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            aborted.cancel()
            await asyncio.gather(aborted, return_exceptions=True)

    async def cancel(self, session_id: str) -> None:
        """Cancel the assembly currently registered for this session.

        The registry lock is held only while reading the target, and the
        ordering gate is never taken, so the cancel neither waits for a queued
        submit nor for the turn to produce an answer. Registration cannot
        change between that read and ``host.cancel`` without a new turn
        starting, and a new turn always builds a new runtime, so this call can
        only interrupt the turn that was registered when it was made.
        """
        async with self._registry_lock:
            if self._closed:
                return
            slot = self._slots.get(session_id)
            summary = None if slot is None else slot.compact
            state = None if slot is None else slot.current
            if state is None:
                # No turn has run yet: ask the seed host so capability
                # cancellation still receives the request.
                state = _AssemblyState(self._seed_selection, self._seed)
        if summary is not None and not summary.done():
            # Cancelling the summary task makes the compaction service record
            # an interrupted attempt and re-raise; no other session or queued
            # turn is touched.
            summary.cancel()
            await asyncio.gather(summary, return_exceptions=True)
            return
        await state.assembly.host.cancel(session_id)

    async def undo(self, session_id: str):
        """Revert this session's last turn at a turn boundary.

        A running or queued turn and a manual compaction both refuse the
        request: the store must not select a target while another write for
        this session is in flight. The store owns target selection, the
        reversion marker, the checkpoint check and the original request text;
        a store that cannot undo raises instead of reporting a silent success.
        """
        async with self._registry_lock:
            if self._closed:
                raise RuntimeError("agent session is closed")
            slot = self._slots.get(session_id)
            compacting = None if slot is None else slot.compact
            tail = None if slot is None else slot.tail
        if compacting is not None and not compacting.done():
            raise RuntimeError(
                "context compaction is running for this session; wait for it to "
                "finish before /undo"
            )
        if tail is not None and not tail.done():
            # ``tail`` is done only once that submit settled, so this also
            # covers a queued turn that has not started yet.
            raise RuntimeError(
                "a turn is running or queued for this session; wait for it to "
                "settle before /undo"
            )
        undo_last_turn = getattr(self._store, "undo_last_turn", None)
        if not callable(undo_last_turn):
            raise RuntimeError(
                "this session store does not support undo; nothing was reverted."
            )
        return await undo_last_turn(session_id)

    def model_command(self, session_id: str, text: str) -> str | None:
        """Apply one /provider, /model, /effort or /status command."""
        return self.sessions.command(session_id, text)

    def tool_selection(self, session_id: str) -> RuntimeSelection:
        """Keep auxiliary model calls on the running turn's provider snapshot."""
        slot = self._slots.get(session_id)
        if slot is not None and slot.current is not None:
            return slot.current.selection
        return self.sessions.get(session_id)

    async def compact(self, session_id: str):
        """Manually compact one session at a turn boundary.

        The same ordering gate as ``submit`` is used, so no turn is active and
        no queued turn can start while the summary runs. This never schedules
        itself and never executes a user task. A failure leaves the original
        event log and projection unchanged; only a success writes a checkpoint.
        """
        async with self._registry_lock:
            if self._closed:
                raise RuntimeError("agent session is closed")
            slot = self._slots.setdefault(session_id, _SessionSlot())
            previous = slot.tail
            marker: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            slot.tail = marker
            self._pending += 1
            self._idle.clear()
        task: asyncio.Task | None = None
        try:
            await self._wait_turn(slot, previous)
            if self._closed:
                raise RuntimeError("agent session is closed")
            selection = self.sessions.get(session_id)
            # Register the summary as the session's current local work before
            # it can run. It is cancellable by ``cancel(session_id)`` and by
            # ``aclose`` without touching another session or a queued turn.
            async with self._registry_lock:
                if self._closed:
                    raise RuntimeError("agent session is closed")
                task = asyncio.create_task(self._run_compact(session_id, selection))
                slot.compact = task
            try:
                return await task
            finally:
                if slot.compact is task:
                    slot.compact = None
        finally:
            if not marker.done():
                marker.set_result(None)
            self._pending -= 1
            if self._pending == 0:
                self._idle.set()

    async def _run_compact(self, session_id: str, selection):
        if self._compact is not None:
            return await self._compact(session_id, selection)
        if self.raw_store is None:
            raise RuntimeError(
                "Context compaction is unavailable: the raw session store was not attached."
            )
        defaults = getattr(getattr(self.config, "agents", None), "defaults", None)
        reply_budget = getattr(defaults, "max_tokens", None)
        # Same effective window the request guard and status line use.
        window, _window_source = _effective_window(self.config, selection)

        events = list(await self.raw_store.read_session(session_id))
        system_text = _last_request_system(events) or self.instructions or None
        tools = _last_request_tools(events)

        def token_estimator(messages):
            tokens, _source = estimate_prompt_tokens_chain(
                selection.provider, selection.model, list(messages), None
            )
            return tokens

        def request_token_estimator(messages):
            # Use the same fixed system prompt and visible tools the status
            # line measures; the summary prompt budget above stays free of
            # them because the summary call itself sends neither.
            request_messages = (
                [{"role": "system", "content": system_text}] if system_text else []
            )
            request_messages.extend(dict(message) for message in messages)
            request_messages = _project_request_messages(request_messages)
            tokens, _source = estimate_prompt_tokens_chain(
                selection.provider, selection.model, request_messages, tools
            )
            return tokens

        return await compact_session(
            self.raw_store,
            selection.provider,
            session_id,
            model=selection.model,
            effort=selection.effort,
            provider_name=selection.name,
            max_tokens=reply_budget,
            context_window_tokens=window,
            reserve_tokens=reply_budget,
            token_estimator=token_estimator,
            request_token_estimator=request_token_estimator,
        )

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            async with self._registry_lock:
                self._closed = True
                # Queued submits leave the ordering gate here, before they
                # build an assembly or open a turn, so no turn starts after
                # shutdown began.
                for slot in self._slots.values():
                    slot.abort.set()
                summaries = [
                    slot.compact
                    for slot in self._slots.values()
                    if slot.compact is not None and not slot.compact.done()
                ]
            # A summary blocked on its provider is stopped here, so shutdown
            # never waits for the provider; the compaction service records the
            # interrupted attempt before the task returns.
            for task in summaries:
                task.cancel()
            if summaries:
                await asyncio.gather(*summaries, return_exceptions=True)
            failures: list[tuple[str, Exception]] = []

            # Stop every runtime that may still own a turn: the seed and the
            # assembly registered for each session. Retired runtimes were
            # already stopped when their turn ended, and ``stop`` is
            # idempotent, so a second stop is harmless.
            runtimes = [("seed", self._seed.runtime)]
            runtimes.extend(
                (f"assembly[{state.assembly.host!r}]", state.assembly.runtime)
                for state in self._current_states()
            )
            for label, runtime in runtimes:
                try:
                    await runtime.stop()
                except Exception as exc:  # keep settling the remaining runtimes
                    failures.append((f"{label}.stop", exc))

            # Every submit that entered this session must leave before the
            # store closes; queued ones only raise and do no I/O.
            await self._idle.wait()

            for session_id in self._accepted_sessions:
                for index, capability in enumerate(self._canonical_capabilities):
                    try:
                        await capability.cancel(session_id, None)
                    except Exception as exc:  # keep notifying the others
                        failures.append((f"capability[{index}].cancel", exc))

            for index, capability in enumerate(self._canonical_capabilities):
                try:
                    await capability.aclose()
                except Exception as exc:  # keep closing the others
                    failures.append((f"capability[{index}].aclose", exc))

            try:
                await self._store.aclose()
            except Exception as exc:
                failures.append(("store.aclose", exc))

            if failures:
                raise AgentHostCloseError(failures) from failures[0][1]

    def _current_states(self) -> list[_AssemblyState]:
        """Registered assemblies, deduplicated by runtime identity."""
        seen: dict[int, _AssemblyState] = {}
        for slot in self._slots.values():
            if slot.current is not None:
                seen.setdefault(id(slot.current.assembly.runtime), slot.current)
        return list(seen.values())


def _compose_for_selection(
    selection: RuntimeSelection,
    *,
    config,
    store,
    coordinator,
    client,
    instructions: str,
    skill_capability: SkillAccessCapability,
    forge_capability: ForgeCapability,
    additional_capabilities=(),
    notice=None,
) -> AgentAssembly:
    """Compose Skill, shared tools and entry capabilities, then Forge.

    ``additional_capabilities`` is the only extension point another entry
    (including gateway cron/message capabilities) needs. They become part of the
    canonical capability tuple, so ``host.aclose`` closes them exactly once
    with the rest.
    """
    defaults = config.agents.defaults
    window, window_source = _effective_window(config, selection)
    provider = _SelectedProvider(
        selection, window, defaults.max_tokens, notice=notice, window_source=window_source
    )
    return compose_agent(
        provider=provider,
        store=store,
        coordinator=coordinator,
        client=client,
        system_instructions=instructions,
        model=selection.model,
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=selection.effort,
        max_steps=defaults.max_tool_iterations,
        additional_capabilities=(
            skill_capability,
            *(
                item.for_selection(selection) if isinstance(item, AgentModeCapability) else item
                for item in additional_capabilities
            ),
        ),
        forge_capability=forge_capability,
    )


@asynccontextmanager
async def open_core_session(
    config,
    *,
    provider_name=None,
    model=None,
    effort=None,
    output,
    additional_capabilities=(),
    routes=None,
    send=None,
    service=None,
    selected=None,
):
    """Open one Core session over the workspace database.

    ``service``/``selected`` let another entry (the gateway background
    runtime) reuse a provider it already resolved instead of resolving a
    second time. Shared general, image, MCP and spawn capabilities are built
    once; ``additional_capabilities`` follows them before Forge. Canonical
    capabilities are closed by session shutdown. ``routes`` and ``send`` let
    gateway tools capture the current turn's outbound destination.

    Required ``output`` methods are ``event(event)`` and ``error(text)``.
    ``info(text)`` is optional and only receives one-time notices (for
    example a provider without streaming).
    """
    service = service if service is not None else ProviderService(config)
    selected = (
        selected if selected is not None else service.resolve(provider_name, model, effort)
    )
    config = config.model_copy(deep=True)
    config.agents.defaults.provider = selected.name
    config.agents.defaults.model = selected.model
    config.agents.defaults.reasoning_effort = selected.effort
    async with AsyncExitStack() as resources:
        warm_token_encoder()
        client, _, coordinator, availability = build_forge_components(
            config, selected.provider, write_legacy_lessons=False, resources=resources
        )
        # Recover once per coordinator, before any turn can be submitted. This
        # only reconciles unknown Action/Session records with Gateway GETs; it
        # never resends a POST or changes an unresolved unknown into a terminal
        # state. Core has no interaction supervisor, so no robot stop path is
        # entered here.
        try:
            await coordinator.reconcile_nonterminal()
        except Exception as exc:  # recovery must not make the CLI unusable
            output.error(
                "Forge startup recovery could not complete "
                f"({type(exc).__name__}); unresolved remote state stays unknown "
                "and blocks new AgentTasks until a later reconcile resolves it."
            )
        documents = SkillDocumentLoader(
            config.workspace_path, runtime_availability_provider=availability
        )
        activations = TurnSkillActivationManager(
            documents, binding_resolver=coordinator.binding_resolver
        )
        coordinator.set_activation_manager(activations)
        session_dir = config.workspace_path / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        database = session_dir / "core.db"
        store = SQLiteSessionStore(database)
        # The host takes over the store after successful composition. This
        # callback also covers every later construction failure; aclose is
        # idempotent.
        resources.push_async_callback(store.aclose)
        catalog = SessionCatalog(database)
        resources.push_async_callback(asyncio.to_thread, catalog.close)
        display_store = _DisplayStore(store, output)
        skill_capability = SkillAccessCapability(activations)
        forge_capability = ForgeCapability(coordinator, client=client)
        instructions = _instructions(config)
        notice = getattr(output, "info", None)
        runtimes = SessionRuntimes(service, selected)
        session = None

        def tool_selection(session_id):
            if session is None:
                return runtimes.get(session_id)
            return session.tool_selection(session_id)

        async def notify_subagent(session_id, turn_id, text):
            if notice is not None:
                notice(f"Subagent result [{session_id}, {turn_id}]:\n{text}")

        def subagent_notifier(context):
            binding = routes.get(context.session_id) if routes is not None else None
            if binding is None or send is None:
                return notify_subagent if notice is not None else None
            # A child may finish after the parent turn or a session switch.
            # Capture its destination now, while this turn owns the binding.
            channel, chat_id = binding.channel, binding.chat_id
            reply_to = binding.reply_to
            metadata = copy.deepcopy(dict(binding.metadata))

            async def notify(session_id, turn_id, text):
                await send(OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=text,
                    reply_to=reply_to,
                    metadata={**metadata, "parent_session_id": session_id, "parent_turn_id": turn_id},
                ))

            return notify

        general_capability = GeneralToolsCapability(config)
        resources.push_async_callback(general_capability.aclose)
        image_capability = ImageCapability(
            config, provider_resolver=tool_selection, routes=routes, send=send
        )
        resources.push_async_callback(image_capability.aclose)
        mode_capability = AgentModeCapability(provider_resolver=tool_selection)
        resources.push_async_callback(mode_capability.aclose)
        mcp_capability = MCPCapability(
            config.tools.mcp_servers, workspace=config.workspace_path
        )
        resources.push_async_callback(mcp_capability.aclose)
        await mcp_capability.open()
        for name, failure in mcp_capability.failures.items():
            output.error(
                f"MCP server {name!r} could not initialize ({type(failure).__name__}); "
                "its tools are unavailable. Check the configured transport."
            )
        spawn_capability = SpawnCapability(
            config,
            provider_resolver=tool_selection,
            store=store,
            notify=notify_subagent if notice is not None else None,
            notify_factory=subagent_notifier,
        )
        resources.push_async_callback(spawn_capability.aclose)
        tool_capabilities = (
            general_capability, image_capability, mode_capability,
            mcp_capability, spawn_capability, *tuple(additional_capabilities),
        )

        def build(selection: RuntimeSelection) -> AgentAssembly:
            return _compose_for_selection(
                selection,
                config=config,
                store=display_store,
                coordinator=coordinator,
                client=client,
                instructions=instructions,
                skill_capability=skill_capability,
                forge_capability=forge_capability,
                additional_capabilities=tool_capabilities,
                notice=notice,
            )

        # Build one assembly before yielding so wiring failures surface here
        # and a composed host owns shutdown if session construction fails.
        seed = build(selected)
        try:
            session = CoreAgentSession(
                seed,
                selected,
                build=build,
                store=display_store,
                sessions=runtimes,
                output=output,
                raw_store=store,
            )
        except BaseException:
            await seed.host.aclose()
            raise
        session.catalog = catalog
        session.instructions = instructions
        session.config = config
        resources.push_async_callback(session.aclose)
        yield session


def _project_request_messages(messages):
    """Apply the same request-side projections the assembler uses."""
    projected = [dict(message) for message in messages]
    project_forge_tool_results(projected)
    project_skill_documents(projected)
    return projected


def _last_request_tools(events):
    tools = None
    reverted = reverted_turn_ids(events)
    for event in reversed(events):
        if event.type is not EventType.REQUEST_PREPARED:
            continue
        if event.turn_id is not None and event.turn_id in reverted:
            continue
        payload = event.payload
        raw = payload.get("tools")
        tools = [dict(item) for item in raw] if isinstance(raw, list) else None
        break
    return tools


def _last_request_system(events):
    """The exact system prompt of the newest prepared request.

    The prompt assembler reads live fragments on every step, so the static
    instructions alone miss capability text. The last request snapshot is the
    same input the model actually received; using it keeps compaction and the
    status line on one system/tool overhead.
    """
    reverted = reverted_turn_ids(events)
    for event in reversed(events):
        if event.type is not EventType.REQUEST_PREPARED:
            continue
        if event.turn_id is not None and event.turn_id in reverted:
            continue
        messages = event.payload.get("messages")
        if isinstance(messages, list):
            parts = [
                message["content"]
                for message in messages
                if isinstance(message, dict)
                and message.get("role") == "system"
                and isinstance(message.get("content"), str)
            ]
            if parts:
                return "\n\n".join(parts)
        break
    return None


class _ContextStatus:
    """Estimate the next request without scanning the store per delta.

    The in-memory event list is fed from already-persisted events. A
    checkpoint-aware conversation is loaded once per session and refreshed at
    a turn boundary; while a turn is active, the current request snapshot is
    used instead. After compaction the projected conversation replaces the old
    history until the live turn prepares its first request.
    """

    def __init__(self, session, output) -> None:
        self._session = session
        self._output = output
        self._events: dict[str, list] = {}
        self._conversation: dict[str, tuple] = {}
        self._tools: dict[str, list | None] = {}
        self._system: dict[str, str | None] = {}
        self._loaded: set[str] = set()
        # Newest checkpoint coverage per session: events at or below this
        # sequence are represented by the checkpoint projection, not by an old
        # prepared request snapshot.
        self._checkpoint_covers: dict[str, int] = {}

    async def load(self, session_id: str) -> None:
        if session_id in self._loaded:
            return
        raw = getattr(self._session, "raw_store", None)
        if raw is None:
            return
        events = list(await raw.read_session(session_id))
        self._events[session_id] = events
        self._conversation[session_id] = tuple(await raw.read_conversation(session_id))
        try:
            reader = getattr(raw, "read_checkpoint", None)
            checkpoint = await reader(session_id) if callable(reader) else None
        except Exception:
            checkpoint = None
        if checkpoint is not None:
            self._checkpoint_covers[session_id] = checkpoint.covers_sequence
        else:
            self._checkpoint_covers.pop(session_id, None)
        self._tools[session_id] = _last_request_tools(events)
        self._system[session_id] = self._system_text(events)
        self._loaded.add(session_id)
        self._output.ledger.seed(events)
        await self.ensure_usage(session_id)

    async def ensure_usage(self, session_id: str) -> None:
        """Import persisted compaction attempts for one session, idempotently.

        The attempt rows are the durable cost trail. A successful checkpoint's
        copied usage is deliberately not read here, so it is never counted a
        second time. Listing sessions calls this for every session, not just
        the active one.
        """
        raw = getattr(self._session, "raw_store", None)
        if raw is None:
            return
        try:
            attempts = await raw.list_compaction_attempts(session_id)
        except Exception:
            return
        for attempt in attempts:
            self._output.ledger.record_compaction_attempt(
                session_id,
                attempt.attempt_id,
                attempt.status,
                attempt.usage,
            )

    async def refresh(self, session_id: str) -> None:
        """Refresh the projection and its event source once, at a turn boundary.

        Events are re-read as well: a ``turn.reverted`` marker is written by
        the store itself, not through the observed ``append`` path, so a cached
        log would keep showing the withdrawn request. The usage ledger is only
        seeded by :meth:`load`, never here, so re-reading cannot double-count.
        """
        raw = getattr(self._session, "raw_store", None)
        if raw is None:
            return
        try:
            self._events[session_id] = list(await raw.read_session(session_id))
            self._conversation[session_id] = tuple(await raw.read_conversation(session_id))
            reader = getattr(raw, "read_checkpoint", None)
            checkpoint = await reader(session_id) if callable(reader) else None
        except Exception:
            self._conversation.pop(session_id, None)
            checkpoint = None
        if checkpoint is not None:
            self._checkpoint_covers[session_id] = checkpoint.covers_sequence
        events = self._events.get(session_id, [])
        self._tools[session_id] = _last_request_tools(events)
        self._system[session_id] = self._system_text(events)

    def _system_text(self, events) -> str | None:
        """Keep the status line on the same system prompt the model receives."""
        return _last_request_system(events) or getattr(self._session, "instructions", "") or None

    def ingest(self, event) -> None:
        self._events.setdefault(event.session_id, []).append(event)
        if event.type is EventType.REQUEST_PREPARED:
            # Only a prepared request supersedes the checkpoint/turn
            # projection. TURN_STARTED deliberately keeps it: between the two
            # events the old event log would still project the pre-compaction
            # history and the status line would jump up until the new request
            # snapshot exists.
            self._conversation.pop(event.session_id, None)

    def apply_compaction(self, session_id: str, result) -> None:
        checkpoint = result.checkpoint
        self._conversation[session_id] = (
            {
                "role": "assistant",
                "content": f"{CHECKPOINT_SUMMARY_NOTICE}\n\n{checkpoint.summary}",
            },
            *checkpoint.retained_messages,
        )
        events = self._events.get(session_id, [])
        self._tools[session_id] = _last_request_tools(events)
        self._system[session_id] = self._system_text(events)
        self._loaded.add(session_id)
        covers = getattr(checkpoint, "covers_sequence", None)
        if not isinstance(covers, int):
            # Test/legacy checkpoint objects may omit coverage; treat the whole
            # current log as covered so the pre-compaction snapshot is dropped
            # and only a later prepared request can supersede the checkpoint.
            covers = max((event.sequence or 0) for event in events) if events else 0
        self._checkpoint_covers[session_id] = covers

    def snapshot(self, session_id: str) -> ContextSnapshot:
        selection = self._session.sessions.get(session_id)
        capacity, window_source = _effective_window(getattr(self._session, "config", None), selection)
        system_text = self._system.get(session_id)
        if system_text is None:
            system_text = self._system_text(self._events.get(session_id, []))
        events = self._events.get(session_id, [])
        # Prefer the exact prepared request: it already carries the assembler's
        # request projection (deduplicated Skill docs, projected tool results)
        # and the visible tool set. Raw events remain the replay source.
        prepared_sequences = [
            event.sequence
            for event in events
            if event.type is EventType.REQUEST_PREPARED and event.sequence is not None
        ]
        covers = self._checkpoint_covers.get(session_id)
        prepared_is_current = bool(prepared_sequences) and (
            covers is None or max(prepared_sequences) > covers
        )
        if prepared_is_current:
            return estimate_context(
                events,
                provider=selection.provider,
                model=selection.model,
                capacity=capacity,
                capacity_source=window_source,
                instructions=system_text,
            )
        conversation = self._conversation.get(session_id)
        if conversation is not None:
            return estimate_context_from_conversation(
                _project_request_messages(conversation),
                self._tools.get(session_id),
                instructions=system_text,
                provider=selection.provider,
                model=selection.model,
                capacity=capacity,
                capacity_source=window_source,
            )
        return estimate_context(
            events,
            provider=selection.provider,
            model=selection.model,
            capacity=capacity,
            capacity_source=window_source,
            instructions=system_text,
        )


async def run_core_agent(
    *, message, session_id, workspace, config_path, markdown, logs,
    provider_name, model, effort, console,
) -> int:
    from loguru import logger

    from ..interaction.agent_output import AgentOutput
    from ..interaction.interactive_session import InteractiveAgent
    from ..interaction.model_picker import _interactive_model_command

    config = load_agent_config(config_path, workspace)
    if config.is_fleet_mode:
        raise ValueError("Core CLI currently supports one workspace; use --runtime legacy for fleet mode")
    if session_id is not None and not str(session_id).strip():
        raise ValueError("session id must not be empty")
    if message is not None and not message.strip():
        raise ValueError("message must not be empty")
    logger.enable("PhyAgentOS") if logs else logger.disable("PhyAgentOS")
    output = AgentOutput(console, markdown=markdown)
    output.startup("Forge recovery")
    # The CLI keeps the legacy cron tool without starting a scheduler: the
    # service only persists jobs into the shared store the gateway reads.
    # Tests override ``get_cron_dir`` so no real home directory is touched.
    cron_capability = CronCapability(CronService(get_cron_dir() / "jobs.json"))
    async with open_core_session(
        config,
        provider_name=provider_name,
        model=model,
        effort=effort,
        output=output,
        additional_capabilities=(cron_capability,),
    ) as session:
        catalog = getattr(session, "catalog", None)
        infos: dict[str, SessionInfo | None] = {}
        explicit = str(session_id).strip() if session_id is not None else ""
        if explicit:
            sid = explicit
            if catalog is not None:
                try:
                    infos[sid] = await asyncio.to_thread(catalog.ensure_session, sid)
                except Exception as exc:
                    # Metadata failure must never silently rename the session.
                    output.error(
                        "Session metadata could not be read "
                        f"({type(exc).__name__}); the explicit session id "
                        f"{sid} is kept unchanged."
                    )
        else:
            if catalog is None:
                raise ValueError(
                    "No session catalog is attached; pass --session to reuse an "
                    "explicit id."
                )
            info = await asyncio.to_thread(catalog.create_session, "cli")
            sid = info.session_id
            infos[sid] = info

        startup_selection = session.sessions.get(sid)
        flags_given = any(value is not None for value in (provider_name, model, effort))
        if catalog is not None and flags_given:
            try:
                await asyncio.to_thread(
                    catalog.save_selection,
                    sid,
                    provider=startup_selection.name,
                    model=startup_selection.model,
                    effort=startup_selection.effort,
                )
            except Exception as exc:
                output.error(
                    "The session selection could not be saved "
                    f"({type(exc).__name__}); the session id is unchanged."
                )
        elif catalog is not None:
            saved: SessionSelection | None = None
            try:
                saved = await asyncio.to_thread(catalog.load_selection, sid)
            except Exception as exc:
                output.error(
                    "The saved session selection could not be read "
                    f"({type(exc).__name__}); this session uses the startup selection."
                )
            if saved is not None and (saved.provider or saved.model or saved.effort):
                try:
                    session.sessions.select(
                        sid,
                        saved.provider or startup_selection.name,
                        saved.model or startup_selection.model,
                        saved.effort or "none",
                    )
                except Exception as exc:
                    # A stale saved selection is reported, never repaired by
                    # changing the session id.
                    output.error(
                        f"Saved selection for session {sid} could not be applied: "
                        f"{exc}. The session id is unchanged; startup selection is used."
                    )

        context = _ContextStatus(session, output)
        if getattr(session, "raw_store", None) is not None:
            await context.load(sid)
        output.configure_stats(
            ledger=output.ledger,
            context_provider=context.snapshot,
            event_observer=context.ingest,
        )
        active_session_id = sid

        def refresh_label(target: str | None = None) -> str:
            target = target or active_session_id
            info = infos.get(target)
            selection = session.sessions.get(target)
            base = f"session {info.label}" if info is not None else f"session {target}"
            label = f"{base} · {selection.name}/{selection.model}"
            if selection.effort:
                label += f" · effort {selection.effort}"
            output.set_session_label(label)
            output.refresh_status(target)
            return label

        async def name_session(target: str, text: str) -> None:
            if catalog is None:
                return
            try:
                await asyncio.to_thread(catalog.name_from_first_input, target, text)
                info = await asyncio.to_thread(catalog.get_session, target)
                if info is not None:
                    infos[target] = info
            except Exception as exc:
                # Naming is display metadata; a failure must not rename the
                # session or fail the turn.
                output.error(
                    "The session title could not be saved "
                    f"({type(exc).__name__}); the session id is unchanged."
                )
            if target == active_session_id:
                refresh_label(target)

        output.ready()
        label = refresh_label()
        if message is not None:
            await name_session(sid, message)
            label = refresh_label()
            output.info(label)
            output.turn_started(sid)
            try:
                task = asyncio.create_task(session.submit(TurnRequest(sid, message)))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    output.cancel_requested()
                    await session.cancel(sid)
                    await task
                    raise
            finally:
                output.turn_finished()
            await context.refresh(sid)
            output.result(result)
            return {TurnStatus.COMPLETED: 0, TurnStatus.FAILED: 1, TurnStatus.INTERRUPTED: 130}[result.status]

        output.info(
            "Local commands: /help · /tools · /new · /session · /compact · "
            "/undo · /model · /effort · /status · exit. "
            "Press Esc twice while a turn runs to stop it locally. "
            "Accepted or running is not task success."
        )

        from ..interaction.model_picker import _session_picker
        from ..interaction.terminal_input import TerminalInput

        terminal = TerminalInput(display=output.display)

        def on_active_session(target) -> None:
            nonlocal active_session_id
            if target:
                active_session_id = target
            output.refresh_status(active_session_id)

        async def apply_saved_selection(target: str) -> None:
            if catalog is None:
                return
            try:
                saved = await asyncio.to_thread(catalog.load_selection, target)
            except Exception as exc:
                output.error(
                    "The saved session selection could not be read "
                    f"({type(exc).__name__}); the session id is unchanged."
                )
                return
            if saved is None or not (saved.provider or saved.model or saved.effort):
                return
            current = session.sessions.get(target)
            try:
                session.sessions.select(
                    target,
                    saved.provider or current.name,
                    saved.model or current.model,
                    saved.effort or "none",
                )
            except Exception as exc:
                output.error(
                    f"Saved selection for session {target} could not be applied: "
                    f"{exc}. The session id is unchanged."
                )

        async def switch_to(target: str, info: SessionInfo | None) -> None:
            nonlocal active_session_id
            infos[target] = info or infos.get(target)
            await context.load(target)
            await apply_saved_selection(target)
            active_session_id = target
            refresh_label(target)

        async def model_command(text: str) -> str | None:
            target = active_session_id
            try:
                parts = text.strip().split(maxsplit=1)
                command = parts[0].lower() if parts else ""
                if command in {"/model", "/effort"} and len(parts) == 1 and terminal.menu_capable:
                    # A menu needs exclusive stdin: end the prompt first, and only
                    # resume reading after the menu has returned or been cancelled.
                    await terminal.pause()
                    try:
                        selected = await _interactive_model_command(
                            text, session.sessions, target
                        )
                    finally:
                        terminal.resume()
                    if selected is not None:
                        if catalog is not None and command in {"/provider", "/model", "/effort"}:
                            current = session.sessions.get(target)
                            await asyncio.to_thread(
                                catalog.save_selection,
                                target,
                                provider=current.name,
                                model=current.model,
                                effort=current.effort,
                            )
                        return selected
                message_text = session.model_command(target, text)
                if (
                    catalog is not None
                    and command in {"/provider", "/model", "/effort"}
                    and message_text is not None
                ):
                    current = session.sessions.get(target)
                    await asyncio.to_thread(
                        catalog.save_selection,
                        target,
                        provider=current.name,
                        model=current.model,
                        effort=current.effort,
                    )
                return message_text
            finally:
                # Every outcome (text change, menu, reset, cancellation or a
                # failed command) republishes the selection that is actually in
                # force, so the toolbar can never keep a stale provider/model.
                refresh_label(target)

        async def session_command(text: str, binding) -> str | None:
            nonlocal active_session_id
            parts = text.strip().split(maxsplit=1)
            command = parts[0].lower() if parts else ""
            argument = parts[1].strip() if len(parts) == 2 else ""
            fallback = binding.fallback or active_session_id

            def failed(message: str) -> None:
                binding.session_id = fallback
                output.error(message)

            if catalog is None:
                failed(
                    "Session switching is unavailable because no session catalog "
                    "is attached; the current session is unchanged."
                )
                return None
            if command == "/new":
                try:
                    info = await asyncio.to_thread(catalog.create_session, "cli")
                except Exception as exc:
                    failed(
                        "A new session could not be created "
                        f"({type(exc).__name__}); the current session is unchanged."
                    )
                    return None
                await switch_to(info.session_id, info)
                binding.session_id = info.session_id
                return f"Started new session {info.label}."
            if command != "/session":
                failed("Unknown session command; the current session is unchanged.")
                return None
            if argument:
                try:
                    info = await asyncio.to_thread(catalog.get_session, argument)
                except Exception as exc:
                    failed(
                        "The session could not be read "
                        f"({type(exc).__name__}); the current session is unchanged."
                    )
                    return None
                if info is None:
                    failed(
                        f"Unknown session {argument[:80]!r}; no switch was made. "
                        "Use /session to list sessions."
                    )
                    return None
                await switch_to(info.session_id, info)
                binding.session_id = info.session_id
                return f"Switched to session {info.label}."

            try:
                listed = await asyncio.to_thread(catalog.list_sessions)
            except Exception as exc:
                failed(
                    "Sessions could not be listed "
                    f"({type(exc).__name__}); the current session is unchanged."
                )
                return None
            if not listed:
                binding.session_id = fallback
                return "No sessions in this workspace yet. Use /new to start one."
            for info in listed:
                await context.ensure_usage(info.session_id)
            if terminal.menu_capable:
                await terminal.pause()
                try:
                    chosen = await _session_picker(listed, current=active_session_id)
                finally:
                    terminal.resume()
                if chosen is None:
                    binding.session_id = fallback
                    output.info("Session selection cancelled; the current session is unchanged.")
                    return None
                info = next((item for item in listed if item.session_id == chosen), None)
                if info is None:
                    failed("The selected session disappeared; the current session is unchanged.")
                    return None
                await switch_to(info.session_id, info)
                binding.session_id = info.session_id
                return f"Switched to session {info.label}."
            binding.session_id = fallback
            rows = [
                f"{'*' if item.session_id == active_session_id else ' '} "
                f"{item.label}"
                for item in listed[:20]
            ]
            note = "" if len(listed) <= 20 else f"\n... and {len(listed) - 20} more"
            return "Sessions (use /session <id> to switch):\n" + "\n".join(rows) + note

        async def compact_command(text: str, binding) -> str | None:
            target = binding.session_id or binding.fallback or active_session_id
            if not target:
                output.error("No active session to compact.")
                return None
            selection = session.sessions.get(target)
            output.info(
                f"Compacting session {target} with {selection.name}/{selection.model}; "
                "this is a manual summary, not a new task."
            )
            output.compact_started()
            succeeded = False
            try:
                result = await session.compact(target)
                succeeded = True
            except Exception as exc:
                # A failed attempt changes no projection, but its audit row and
                # any known usage are durable. Do not claim no write happened.
                output.error(
                    "Context compaction failed "
                    f"({type(exc).__name__}): {exc}. The conversation projection "
                    "is unchanged; the attempt is recorded for accounting."
                )
                return None
            finally:
                # Import the attempt rows on success and failure, then refresh
                # the projection. This keeps an in-process failure visible
                # without waiting for a restart.
                await context.ensure_usage(target)
                if succeeded:
                    context.apply_compaction(target, result)
                else:
                    await context.refresh(target)
                refresh_label(target)
            message_text = (
                f"Context compacted: {result.before_tokens} -> {result.after_tokens} "
                "estimated tokens. The raw event log and earlier history are unchanged."
            )
            if result.warnings:
                message_text += "\nWarnings: " + "; ".join(result.warnings)
            return message_text

        async def undo_command(binding) -> str | None:
            target = binding.session_id or binding.fallback or active_session_id
            if not target:
                return "No active session to undo."
            outcome = await session.undo(target)
            # Keep the status-bar estimate on the projection the store now
            # returns, so a reverted request is not counted twice.
            await context.refresh(target)
            refresh_label(target)
            if outcome is None:
                return "No earlier request to undo in this session."
            text = outcome.request_text
            if text is not None and terminal.prefill(text):
                placement = (
                    "Its text is back in the input line; edit it or press Enter "
                    "to send it again."
                )
            elif text is not None:
                placement = (
                    "No input line is available to refill; the request text is:\n"
                    f"{text}"
                )
            else:
                placement = (
                    "The original request text is not available, so nothing was "
                    "refilled."
                )
            warning = (
                "Tool side effects (robot actions, file changes) are not undone."
            )
            if outcome.block_reason == "checkpoint":
                return (
                    f"Request {outcome.turn_id} is covered by a compaction summary "
                    "and cannot be removed from the context used by later turns. "
                    f"{placement} {warning}"
                )
            if outcome.context_reverted:
                return (
                    f"Request {outcome.turn_id} was removed from the context used "
                    f"by later turns. {placement} {warning}"
                )
            if outcome.status.rsplit(".", 1)[-1] in {"failed", "interrupted"}:
                return (
                    f"Request {outcome.turn_id} never entered the context "
                    f"({outcome.status}), so only the input was refilled. "
                    f"{placement} {warning}"
                )
            return f"Request {outcome.turn_id} was withdrawn. {placement} {warning}"

        async def on_turn_settled(target: str) -> None:
            await context.refresh(target)
            refresh_label(target)

        try:
            terminal.initialize()
            agent = InteractiveAgent(
                session,
                session_id=sid,
                output=output,
                read_input=terminal.read,
                model_command=model_command,
                session_command=session_command,
                compact_command=compact_command,
                undo_command=undo_command,
                name_session=name_session,
                on_active_session=on_active_session,
                on_turn_settled=on_turn_settled,
            )
            terminal.bind_stop(agent.request_stop)
            await agent.run()
        finally:
            terminal.restore()
    return 0


def register_agent(app, *, legacy_runner=None):
    """Register one explicit command; never fall back after a Core failure.

    ``legacy_runner`` is the already-resolved legacy entry point when a caller
    injects one (tests, embedding code). The default registration passes
    nothing and the legacy package is imported only inside the ``legacy``
    branch, so building the CLI app and running Core never load it.
    """

    @app.command("agent")
    def agent(
        message: str | None = typer.Option(None, "--message", "-m"),
        session_id: str | None = typer.Option(
            None,
            "--session",
            "-s",
            help="Reuse an explicit session id; omit to start a new session.",
        ),
        workspace: str | None = typer.Option(None, "--workspace", "-w"),
        config: str | None = typer.Option(None, "--config", "-c"),
        markdown: bool = typer.Option(True, "--markdown/--no-markdown"),
        logs: bool = typer.Option(False, "--logs/--no-logs"),
        provider: str | None = typer.Option(None, "--provider"),
        model: str | None = typer.Option(None, "--model"),
        reasoning_effort: str | None = typer.Option(None, "--reasoning-effort"),
        runtime: str = typer.Option(
            "core",
            "--runtime",
            help="Agent runtime: core (default) or legacy.",
        ),
    ):
        """Interact with the Agent using an explicitly selected runtime."""
        if runtime not in {"core", "legacy"}:
            raise typer.BadParameter("choose core or legacy", param_hint="--runtime")
        if runtime == "legacy":
            runner = legacy_runner
            if runner is None:
                # Imported only for an explicit legacy selection: the Core
                # default must be usable without the old implementation.
                from ..legacy import legacy_agent

                runner = legacy_agent.agent
            return runner(
                message=message,
                # The legacy entry point has its own default; Typer no longer
                # supplies it for the Core path's new-session default. Never
                # use ``or`` here: an explicitly empty id stays explicit.
                session_id=session_id if session_id is not None else "cli:direct",
                workspace=workspace,
                config=config,
                markdown=markdown,
                logs=logs,
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        try:
            code = asyncio.run(run_core_agent(
                message=message, session_id=session_id, workspace=workspace,
                config_path=config, markdown=markdown, logs=logs,
                provider_name=provider, model=model, effort=reasoning_effort,
                console=Console(),
            ))
        except KeyboardInterrupt:
            code = 130
        except (ProviderError, ValueError, OSError, RuntimeError) as exc:
            Console().print(str(exc), style="red", markup=False)
            code = 1
        raise typer.Exit(code)

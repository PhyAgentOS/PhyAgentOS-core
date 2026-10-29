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
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console

from PhyAgentOS.agent.bootstrap import build_forge_components
from PhyAgentOS.agent.capabilities.forge_capability import ForgeCapability
from PhyAgentOS.agent.capabilities.skill_access_capability import SkillAccessCapability
from PhyAgentOS.agent.composition import AgentAssembly, compose_agent
from PhyAgentOS.agent.core import SQLiteSessionStore, TurnRequest, TurnStatus
from PhyAgentOS.agent.core.host import AgentHostCloseError
from PhyAgentOS.agent.services.skill_documents import SkillDocumentLoader
from PhyAgentOS.agent.services.turn_activation import TurnSkillActivationManager
from PhyAgentOS.config.loader import load_config, set_config_path
from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    RuntimeSelection,
    SessionRuntimes,
)
from PhyAgentOS.utils.helpers import estimate_prompt_tokens_chain


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
        "You are PhyAgentOS. Use the registered tools and report only observed results. "
        "Physical execution must use Forge Tool API. For physical work first activate "
        "a primary Skill in the current turn, then create an AgentTask with its activation_id. "
        "Do not create tasks for ordinary conversation. Query tool definitions and context "
        "rather than guessing tool ids or arguments. Accepted/running is not success. "
        "Poll an admitted action to a terminal result before finalizing its task. "
        "Unknown remote state must not cause an automatic resubmission. "
        "Local cancellation is not confirmation that remote execution stopped.",
        "The existing task contract defaults to verification off (execution facts only). "
        "Verification modes are off, audit, enforce, and recovery. Respect the user's "
        "verification requirement; never silently turn it off. Semantic verification "
        "requires its service, a goal, success criteria and evidence. Use only available "
        "evidence and explain missing evidence rather than inventing it. Execution success "
        "without semantic verification does not establish a user-level goal was achieved.",
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


class _SelectedProvider:
    """Forward a turn to its frozen selection; leave SDK retries to the provider.

    The adapter builds the generation arguments from the model request. This
    wrapper never substitutes the selection for them: if the request says a
    different model or effort, that is the call that is made, so the persisted
    ``request.prepared`` snapshot cannot disagree with the actual request.
    """

    def __init__(self, selection, context_window, max_tokens):
        self.selection = selection
        self.context_window = min(
            context_window, selection.context_window_tokens or context_window
        )
        self.max_tokens = max_tokens

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        selected = self.selection
        model = kwargs.get("model") or selected.model
        estimated, _ = estimate_prompt_tokens_chain(
            selected.provider, model, messages, tools
        )
        if estimated and estimated + self.max_tokens > self.context_window:
            raise ProviderError(
                f"Context limit reached (estimated prompt {estimated}, reply budget "
                f"{self.max_tokens}, limit {self.context_window}). Start a new --session "
                "or use --runtime legacy for history compaction. No further model call "
                "was sent."
            )
        return await selected.provider.chat_with_retry(
            messages=messages, tools=tools, **kwargs
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
    ):
        self._seed = seed
        self._seed_selection = selection
        self._build = build
        self._store = store
        self.sessions = sessions
        self.output = output
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
            state = None if slot is None else slot.current
            if state is None:
                # No turn has run yet: ask the seed host so capability
                # cancellation still receives the request.
                state = _AssemblyState(self._seed_selection, self._seed)
        await state.assembly.host.cancel(session_id)

    def model_command(self, session_id: str, text: str) -> str | None:
        """Apply one /provider, /model, /effort or /status command."""
        return self.sessions.command(session_id, text)

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
) -> AgentAssembly:
    defaults = config.agents.defaults
    provider = _SelectedProvider(
        selection, defaults.context_window_tokens, defaults.max_tokens
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
        additional_capabilities=(skill_capability,),
        forge_capability=forge_capability,
    )


@asynccontextmanager
async def open_core_session(config, *, provider_name=None, model=None, effort=None, output):
    service = ProviderService(config)
    selected = service.resolve(provider_name, model, effort)
    config = config.model_copy(deep=True)
    config.agents.defaults.provider = selected.name
    config.agents.defaults.model = selected.model
    config.agents.defaults.reasoning_effort = selected.effort
    async with AsyncExitStack() as resources:
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
        store = SQLiteSessionStore(session_dir / "core.db")
        # The host takes over the store after successful composition. This
        # callback also covers every later construction failure; aclose is
        # idempotent.
        resources.push_async_callback(store.aclose)
        display_store = _DisplayStore(store, output)
        skill_capability = SkillAccessCapability(activations)
        forge_capability = ForgeCapability(coordinator, client=client)
        instructions = _instructions(config)

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
                sessions=SessionRuntimes(service, selected),
                output=output,
            )
        except BaseException:
            await seed.host.aclose()
            raise
        resources.push_async_callback(session.aclose)
        yield session


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
    if not session_id.strip():
        raise ValueError("session id must not be empty")
    if message is not None and not message.strip():
        raise ValueError("message must not be empty")
    logger.enable("PhyAgentOS") if logs else logger.disable("PhyAgentOS")
    output = AgentOutput(console, markdown=markdown)
    output.startup("Forge recovery")
    async with open_core_session(
        config, provider_name=provider_name, model=model, effort=effort, output=output
    ) as session:
        def refresh_label() -> str:
            selection = session.sessions.get(session_id)
            label = f"session {session_id} · {selection.name}/{selection.model}"
            if selection.effort:
                label += f" · effort {selection.effort}"
            output.set_session_label(label)
            return label

        output.ready()
        label = refresh_label()
        if message is not None:
            output.info(label)
            output.turn_started()
            try:
                task = asyncio.create_task(session.submit(TurnRequest(session_id, message)))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    output.cancel_requested()
                    await session.cancel(session_id)
                    await task
                    raise
            finally:
                output.turn_finished()
            output.result(result)
            return {TurnStatus.COMPLETED: 0, TurnStatus.FAILED: 1, TurnStatus.INTERRUPTED: 130}[result.status]

        output.info(
            "Local commands: /help · /tools · /model · /effort · /status · exit. "
            "Press Esc twice while a turn runs to stop it locally. "
            "Accepted or running is not task success."
        )

        from ..interaction.terminal_input import TerminalInput

        terminal = TerminalInput(display=output.display)

        async def model_command(text: str) -> str | None:
            try:
                parts = text.strip().split(maxsplit=1)
                command = parts[0].lower() if parts else ""
                if command in {"/model", "/effort"} and len(parts) == 1 and terminal.menu_capable:
                    # A menu needs exclusive stdin: end the prompt first, and only
                    # resume reading after the menu has returned or been cancelled.
                    await terminal.pause()
                    try:
                        selected = await _interactive_model_command(
                            text, session.sessions, session_id
                        )
                    finally:
                        terminal.resume()
                    if selected is not None:
                        return selected
                return session.model_command(session_id, text)
            finally:
                # Every outcome (text change, menu, reset, cancellation or a
                # failed command) republishes the selection that is actually in
                # force, so the toolbar can never keep a stale provider/model.
                refresh_label()

        try:
            terminal.initialize()
            agent = InteractiveAgent(
                session,
                session_id=session_id,
                output=output,
                read_input=terminal.read,
                model_command=model_command,
            )
            terminal.bind_stop(agent.request_stop)
            await agent.run()
        finally:
            terminal.restore()
    return 0


def register_agent(app, *, legacy_runner):
    """Register one explicit command; never fall back after a Core failure."""

    @app.command("agent")
    def agent(
        message: str | None = typer.Option(None, "--message", "-m"),
        session_id: str = typer.Option("cli:direct", "--session", "-s"),
        workspace: str | None = typer.Option(None, "--workspace", "-w"),
        config: str | None = typer.Option(None, "--config", "-c"),
        markdown: bool = typer.Option(True, "--markdown/--no-markdown"),
        logs: bool = typer.Option(False, "--logs/--no-logs"),
        provider: str | None = typer.Option(None, "--provider"),
        model: str | None = typer.Option(None, "--model"),
        reasoning_effort: str | None = typer.Option(None, "--reasoning-effort"),
        runtime: str = typer.Option(
            "legacy", "--runtime", help="Agent runtime: core (new, opt-in) or legacy (default)."
        ),
    ):
        """Interact with the Agent using an explicitly selected runtime."""
        if runtime not in {"core", "legacy"}:
            raise typer.BadParameter("choose core or legacy", param_hint="--runtime")
        if runtime == "legacy":
            return legacy_runner(
                message=message, session_id=session_id, workspace=workspace, config=config,
                markdown=markdown, logs=logs, provider=provider, model=model,
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

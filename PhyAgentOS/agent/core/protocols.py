"""Component interfaces for the agent core.

These Protocols are the integration contract for the seven runtime
components. They describe only what the core needs; concrete adapters own
provider SDKs, SQLite, Forge, and any other dependency.

Assembly shape::

    # composition root: entry bootstrap or test
    store = ...
    adapter = ...
    assembler = ...
    executor = ...
    driver = LoopDriverImpl(
        assembler=assembler, adapter=adapter, executor=executor, store=store
    )
    runtime = AgentRuntimeImpl(driver=driver, store=store)
    host = AgentHostImpl(runtime=runtime, capabilities=(capability, ...))

    AgentHost.submit(TurnRequest)
        -> AgentRuntime.submit(TurnRequest)
            -> SessionStore.read_conversation(session_id)   # TurnStart.history
            -> LoopDriver.run(TurnStart)
                -> PromptAssembler.assemble(context, history=..., tools=...)
                -> ModelAdapter.complete(ModelRequest)
                -> ToolExecutor.execute(ToolCall, context)   # per call
            -> TurnResult
        -> TurnResult

Wiring is external and deliberately absent from these Protocols: the entry
(or a test) builds the store, assembler, adapter, executor, driver, and
runtime, then injects the runtime and the already-registered capabilities
into ``AgentHost``. There is no config lookup, global registry, or component
factory in this layer.

Event writers are split so no transition is recorded twice: ``AgentRuntime``
writes ``input.accepted``, ``turn.started``, terminal ``turn.*`` events, and
``session.snapshot.created``; ``LoopDriver`` writes ``request.prepared``,
``model.attempt.*``, ``model.response.delta``, and ``context.compacted``;
``ToolExecutor`` writes ``tool.intent``, ``tool.dispatched``, and
``tool.settled``.

The store API is async and must stay async across every implementation. Its
ordering rules are what make a crashed turn recoverable: ``append`` assigns
``sequence`` and returns only after a durable write, and recovery events are
appended before the session is served as recovered. The store also answers
one derived read, ``read_conversation``: a read-only projection of completed
turns for the runtime to hand to the loop driver. There is no other
conversation state in the core. A store may additionally implement
``read_startup_snapshot`` (see :class:`StartupSnapshotStore`) to return the
recovery report and that projection from one consistent read; the runtime uses
it when present and otherwise keeps the explicit recover-then-read pair. It may
also implement ``undo_last_turn`` (see :class:`UndoTurnStore`) to withdraw the
newest terminal turn with one appended marker event; that is optional, and the
core never requires it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .events import RecoveryReport, RuntimeEvent
from .types import (
    ExecutionContext,
    ModelRequest,
    ModelResponse,
    ModelStreamDelta,
    PromptBundle,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolRegistration,
    TurnRequest,
    TurnResult,
    TurnStart,
)

if TYPE_CHECKING:  # annotation only; the store module imports these protocols
    from .session import UndoOutcome

__all__ = [
    "AgentHost",
    "AgentRuntime",
    "Capability",
    "LoopDriver",
    "ModelAdapter",
    "PromptAssembler",
    "SessionStore",
    "SessionSnapshot",
    "StartupSnapshotStore",
    "StreamingModelAdapter",
    "ToolExecutor",
    "UndoTurnStore",
]


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """One consistent startup read: recovery plus the next turn's history.

    ``recovery`` is the report the runtime would otherwise get from
    :meth:`SessionStore.recover`, and ``conversation`` is the projection
    :meth:`SessionStore.read_conversation` would return. Both come from the
    same stored view, so a turn cannot read a conversation older than the
    recovery it just performed.
    """

    recovery: RecoveryReport
    conversation: tuple[Mapping[str, Any], ...]
    head_sequence: int | None = None


@runtime_checkable
class StartupSnapshotStore(Protocol):
    """Optional ``SessionStore`` extension for one-read turn startup.

    Implementations may return both values from one consistent read. The
    runtime uses it when present and otherwise falls back to ``recover`` plus
    ``read_conversation``, so a store that only implements ``SessionStore``
    keeps working unchanged. ``head_sequence`` is the session head the report
    was folded at; the runtime uses it to close unfinished turns conditionally.
    The returned conversation must reflect work visible at the same read,
    including a checkpoint that covers it; an implementation that caches across
    calls must invalidate on a changed head sequence and on a changed checkpoint
    identity, including another process's append.
    """

    async def read_startup_snapshot(self, session_id: str) -> SessionSnapshot: ...

    async def append_recovery(
        self,
        session_id: str,
        events: Sequence[RuntimeEvent],
        *,
        expected_head: int,
    ) -> tuple[RuntimeEvent, ...] | None: ...


@runtime_checkable
class UndoTurnStore(Protocol):
    """Optional ``SessionStore`` extension for withdrawing the last request.

    ``undo_last_turn`` withdraws the newest terminal turn that is not already
    reverted and reports what it actually did, or returns ``None`` when no such
    turn exists. It is append-only like every other write: the store adds one
    session-scoped ``turn.reverted`` event naming the turn in
    ``payload["reverted_turn_id"]`` and never deletes or rewrites rows, so the
    conversation projection simply falls back to the previous completed turn.

    Like :class:`StartupSnapshotStore` this is optional: a store that only
    implements ``SessionStore`` keeps working, and callers test for it with
    ``isinstance`` before using it. An implementation must refuse with
    ``RuntimeError`` while the session still has an unfinished turn, must not
    claim that a turn folded into a context checkpoint left the context, and
    must read ``request_text`` back from the stored request snapshot instead of
    rebuilding it.
    """

    async def undo_last_turn(self, session_id: str) -> UndoOutcome | None: ...


@runtime_checkable
class PromptAssembler(Protocol):
    """Builds the messages and visible tool definitions for one step.

    ``history`` is the minimal conversation for this turn, built by the loop
    driver from ``TurnStart.history`` (completed turns) plus
    ``TurnRequest.text`` and the model/tool results produced so far in this
    turn. The assembler does not read the store or session state.

    Any capability prompt text is obtained by calling
    ``Capability.prompt_context(context)`` (or an equivalent injected
    fragment) during ``assemble``, with the current step context, every time:
    fragments are evaluated per call and never cached or shared across
    sessions. The assembler may prepend static instructions and append those
    fragments, and may narrow ``tools``, but it must not add a tool the
    caller did not offer.
    """

    async def assemble(
        self,
        context: ExecutionContext,
        *,
        history: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition],
    ) -> PromptBundle: ...


@runtime_checkable
class ModelAdapter(Protocol):
    """Wraps a provider behind the core request/response types.

    The adapter translates a :class:`ModelRequest` snapshot into a provider
    call and maps the answer back to :class:`ModelResponse`, preserving
    ``raw_arguments`` and provider-specific tool-call fields. It raises
    :class:`~PhyAgentOS.agent.core.types.ModelCallError` when no usable answer
    was obtained; a provider error must never be returned as an assistant
    reply (``finish_reason="error"`` is rejected by the response contract).

    Streaming is an optional extension of this contract, described by
    :class:`StreamingModelAdapter`. A driver that only has ``complete`` still
    works; it simply cannot display partial text.
    """

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class StreamingModelAdapter(Protocol):
    """A :class:`ModelAdapter` that can report body text before the answer ends.

    ``stream`` returns an async iterator that yields zero or more
    :class:`~PhyAgentOS.agent.core.types.ModelStreamDelta` values (body text
    only, never reasoning) and then exactly one terminal
    :class:`~PhyAgentOS.agent.core.types.ModelResponse`. Provider tool call
    arguments must already be fully assembled and structurally complete in
    that response; the driver still validates them before execution and must
    never execute a truncated payload.

    A failure raises :class:`~PhyAgentOS.agent.core.types.ModelCallError`,
    carrying any usage the provider had reported, so partial output stays
    traceable without becoming a successful turn. Closing the iterator stops
    the underlying provider stream; the adapter must not retry after its first
    delta.
    """

    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelStreamDelta | ModelResponse]: ...


@runtime_checkable
class ToolExecutor(Protocol):
    """Validates, authorizes, executes, and settles one tool call.

    The executor owns raw-payload and JSON Schema validation, policy
    enforcement, event writing, and the mapping of exceptions to
    ``ToolOutcome`` states. After validation it passes the handler a
    :class:`ToolCall` snapshot rebuilt with the validated arguments
    (consistent with the raw payload) while preserving the original
    ``tool_call_id``, ``name`` and provider fields, plus the explicit
    :class:`ExecutionContext`. The handler reads arguments via
    ``call.arguments_dict()`` and builds its result with
    ``call.tool_call_id`` / ``call.name``; the executor never passes a bare
    identity-less argument dict and never injects context through global
    state. Argument validation is not the handler's job.

    Persistence order is part of the contract. Version one treats every tool
    call as potentially side-effecting: ``tool.intent`` and
    ``tool.dispatched`` must be durably appended before the handler is
    invoked. If either append fails, the handler is not called, the result is
    a ``failed`` outcome (the call never ran), and the driver fails the turn;
    a failed write must never produce a success settlement. ``tool.settled``
    records a terminal status only. A cancellation or dispatch timeout is
    settled as ``cancelled`` or ``unknown`` and is never retried
    automatically.
    """

    async def execute(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome: ...


@runtime_checkable
class LoopDriver(Protocol):
    """Advances one turn: model request, tool batch, repeat until terminal.

    The driver mints ``step_id`` values through :meth:`TurnStart.step_context`,
    enforces ``TurnStart.budget``, and builds the minimal message history from
    ``TurnStart.history`` (completed turns, loaded by the runtime) plus the
    current ``TurnRequest.text`` and this turn's model/tool results. It does
    not read the store itself and does not ask it for conversation history:
    the runtime passes that snapshot in.

    The driver owns ``request.prepared``, ``model.attempt.*``, and
    ``context.compacted`` events and must not write ``turn.*`` (owned by
    :class:`AgentRuntime`) or ``tool.*`` (owned by
    :class:`ToolExecutor`).

    Persistence order: ``request.prepared`` and ``model.attempt.started`` must
    be durably appended before ``ModelAdapter.complete`` is called. If either
    append fails, the model is not called. Every tool outcome placed in the
    next model request must be terminal; if the executor returns ``RUNNING``
    (or leaves a call unsettled), the driver stops and returns
    ``TurnStatus.FAILED`` so the runtime records ``turn.failed``. It must not
    show the running call to the model, record it as settled, or complete the
    turn.
    """

    async def run(self, turn: TurnStart) -> TurnResult: ...


@runtime_checkable
class SessionStore(Protocol):
    """Append-only event log for sessions, plus one derived conversation read.

    ``append`` returns the stored event with ``sequence`` assigned; the store
    assigns it under the session write lock so ordering matches commit order.
    It returns only after the event is durable and raises on write failure.
    Callers must treat a failed append as fatal for the corresponding step:
    they may not continue the model or tool call, and may not fabricate a
    success settlement or ``turn.completed``.

    ``read_turn`` returns events in sequence order; a turn counts as complete
    only when the log contains ``turn.started`` and a terminal ``turn.*`` event
    with the same ``turn_id``. ``recover`` inspects a reopened session, reports
    unfinished turns and calls, and appends
    :meth:`RecoveryReport.interruption_events` before the session is served,
    so a crashed attempt is never read as still running or as successful.
    Recovery only identifies unfinished work and makes stored request
    snapshots readable (the payload of ``request.prepared``); it does not
    restore Forge tasks, resume execution, or build conversation history.

    ``read_conversation`` provides the message history of the last completed
    turn for the runtime to pass to the driver. It is a projection over
    already stored events, not a second chat database: the documented minimal
    implementation takes the last ``request.prepared`` payload of the newest
    ``COMPLETED`` turn, drops leading system messages, and appends the
    ``turn.completed`` reply as an assistant message. A session with no
    completed turn returns an empty sequence. Unfinished, failed and
    interrupted turns contribute nothing; compression and long-term memory
    are out of scope. Because it reads only persisted events, the same
    history is available after a restart and consecutive turns of one session
    build on it. Implementations must therefore retain the ``request.prepared``
    payload ``messages`` and the ``turn.completed`` payload ``reply``. Domain
    state (Forge tasks, revisions, verification) is not copied here.
    """

    async def append(self, event: RuntimeEvent) -> RuntimeEvent: ...

    async def read_turn(self, session_id: str, turn_id: str) -> Sequence[RuntimeEvent]: ...

    async def read_session(
        self, session_id: str, *, after_sequence: int = 0
    ) -> Sequence[RuntimeEvent]: ...

    async def read_conversation(
        self, session_id: str
    ) -> Sequence[Mapping[str, Any]]: ...

    async def recover(self, session_id: str) -> RecoveryReport: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Capability(Protocol):
    """A pluggable domain feature injected at assembly time.

    A capability may contribute tool registrations and one optional prompt
    fragment. ``prompt_context(context)`` is read with the current step
    context when the assembler builds that step's prompt, on every call; the
    core does not cache it and does not query Forge or another domain service
    for text. Passing the context keeps concurrent sessions from sharing
    global state. It returns ``None`` when the capability has nothing to add.

    ``cancel(session_id, context)`` asks the external system to stop work for
    that session. ``context`` is the interrupted turn's
    :class:`ExecutionContext`, or ``None`` when no local turn was active; the
    host calls this for every session it is asked to cancel, so a completed
    session with a still-running remote task can be stopped. The core keeps no
    domain state: the capability must look up its own authoritative state by
    ``session_id`` and must not expect cached or merged ``DomainRefs``.
    Returning without an exception means the stop request was delivered, not
    that remote work stopped; the outcome is domain state owned by the
    capability, and tool-level results reach the core only through
    ``ToolOutcome``.

    Capabilities own their persisted domain tasks even when the host has not
    seen the session recently; ``aclose`` must release those on shutdown. A
    capability must not append runtime events, subscribe to events, hot-swap
    components, or mutate runtime internals.
    """

    @property
    def name(self) -> str: ...

    def tool_registrations(self) -> Sequence[ToolRegistration]: ...

    def prompt_context(self, context: ExecutionContext) -> str | None: ...

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class AgentRuntime(Protocol):
    """Owns per-session serialization, turn lifecycle, and cancellation.

    ``submit`` recovers the session, loads ``store.read_conversation`` into
    ``TurnStart.history``, mints the turn identity, appends ``input.accepted``
    and ``turn.started``, delegates the loop, appends the terminal ``turn.*``
    event, and returns the result. It is the only writer of input and turn
    events, plus ``session.snapshot.created``. A second submit for the same
    session waits for the active turn; different sessions may run
    concurrently.

    Recovery closes unfinished *turns* by appending ``turn.interrupted`` (the
    runtime owns terminal turn events); the store closes unfinished calls with
    its interruption events. Neither path continues the crashed work.

    ``stop`` immediately rejects new ``submit`` calls (they raise
    ``RuntimeError``) and cancels the active turn. It waits until every
    accepted submit — including turns still queued behind the active one — has
    finished, so a queued turn cannot start after ``stop`` returns. The store
    stays open so capabilities can still cancel and report, and callers can
    audit events. It is idempotent; ``aclose`` closes the store afterwards.

    ``cancel`` requests local cancellation, lets calls already dispatched
    settle as ``cancelled`` or ``unknown``, and returns the last
    :class:`ExecutionContext` of the interrupted turn. It returns ``None``
    when no turn was active or the turn did not end as interrupted. When it
    returns, the turn has reached a terminal state and its terminal event has
    been persisted, so callers may safely shut down next. It does not assert
    that remote work stopped; the host notifies capabilities regardless of
    whether a context was returned.
    """

    async def submit(self, request: TurnRequest) -> TurnResult: ...

    async def stop(self) -> None: ...

    async def cancel(self, session_id: str) -> ExecutionContext | None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class AgentHost(Protocol):
    """Entry-facing facade over an injected runtime and capabilities.

    Construction convention (this phase defines no implementation)::

        AgentHost(runtime: AgentRuntime, capabilities: Sequence[Capability])

    The host receives already-built objects. It must not read config or
    environment, construct stores/adapters/executors/drivers/Forge clients,
    open resources, or start background tasks in its constructor; it only
    keeps the injected references, the capability order, and a closed flag.

    ``submit`` delegates exactly once to ``runtime.submit`` and returns its
    result. It runs no business loop and never touches the model, tools, or
    store directly.

    ``cancel`` first delegates to ``runtime.cancel(session_id)`` to stop the
    local turn, then calls ``capability.cancel(session_id, context)`` for each
    capability in registration order, passing the returned context or
    ``None`` when no turn was active or it did not end as interrupted.
    Notifying capabilities is unconditional: a completed session may still own
    a running remote task. A capability returning normally does not mean
    remote work stopped, and the host must not claim otherwise or convert
    capability errors into a successful stop.

    ``aclose`` shuts down in this fixed order: stop accepting input (further
    ``submit`` calls raise ``RuntimeError``); ``await runtime.stop()``, which
    cancels the active turn and waits for every accepted submit (queued turns
    included) while keeping the store open; then for every session accepted
    through ``submit`` call ``capability.cancel(session_id, None)`` for each
    capability in registration order; close capabilities in registration
    order; finally ``runtime.aclose()`` closes the store. It must not call
    ``runtime.cancel`` per session here: ``stop`` already settled every local
    turn, and a queued turn must never start after capabilities are closed.
    It must be idempotent. Capabilities remain responsible for their
    persisted domain tasks even when the host has not seen the session
    recently.
    """

    async def submit(self, request: TurnRequest) -> TurnResult: ...

    async def cancel(self, session_id: str) -> None: ...

    async def aclose(self) -> None: ...

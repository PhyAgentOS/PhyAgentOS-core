"""Data contracts shared by the agent core components.

This module holds pure data plus the local cancellation and budget
primitives. It must stay dependency-free: no legacy ``agent.loop`` import, no
Forge, verification, skill runtime, or provider SDK. Concrete adapters
(``model.py``, tool implementations, stores) own those dependencies.

Identity rules:

* ``session_id`` / ``turn_id`` / ``step_id`` are minted by the runtime and are
  always non-empty. A turn is one input-driven interaction; a step is one
  model request plus the settlement of the tool calls it produced.
* A model call (:attr:`ModelRequest.model_call_id`) and a tool call
  (:attr:`ToolCall.tool_call_id`) each carry their own id. They are not
  interchangeable and must never be derived from one another.
* ``task_id`` / ``revision_id`` / ``invocation_id`` / ``attempt_id`` belong to
  the Forge domain. This module only stores them as opaque references in
  :class:`DomainRefs`; the core never interprets, invents, or derives them.

All frozen dataclasses here snapshot their mutable inputs (deep copy) so a
caller mutating the original list or dict cannot change a request that was
already recorded. Nested values are plain Python objects; treat them as
read-only.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any

__all__ = [
    "Budget",
    "CancellationToken",
    "DomainRefs",
    "ExecutionContext",
    "ModelCallError",
    "ModelRequest",
    "ModelResponse",
    "ModelStreamDelta",
    "OperationCancelledError",
    "PromptBundle",
    "ToolCall",
    "ToolDefinition",
    "ToolError",
    "ToolHandler",
    "ToolOutcome",
    "ToolPolicy",
    "ToolRegistration",
    "ToolStatus",
    "TurnRequest",
    "TurnResult",
    "TurnStart",
    "TurnStatus",
    "require_non_empty",
]


def require_non_empty(name: str, value: str) -> str:
    """Return ``value`` unchanged, or raise when it is not a non-empty string.

    Identity fields use this so an empty or whitespace-only id cannot be
    persisted and later mistaken for a real one.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_non_empty(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return require_non_empty(name, value)


def _freeze_mapping(name: str, value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return MappingProxyType(copy.deepcopy(dict(value)))


def _freeze_messages(
    messages: Sequence[Mapping[str, Any]], *, allow_empty: bool = False
) -> tuple[Mapping[str, Any], ...]:
    frozen: list[Mapping[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise TypeError("messages must be mappings")
        snapshot = copy.deepcopy(dict(message))
        require_non_empty("message role", snapshot.get("role"))
        frozen.append(MappingProxyType(snapshot))
    if not frozen and not allow_empty:
        raise ValueError("messages must not be empty")
    return tuple(frozen)


def _freeze_tools(tools: Sequence[ToolDefinition]) -> tuple[ToolDefinition, ...]:
    frozen = tuple(tools)
    for tool in frozen:
        if not isinstance(tool, ToolDefinition):
            raise TypeError("tools must be ToolDefinition instances")
    return frozen


class OperationCancelledError(Exception):
    """Local work was asked to stop before it finished.

    This says nothing about remote side effects: raising it only means the
    caller must stop local work. Whether a dispatched physical action stopped
    is reported separately by the tool outcome or capability.
    """

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__(reason or "operation cancelled")


class CancellationToken:
    """Cooperative cancellation signal for one turn.

    ``request()`` only records intent; it never stops remote work and never
    claims a remote stop. Tool handlers and drivers pass the token to
    ``await asyncio.wait_for(...)``-style races via :meth:`wait` and call
    :meth:`raise_if_cancelled` at safe points. One token belongs to one event
    loop; it is not thread-safe.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    def request(self, reason: str | None = None) -> None:
        """Record cancellation; the first reason wins, later calls are no-ops."""
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelledError(self._reason)

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(frozen=True, slots=True)
class Budget:
    """Turn budget: a step ceiling and an optional absolute deadline.

    ``deadline`` is an absolute :func:`time.monotonic` reading, so callers
    must not mix it with wall-clock values. Billing and token accounting are
    deliberately out of scope for the first version.
    """

    max_steps: int
    deadline: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool):
            raise TypeError("max_steps must be an int")
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.deadline is not None:
            if not isinstance(self.deadline, (int, float)) or isinstance(self.deadline, bool):
                raise TypeError("deadline must be a monotonic timestamp")
            object.__setattr__(self, "deadline", float(self.deadline))

    def remaining_seconds(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Which tools this step may use.

    ``allowed_tools=None`` means "everything registered"; a frozenset narrows
    it. ``denied_tools`` always wins. The executor enforces the policy before
    calling a handler; handlers must not receive tools the policy hides.
    """

    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        allowed = None if self.allowed_tools is None else frozenset(self.allowed_tools)
        denied = frozenset(self.denied_tools)
        names = list(allowed) if allowed is not None else []
        names.extend(denied)
        for name in names:
            require_non_empty("tool name", name)
        object.__setattr__(self, "allowed_tools", allowed)
        object.__setattr__(self, "denied_tools", denied)

    def allows(self, tool_name: str) -> bool:
        if tool_name in self.denied_tools:
            return False
        return self.allowed_tools is None or tool_name in self.allowed_tools


@dataclass(frozen=True, slots=True)
class DomainRefs:
    """Opaque references into domain state owned elsewhere (Forge).

    The core copies these through and never reads their meaning: a task id
    existing, or a domain status being ``accepted``, is not a local success
    and not a task success.
    """

    task_id: str | None = None
    revision_id: str | None = None
    invocation_id: str | None = None
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "revision_id", "invocation_id", "attempt_id"):
            _optional_non_empty(name, getattr(self, name))


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Everything one tool execution or model step needs to know.

    It is built per step and never mutated. Tools receive it explicitly
    instead of reading module-level :class:`~contextvars.ContextVar` values,
    so concurrent sessions cannot leak identities into each other.

    ``reply_target`` and ``parent_session_id`` are optional entry concerns
    copied from :class:`TurnRequest`. ``cancellation`` is mutable by design
    (it is a signal, not state), while all identity and budget fields are
    frozen. ``budget`` is the turn budget shared by every step; the driver
    owns step counting.
    """

    session_id: str
    turn_id: str
    step_id: str
    cancellation: CancellationToken
    budget: Budget
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    reply_target: str | None = None
    parent_session_id: str | None = None
    domain_refs: DomainRefs = field(default_factory=DomainRefs)

    def __post_init__(self) -> None:
        require_non_empty("session_id", self.session_id)
        require_non_empty("turn_id", self.turn_id)
        require_non_empty("step_id", self.step_id)
        _optional_non_empty("reply_target", self.reply_target)
        _optional_non_empty("parent_session_id", self.parent_session_id)
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")
        if not isinstance(self.budget, Budget):
            raise TypeError("budget must be a Budget")
        if not isinstance(self.policy, ToolPolicy):
            raise TypeError("policy must be a ToolPolicy")
        if not isinstance(self.domain_refs, DomainRefs):
            raise TypeError("domain_refs must be DomainRefs")

    def with_step(self, step_id: str) -> ExecutionContext:
        """Return the next step's context, sharing token, budget and policy."""
        return replace(self, step_id=step_id)


class ToolStatus(str, Enum):
    """Local settlement state of one tool call.

    The values describe the local call only. ``RUNNING`` is not a result;
    ``UNKNOWN`` means the call was dispatched and the outcome is unknown, so
    it must never be retried automatically. Domain state such as ``accepted``
    is separate and does not map to ``SUCCEEDED``.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class ToolError:
    """Machine-readable error attached to a failed or rejected call."""

    code: str
    message: str

    def __post_init__(self) -> None:
        require_non_empty("code", self.code)
        require_non_empty("message", self.message)


_MODEL_TEXT_PREFIX: Mapping[ToolStatus, str] = {
    ToolStatus.SUCCEEDED: "",
    ToolStatus.FAILED: "Tool failed: ",
    ToolStatus.REJECTED: "Tool call rejected: ",
    ToolStatus.CANCELLED: "Tool call cancelled locally; remote side effects are unconfirmed. ",
    ToolStatus.UNKNOWN: "Tool call result is unknown; do not retry automatically. ",
}


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Structured result of one local tool call.

    ``text`` is the model-facing body; :meth:`for_model` adds a status prefix
    so a cancellation or unknown result is never read as success. The
    executor may truncate ``text`` for budget reasons, but identity, status
    and error fields must never be truncated. ``content`` is optional
    structured data for the caller and is not shown to the model.
    ``domain_refs`` records references returned by a domain tool; the core
    never inspects them to decide success.

    ``RUNNING`` is progress, not a result. It is never placed in a model
    request: the loop driver must fail the turn if a call is still running
    when a step needs its result, instead of showing it or claiming success.

    Error rules keep states distinguishable: a successful call must not carry
    an error, failed/rejected calls must, and a running call cannot carry a
    terminal error.
    """

    tool_call_id: str
    tool_name: str
    status: ToolStatus
    text: str
    content: Mapping[str, Any] | None = None
    error: ToolError | None = None
    domain_refs: DomainRefs = field(default_factory=DomainRefs)

    def __post_init__(self) -> None:
        require_non_empty("tool_call_id", self.tool_call_id)
        require_non_empty("tool_name", self.tool_name)
        status = self.status if isinstance(self.status, ToolStatus) else ToolStatus(self.status)
        object.__setattr__(self, "status", status)
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if self.error is not None and not isinstance(self.error, ToolError):
            raise TypeError("error must be a ToolError")
        if status is ToolStatus.SUCCEEDED and self.error is not None:
            raise ValueError("a succeeded outcome must not carry an error")
        if status in (ToolStatus.FAILED, ToolStatus.REJECTED) and self.error is None:
            raise ValueError(f"{status.value} outcome requires an error")
        if status is ToolStatus.RUNNING and self.error is not None:
            raise ValueError("a running outcome must not carry a terminal error")
        if not isinstance(self.domain_refs, DomainRefs):
            raise TypeError("domain_refs must be DomainRefs")
        object.__setattr__(self, "content", _freeze_mapping("content", self.content))

    @property
    def is_success(self) -> bool:
        return self.status is ToolStatus.SUCCEEDED

    @property
    def is_terminal(self) -> bool:
        """``RUNNING`` is the only non-terminal state."""
        return self.status is not ToolStatus.RUNNING

    def for_model(self) -> str:
        """Render the safe tool-message text for the next model request.

        A ``RUNNING`` outcome raises: unfinished work must be settled first,
        otherwise the model would read pending work as a delivered result.
        """
        if self.status is ToolStatus.RUNNING:
            raise ValueError("a running outcome cannot be shown to the model")
        body = self.text or "(no output)"
        return f"{_MODEL_TEXT_PREFIX[self.status]}{body}"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool as declared to the model: name, description, JSON Schema."""

    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_non_empty("tool name", self.name)
        if any(char.isspace() for char in self.name):
            raise ValueError("tool name must not contain whitespace")
        if not isinstance(self.description, str):
            raise TypeError("description must be a string")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters must be a JSON Schema object")
        if self.parameters.get("type") != "object":
            raise ValueError('parameters must be a JSON Schema object with "type": "object"')
        object.__setattr__(self, "parameters", _freeze_mapping("parameters", self.parameters))

    def to_provider_dict(self) -> dict[str, Any]:
        """Return a plain, provider-serializable function definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(dict(self.parameters)),
            },
        }


# Argument validation against ``ToolDefinition.parameters`` belongs to the
# executor. The handler receives the validated :class:`ToolCall` snapshot, so
# it has the call identity and can return a matching ``ToolOutcome``.
ToolHandler = Callable[["ToolCall", ExecutionContext], Awaitable[ToolOutcome]]


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    """A tool definition plus its async handler.

    The handler signature is ``(call: ToolCall, context: ExecutionContext)``:
    it reads validated arguments with ``call.arguments_dict()`` and builds the
    outcome with ``call.tool_call_id`` / ``call.name``. The executor, not the
    handler, validates the raw payload and schema.

    ``parallel_safe`` is a declaration made by the tool author: only calls
    marked safe may run concurrently inside one step.
    """

    definition: ToolDefinition
    handler: ToolHandler
    parallel_safe: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.definition, ToolDefinition):
            raise TypeError("definition must be a ToolDefinition")
        if not callable(self.handler):
            raise TypeError("handler must be callable and return an awaitable")
        if not isinstance(self.parallel_safe, bool):
            raise TypeError("parallel_safe must be a bool")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call requested by the model.

    ``arguments`` is the parsed payload as a read-only snapshot.
    ``raw_arguments`` keeps the provider's original JSON so a truncated
    payload can be rejected instead of being executed, and
    ``provider_fields`` keeps provider-specific extras that must survive the
    round trip back to the provider.

    The executor validates the raw payload and the JSON Schema, then hands the
    handler a ``ToolCall`` snapshot whose ``arguments`` match the validated
    raw payload while keeping ``tool_call_id``, ``name`` and provider fields.
    The handler takes a plain dict with :meth:`arguments_dict` and uses
    ``tool_call_id`` / ``name`` to build its :class:`ToolOutcome`.
    """

    tool_call_id: str
    name: str
    arguments: Mapping[str, Any]
    raw_arguments: str | None = None
    provider_fields: Mapping[str, Any] | None = None
    function_provider_fields: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        require_non_empty("tool_call_id", self.tool_call_id)
        require_non_empty("tool name", self.name)
        if self.raw_arguments is not None and not isinstance(self.raw_arguments, str):
            raise TypeError("raw_arguments must be a string when provided")
        arguments = _freeze_mapping("arguments", self.arguments)
        if arguments is None:
            raise TypeError("arguments must be a mapping")
        object.__setattr__(self, "arguments", arguments)
        object.__setattr__(
            self, "provider_fields", _freeze_mapping("provider_fields", self.provider_fields)
        )
        object.__setattr__(
            self,
            "function_provider_fields",
            _freeze_mapping("function_provider_fields", self.function_provider_fields),
        )

    def arguments_dict(self) -> dict[str, Any]:
        """Return a plain dict copy of the validated arguments for the handler."""
        return copy.deepcopy(dict(self.arguments))


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """An immutable snapshot of one model call.

    ``messages`` is the exact message history the loop driver built for this
    attempt: ``TurnStart.history`` (completed turns), the current turn input,
    and this turn's settled results. ``tools`` holds the exact visible
    definitions, so a request can be recorded and replayed without re-reading
    mutable session state. ``model_call_id`` identifies this attempt; it is
    unrelated to any ``tool_call_id``. Only non-sensitive generation
    parameters are carried.
    """

    model_call_id: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[ToolDefinition, ...] = ()
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    tool_choice: str | None = None

    def __post_init__(self) -> None:
        require_non_empty("model_call_id", self.model_call_id)
        object.__setattr__(self, "messages", _freeze_messages(self.messages))
        object.__setattr__(self, "tools", _freeze_tools(self.tools))
        _optional_non_empty("model", self.model)
        _optional_non_empty("reasoning_effort", self.reasoning_effort)
        _optional_non_empty("tool_choice", self.tool_choice)
        if self.temperature is not None:
            if not isinstance(self.temperature, (int, float)) or isinstance(self.temperature, bool):
                raise TypeError("temperature must be a number")
            object.__setattr__(self, "temperature", float(self.temperature))
            if not 0.0 <= self.temperature <= 2.0:
                raise ValueError("temperature must be between 0 and 2")
        if self.max_tokens is not None:
            if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool):
                raise TypeError("max_tokens must be an int")
            if self.max_tokens < 1:
                raise ValueError("max_tokens must be >= 1")

    def to_messages(self) -> list[dict[str, Any]]:
        """Return a plain, mutable copy for the provider layer."""
        return [copy.deepcopy(dict(message)) for message in self.messages]

    def to_provider_tools(self) -> list[dict[str, Any]]:
        return [tool.to_provider_dict() for tool in self.tools]


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A completed model answer as returned by a :class:`ModelAdapter`.

    ``content``, ``tool_calls``, ``finish_reason`` and ``usage`` mirror the
    existing provider response. ``finish_reason`` is an opaque non-empty
    provider value (for example ``stop``, ``length``, ``tool_calls``); the
    core does not branch on it beyond requiring that the answer exists.
    ``finish_reason="error"`` is not a valid response: adapters must raise
    :class:`ModelCallError` so no provider failure text can be mistaken for an
    assistant reply.

    ``thinking_blocks`` keeps the provider's thinking payload as a read-only
    snapshot (the existing provider ``list[dict] | None`` semantics, frozen
    here). The core does not interpret it; the loop driver may place it on the
    assistant message so a provider round trip loses nothing.
    """

    content: str | None
    finish_reason: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None
    thinking_blocks: tuple[Mapping[str, Any], ...] | None = None

    def __post_init__(self) -> None:
        require_non_empty("finish_reason", self.finish_reason)
        if self.finish_reason == "error":
            raise ValueError(
                'finish_reason "error" is invalid; raise ModelCallError instead of returning one'
            )
        if self.content is not None and not isinstance(self.content, str):
            raise TypeError("content must be a string or None")
        if self.reasoning_content is not None and not isinstance(self.reasoning_content, str):
            raise TypeError("reasoning_content must be a string or None")
        if self.thinking_blocks is not None:
            blocks: list[Mapping[str, Any]] = []
            for block in self.thinking_blocks:
                if not isinstance(block, Mapping):
                    raise TypeError("thinking_blocks must contain mappings")
                blocks.append(MappingProxyType(copy.deepcopy(dict(block))))
            object.__setattr__(self, "thinking_blocks", tuple(blocks))
        calls = tuple(self.tool_calls)
        for call in calls:
            if not isinstance(call, ToolCall):
                raise TypeError("tool_calls must contain ToolCall instances")
        object.__setattr__(self, "tool_calls", calls)
        usage: dict[str, int] = {}
        for key, value in self.usage.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"usage[{key!r}] must be a non-negative int")
            usage[str(key)] = value
        object.__setattr__(self, "usage", MappingProxyType(usage))

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class ModelStreamDelta:
    """One incremental piece of assistant body text from a streaming adapter.

    Only visible answer text is carried here; provider reasoning stays in the
    terminal :class:`ModelResponse`. The loop driver persists these in batches
    so a terminal answer is displayed before the model call completes, and the
    terminal response remains the single source for conversation history.
    """

    content: str

    def __post_init__(self) -> None:
        # A provider may legally send whitespace as its own delta (a space
        # between tokens, or a newline before a Markdown block). It carries
        # body text and must be kept verbatim; only empty or non-string
        # content is invalid.
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        if not self.content:
            raise ValueError("content must be a non-empty string")


class ModelCallError(RuntimeError):
    """The adapter could not obtain a usable model answer.

    The driver records ``model.attempt.failed`` and must not treat this as an
    empty assistant reply. ``detail`` is the provider's own error text.
    ``usage`` holds whatever token accounting the provider had already
    reported when the stream failed; it is empty when nothing was observed, so
    a failure never invents usage numbers.
    """

    def __init__(
        self,
        model_call_id: str,
        detail: str,
        *,
        usage: Mapping[str, int] | None = None,
    ) -> None:
        self.model_call_id = require_non_empty("model_call_id", model_call_id)
        self.detail = detail
        observed: dict[str, int] = {}
        for key, value in (usage or {}).items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                observed[str(key)] = value
        self.usage: Mapping[str, int] = MappingProxyType(observed)
        super().__init__(detail)


class TurnStatus(str, Enum):
    """Terminal state of one turn, from the runtime's point of view."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What an entry point receives when a turn ends.

    ``INTERRUPTED`` means local work stopped (``/stop``); it does not assert
    that remote work stopped. ``COMPLETED`` must not carry an error, and
    ``FAILED`` must explain why.
    """

    session_id: str
    turn_id: str
    status: TurnStatus
    reply: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        require_non_empty("session_id", self.session_id)
        require_non_empty("turn_id", self.turn_id)
        status = self.status if isinstance(self.status, TurnStatus) else TurnStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reply is not None and not isinstance(self.reply, str):
            raise TypeError("reply must be a string or None")
        if status is TurnStatus.COMPLETED and self.error is not None:
            raise ValueError("a completed turn must not carry an error")
        if status is TurnStatus.FAILED and not self.error:
            raise ValueError("a failed turn requires an error")


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Entry-point input converted into one runtime request.

    ``text`` must contain a non-whitespace character. An entry with nothing
    to say must not open a turn, and a blank prompt is never forwarded as a
    user message. ``reply_target`` is the entry's own delivery address and is
    never interpreted by the core; ``parent_session_id`` marks a subagent
    session.
    """

    session_id: str
    text: str
    reply_target: str | None = None
    parent_session_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty("session_id", self.session_id)
        require_non_empty("text", self.text)
        _optional_non_empty("reply_target", self.reply_target)
        _optional_non_empty("parent_session_id", self.parent_session_id)


@dataclass(frozen=True, slots=True)
class TurnStart:
    """Runtime-owned turn state handed to the loop driver.

    ``turn_id`` and the cancellation token belong to the runtime; step
    identity is minted by the driver, so the driver calls
    :meth:`step_context` for every step instead of receiving a pre-built
    context with a step id it does not own.

    ``history`` is the read-only conversation of already completed turns,
    loaded by the runtime through ``SessionStore.read_conversation``. It is
    empty for a new session and never contains an unfinished turn. The driver
    appends the current ``TurnRequest.text`` (and this turn's results) to it;
    the history is snapshotted here and must not be mutated.
    """

    request: TurnRequest
    turn_id: str
    cancellation: CancellationToken
    budget: Budget
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    domain_refs: DomainRefs = field(default_factory=DomainRefs)
    history: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.request, TurnRequest):
            raise TypeError("request must be a TurnRequest")
        require_non_empty("turn_id", self.turn_id)
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")
        if not isinstance(self.budget, Budget):
            raise TypeError("budget must be a Budget")
        if not isinstance(self.policy, ToolPolicy):
            raise TypeError("policy must be a ToolPolicy")
        if not isinstance(self.domain_refs, DomainRefs):
            raise TypeError("domain_refs must be DomainRefs")
        object.__setattr__(
            self, "history", _freeze_messages(self.history, allow_empty=True)
        )

    def step_context(self, step_id: str) -> ExecutionContext:
        return ExecutionContext(
            session_id=self.request.session_id,
            turn_id=self.turn_id,
            step_id=step_id,
            cancellation=self.cancellation,
            budget=self.budget,
            policy=self.policy,
            reply_target=self.request.reply_target,
            parent_session_id=self.request.parent_session_id,
            domain_refs=self.domain_refs,
        )


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """Assembled prompt for one step: messages plus the visible tools.

    The assembler may only return a subset of the tool definitions it was
    given; it cannot invent tools the caller did not offer.
    """

    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[ToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", _freeze_messages(self.messages))
        object.__setattr__(self, "tools", _freeze_tools(self.tools))

    def to_messages(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(dict(message)) for message in self.messages]

    def to_provider_tools(self) -> list[dict[str, Any]]:
        return [tool.to_provider_dict() for tool in self.tools]

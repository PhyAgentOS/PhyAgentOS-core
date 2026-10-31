"""Core-native child agent for the ``spawn`` capability.

One child run is an ordinary Core turn on its own session id. It is assembled
from the same seven components as a parent turn:

``AgentHostImpl -> AgentRuntimeImpl -> LoopDriverImpl -> ToolExecutorImpl``
plus ``ModelAdapterImpl``, ``PromptAssemblerImpl`` and the shared
``SessionStore``.

The child never reuses the legacy standalone model loop. It gets the parent's
provider selection captured at spawn, a focused system prompt, and only the
general tools supplied by the caller; the recursive ``spawn`` registration is
removed and no Forge capability is injected, so physical execution keeps its
one Gateway path.

The injected store is shared read/write with the parent process and is never
closed here. A child host is used for ``submit``/``cancel`` only; its
``aclose`` is deliberately not called because that would close the shared
store through the runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core import (
    AgentHostImpl,
    AgentRuntimeImpl,
    EventType,
    LoopDriverImpl,
    ModelAdapterImpl,
    PromptAssemblerImpl,
    SessionStore,
    ToolExecutorImpl,
    TurnRequest,
    TurnStatus,
)

if TYPE_CHECKING:
    from ...providers.service import RuntimeSelection

__all__ = [
    "CHILD_SYSTEM_PROMPT",
    "SPAWN_TOOL_NAME",
    "CoreSubagent",
    "ParentBudgetDeniedError",
    "SubagentOutcome",
    "build_child_system_prompt",
    "collect_child_usage",
    "child_tool_is_allowed",
]

SPAWN_TOOL_NAME = "spawn"
# The child gets general tools only. These names can never be offered even if
# an injected general-tools object accidentally contains them.
_CHILD_FORBIDDEN_PREFIXES = ("forge",)

StepUsageObserver = Callable[[Mapping[str, int]], None]
BudgetGuard = Callable[[], bool]


class ParentBudgetDeniedError(RuntimeError):
    """The fixed parent budget guard refused another child model request.

    The check happens before the provider call. No model request and no tool
    side effect is started by this error.
    """


def _effective_context_capacity(
    selection: Any, configured_cap: int | None
) -> int | None:
    """Return the same child window the parent would use for this selection."""
    capacity = getattr(selection, "context_window_tokens", None)
    source = getattr(selection, "context_window_source", None)
    if capacity:
        capacity = int(capacity)
        if source in {"model", "configured", "fallback"}:
            return capacity
        if configured_cap:
            return min(int(configured_cap), capacity)
        return capacity
    if configured_cap:
        return int(configured_cap)
    model_window = getattr(selection, "model_context_window_tokens", None)
    if model_window:
        return int(model_window)
    return None


def _reported_usage(raw: object) -> dict[str, int]:
    """Return only flat non-negative int usage fields the provider reported.

    A field that is absent stays absent; an unreadable usage payload becomes
    ``{}`` rather than invented zeros.
    """
    if not isinstance(raw, Mapping):
        return {}
    usage: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[str(key)] = value
    return usage


class _GuardedProvider:
    """Check the fixed parent budget and report each model call's usage.

    The child still uses ``ModelAdapterImpl`` and its normal request/response
    path; this wrapper only sits between that adapter and the injected provider.
    It checks immediately before every ``chat_with_retry`` and reports exactly
    one mapping after every real response or provider error. A denied request
    is not reported as usage, because no request was made.
    """

    def __init__(
        self,
        provider: Any,
        *,
        budget_guard: BudgetGuard | None,
        step_usage_observer: StepUsageObserver | None,
    ) -> None:
        self._provider = provider
        self._budget_guard = budget_guard
        self._step_usage_observer = step_usage_observer

    def __getattr__(self, name: str) -> Any:
        # ContextPolicy and the token estimator may need provider metadata and
        # counters. Delegate reads, but every model request still goes through
        # this wrapper's chat_with_retry so guard/observation are not bypassed.
        return getattr(self._provider, name)

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        if self._budget_guard is not None and not self._budget_guard():
            raise ParentBudgetDeniedError(
                "the parent turn budget has no room for another child model request"
            )
        try:
            response = await self._provider.chat_with_retry(
                messages=messages, tools=tools, **kwargs
            )
        except Exception as exc:
            self._observe(getattr(exc, "usage", None))
            raise
        self._observe(getattr(response, "usage", None))
        return response

    def _observe(self, raw: object) -> None:
        if self._step_usage_observer is not None:
            self._step_usage_observer(_reported_usage(raw))

CHILD_SYSTEM_PROMPT = """# Subagent

You are a subagent spawned by the main agent to complete one specific task.
Stay focused on that task and finish with a concise final answer. Your final
response is reported back to the main agent. Do not spawn another subagent.
"""

_UNCONFIRMED_STATUSES = frozenset({"unknown", "cancelled"})


def build_child_system_prompt(
    workspace: str, skills_summary: str, *, role: str | None = None
) -> str:
    """Return the focused child prompt with its workspace and Skill catalog.

    The workspace is the same one the default general tools use. The Skill
    summary comes from :class:`~PhyAgentOS.agent.services.skill_documents.
    SkillDocumentLoader`; it is progressive-loading metadata, so the child can
    read a ``SKILL.md`` with ``read_file`` when it needs the full document.
    ``role`` is display/instruction text only; it never changes the tool set,
    provider, or model.
    """
    parts = [CHILD_SYSTEM_PROMPT.strip(), f"## Workspace\n{workspace}"]
    if role:
        parts.append(f"## Role\n{role.strip()}")
    summary = skills_summary.strip()
    if summary:
        parts.append(
            "## Skills\n\nRead SKILL.md with read_file to use a skill.\n\n"
            + summary
        )
    return "\n\n".join(parts)


def child_tool_is_allowed(
    name: str, allowlist: frozenset[str] | None
) -> bool:
    """Return whether a child may see ``name``.

    ``spawn`` is always removed (no recursive spawn). Names with a Forge prefix
    are removed even if an injected general-tools object contains them, so no
    child ever gets a second physical-execution route. ``allowlist`` can only
    narrow the remaining set.
    """
    if name == SPAWN_TOOL_NAME:
        return False
    lowered = name.lower()
    if any(lowered.startswith(prefix) for prefix in _CHILD_FORBIDDEN_PREFIXES):
        return False
    return allowlist is None or name in allowlist


def collect_child_usage(
    events: Sequence[Any], *, turn_id: str | None = None
) -> dict[str, int]:
    """Sum only provider-reported usage fields from stored events.

    A field the provider did not send is absent, never zero. With ``turn_id``
    the result covers one run; without it the call covers the whole session, so
    a parent turn-budget owner can aggregate parent events the same way.
    """
    from .usage import parse_usage

    totals: dict[str, int] = {}
    terminal_usage_events = {
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.MODEL_ATTEMPT_FAILED,
        EventType.MODEL_ATTEMPT_INTERRUPTED,
    }
    for event in events:
        if event.type not in terminal_usage_events:
            continue
        if turn_id is not None and event.turn_id != turn_id:
            continue
        parsed = parse_usage(event.payload.get("usage"))
        for key, value in parsed.as_fields().items():
            if value is not None:
                totals[key] = totals.get(key, 0) + value
    return totals


@dataclass(frozen=True, slots=True)
class SubagentOutcome:
    """One settled child run, including the text a parent entry can deliver.

    ``status`` is ``completed``, ``failed``, ``interrupted`` or ``cancelled``.
    ``unknown_work`` is true when this child dispatched a tool whose durable
    outcome is unknown or locally cancelled; the report then says that work
    must not be retried automatically. ``note`` carries an inspection problem
    (for example, the child event log could not be read) without inventing a
    success or failure.
    """

    session_id: str
    parent_session_id: str
    parent_turn_id: str
    turn_id: str | None
    label: str
    task: str
    status: str
    reply: str | None = None
    error: str | None = None
    unknown_work: bool = False
    note: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed", "interrupted", "cancelled"}:
            raise ValueError(f"unknown child status: {self.status!r}")

    def report(self) -> str:
        """Return a parent-facing result that never upgrades an outcome."""
        if self.status == "completed":
            header = f"[Subagent {self.label!r} completed]"
        elif self.status == "failed":
            header = f"[Subagent {self.label!r} failed]"
        elif self.status == "interrupted":
            header = f"[Subagent {self.label!r} was interrupted locally]"
        else:
            header = f"[Subagent {self.label!r} was cancelled before it ran]"

        lines = [header, "", f"Task: {self.task}", ""]
        if self.status == "completed":
            lines.append("Result:")
            lines.append(self.reply or "(the child finished without final text)")
        elif self.status == "failed":
            lines.extend(
                [
                    "Error:",
                    self.error or "the child reported no error detail",
                    "",
                    "The child did not complete this task; do not report it as done.",
                ]
            )
        elif self.status == "interrupted":
            lines.extend(
                [
                    "No final answer was recorded.",
                    "",
                    "This is a local stop only; it does not prove any remote work "
                    "stopped.",
                ]
            )
        else:
            lines.append("No model call was made.")

        if self.unknown_work:
            lines.extend(
                [
                    "",
                    "Some tools this child dispatched have an unknown or locally "
                    "cancelled outcome. Do not retry them automatically and do not "
                    "treat that work as confirmed.",
                ]
            )
        if self.note:
            lines.extend(["", self.note])
        lines.extend(["", f"Child session: {self.session_id}"])
        return "\n".join(lines)


class CoreSubagent:
    """Build and run one child turn on the shared Core store.

    ``run`` returns a :class:`SubagentOutcome`; expected Core failures come
    back as a failed outcome, not as an exception. ``cancel`` asks the child
    host to stop its local turn and never calls ``aclose`` on that host.
    """

    def __init__(
        self,
        *,
        session_id: str,
        parent_session_id: str,
        parent_turn_id: str,
        label: str,
        task: str,
        selection: RuntimeSelection,
        store: SessionStore,
        general_tools: Any,
        workspace: str,
        max_steps: int,
        temperature: float,
        max_tokens: int,
        role: str | None = None,
        tool_allowlist: frozenset[str] | None = None,
        budget_guard: BudgetGuard | None = None,
        step_usage_observer: StepUsageObserver | None = None,
        performance: Any | None = None,
        context_window_cap: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.parent_session_id = parent_session_id
        self.parent_turn_id = parent_turn_id
        self.label = label
        self.task = task
        self._selection = selection
        self._store = store
        self._general_tools = general_tools
        self._workspace = workspace
        self._max_steps = max_steps
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._role = role
        self._tool_allowlist = tool_allowlist
        self._budget_guard = budget_guard
        self._step_usage_observer = step_usage_observer
        self._performance = performance
        self._context_window_cap = context_window_cap
        self._usage_observer_error: str | None = None
        self._host: AgentHostImpl | None = None

    @property
    def host(self) -> AgentHostImpl | None:
        return self._host

    async def cancel(self) -> None:
        """Ask the child host to settle its local turn; the store stays open."""
        host = self._host
        if host is not None:
            await host.cancel(self.session_id)

    async def run(self) -> SubagentOutcome:
        general = self._general_tools
        registrations = tuple(
            registration
            for registration in general.tool_registrations()
            if child_tool_is_allowed(
                registration.definition.name, self._tool_allowlist
            )
        )
        definitions = tuple(item.definition for item in registrations)

        fragments = ()
        prompt_context = getattr(general, "prompt_context", None)
        if callable(prompt_context):
            fragments = (prompt_context,)

        try:
            from .skill_documents import SkillDocumentLoader

            skills_summary = SkillDocumentLoader(
                Path(self._workspace)
            ).build_skills_summary()
        except Exception:  # noqa: BLE001 - a broken catalog must not hide tools
            skills_summary = ""
        system_prompt = build_child_system_prompt(
            self._workspace, skills_summary, role=self._role
        )

        performance = self._performance
        cache_stable = (
            bool(getattr(performance, "cache_stable_prompts", False))
            if performance is not None
            else False
        )
        max_parallel_tools = (
            int(getattr(performance, "max_parallel_tools", 4))
            if performance is not None
            else 4
        )
        assembler = PromptAssemblerImpl(
            system_prompt,
            fragments=fragments,
            cache_stable=cache_stable,
        )
        provider = self._selection.provider
        step_observer = (
            self._observe_step
            if self._step_usage_observer is not None
            else None
        )
        if self._budget_guard is not None or step_observer is not None:
            provider = _GuardedProvider(
                provider,
                budget_guard=self._budget_guard,
                step_usage_observer=step_observer,
            )
        adapter = ModelAdapterImpl(provider, stream=False)
        executor = ToolExecutorImpl(registrations, self._store)

        context_policy = None
        turn_budget = None
        if performance is not None:
            auto_compact = bool(getattr(performance, "auto_compact", False))
            capacity = _effective_context_capacity(
                self._selection, self._context_window_cap
            )
            if auto_compact and capacity is not None and capacity > self._max_tokens:
                from .context_policy import ContextPolicy

                context_policy = ContextPolicy(
                    getattr(self._store, "store", self._store),
                    provider,
                    model=self._selection.model,
                    capacity=capacity,
                    reserve_tokens=self._max_tokens,
                    threshold=float(
                        getattr(performance, "compaction_threshold", 0.8)
                    ),
                    system_instructions=system_prompt,
                    provider_name=getattr(self._selection, "name", None),
                    effort=self._selection.effort,
                    max_tokens=self._max_tokens,
                )
            from .turn_budget import TurnBudget

            # The parent-owned guard/observer is the authoritative token cap.
            # This local budget must not count the same tokens a second time;
            # it keeps the no-progress check and the configured reserve step.
            turn_budget = TurnBudget(
                max_tokens=None,
                repeated_call_limit=getattr(
                    performance, "repeated_call_limit", None
                ),
                reserve_steps=int(getattr(performance, "reserve_steps", 0)),
            )

        driver = LoopDriverImpl(
            assembler=assembler,
            adapter=adapter,
            executor=executor,
            store=self._store,
            tools=definitions,
            model=self._selection.model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            reasoning_effort=self._selection.effort,
            max_parallel_tools=max_parallel_tools,
            context_policy=context_policy,
            turn_budget=turn_budget,
        )
        runtime = AgentRuntimeImpl(
            driver=driver,
            store=self._store,
            max_steps=self._max_steps,
        )
        host = AgentHostImpl(runtime=runtime, capabilities=(general,))
        self._host = host

        try:
            result = await host.submit(
                TurnRequest(
                    session_id=self.session_id,
                    text=self.task,
                    parent_session_id=self.parent_session_id,
                )
            )
        finally:
            # Retire this child runtime on every path. ``stop`` settles local
            # work and rejects later turns but never closes the shared store;
            # ``aclose`` is deliberately not called. The general-tools object
            # is shared and stays owned by the capability, not by this turn.
            await runtime.stop()

        if result.status is TurnStatus.COMPLETED:
            status = "completed"
        elif result.status is TurnStatus.FAILED:
            status = "failed"
        else:
            status = "interrupted"

        unknown_work, note, usage = await self._inspect_work(result.turn_id)
        if self._usage_observer_error:
            note = (
                f"{note}\n{self._usage_observer_error}"
                if note
                else self._usage_observer_error
            )
        return SubagentOutcome(
            session_id=self.session_id,
            parent_session_id=self.parent_session_id,
            parent_turn_id=self.parent_turn_id,
            turn_id=result.turn_id,
            label=self.label,
            task=self.task,
            status=status,
            reply=result.reply,
            error=result.error,
            unknown_work=unknown_work,
            note=note,
            usage=usage,
        )

    def _observe_step(self, usage: Mapping[str, int]) -> None:
        observer = self._step_usage_observer
        if observer is None:
            return
        try:
            observer(usage)
        except Exception as exc:  # noqa: BLE001 - never fails the child turn
            self._usage_observer_error = (
                "The step usage observer failed "
                f"({type(exc).__name__}: {exc}); budget observation is incomplete."
            )

    async def _inspect_work(
        self, turn_id: str | None
    ) -> tuple[bool, str | None, dict[str, int]]:
        """Check the child log for unconfirmed work and this run's usage."""
        try:
            events = await self._store.read_session(self.session_id)
        except Exception as exc:  # noqa: BLE001 - report the gap, keep the outcome
            return (
                False,
                "The child's tool settlement history could not be read "
                f"({type(exc).__name__}: {exc}); confirmation is incomplete.",
                {},
            )
        return (
            _has_unconfirmed_work(events),
            None,
            collect_child_usage(events, turn_id=turn_id),
        )


def _has_unconfirmed_work(events: Sequence[Any]) -> bool:
    """Fold tool events into "dispatched but not confirmed"."""
    dispatched: set[tuple[str | None, str | None, str]] = set()
    settled: dict[tuple[str | None, str | None, str], str] = {}
    for event in events:
        if event.type is EventType.TOOL_DISPATCHED:
            key = _tool_key(event)
            if key is not None:
                dispatched.add(key)
        elif event.type is EventType.TOOL_SETTLED:
            key = _tool_key(event)
            if key is None:
                continue
            status = event.payload.get("status")
            if isinstance(status, str):
                settled[key] = status
    if any(key not in settled for key in dispatched):
        return True
    return any(status in _UNCONFIRMED_STATUSES for status in settled.values())


def _tool_key(event: Any) -> tuple[str | None, str | None, str] | None:
    payload: Mapping[str, Any] = event.payload
    call_id = payload.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    return (event.turn_id, event.step_id, call_id)

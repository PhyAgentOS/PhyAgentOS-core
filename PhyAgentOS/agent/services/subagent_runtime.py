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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    "SubagentOutcome",
    "build_child_system_prompt",
]

SPAWN_TOOL_NAME = "spawn"

CHILD_SYSTEM_PROMPT = """# Subagent

You are a subagent spawned by the main agent to complete one specific task.
Stay focused on that task and finish with a concise final answer. Your final
response is reported back to the main agent. Do not spawn another subagent.
"""

_UNCONFIRMED_STATUSES = frozenset({"unknown", "cancelled"})


def build_child_system_prompt(workspace: str, skills_summary: str) -> str:
    """Return the focused child prompt with its workspace and Skill catalog.

    The workspace is the same one the default general tools use. The Skill
    summary comes from :class:`~PhyAgentOS.agent.services.skill_documents.
    SkillDocumentLoader`; it is progressive-loading metadata, so the child can
    read a ``SKILL.md`` with ``read_file`` when it needs the full document.
    """
    parts = [CHILD_SYSTEM_PROMPT.strip(), f"## Workspace\n{workspace}"]
    summary = skills_summary.strip()
    if summary:
        parts.append(
            "## Skills\n\nRead SKILL.md with read_file to use a skill.\n\n"
            + summary
        )
    return "\n\n".join(parts)


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
            if registration.definition.name != SPAWN_TOOL_NAME
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
            self._workspace, skills_summary
        )

        assembler = PromptAssemblerImpl(system_prompt, fragments=fragments)
        adapter = ModelAdapterImpl(self._selection.provider, stream=False)
        executor = ToolExecutorImpl(registrations, self._store)
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

        unknown_work, note = await self._inspect_work()
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
        )

    async def _inspect_work(self) -> tuple[bool, str | None]:
        """Check the child log for dispatched work that is not confirmed."""
        try:
            events = await self._store.read_session(self.session_id)
        except Exception as exc:  # noqa: BLE001 - report the gap, keep the outcome
            return (
                False,
                "The child's tool settlement history could not be read "
                f"({type(exc).__name__}: {exc}); confirmation is incomplete.",
            )
        return _has_unconfirmed_work(events), None


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

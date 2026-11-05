"""Agent-level single-Action orchestration and terminal tool barrier."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from PhyAgentOS.agent.capabilities import forge_capability as forge_module
from PhyAgentOS.agent.capabilities.forge_capability import ForgeCapability
from PhyAgentOS.agent.capabilities.skill_access_capability import SkillAccessCapability
from PhyAgentOS.agent.composition import compose_agent
from PhyAgentOS.agent.core import SQLiteSessionStore, TurnRequest, TurnStatus
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.agent.services.skill_documents import SkillDocumentLoader
from PhyAgentOS.agent.services.turn_activation import TurnSkillActivationManager
from PhyAgentOS.forge.task import (
    AgentTaskRecord,
    AgentTaskStatus,
    PlanRevision,
    ToolExecutionRecord,
)
from PhyAgentOS.verification.contracts import TaskVerificationContract

_RUN_ACTION = "forge_task_run_action"


def _context(turn: str = "turn-1", session: str = "session-1") -> ExecutionContext:
    return ExecutionContext(
        session_id=session,
        turn_id=turn,
        step_id=f"{turn}:step:1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=6),
    )


def _invoke_arguments() -> dict[str, Any]:
    return {
        "task_description": "reset the FR3",
        "activation_id": "activation_abc123",
        "verification": {"mode": "off"},
        "tool_id": "motion.move_pose",
        "arguments": {"target_pose": {"x": 0.1}},
    }


def _task_record(
    *,
    status: str = "succeeded",
    execution_records: list[dict[str, Any]] | None = None,
    evidence_errors: list[str] | None = None,
    verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "task_id": "task_1",
        "status": status,
        "revisions": [
            {
                "revision_id": "revision_1",
                "number": 1,
                "execution_records": execution_records or [],
            }
        ],
        "active_revision_id": "revision_1",
    }
    if evidence_errors:
        record["evidence_errors"] = evidence_errors
    if verdict is not None:
        record["verdict"] = verdict
    return record


def _action_record(
    *,
    status: str = "succeeded",
    invocation_id: str = "inv_1",
    attempt_id: str = "attempt_1",
) -> dict[str, Any]:
    return {
        "record_id": "record_1",
        "revision_id": "revision_1",
        "tool_id": "motion.move_pose",
        "semantics": "action",
        "status": status,
        "invocation_id": invocation_id,
        "attempt_id": attempt_id,
        "response": {"reset": "done"},
    }


class _Coordinator:
    def __init__(
        self, *, terminal: bool = True, status: str = "succeeded"
    ) -> None:
        self.record = SimpleNamespace(
            terminal=terminal,
            status=AgentTaskStatus(status),
        )
        self.store = SimpleNamespace(latest_for_session=lambda _session: None)

    def get_task(self, _task_id: str) -> Any:
        return self.record


class _ScriptedForge:
    """Record every internal Forge operation and answer with fixed payloads."""

    def __init__(
        self,
        *,
        create: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        start: dict[str, Any] | None = None,
        finalize: dict[str, Any] | None = None,
        start_error: BaseException | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.session_ids: list[str | None] = []
        self.after = dict(after or {})
        self.create = create if create is not None else {
            "ok": True,
            "data": {
                "task_id": "task_1",
                "revision_id": "revision_1",
                "status": "executing",
            },
        }
        self.context = context if context is not None else {
            "ok": True,
            "data": {"tool": {"tool_id": "motion.move_pose"}, "context": {"ready": True}},
        }
        self.start = start if start is not None else {
            "ok": True,
            "data": {
                "status": "succeeded",
                "invocation_id": "inv_1",
                "attempt_id": "attempt_1",
            },
        }
        self.finalize = finalize if finalize is not None else {
            "ok": True,
            "data": _task_record(execution_records=[_action_record()]),
        }
        self.start_error = start_error

    def _run_hook(self, name: str) -> None:
        hook = self.after.get(name)
        if hook is not None:
            hook()

    async def task(
        self,
        name: str,
        _coordinator: Any,
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        self.session_ids.append(session_id)
        if name == "forge_task_create":
            payload = self.create
        elif name == "forge_task_finalize":
            payload = self.finalize
        else:
            raise AssertionError(f"unexpected task tool {name}")
        self._run_hook(name)
        return payload

    async def api(
        self,
        name: str,
        _client: Any,
        _coordinator: Any,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if name == "forge_tool_context":
            payload = self.context
        elif name == "forge_tool_start_action":
            if self.start_error is not None:
                raise self.start_error
            payload = self.start
        else:
            raise AssertionError(f"unexpected Tool API tool {name}")
        self._run_hook(name)
        return payload


def _patched_capability(
    monkeypatch: pytest.MonkeyPatch,
    script: _ScriptedForge,
    *,
    coordinator: _Coordinator | None = None,
) -> ForgeCapability:
    monkeypatch.setattr(forge_module, "execute_task_tool", script.task)
    monkeypatch.setattr(forge_module, "execute_tool_api_tool", script.api)
    capability = ForgeCapability(
        (coordinator or _Coordinator()),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
    )
    return capability


async def _run(
    capability: ForgeCapability, arguments: dict[str, Any] | None = None, *,
    context: ExecutionContext | None = None,
) -> ToolOutcome:
    registrations = {
        item.definition.name: item for item in capability.tool_registrations()
    }
    return await registrations[_RUN_ACTION].handler(
        ToolCall(
            tool_call_id="call-1",
            name=_RUN_ACTION,
            arguments=arguments or _invoke_arguments(),
        ),
        context or _context(),
    )


async def test_single_action_runs_handlers_in_order_once_and_returns_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge()
    capability = _patched_capability(monkeypatch, script)

    outcome = await _run(capability)

    assert [name for name, _ in script.calls] == [
        "forge_task_create",
        "forge_tool_context",
        "forge_tool_start_action",
        "forge_task_finalize",
    ]
    create_args = script.calls[0][1]
    assert create_args["activation_id"] == "activation_abc123"
    assert create_args["task_description"] == "reset the FR3"
    assert create_args["verification"] == {"mode": "off"}
    assert script.session_ids[0] == "session-1"

    context_args = script.calls[1][1]
    assert context_args == {"tool_id": "motion.move_pose"}
    start_args = script.calls[2][1]
    assert start_args["task_id"] == "task_1"
    assert start_args["tool_id"] == "motion.move_pose"
    assert start_args["arguments"] == {"target_pose": {"x": 0.1}}
    assert start_args["wait_ms"] == 60_000
    assert script.calls[3][1] == {"task_id": "task_1"}
    assert [name for name, _ in script.calls].count("forge_tool_start_action") == 1

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.domain_refs.task_id == "task_1"
    assert outcome.domain_refs.invocation_id == "inv_1"
    data = outcome.content["data"]
    assert data["task_id"] == "task_1"
    assert data["status"] == "succeeded"
    assert data["action"]["status"] == "succeeded"
    assert data["action"]["response"] == {"reset": "done"}
    # Compact means the full AgentTask record was not copied through.
    assert "revisions" not in data
    assert "execution_records" not in data
    assert json.loads(outcome.text)["data"]["task_id"] == "task_1"


async def test_pending_action_is_unknown_never_finalized_and_not_resent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge(
        start={
            "ok": True,
            "data": {
                "status": "pending",
                "invocation_id": "inv_pending",
                "attempt_id": "attempt_pending",
            },
        }
    )
    capability = _patched_capability(
        monkeypatch, script, coordinator=_Coordinator(terminal=False, status="executing")
    )

    outcome = await _run(capability)

    assert outcome.status is ToolStatus.UNKNOWN
    assert not outcome.is_success
    assert "Do not resend" in outcome.for_model()
    assert outcome.domain_refs.invocation_id == "inv_pending"
    assert outcome.domain_refs.attempt_id == "attempt_pending"
    assert [name for name, _ in script.calls].count("forge_tool_start_action") == 1
    assert "forge_task_finalize" not in [name for name, _ in script.calls]
    assert capability.suppress_tools(_context()) is False


async def test_timeout_start_is_unknown_and_no_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge(
        start={
            "ok": False,
            "error": {
                "type": "timeout",
                "message": "gateway timed out",
                "invocation_id": "inv_timeout",
                "attempt_id": "attempt_timeout",
            },
        }
    )
    capability = _patched_capability(
        monkeypatch, script, coordinator=_Coordinator(terminal=False, status="executing")
    )

    outcome = await _run(capability)

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.domain_refs.invocation_id == "inv_timeout"
    assert "remote state is unknown" in outcome.for_model()
    assert "forge_task_finalize" not in [name for name, _ in script.calls]


async def test_failed_finalize_is_reported_as_failure_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge(
        finalize={
            "ok": True,
            "data": _task_record(
                status="failed",
                execution_records=[_action_record()],
                evidence_errors=["Evidence Bundle is incomplete: after:robot_state"],
                verdict={"verdict": "failure", "reason": "gripper did not move"},
            ),
        }
    )
    capability = _patched_capability(monkeypatch, script)

    outcome = await _run(capability)

    assert outcome.status is ToolStatus.FAILED
    assert not outcome.is_success
    assert outcome.error is not None
    assert "not success" in outcome.for_model()
    assert "gripper did not move" in outcome.error.message
    data = outcome.content["data"]
    assert data["status"] == "failed"
    assert data["verdict"]["verdict"] == "failure"
    assert data["evidence_errors"] == [
        "Evidence Bundle is incomplete: after:robot_state"
    ]


async def test_create_failure_stops_before_any_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge(
        create={
            "ok": False,
            "error": {
                "type": "agent_task",
                "code": "forge_agent_task",
                "message": "an earlier task is still non-terminal",
            },
        }
    )
    capability = _patched_capability(monkeypatch, script)

    outcome = await _run(capability)

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "forge_agent_task"
    assert [name for name, _ in script.calls] == ["forge_task_create"]


async def test_unknown_readiness_check_stops_before_any_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge(
        context={
            "ok": False,
            "error": {"type": "gateway_tool_api", "message": "gateway unavailable"},
        }
    )
    capability = _patched_capability(
        monkeypatch, script, coordinator=_Coordinator(terminal=False, status="executing")
    )

    outcome = await _run(capability)

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.content["task_id"] == "task_1"
    assert [name for name, _ in script.calls] == [
        "forge_task_create",
        "forge_tool_context",
    ]
    assert "forge_tool_start_action" not in [name for name, _ in script.calls]


async def test_cancelled_action_never_becomes_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge(
        start={
            "ok": True,
            "data": {
                "status": "cancelled",
                "invocation_id": "inv_1",
                "attempt_id": "attempt_1",
            },
        },
        finalize={
            "ok": True,
            "data": _task_record(
                status="cancelled",
                execution_records=[_action_record(status="cancelled")],
            ),
        },
    )
    capability = _patched_capability(monkeypatch, script)

    outcome = await _run(capability)

    assert outcome.status is ToolStatus.CANCELLED
    assert not outcome.is_success
    assert outcome.content["data"]["status"] == "cancelled"


async def test_local_cancellation_propagates_instead_of_being_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge(start_error=asyncio.CancelledError())
    capability = _patched_capability(monkeypatch, script)

    with pytest.raises(asyncio.CancelledError):
        await _run(capability)


async def test_pre_dispatch_action_failure_does_not_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge(
        start={
            "ok": False,
            "error": {
                "type": "agent_task",
                "code": "forge_agent_task",
                "message": "Forge Tool 'motion.move_pose' is no longer ready",
            },
        }
    )
    capability = _patched_capability(
        monkeypatch, script, coordinator=_Coordinator(terminal=False, status="executing")
    )

    outcome = await _run(capability)

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert "no longer ready" in outcome.error.message
    assert [name for name, _ in script.calls] == [
        "forge_task_create",
        "forge_tool_context",
        "forge_tool_start_action",
    ]
    assert capability.suppress_tools(_context()) is False


async def test_cancellation_before_create_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge()
    capability = _patched_capability(monkeypatch, script)
    context = _context()
    context.cancellation.request("user stopped")

    outcome = await _run(capability, context=context)

    assert outcome.status is ToolStatus.CANCELLED
    assert outcome.error is not None
    assert outcome.error.code == "forge_single_action_cancelled"
    assert "no task was created" in outcome.text
    assert script.calls == []


async def test_cancellation_after_context_never_starts_the_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    script = _ScriptedForge(
        after={"forge_tool_context": lambda: context.cancellation.request("user stopped")}
    )
    capability = _patched_capability(
        monkeypatch, script, coordinator=_Coordinator(terminal=False, status="executing")
    )

    outcome = await _run(capability, context=context)

    assert outcome.status is ToolStatus.CANCELLED
    assert outcome.error is not None
    assert outcome.error.code == "forge_single_action_cancelled"
    assert "the Action was not started" in outcome.text
    assert outcome.content["data"]["task_id"] == "task_1"
    names = [name for name, _ in script.calls]
    assert names == ["forge_task_create", "forge_tool_context"]
    assert "forge_tool_start_action" not in names
    assert "forge_task_finalize" not in names
    assert capability.suppress_tools(context) is False


async def test_cancellation_after_action_dispatch_does_not_resend_or_claim_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    script = _ScriptedForge(
        after={"forge_tool_start_action": lambda: context.cancellation.request("user stopped")}
    )
    capability = _patched_capability(monkeypatch, script)

    outcome = await _run(capability, context=context)

    assert outcome.status is ToolStatus.CANCELLED
    assert not outcome.is_success
    assert outcome.error is not None
    assert outcome.error.code == "forge_single_action_cancelled"
    assert "already dispatched at most once" in outcome.text
    assert "do not resend" in outcome.text
    assert outcome.domain_refs.task_id == "task_1"
    assert outcome.domain_refs.invocation_id == "inv_1"
    assert outcome.domain_refs.attempt_id == "attempt_1"
    assert [name for name, _ in script.calls].count("forge_tool_start_action") == 1
    assert "forge_task_finalize" not in [name for name, _ in script.calls]
    # No finalize means no terminal barrier, so the next step is still normal.
    assert capability.suppress_tools(context) is False


# ---------------------------------------------------------------------------
# terminal barrier: after a definitive finalize the next step offers no tools
# ---------------------------------------------------------------------------


def _finalize_capability(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, Any],
    terminal: bool,
    status: str = "succeeded",
) -> ForgeCapability:
    script = _ScriptedForge(finalize=payload)
    return _patched_capability(
        monkeypatch,
        script,
        coordinator=_Coordinator(terminal=terminal, status=status),
    )


async def test_terminal_finalize_hides_every_tool_and_requests_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = _finalize_capability(
        monkeypatch,
        payload={"ok": True, "data": _task_record(status="succeeded")},
        terminal=True,
    )
    registrations = {
        item.definition.name: item for item in capability.tool_registrations()
    }
    context = _context()

    outcome = await registrations["forge_task_finalize"].handler(
        ToolCall(
            tool_call_id="call-finalize",
            name="forge_task_finalize",
            arguments={"task_id": "task_1"},
        ),
        context,
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert capability.suppress_tools(context) is True
    assert capability.hidden_tools(context) == frozenset(registrations)
    prompt = capability.prompt_context(context)
    assert prompt is not None
    assert "status=succeeded" in prompt
    assert "every tool is unavailable" in prompt
    assert "Claim success only if status=succeeded" in prompt
    # A later turn starts with normal visibility again.
    assert capability.suppress_tools(_context(turn="turn-2")) is False


async def test_nonterminal_finalize_does_not_trigger_the_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = _finalize_capability(
        monkeypatch,
        payload={"ok": True, "data": _task_record(status="executing")},
        terminal=False,
        status="executing",
    )
    registrations = {
        item.definition.name: item for item in capability.tool_registrations()
    }
    context = _context()

    outcome = await registrations["forge_task_finalize"].handler(
        ToolCall(
            tool_call_id="call-finalize",
            name="forge_task_finalize",
            arguments={"task_id": "task_1"},
        ),
        context,
    )

    assert outcome.status is ToolStatus.UNKNOWN
    assert capability.suppress_tools(context) is False
    assert capability.prompt_context(context) is None


async def test_unknown_or_failed_finalize_does_not_trigger_the_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for payload in (
        {"ok": False, "error": {"type": "timeout", "message": "no answer"}},
        {"ok": False, "error": {"type": "runtime", "message": "no verifier"}},
    ):
        capability = _finalize_capability(
            monkeypatch,
            payload=payload,
            terminal=True,
            status="succeeded",
        )
        registrations = {
            item.definition.name: item for item in capability.tool_registrations()
        }
        context = _context()

        outcome = await registrations["forge_task_finalize"].handler(
            ToolCall(
                tool_call_id="call-finalize",
                name="forge_task_finalize",
                arguments={"task_id": "task_1"},
            ),
            context,
        )

        assert not outcome.is_success
        assert capability.suppress_tools(context) is False


@dataclass(frozen=True)
class _ProviderToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None


@dataclass(frozen=True)
class _ProviderResponse:
    content: str | None = None
    finish_reason: str = "stop"
    tool_calls: tuple[_ProviderToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)


class _Provider:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        del kwargs
        self.calls.append({"messages": messages, "tools": tools})
        return await self._responder(len(self.calls), messages, tools)


class _SideCapability:
    """Stands in for exec/image capabilities that must also be suppressed."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self._registrations = tuple(
            ToolRegistration(
                definition=ToolDefinition(
                    name=name,
                    description=f"test {name}",
                    parameters={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                ),
                handler=self._handler(name),
            )
            for name in ("exec", "screenshot")
        )

    @property
    def name(self) -> str:
        return "side"

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return self._registrations

    def prompt_context(self, context: ExecutionContext) -> str | None:
        del context
        return None

    def _handler(self, name: str):
        async def handle(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            del context
            self.executed.append(name)
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.SUCCEEDED,
                text="ran",
            )

        return handle

    async def cancel(self, session_id: str, context: ExecutionContext | None) -> None:
        del session_id, context

    async def aclose(self) -> None:
        return None


class _HostCoordinator:
    def __init__(self, record) -> None:
        self.record = record
        self.store = SimpleNamespace(latest_for_session=lambda _session: None)

    def get_task(self, _task_id: str) -> Any:
        return self.record

    async def finalize_task(self, _task_id: str) -> Any:
        return self.record


def _agent_task(status: str):
    from PhyAgentOS.forge.task import AgentTaskRecord, PlanRevision

    return AgentTaskRecord(
        task_id="task_1",
        task_description="reset the FR3",
        verification=TaskVerificationContract(mode="off"),
        status=status,
        revisions=[PlanRevision(revision_id="revision_1", number=1, reason="initial")],
        active_revision_id="revision_1",
        origin_session_key="session-1",
    )


class _LifecycleClient:
    """Minimal Gateway client for the context read used by the composite tool."""

    def __init__(self) -> None:
        self.tool_calls = 0
        self.context_calls = 0

    async def get_tool(self, tool_id: str) -> dict[str, Any]:
        self.tool_calls += 1
        return {"ok": True, "data": {"tool_id": tool_id, "semantics": "action"}}

    async def get_tool_context(self, tool_id: str) -> dict[str, Any]:
        self.context_calls += 1
        return {
            "ok": True,
            "data": {"tool_id": tool_id, "ready": True, "binding_error": None},
        }


class _LifecycleCoordinator:
    """Small real-shaped AgentTask store for the end-to-end host path."""

    def __init__(self) -> None:
        self.task: AgentTaskRecord | None = None
        self.created: list[dict[str, Any]] = []
        self.started: list[tuple[str, str, dict[str, Any], int | None]] = []
        self.finalized: list[str] = []
        self.store = SimpleNamespace(
            latest_for_session=self._latest,
            nonterminal_for_session=self._nonterminal,
        )

    def _latest(self, _session: str) -> AgentTaskRecord | None:
        return self.task

    def _nonterminal(self, _session: str) -> AgentTaskRecord | None:
        if self.task is not None and not self.task.terminal:
            return self.task
        return None

    async def create_task(
        self,
        *,
        task_description: str,
        verification: TaskVerificationContract,
        activation_id: str | None = None,
        origin_session_key: str | None = None,
    ) -> AgentTaskRecord:
        self.created.append(
            {
                "task_description": task_description,
                "activation_id": activation_id,
                "origin_session_key": origin_session_key,
            }
        )
        self.task = AgentTaskRecord(
            task_id="task_1",
            task_description=task_description,
            verification=verification,
            revisions=[
                PlanRevision(
                    revision_id="revision_1",
                    number=1,
                    reason="initial plan",
                )
            ],
            active_revision_id="revision_1",
            origin_session_key=origin_session_key,
        )
        return self.task

    def get_task(self, _task_id: str) -> AgentTaskRecord:
        assert self.task is not None
        return self.task

    async def start_action(
        self,
        task_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int | None = None,
        wait_ms: int | None = None,
    ) -> dict[str, Any]:
        del timeout_ms
        assert self.task is not None
        self.started.append((task_id, tool_id, dict(arguments), wait_ms))
        self.task.revisions[0].execution_records.append(
            ToolExecutionRecord(
                record_id="record_1",
                revision_id="revision_1",
                tool_id=tool_id,
                semantics="action",
                caller_id="paos:test",
                status="succeeded",
                invocation_id="inv_1",
                attempt_id="attempt_1",
                response={"reset": "done"},
            )
        )
        return {
            "ok": True,
            "data": {
                "status": "succeeded",
                "invocation_id": "inv_1",
                "attempt_id": "attempt_1",
            },
        }

    async def finalize_task(self, task_id: str) -> AgentTaskRecord:
        assert self.task is not None
        self.finalized.append(task_id)
        self.task.status = AgentTaskStatus.SUCCEEDED
        return self.task

    async def cancel_task(self, task_id: str, *, reason: str) -> AgentTaskRecord:
        del reason
        assert self.task is not None
        assert self.task.task_id == task_id
        return self.task


async def test_host_single_action_path_uses_exactly_three_model_requests(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "primary"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: primary\ndescription: reset workflow\n---\n# primary\n"
        "Use motion.move_pose for the reset.\n"
    )
    documents = SkillDocumentLoader(
        tmp_path,
        builtin_skills_dir=tmp_path / "builtin",
        installed_skills_dir=tmp_path / "installed",
        runtime_availability_provider=lambda _name: True,
    )
    activations = TurnSkillActivationManager(documents)
    skill_capability = SkillAccessCapability(activations)
    coordinator = _LifecycleCoordinator()
    client = _LifecycleClient()
    forge_capability = ForgeCapability(
        coordinator,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
    )
    seen_tools: list[list[str] | None] = []
    action_arguments = {
        "target_pose": {"x": 0.1},
    }

    def activation_id(messages) -> str:
        for message in messages:
            if message.get("role") != "tool":
                continue
            try:
                payload = json.loads(message.get("content") or "")
            except (TypeError, ValueError):
                continue
            activation = payload.get("activation") if isinstance(payload, dict) else None
            if isinstance(activation, dict):
                found = activation.get("activation_id")
                if isinstance(found, str):
                    return found
        raise AssertionError("the activate_skill result did not carry an activation_id")

    async def responder(call_no, messages, tools):
        names = [item["function"]["name"] for item in tools or []]
        seen_tools.append([item["function"]["name"] for item in tools] if tools else None)
        if call_no == 1:
            assert "activate_skill" in names
            assert "forge_task_run_action" not in names
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(
                    _ProviderToolCall(
                        id="call-activate",
                        name="activate_skill",
                        arguments={"name": "primary", "role": "primary"},
                        raw_arguments=json.dumps(
                            {"name": "primary", "role": "primary"}
                        ),
                    ),
                ),
            )
        if call_no == 2:
            assert "forge_task_run_action" in names
            arguments = {
                "task_description": "reset the FR3",
                "activation_id": activation_id(messages),
                "verification": {"mode": "off"},
                "tool_id": "motion.move_pose",
                "arguments": action_arguments,
            }
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(
                    _ProviderToolCall(
                        id="call-run-action",
                        name="forge_task_run_action",
                        arguments=arguments,
                        raw_arguments=json.dumps(arguments),
                    ),
                ),
            )
        assert not names
        return _ProviderResponse(
            content="The FR3 reset completed.", finish_reason="stop"
        )

    provider = _Provider(responder)
    assembly = compose_agent(
        provider=provider,  # type: ignore[arg-type]
        store=SQLiteSessionStore(tmp_path / "agent.db"),
        coordinator=coordinator,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        system_instructions="base instructions",
        model="fake-model",
        max_steps=6,
        additional_capabilities=(skill_capability,),
        forge_capability=forge_capability,
    )
    try:
        result = await assembly.host.submit(
            TurnRequest(session_id="session-1", text="reset the FR3")
        )
    finally:
        await assembly.host.aclose()

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "The FR3 reset completed."
    assert len(provider.calls) == 3
    assert seen_tools[0] is not None and "activate_skill" in seen_tools[0]
    assert seen_tools[1] is not None
    assert "forge_task_run_action" in seen_tools[1]
    assert "forge_task_finalize" not in seen_tools[1]
    # The terminal barrier removed every tool for the final answer step.
    assert seen_tools[2] is None

    assert len(coordinator.created) == 1
    assert coordinator.created[0]["activation_id"].startswith("activation_")
    assert coordinator.created[0]["origin_session_key"] == "session-1"
    assert len(coordinator.started) == 1
    assert coordinator.started[0][0] == "task_1"
    assert coordinator.started[0][1] == "motion.move_pose"
    assert coordinator.started[0][2] == action_arguments
    assert coordinator.started[0][3] == 60_000
    assert coordinator.finalized == ["task_1"]
    assert client.context_calls == 1

    final_prompt = "\n".join(
        str(message.get("content")) for message in provider.calls[2]["messages"]
    )
    assert "finalized with status=succeeded" in final_prompt
    assert "every tool is unavailable" in final_prompt


async def test_host_next_step_has_no_tools_and_rejects_fabricated_exec(
    tmp_path: Path,
) -> None:
    record = _agent_task("succeeded")
    coordinator = _HostCoordinator(record)
    side = _SideCapability()
    prompts: list[str] = []

    async def responder(call_no, messages, tools):
        prompts.append(
            "\n".join(str(message.get("content")) for message in messages)
        )
        if call_no == 1:
            assert tools is not None
            names = {item["function"]["name"] for item in tools}
            assert "forge_task_finalize" in names
            assert "exec" in names and "screenshot" in names
            arguments = {"task_id": "task_1"}
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(
                    _ProviderToolCall(
                        id="call-finalize",
                        name="forge_task_finalize",
                        arguments=arguments,
                        raw_arguments=json.dumps(arguments),
                    ),
                ),
            )
        if call_no == 2:
            # The terminal barrier removed every offered tool, including the
            # side capability's exec/image tools.
            assert not tools
            # Even a fabricated call is not authorized; it is rejected and the
            # handler never runs.
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(
                    _ProviderToolCall(
                        id="call-exec",
                        name="exec",
                        arguments={},
                        raw_arguments="{}",
                    ),
                ),
            )
        assert not tools
        return _ProviderResponse(content="The reset finished.", finish_reason="stop")

    provider = _Provider(responder)
    assembly = compose_agent(
        provider=provider,  # type: ignore[arg-type]
        store=SQLiteSessionStore(tmp_path / "agent.db"),
        coordinator=coordinator,  # type: ignore[arg-type]
        system_instructions="base instructions",
        model="fake-model",
        max_steps=4,
        additional_capabilities=(side,),
    )
    try:
        result = await assembly.host.submit(
            TurnRequest(session_id="session-1", text="reset the FR3")
        )
    finally:
        await assembly.host.aclose()

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "The reset finished."
    assert side.executed == []
    assert len(provider.calls) == 3
    assert "finalized with status=succeeded" in prompts[1]
    assert "every tool is unavailable" in prompts[1]


async def test_activation_id_is_passed_to_the_existing_create_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _ScriptedForge()
    capability = _patched_capability(monkeypatch, script)

    await _run(
        capability,
        {
            **_invoke_arguments(),
            "activation_id": "activation_second",
        },
        context=_context(session="cli:test"),
    )

    assert script.session_ids[0] == "cli:test"
    assert script.calls[0][1]["activation_id"] == "activation_second"

"""Integration tests for the agent-side Forge capability."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PhyAgentOS.agent.capabilities.forge import ForgeCapability
from PhyAgentOS.agent.composition import compose_agent
from PhyAgentOS.agent.core import (
    EventType,
    SQLiteSessionStore,
    TurnRequest,
    TurnStatus,
)
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolStatus,
)
from PhyAgentOS.agent_legacy.tools.forge_task import build_forge_task_tools
from PhyAgentOS.agent_legacy.tools.forge_tool_api import build_forge_tool_api_tools
from PhyAgentOS.forge.task import (
    AgentTaskRecord,
    AgentTaskStatus,
    PlanRevision,
    ToolExecutionRecord,
)
from PhyAgentOS.forge.tool_client import ForgeToolAPIError, ForgeToolAPITimeoutError
from PhyAgentOS.verification.contracts import TaskVerificationContract


def _task(
    *,
    task_id: str = "task_1",
    status: str = "executing",
    session: str | None = "session-1",
    execution_records: list[ToolExecutionRecord] | None = None,
) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=task_id,
        task_description="test task",
        verification=TaskVerificationContract(mode="off"),
        status=status,
        revisions=[
            PlanRevision(
                revision_id="revision_1",
                number=1,
                reason="initial plan",
                execution_records=execution_records or [],
            )
        ],
        active_revision_id="revision_1",
        origin_session_key=session,
    )


class _FakeClient:
    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def get_tool(self, tool_id: str) -> dict[str, Any]:
        return {"ok": True, "data": {"tool_id": tool_id, "semantics": "action"}}

    async def get_tool_context(self, tool_id: str) -> dict[str, Any]:
        return {"ok": True, "data": {"tool_id": tool_id, "ready": True}}

    async def invoke_query_tool(self, tool_id: str, arguments: dict, **_: Any) -> dict:
        self.calls.append(("invoke_query_tool", (tool_id, arguments)))
        return {"ok": True, "data": {"tool_id": tool_id, "response": arguments}}

    async def invocation_status(self, invocation_id: str) -> dict[str, Any]:
        self.calls.append(("invocation_status", (invocation_id,)))
        return self.responses.get("status", {"ok": True, "data": {"status": "running"}})

    async def invocation_result(self, invocation_id: str) -> dict[str, Any]:
        self.calls.append(("invocation_result", (invocation_id,)))
        return self.responses.get("result", {"ok": True, "data": {"status": "pending"}})

    async def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_invocation", (invocation_id,)))
        return {"ok": True, "data": {"cancel_status": "requested"}}

    async def start_session(self, tool_id: str, arguments: dict, **_: Any) -> dict:
        return {"ok": True, "data": {"invocation_id": "inv_session", "attempt_id": "a1"}}

    async def stop_session(self, invocation_id: str) -> dict[str, Any]:
        self.calls.append(("stop_session", (invocation_id,)))
        return {"ok": True, "data": {"status": "requested"}}


class _FakeCoordinator:
    def __init__(self, *, task: AgentTaskRecord | None = None) -> None:
        self.task = task or _task()
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.started: list[tuple[str, str]] = []
        self.start_action_error: Exception | None = None
        self.records: dict[str, AgentTaskRecord] = {}
        self.store = SimpleNamespace(latest_for_session=self._latest_for_session)
        self.interaction = SimpleNamespace(
            restricted=lambda: False,
            managed=lambda _task_id: False,
        )

    def _latest_for_session(self, session_id: str) -> AgentTaskRecord | None:
        return self.records.get(session_id)

    async def create_task(self, **kwargs: Any) -> AgentTaskRecord:
        self.created.append(kwargs)
        return self.task

    def get_task(self, _task_id: str) -> AgentTaskRecord:
        return self.task

    def begin_revision(self, _task_id: str, *, reason: str) -> AgentTaskRecord:
        assert reason
        return self.task

    async def finalize_task(self, _task_id: str) -> AgentTaskRecord:
        return self.task

    async def cancel_task(self, task_id: str, *, reason: str) -> AgentTaskRecord:
        assert reason
        self.cancelled.append(task_id)
        return self.task

    async def invoke_query(self, _task_id: str, _tool_id: str, _args: dict, **_) -> dict:
        return {"ok": True, "data": {"response": "bound query"}}

    async def start_action(self, _task_id: str, tool_id: str, _args: dict, **_) -> dict:
        self.started.append((_task_id, tool_id))
        if self.start_action_error is not None:
            raise self.start_action_error
        return {
            "ok": True,
            "data": {"tool_id": tool_id, "invocation_id": "inv_1", "attempt_id": "attempt_1"},
        }

    async def cancel_action(self, _task_id: str, _invocation_id: str) -> dict:
        return {"ok": True, "data": {"cancel_status": "requested"}}

    async def start_session(self, _task_id: str, _tool_id: str, _args: dict, **_) -> dict:
        return {"ok": True, "data": {"invocation_id": "inv_session", "attempt_id": "a1"}}

    async def stop_session(self, _task_id: str, _invocation_id: str) -> dict:
        return {"ok": True, "data": {"status": "requested"}}

    def require_action_invocation(self, _task_id: str, _invocation_id: str) -> None:
        return None

    def require_session_invocation(self, _task_id: str, _invocation_id: str) -> None:
        return None

    def observe_action(self, _task_id: str, _invocation_id: str, _response: dict) -> None:
        return None

    def observe_session(self, _task_id: str, _invocation_id: str, _response: dict) -> None:
        return None


def _context(session_id: str = "session-1") -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="turn-1",
        step_id="step-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> ToolCall:
    return ToolCall(tool_call_id=call_id, name=name, arguments=arguments)


async def _invoke(
    capability: ForgeCapability,
    name: str,
    arguments: dict[str, Any],
    *,
    context: ExecutionContext | None = None,
):
    registrations = {item.definition.name: item for item in capability.tool_registrations()}
    return await registrations[name].handler(
        _call(name, arguments), context or _context()
    )


def test_registers_the_existing_task_and_tool_api_groups() -> None:
    coordinator = _FakeCoordinator()
    client = _FakeClient()

    task_only = ForgeCapability(coordinator)  # type: ignore[arg-type]
    with_client = ForgeCapability(coordinator, client=client)  # type: ignore[arg-type]

    task_names = [tool.name for tool in build_forge_task_tools(coordinator)]  # type: ignore[arg-type]
    api_names = [
        tool.name
        for tool in build_forge_tool_api_tools(client, coordinator=coordinator)  # type: ignore[arg-type]
    ]

    assert [item.definition.name for item in task_only.tool_registrations()] == task_names
    assert [item.definition.name for item in with_client.tool_registrations()] == (
        task_names + api_names
    )
    assert set(task_names) == {
        "forge_task_create",
        "forge_task_get",
        "forge_task_begin_revision",
        "forge_task_finalize",
        "forge_task_cancel",
    }
    assert set(api_names) == {
        "forge_tool_context",
        "forge_tool_query",
        "forge_tool_start_action",
        "forge_tool_action_status",
        "forge_tool_action_result",
        "forge_tool_cancel_action",
        "forge_tool_start_session",
        "forge_tool_session_status",
        "forge_tool_session_result",
        "forge_tool_stop_session",
    }


async def test_task_create_passes_execution_context_session_explicitly() -> None:
    coordinator = _FakeCoordinator()
    capability = ForgeCapability(coordinator)  # type: ignore[arg-type]

    outcome = await _invoke(
        capability,
        "forge_task_create",
        {
            "task_description": "move the arm",
            "activation_id": "activation_test",
            "verification": {"mode": "off"},
        },
        context=_context("telegram:42"),
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.domain_refs.task_id == "task_1"
    assert outcome.domain_refs.revision_id == "revision_1"
    assert coordinator.created[0]["origin_session_key"] == "telegram:42"


async def test_accepted_start_is_unknown_and_carries_forge_identity() -> None:
    coordinator = _FakeCoordinator()
    capability = ForgeCapability(coordinator, client=_FakeClient())  # type: ignore[arg-type]

    outcome = await _invoke(
        capability,
        "forge_tool_start_action",
        {"task_id": "task_1", "tool_id": "motion.move", "arguments": {}},
    )

    assert outcome.status is ToolStatus.UNKNOWN
    assert not outcome.is_success
    assert outcome.domain_refs.task_id == "task_1"
    assert outcome.domain_refs.invocation_id == "inv_1"
    assert outcome.domain_refs.attempt_id == "attempt_1"
    assert outcome.content["domain_status"] == "accepted"
    assert outcome.content["remote_state"] == "unknown"
    assert "not confirmed" in outcome.for_model()


async def test_remote_state_reads_translate_pending_failed_and_success() -> None:
    coordinator = _FakeCoordinator()
    client = _FakeClient()
    capability = ForgeCapability(coordinator, client=client)  # type: ignore[arg-type]

    client.responses["status"] = {"ok": True, "data": {"phase": "pending"}}
    pending = await _invoke(
        capability,
        "forge_tool_action_status",
        {"task_id": "task_1", "invocation_id": "inv_1"},
    )
    assert pending.status is ToolStatus.UNKNOWN
    assert not pending.is_success

    client.responses["status"] = {"ok": True, "data": {"phase": "unknown"}}
    unknown = await _invoke(
        capability,
        "forge_tool_action_status",
        {"task_id": "task_1", "invocation_id": "inv_1"},
    )
    assert unknown.status is ToolStatus.UNKNOWN

    client.responses["status"] = {"ok": True, "data": {"phase": "succeeded"}}
    succeeded = await _invoke(
        capability,
        "forge_tool_action_status",
        {"task_id": "task_1", "invocation_id": "inv_1"},
    )
    assert succeeded.status is ToolStatus.SUCCEEDED

    client.responses["status"] = {
        "ok": True,
        "data": {"phase": "failed", "error": {"message": "controller fault"}},
    }
    failed = await _invoke(
        capability,
        "forge_tool_action_status",
        {"task_id": "task_1", "invocation_id": "inv_1"},
    )
    assert failed.status is ToolStatus.FAILED
    assert failed.error is not None
    assert failed.error.message == "controller fault"

    client.responses["status"] = {"ok": True, "data": {"phase": "stopped"}}
    stopped = await _invoke(
        capability,
        "forge_tool_action_status",
        {"task_id": "task_1", "invocation_id": "inv_1"},
    )
    assert stopped.status is ToolStatus.CANCELLED


async def test_remote_state_missing_or_unrecognized_status_is_unknown() -> None:
    coordinator = _FakeCoordinator()
    client = _FakeClient()
    capability = ForgeCapability(coordinator, client=client)  # type: ignore[arg-type]
    arguments = {"task_id": "task_1", "invocation_id": "inv_1"}

    client.responses["status"] = {"ok": True, "data": {}}
    missing = await _invoke(capability, "forge_tool_action_status", arguments)
    assert missing.status is ToolStatus.UNKNOWN
    assert not missing.is_success
    assert "domain_status" not in missing.content
    assert "not success" in missing.for_model()

    client.responses["status"] = {"ok": True, "data": {"phase": "queued"}}
    unrecognized = await _invoke(capability, "forge_tool_action_status", arguments)
    assert unrecognized.status is ToolStatus.UNKNOWN
    assert not unrecognized.is_success
    assert unrecognized.content["domain_status"] == "queued"
    assert "not success" in unrecognized.for_model()


async def test_timeout_and_gateway_failures_never_become_success() -> None:
    coordinator = _FakeCoordinator()
    capability = ForgeCapability(coordinator, client=_FakeClient())  # type: ignore[arg-type]
    arguments = {"task_id": "task_1", "tool_id": "motion.move", "arguments": {}}

    coordinator.start_action_error = ForgeToolAPITimeoutError(
        "gateway timed out",
        payload={
            "ok": False,
            "data": {"invocation_id": "inv_timeout", "attempt_id": "attempt_timeout"},
        },
    )
    timeout = await _invoke(capability, "forge_tool_start_action", arguments)
    assert timeout.status is ToolStatus.UNKNOWN
    assert timeout.content["remote_state"] == "unknown"
    assert timeout.domain_refs.invocation_id == "inv_timeout"
    assert timeout.domain_refs.attempt_id == "attempt_timeout"

    coordinator.start_action_error = ForgeToolAPIError(
        "gateway unavailable", status_code=503
    )
    unavailable = await _invoke(capability, "forge_tool_start_action", arguments)
    assert unavailable.status is ToolStatus.UNKNOWN

    coordinator.start_action_error = ForgeToolAPIError(
        "invalid tool", status_code=400, error_code="invalid_tool"
    )
    rejected = await _invoke(capability, "forge_tool_start_action", arguments)
    assert rejected.status is ToolStatus.REJECTED
    assert rejected.error is not None
    assert rejected.error.code == "invalid_tool"


async def test_task_cancel_reports_requested_versus_confirmed_stop() -> None:
    requested = _FakeCoordinator(
        task=_task(status="cancelling", execution_records=[
            ToolExecutionRecord(
                record_id="record_1",
                revision_id="revision_1",
                tool_id="motion.move",
                semantics="action",
                caller_id="paos:test",
                status="running",
                invocation_id="inv_1",
            )
        ])
    )
    capability = ForgeCapability(requested)  # type: ignore[arg-type]
    outcome = await _invoke(capability, "forge_task_cancel", {"task_id": "task_1"})
    assert outcome.status is ToolStatus.UNKNOWN
    assert "may still run" in outcome.for_model()

    confirmed = _FakeCoordinator(task=_task(status="cancelled"))
    capability = ForgeCapability(confirmed)  # type: ignore[arg-type]
    outcome = await _invoke(capability, "forge_task_cancel", {"task_id": "task_1"})
    assert outcome.status is ToolStatus.SUCCEEDED


async def test_cancel_uses_latest_task_for_the_session() -> None:
    coordinator = _FakeCoordinator()
    coordinator.records["session-1"] = _task(task_id="task_for_session")
    capability = ForgeCapability(coordinator)  # type: ignore[arg-type]

    await capability.cancel("session-1", None)
    await capability.cancel("other-session", None)

    assert coordinator.cancelled == ["task_for_session"]


def test_prompt_context_uses_authoritative_store_state() -> None:
    coordinator = _FakeCoordinator()
    capability = ForgeCapability(coordinator)  # type: ignore[arg-type]

    assert capability.prompt_context(_context("missing")) is None

    coordinator.records["session-1"] = _task(
        status="executing",
        execution_records=[
            ToolExecutionRecord(
                record_id="record_1",
                revision_id="revision_1",
                tool_id="motion.move",
                semantics="action",
                caller_id="paos:test",
                status="unknown",
                invocation_id="inv_1",
            )
        ],
    )
    text = capability.prompt_context(_context("session-1"))
    assert text is not None
    assert "task_1" in text
    assert "status=executing" in text
    assert "unknown_invocations=1" in text


async def test_two_sessions_do_not_share_prompt_or_creation_identity() -> None:
    coordinator = _FakeCoordinator()
    coordinator.records["session-a"] = _task(task_id="task_a")
    capability = ForgeCapability(coordinator)  # type: ignore[arg-type]

    assert "task_a" in (capability.prompt_context(_context("session-a")) or "")
    assert capability.prompt_context(_context("session-b")) is None

    await _invoke(
        capability,
        "forge_task_create",
        {
            "task_description": "a",
            "activation_id": "activation_test",
            "verification": {"mode": "off"},
        },
        context=_context("session-b"),
    )
    await _invoke(
        capability,
        "forge_task_create",
        {
            "task_description": "b",
            "activation_id": "activation_test",
            "verification": {"mode": "off"},
        },
        context=_context("session-a"),
    )
    assert [item["origin_session_key"] for item in coordinator.created] == [
        "session-b",
        "session-a",
    ]


def test_task_status_in_prompt_does_not_claim_success() -> None:
    # A real record's enum stays authoritative; the fragment only reports it.
    record = _task(status=AgentTaskStatus.CANCELLING)
    assert record.status is AgentTaskStatus.CANCELLING
    assert record.status.value not in {"succeeded", "completed"}


# ---------------------------------------------------------------------------
# host-driven composition: real core + ForgeCapability, faked provider and
# Forge coordinator/client only
# ---------------------------------------------------------------------------
_START_ACTION_ARGUMENTS = {
    "task_id": "task_1",
    "tool_id": "motion.move",
    "arguments": {},
}


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


_Responder = Callable[
    [int, list[dict[str, Any]], list[dict[str, Any]] | None],
    Awaitable[_ProviderResponse],
]


class _FakeProvider:
    """Stand-in for the external chat provider behind the core adapter."""

    def __init__(self, responder: _Responder) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> _ProviderResponse:
        self.calls.append(
            {"messages": messages, "tools": tools, "kwargs": kwargs}
        )
        return await self._responder(len(self.calls), messages, tools)


def _compose(
    tmp_path: Path,
    provider: _FakeProvider,
    coordinator: _FakeCoordinator,
    client: _FakeClient,
):
    return compose_agent(
        provider=provider,
        store=SQLiteSessionStore(tmp_path / "agent.db"),
        coordinator=coordinator,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        system_instructions="You are the Forge composition test agent.",
        model="fake-model",
        max_steps=4,
    )


def _forge_call(arguments: dict[str, Any]) -> _ProviderToolCall:
    return _ProviderToolCall(
        id="call-forge-1",
        name="forge_tool_start_action",
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )


def _tool_payloads(
    events: Sequence[Any], event_type: EventType
) -> list[dict[str, Any]]:
    return [
        dict(event.payload) for event in events if event.type is event_type
    ]


async def test_host_submit_drives_forge_tool_through_executor(tmp_path: Path) -> None:
    coordinator = _FakeCoordinator()
    coordinator.records["session-1"] = _task(status="executing")
    client = _FakeClient()
    seen_tool_results: list[str] = []

    async def responder(call_no, messages, tools):
        if call_no == 1:
            assert tools is not None
            assert "forge_tool_start_action" in {
                item["function"]["name"] for item in tools
            }
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_forge_call(dict(_START_ACTION_ARGUMENTS)),),
            )
        seen_tool_results.extend(
            message["content"] for message in messages if message.get("role") == "tool"
        )
        return _ProviderResponse(content="action admitted", finish_reason="stop")

    provider = _FakeProvider(responder)
    assembly = _compose(tmp_path, provider, coordinator, client)

    assert isinstance(assembly.capabilities[0], ForgeCapability)
    result = await assembly.host.submit(
        TurnRequest(session_id="session-1", text="move the arm")
    )

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "action admitted"
    assert len(provider.calls) == 2
    assert "Forge task task_1" in provider.calls[0]["messages"][0]["content"]
    assert seen_tool_results[0].startswith(
        "Tool call result is unknown; do not retry automatically."
    )
    assert "not confirmed" in seen_tool_results[0]

    # One accepted start call, never retried by the core or capability.
    assert coordinator.started == [("task_1", "motion.move")]

    events = await assembly.store.read_session("session-1")
    assert len(_tool_payloads(events, EventType.TOOL_INTENT)) == 1
    assert len(_tool_payloads(events, EventType.TOOL_DISPATCHED)) == 1
    settled = _tool_payloads(events, EventType.TOOL_SETTLED)
    assert len(settled) == 1
    assert settled[0]["status"] == ToolStatus.UNKNOWN.value
    assert settled[0]["domain_refs"]["task_id"] == "task_1"
    assert settled[0]["domain_refs"]["invocation_id"] == "inv_1"
    assert settled[0]["domain_refs"]["attempt_id"] == "attempt_1"

    await assembly.host.aclose()


async def test_host_submit_timeout_settles_unknown_without_retry(
    tmp_path: Path,
) -> None:
    coordinator = _FakeCoordinator()
    coordinator.start_action_error = ForgeToolAPITimeoutError(
        "gateway timed out",
        payload={
            "ok": False,
            "data": {
                "invocation_id": "inv_timeout",
                "attempt_id": "attempt_timeout",
            },
        },
    )
    seen_tool_results: list[str] = []

    async def responder(call_no, messages, _tools):
        if call_no == 1:
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_forge_call(dict(_START_ACTION_ARGUMENTS)),),
            )
        seen_tool_results.extend(
            message["content"] for message in messages if message.get("role") == "tool"
        )
        return _ProviderResponse(content="timeout recorded", finish_reason="stop")

    provider = _FakeProvider(responder)
    assembly = _compose(tmp_path, provider, coordinator, _FakeClient())

    result = await assembly.host.submit(
        TurnRequest(session_id="session-1", text="move the arm")
    )

    assert result.status is TurnStatus.COMPLETED
    assert len(provider.calls) == 2
    assert coordinator.started == [("task_1", "motion.move")]
    assert "do not retry automatically" in seen_tool_results[0]
    assert "remote state is unknown" in seen_tool_results[0]

    events = await assembly.store.read_session("session-1")
    settled = _tool_payloads(events, EventType.TOOL_SETTLED)
    assert len(settled) == 1
    assert settled[0]["status"] == ToolStatus.UNKNOWN.value
    assert settled[0]["domain_refs"]["invocation_id"] == "inv_timeout"
    assert settled[0]["domain_refs"]["attempt_id"] == "attempt_timeout"

    await assembly.host.aclose()


async def test_executor_rejects_invalid_forge_arguments_before_dispatch(
    tmp_path: Path,
) -> None:
    coordinator = _FakeCoordinator()
    seen_tool_results: list[str] = []

    async def responder(call_no, messages, _tools):
        if call_no == 1:
            # No task_id: the executor must reject this before the handler.
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(
                    _forge_call({"tool_id": "motion.move", "arguments": {}}),
                ),
            )
        seen_tool_results.extend(
            message["content"] for message in messages if message.get("role") == "tool"
        )
        return _ProviderResponse(content="rejection observed", finish_reason="stop")

    provider = _FakeProvider(responder)
    assembly = _compose(tmp_path, provider, coordinator, _FakeClient())

    result = await assembly.host.submit(
        TurnRequest(session_id="session-1", text="start without a task")
    )

    assert result.status is TurnStatus.COMPLETED
    assert coordinator.started == []
    assert seen_tool_results[0].startswith("Tool call rejected:")

    events = await assembly.store.read_session("session-1")
    assert _tool_payloads(events, EventType.TOOL_INTENT) == []
    assert _tool_payloads(events, EventType.TOOL_DISPATCHED) == []
    settled = _tool_payloads(events, EventType.TOOL_SETTLED)
    assert len(settled) == 1
    assert settled[0]["status"] == ToolStatus.REJECTED.value
    assert settled[0]["error"]["code"] == "invalid_arguments"

    await assembly.host.aclose()


async def test_host_cancel_requests_task_stop_without_claiming_remote_stop(
    tmp_path: Path,
) -> None:
    coordinator = _FakeCoordinator()
    coordinator.records["telegram:42"] = _task(task_id="task_for_session")

    async def responder(_call_no, _messages, _tools):
        raise AssertionError("cancel must not call the model")

    provider = _FakeProvider(responder)
    assembly = _compose(tmp_path, provider, coordinator, _FakeClient())

    await assembly.host.cancel("telegram:42")
    await assembly.host.cancel("no-forge-task")

    # The capability looked up its own authoritative state by session and
    # delivered exactly one stop request; it did not report a stop outcome.
    assert coordinator.cancelled == ["task_for_session"]
    assert coordinator.task.status is AgentTaskStatus.EXECUTING
    assert provider.calls == []
    assert await assembly.store.read_session("telegram:42") == ()

    await assembly.host.aclose()

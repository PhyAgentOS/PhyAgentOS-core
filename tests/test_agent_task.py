from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from PhyAgentOS.agent.experience.source import AgentTaskOutcomeSource
from PhyAgentOS.agent.loop import AgentLoop
from PhyAgentOS.agent.tools.forge_tool_api import build_forge_tool_api_tools
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.config.schema import AgentEvolutionConfig, Config, ForgeConfig
from PhyAgentOS.forge.task import (
    AgentTaskBusyError,
    AgentTaskCoordinator,
    AgentTaskError,
    AgentTaskStatus,
)
from PhyAgentOS.forge.tool_client import ForgeToolAPIError
from PhyAgentOS.verification.contracts import (
    CriterionVerdict,
    RecoveryContext,
    TaskVerificationContract,
    VerificationAttempt,
    VerificationEvidencePolicy,
    VerificationVerdict,
)


class FakeToolClient:
    def __init__(self) -> None:
        self.action_calls = 0
        self.query_calls = 0
        self.cancel_calls: list[str] = []

    async def invoke_query_tool(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.query_calls += 1
        return {
            "ok": True,
            "data": {
                "tool_id": tool_id,
                "target_pose": arguments.get("target_pose", {"x": 0.05}),
            },
        }

    async def invoke_action(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.action_calls += 1
        return {
            "ok": True,
            "data": {
                "tool_id": tool_id,
                "invocation_id": f"inv_{self.action_calls}",
                "attempt_id": f"attempt_{self.action_calls}",
                "arguments": arguments,
            },
        }

    async def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        self.cancel_calls.append(invocation_id)
        return {
            "ok": True,
            "data": {"invocation_id": invocation_id, "cancel_requested": True},
        }


class RecoveryVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify_agent_task(self, task, **_: Any):
        self.calls += 1
        criterion = task.verification.success_criteria[0]
        if self.calls == 1:
            verdict = VerificationVerdict(
                verdict="replan_required",
                criteria=[CriterionVerdict(criterion=criterion, status="unsatisfied")],
                reason="The requested displacement has not yet been verified.",
                lesson="Use a smaller validated displacement.",
                recovery_context=RecoveryContext(
                    unmet_criteria=[criterion],
                    guidance="Resolve and execute a smaller displacement.",
                ),
            )
        else:
            verdict = VerificationVerdict(
                verdict="success",
                criteria=[CriterionVerdict(criterion=criterion, status="satisfied")],
                reason="The aggregate evidence satisfies the task.",
                lesson="Resolve before moving and verify the final state.",
            )
        return (
            verdict,
            object(),
            VerificationAttempt(
                attempt_id=f"verification_{self.calls}",
                verdict=verdict.verdict,
            ),
        )


class ExperienceProbe:
    def __init__(self) -> None:
        self.bound: list[tuple[str, str]] = []
        self.completed: list[str] = []

    def bind_forge_task(self, task_id: str, *, session_key: str) -> None:
        self.bound.append((task_id, session_key))

    def verification_lessons_for_root(self, _task_id: str) -> str:
        return "[]"

    def schedule_forge_completion(self, task_id: str) -> None:
        self.completed.append(task_id)


def _contract(mode: str = "off") -> TaskVerificationContract:
    if mode == "off":
        return TaskVerificationContract(
            evidence_policy=VerificationEvidencePolicy(required_kinds=[])
        )
    return TaskVerificationContract(
        mode=mode,
        goal="Move the gripper forward by five centimetres.",
        success_criteria=["The gripper is five centimetres forward."],
        evidence_policy=VerificationEvidencePolicy(required_kinds=[]),
    )


def _coordinator(
    tmp_path,
    *,
    client=None,
    verifier=None,
    experience=None,
    max_replans: int = 2,
):
    return AgentTaskCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(),
        client=client or FakeToolClient(),
        verifier=verifier,
        experience=experience,
        max_replans=max_replans,
    )


def test_only_one_nonterminal_task_occupies_the_global_slot(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.create_task(
        task_description="first",
        verification=_contract(),
    )

    with pytest.raises(AgentTaskBusyError):
        coordinator.create_task(task_description="second", verification=_contract())

    assert coordinator.store.active().task_id == first.task_id


def test_non_off_task_requires_existing_verification_service(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)

    with pytest.raises(AgentTaskError, match="verification service"):
        coordinator.create_task(
            task_description="verified move",
            verification=_contract("recovery"),
        )


def test_legacy_forge_execution_config_is_rejected() -> None:
    data = {
        "forge": {
            "enabled": True,
            "apiVersion": "paos-forge-gateway-mvp-plus.v1",
        }
    }

    with pytest.raises(ValueError, match="active installed Skill"):
        Config.model_validate(data)


def test_agent_loop_keeps_general_tools_and_incrementally_registers_forge(tmp_path) -> None:
    class FakeProvider:
        def get_default_model(self) -> str:
            return "test-model"

    coordinator = _coordinator(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=FakeProvider(),
        workspace=tmp_path,
        forge_tool_client=coordinator.client,
        forge_task_coordinator=coordinator,
        evolution_config=AgentEvolutionConfig(enabled=True),
    )

    names = set(loop.tools.tool_names)
    assert {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "exec",
        "web_search",
        "web_fetch",
        "message",
        "spawn",
        "query_scene_graph",
        "activate_skill",
    } <= names
    assert {
        "forge_task_create",
        "forge_task_get",
        "forge_task_begin_revision",
        "forge_task_finalize",
        "forge_task_cancel",
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
    } <= names
    assert not names & {
        "forge_execute_task",
        "forge_get_session",
        "forge_cancel_session",
        "forge_get_context",
        "forge_reset",
        "verify_forge_session",
        "create_replanned_forge_session",
    }
    assert loop.experience is not None


async def test_diagnostic_query_and_bound_action_share_tool_api_but_only_bound_is_recorded(
    tmp_path,
) -> None:
    client = FakeToolClient()
    coordinator = _coordinator(tmp_path, client=client)
    task = coordinator.create_task(task_description="move", verification=_contract())
    tools = {
        tool.name: tool
        for tool in build_forge_tool_api_tools(client, coordinator=coordinator)
    }

    diagnostic = json.loads(
        await tools["forge_tool_query"].execute("motion.resolve_relative_pose", {})
    )
    bound = json.loads(
        await tools["forge_tool_start_action"].execute(
            task.task_id, "motion.move_pose", {"distance_m": 0.05}
        )
    )
    coordinator.observe_action(
        task.task_id,
        bound["data"]["invocation_id"],
        {"ok": True, "data": {"phase": "succeeded"}},
    )
    finalized = await coordinator.finalize_task(task.task_id)

    assert diagnostic["data"]["tool_id"] == "motion.resolve_relative_pose"
    assert bound["data"]["invocation_id"] == "inv_1"
    assert finalized.status == AgentTaskStatus.SUCCEEDED
    assert len(finalized.execution_records) == 1
    record = finalized.execution_records[0]
    assert record.record_id != record.invocation_id != record.attempt_id
    assert record.revision_id == finalized.active_revision_id
    outcome = AgentTaskOutcomeSource(coordinator).build(task.task_id)
    assert outcome.agent_task_ref is not None
    assert len(outcome.tool_invocation_refs) == 1
    assert outcome.lineage[0].revision_ref is not None
    assert outcome.lineage[0].invocation_ref is not None
    assert outcome.lineage[0].attempt_ref is not None


async def test_recovery_appends_revision_to_same_task_and_schedules_experience(
    tmp_path,
) -> None:
    verifier = RecoveryVerifier()
    experience = ExperienceProbe()
    coordinator = _coordinator(
        tmp_path,
        verifier=verifier,
        experience=experience,
    )
    task = coordinator.create_task(
        task_description="move forward",
        verification=_contract("recovery"),
        origin_session_key="cli:direct",
    )
    original_revision_id = task.active_revision_id
    await coordinator.invoke_query(task.task_id, "motion.resolve_relative_pose", {})
    failed_action = await coordinator.start_action(
        task.task_id, "motion.move_pose", {"target_pose": {"x": 0.05}}
    )
    coordinator.observe_action(
        task.task_id,
        failed_action["data"]["invocation_id"],
        {"ok": True, "data": {"phase": "failed"}},
    )

    first = await coordinator.finalize_task(task.task_id)
    assert first.status == AgentTaskStatus.AWAITING_REPLAN

    revised = coordinator.begin_revision(
        task.task_id,
        reason="Verification requires a smaller displacement.",
    )
    assert revised.task_id == task.task_id
    assert revised.active_revision_id != original_revision_id
    assert len(revised.revisions) == 2
    assert revised.revisions[0].closed_at is not None

    await coordinator.invoke_query(task.task_id, "motion.resolve_relative_pose", {})
    move_action = await coordinator.start_action(
        task.task_id, "motion.move_pose", {"target_pose": {"x": 0.025}}
    )
    coordinator.observe_action(
        task.task_id,
        move_action["data"]["invocation_id"],
        {"ok": True, "data": {"phase": "completed"}},
    )
    gripper_action = await coordinator.start_action(
        task.task_id, "gripper.set_opening", {"opening_m": 0.05}
    )
    coordinator.observe_action(
        task.task_id,
        gripper_action["data"]["invocation_id"],
        {
            "ok": True,
            "data": {
                "status": "available",
                "result": {"status": "succeeded", "outputs": {"reached_goal": True}},
            },
        },
    )
    final = await coordinator.finalize_task(task.task_id)

    assert final.status == AgentTaskStatus.SUCCEEDED
    assert [item.number for item in final.revisions] == [1, 2]
    assert [item.tool_id for item in final.execution_records] == [
        "motion.resolve_relative_pose",
        "motion.move_pose",
        "motion.resolve_relative_pose",
        "motion.move_pose",
        "gripper.set_opening",
    ]
    assert [item.status for item in final.execution_records] == [
        "succeeded",
        "failed",
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert verifier.calls == 2
    assert experience.bound == [(task.task_id, "cli:direct")]
    assert experience.completed == [task.task_id]
    outcome = AgentTaskOutcomeSource(coordinator).build(task.task_id)
    assert outcome.final_verdict == "success"
    assert outcome.has_failed_attempt is True
    assert [item.semantic_verdict for item in outcome.lineage] == [
        None,
        "replan_required",
        None,
        None,
        "success",
    ]


async def test_unknown_action_is_terminal_but_never_interpreted_as_success_or_retried(
    tmp_path,
) -> None:
    client = FakeToolClient()
    coordinator = _coordinator(tmp_path, client=client)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]

    coordinator.observe_action(
        task.task_id,
        invocation_id,
        {"ok": True, "data": {"phase": "unknown"}},
    )
    final = await coordinator.finalize_task(task.task_id)

    assert final.status == AgentTaskStatus.FAILED
    assert final.execution_records[0].status == "unknown"
    assert client.action_calls == 1


async def test_cancel_acceptance_followed_by_unknown_is_failed_not_stopped(
    tmp_path,
) -> None:
    client = FakeToolClient()
    experience = ExperienceProbe()
    coordinator = _coordinator(tmp_path, client=client, experience=experience)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]

    cancelling = await coordinator.cancel_task(task.task_id, reason="operator stop")
    assert cancelling.status == AgentTaskStatus.FAILED
    assert cancelling.execution_records[0].status == "unknown"
    assert client.cancel_calls == [invocation_id]

    coordinator.observe_action(
        task.task_id,
        invocation_id,
        {"ok": True, "data": {"phase": "unknown"}},
    )
    observed = coordinator.get_task(task.task_id)
    assert observed.status == AgentTaskStatus.FAILED
    final = await coordinator.finalize_task(task.task_id)

    assert final.status == AgentTaskStatus.FAILED
    assert final.execution_records[0].status == "unknown"
    assert experience.completed == [task.task_id]


async def test_replan_budget_is_enforced_on_the_same_task(tmp_path) -> None:
    verifier = RecoveryVerifier()
    coordinator = _coordinator(
        tmp_path,
        verifier=verifier,
        max_replans=0,
    )
    task = coordinator.create_task(
        task_description="move forward",
        verification=_contract("recovery"),
    )
    await coordinator.invoke_query(task.task_id, "motion.resolve_relative_pose", {})

    final = await coordinator.finalize_task(task.task_id)

    assert final.status == AgentTaskStatus.FAILED
    assert len(final.revisions) == 1
    assert any("replan limit reached (0)" in item for item in final.evidence_errors)


class HangingActionClient(FakeToolClient):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def invoke_action(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.action_calls += 1
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("hanging Action unexpectedly returned")


class GatedActionClient(FakeToolClient):
    """Hold the invoke POST until the test releases the 202 response."""

    def __init__(self, *, status: str = "stopped") -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.phase = status
        self.status_calls: list[str] = []

    async def invoke_action(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.action_calls += 1
        self.entered.set()
        await self.release.wait()
        return {
            "ok": True,
            "data": {
                "tool_id": tool_id,
                "invocation_id": f"inv_{self.action_calls}",
                "attempt_id": f"attempt_{self.action_calls}",
                "arguments": arguments,
            },
        }

    async def invocation_status(self, invocation_id: str) -> dict[str, Any]:
        self.status_calls.append(invocation_id)
        return {"ok": True, "data": {"invocation_id": invocation_id, "phase": self.phase}}

    async def invocation_result(self, invocation_id: str) -> dict[str, Any]:
        return {"ok": True, "data": {"invocation_id": invocation_id, "phase": "running"}}



class ScriptedCancelClient(FakeToolClient):
    def __init__(self, statuses: list[str]) -> None:
        super().__init__()
        self._statuses = list(statuses)
        self.status_calls: list[str] = []
        self.result_calls: list[str] = []
        self.cancel_started = asyncio.Event()
        self.cancel_gate: asyncio.Event | None = None

    async def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        self.cancel_calls.append(invocation_id)
        self.cancel_started.set()
        if self.cancel_gate is not None:
            await self.cancel_gate.wait()
        return {
            "ok": True,
            "data": {"invocation_id": invocation_id, "cancel_requested": True},
        }

    async def invocation_status(self, invocation_id: str) -> dict[str, Any]:
        self.status_calls.append(invocation_id)
        if len(self._statuses) > 1:
            phase = self._statuses.pop(0)
        else:
            phase = self._statuses[0] if self._statuses else "running"
        return {"ok": True, "data": {"invocation_id": invocation_id, "phase": phase}}

    async def invocation_result(self, invocation_id: str) -> dict[str, Any]:
        self.result_calls.append(invocation_id)
        return {"ok": True, "data": {"invocation_id": invocation_id, "phase": "running"}}


class UnreachableCancelClient(FakeToolClient):
    async def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        self.cancel_calls.append(invocation_id)
        raise ForgeToolAPIError("Gateway is unreachable")

    async def invocation_status(self, invocation_id: str) -> dict[str, Any]:
        raise ForgeToolAPIError("Gateway is unreachable")

    async def invocation_result(self, invocation_id: str) -> dict[str, Any]:
        raise ForgeToolAPIError("Gateway is unreachable")


class FlakyStatusClient(ScriptedCancelClient):
    def __init__(self) -> None:
        super().__init__(["stopped"])
        self.failures = 1

    async def invocation_status(self, invocation_id: str) -> dict[str, Any]:
        self.status_calls.append(invocation_id)
        if self.failures:
            self.failures -= 1
            raise ForgeToolAPIError(
                "Gateway temporarily unavailable", status_code=503, retryable=True
            )
        return {"ok": True, "data": {"invocation_id": invocation_id, "phase": "stopped"}}


def _short_cancel_budget(coordinator: AgentTaskCoordinator) -> None:
    coordinator.cancel_timeout_s = 0.2
    coordinator.cancel_poll_interval_s = 0.01
    coordinator.cancel_grace_s = 0.05


async def test_cancel_before_dispatch_closes_the_gate_without_invoking(tmp_path) -> None:
    client = FakeToolClient()
    coordinator = _coordinator(tmp_path, client=client)
    task = coordinator.create_task(task_description="move", verification=_contract())

    cancelled = await coordinator.cancel_task(task.task_id, reason="operator stop")

    assert cancelled.status == AgentTaskStatus.CANCELLED
    assert cancelled.cancellation_requested is True
    assert cancelled.execution_records == []
    assert client.action_calls == 0
    with pytest.raises(AgentTaskError, match="cancellation was requested"):
        await coordinator.start_action(task.task_id, "motion.move_pose", {})
    assert client.action_calls == 0


async def test_cancellation_after_intent_blocks_dispatch_before_the_post(
    tmp_path, monkeypatch
) -> None:
    client = FakeToolClient()
    coordinator = _coordinator(tmp_path, client=client)
    task = coordinator.create_task(task_description="move", verification=_contract())
    append = coordinator._append_execution

    def append_then_cancel(*args: Any, **kwargs: Any):
        record = append(*args, **kwargs)
        coordinator.store.update(
            task.task_id,
            lambda current: setattr(current, "cancellation_requested", True),
            event_type="test_cancellation_arrived",
        )
        return record

    monkeypatch.setattr(coordinator, "_append_execution", append_then_cancel)

    with pytest.raises(AgentTaskError, match="request was not resent"):
        await coordinator.start_action(task.task_id, "motion.move_pose", {})

    record = coordinator.get_task(task.task_id).execution_records[-1]
    assert record.status == "cancelled"
    assert client.action_calls == 0


async def test_bare_cancelled_dispatch_leaves_unknown_without_identity(tmp_path) -> None:
    client = HangingActionClient()
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    task = coordinator.create_task(task_description="move", verification=_contract())

    dispatch = asyncio.create_task(
        coordinator.start_action(task.task_id, "motion.move_pose", {})
    )
    await asyncio.wait_for(client.entered.wait(), 1)
    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    record = coordinator.get_task(task.task_id).execution_records[0]
    assert record.status == "unknown"
    assert record.invocation_id is None
    assert record.error is not None
    assert "could not be confirmed" in record.error["message"]
    assert "not resent" in record.error["message"]
    assert client.action_calls == 1
    assert client.cancel_calls == []
    with pytest.raises(AgentTaskError, match="unknown remote state"):
        await coordinator.start_action(task.task_id, "motion.move_pose", {})
    assert client.action_calls == 1


async def test_cancelled_dispatch_uses_grace_identity_and_requests_stop(tmp_path) -> None:
    client = GatedActionClient()
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    coordinator.cancel_grace_s = 0.5
    task = coordinator.create_task(task_description="move", verification=_contract())

    dispatch = asyncio.create_task(
        coordinator.start_action(task.task_id, "motion.move_pose", {})
    )
    await asyncio.wait_for(client.entered.wait(), 1)
    dispatch.cancel()
    await asyncio.sleep(0.01)
    client.release.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    record = coordinator.get_task(task.task_id).execution_records[0]
    assert record.invocation_id == "inv_1"
    assert record.attempt_id == "attempt_1"
    assert record.cancel_requested is True
    assert record.status == "stopped"
    assert client.cancel_calls == ["inv_1"]
    assert client.action_calls == 1


async def test_cancel_after_accept_polls_until_gateway_terminal(tmp_path) -> None:
    client = ScriptedCancelClient(["running", "stopped"])
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]

    cancelling = await coordinator.cancel_task(task.task_id, reason="operator stop")

    record = cancelling.execution_records[0]
    assert record.cancel_requested is True
    assert record.status == "stopped"
    assert client.cancel_calls == [invocation_id]
    assert client.status_calls
    assert cancelling.status == AgentTaskStatus.CANCELLED
    final = await coordinator.finalize_task(task.task_id)
    assert final.status == AgentTaskStatus.CANCELLED


async def test_cancel_without_gateway_terminal_stays_unknown_not_stopped(tmp_path) -> None:
    client = ScriptedCancelClient(["running"])
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    task = coordinator.create_task(task_description="move", verification=_contract())
    await coordinator.start_action(task.task_id, "motion.move_pose", {})

    cancelling = await coordinator.cancel_task(task.task_id, reason="operator stop")

    record = cancelling.execution_records[0]
    assert record.cancel_requested is True
    assert record.status == "unknown"
    assert record.error is not None
    assert "not resent" in record.error["message"]
    assert client.action_calls == 1
    final = await coordinator.finalize_task(task.task_id)
    assert final.status == AgentTaskStatus.FAILED


async def test_unreachable_gateway_keeps_unknown_and_never_resends_the_action(
    tmp_path,
) -> None:
    client = UnreachableCancelClient()
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]

    cancelling = await coordinator.cancel_task(task.task_id, reason="operator stop")

    record = cancelling.execution_records[0]
    assert record.cancel_requested is True
    assert record.status == "unknown"
    assert record.error is not None
    assert record.error["type"] == "CancellationUnconfirmed"
    assert "not resent" in record.error["message"]
    assert client.action_calls == 1
    assert client.cancel_calls == [invocation_id]


async def test_new_actions_are_rejected_while_the_cancel_request_is_in_flight(
    tmp_path,
) -> None:
    client = ScriptedCancelClient(["stopped"])
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    task = coordinator.create_task(task_description="move", verification=_contract())
    await coordinator.start_action(task.task_id, "motion.move_pose", {})
    client.cancel_gate = asyncio.Event()

    cancellation = asyncio.create_task(
        coordinator.cancel_task(task.task_id, reason="operator stop")
    )
    await asyncio.wait_for(client.cancel_started.wait(), 1)
    with pytest.raises(AgentTaskError, match="cancellation was requested"):
        await coordinator.start_action(task.task_id, "motion.move_pose", {"x": 0.1})
    assert client.action_calls == 1

    client.cancel_gate.set()
    result = await asyncio.wait_for(cancellation, 1)
    assert result.cancellation_requested is True
    assert result.status == AgentTaskStatus.CANCELLED


async def test_late_202_after_cancel_is_stopped_not_left_accepted(tmp_path) -> None:
    client = GatedActionClient()
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    task = coordinator.create_task(task_description="move", verification=_contract())

    dispatch = asyncio.create_task(
        coordinator.start_action(task.task_id, "motion.move_pose", {})
    )
    await asyncio.wait_for(client.entered.wait(), 1)
    cancelling = await coordinator.cancel_task(task.task_id, reason="operator stop")
    assert cancelling.status == AgentTaskStatus.CANCELLING
    assert client.cancel_calls == []

    client.release.set()
    response = await asyncio.wait_for(dispatch, 1)
    invocation_id = response["data"]["invocation_id"]

    record = coordinator.get_task(task.task_id).execution_records[0]
    assert record.invocation_id == invocation_id
    assert record.cancel_requested is True
    assert record.status == "stopped"
    assert record.status != "accepted"
    assert client.cancel_calls == [invocation_id]
    assert client.action_calls == 1
    assert coordinator.get_task(task.task_id).status == AgentTaskStatus.CANCELLED


async def test_cancel_task_requests_stop_for_unknown_records_with_identity(tmp_path) -> None:
    client = ScriptedCancelClient(["stopped"])
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]
    coordinator.observe_action(
        task.task_id,
        invocation_id,
        {"ok": True, "data": {"phase": "unknown"}},
    )
    assert coordinator.get_task(task.task_id).execution_records[0].status == "unknown"

    result = await coordinator.cancel_task(task.task_id, reason="operator stop")

    record = result.execution_records[0]
    assert record.cancel_requested is True
    assert record.status == "stopped"
    assert client.cancel_calls == [invocation_id]
    assert client.action_calls == 1


async def test_repeated_cancel_does_not_resend_an_already_requested_record(tmp_path) -> None:
    client = ScriptedCancelClient(["stopped"])
    coordinator = _coordinator(tmp_path, client=client)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]

    def mark_requested(current) -> None:
        record = next(
            item for item in current.execution_records if item.invocation_id == invocation_id
        )
        record.cancel_requested = True

    coordinator.store.update(
        task.task_id, mark_requested, event_type="test_cancel_already_requested"
    )
    result = await coordinator.cancel_task(task.task_id, reason="operator stop")

    assert client.cancel_calls == []
    assert client.status_calls == []
    assert result.status == AgentTaskStatus.CANCELLING


async def test_cancel_polling_retries_after_a_retryable_gateway_5xx(tmp_path) -> None:
    client = FlakyStatusClient()
    coordinator = _coordinator(tmp_path, client=client)
    _short_cancel_budget(coordinator)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]

    result = await coordinator.cancel_task(task.task_id, reason="operator stop")

    record = result.execution_records[0]
    assert record.status == "stopped"
    assert len(client.status_calls) >= 2
    assert result.status == AgentTaskStatus.CANCELLED
    assert client.cancel_calls == [invocation_id]


async def test_reconcile_confirms_terminal_state_for_unknown_record_without_resending(
    tmp_path,
) -> None:
    client = ScriptedCancelClient(["cancelled"])
    coordinator = _coordinator(tmp_path, client=client)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]
    coordinator.observe_action(
        task.task_id,
        invocation_id,
        {"ok": True, "data": {"phase": "unknown"}},
    )

    reconciled = await coordinator.reconcile_nonterminal()

    assert reconciled is not None
    assert reconciled.execution_records[0].status == "cancelled"
    assert client.action_calls == 1
    assert client.cancel_calls == []


async def test_cancel_task_reaches_cancelled_when_owned_records_are_terminal(
    tmp_path,
) -> None:
    client = FakeToolClient()
    coordinator = _coordinator(tmp_path, client=client)
    task = coordinator.create_task(task_description="move", verification=_contract())
    accepted = await coordinator.start_action(task.task_id, "motion.move_pose", {})
    invocation_id = accepted["data"]["invocation_id"]
    coordinator.observe_action(
        task.task_id,
        invocation_id,
        {"ok": True, "data": {"phase": "cancelled"}},
    )

    result = await coordinator.cancel_task(task.task_id, reason="operator stop")

    assert result.status == AgentTaskStatus.CANCELLED
    assert client.cancel_calls == []

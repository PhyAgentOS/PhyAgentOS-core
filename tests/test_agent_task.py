from __future__ import annotations

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
    assert cancelling.status == AgentTaskStatus.CANCELLING
    assert client.cancel_calls == [invocation_id]

    coordinator.observe_action(
        task.task_id,
        invocation_id,
        {"ok": True, "data": {"phase": "unknown"}},
    )
    observed = coordinator.get_task(task.task_id)
    assert observed.status == AgentTaskStatus.CANCELLING
    final = await coordinator.finalize_task(task.task_id)

    assert final.status == AgentTaskStatus.FAILED
    assert final.execution_records[0].status == "unknown"
    assert any("Gateway URL" in item for item in final.evidence_errors)
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

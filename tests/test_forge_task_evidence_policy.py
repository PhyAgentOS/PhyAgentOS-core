from __future__ import annotations

from typing import Any

import pytest

from PhyAgentOS.agent.tools.forge_task import ForgeTaskCreateTool
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.task import AgentTaskRecord, PlanRevision
from PhyAgentOS.verification.contracts import TaskVerificationContract


class RecordingCoordinator:
    def __init__(self, config: ForgeConfig | None = None) -> None:
        self.config = config or ForgeConfig()
        self.verification: TaskVerificationContract | None = None

    async def create_task(self, *, verification: TaskVerificationContract, **_: Any) -> AgentTaskRecord:
        self.verification = verification
        return AgentTaskRecord(
            task_id="task_evidence_policy",
            task_description="record the verification contract",
            verification=verification,
            revisions=[
                PlanRevision(
                    revision_id="revision_evidence_policy",
                    number=1,
                    reason="initial plan",
                )
            ],
            active_revision_id="revision_evidence_policy",
        )


async def _create(coordinator: RecordingCoordinator, verification: dict[str, Any]) -> str:
    tool = ForgeTaskCreateTool(coordinator)  # type: ignore[arg-type]
    return await tool.execute(
        task_description="move the arm",
        activation_id="activation_evidence",
        verification=verification,
    )


async def test_missing_policy_defaults_to_robot_state() -> None:
    coordinator = RecordingCoordinator()
    await _create(coordinator, {"mode": "enforce", "goal": "move", "success_criteria": ["moved"]})
    assert coordinator.verification is not None
    policy = coordinator.verification.evidence_policy
    assert policy.required_kinds == ["robot_state"]
    assert policy.minimum_association == "best_effort"


async def test_unsupported_required_kind_is_rejected_before_task_creation() -> None:
    coordinator = RecordingCoordinator()
    with pytest.raises(ValueError, match="unsupported evidence required_kinds"):
        await _create(
            coordinator,
            {
                "mode": "enforce",
                "goal": "move",
                "success_criteria": ["moved"],
                "evidence_policy": {"required_kinds": ["query_result", "action_result"]},
            },
        )
    assert coordinator.verification is None


async def test_authoritative_association_is_rejected() -> None:
    coordinator = RecordingCoordinator()
    with pytest.raises(ValueError, match="best-effort"):
        await _create(
            coordinator,
            {
                "mode": "enforce",
                "goal": "move",
                "success_criteria": ["moved"],
                "evidence_policy": {"minimum_association": "authoritative"},
            },
        )


async def test_rgb_image_without_configured_sources_is_rejected() -> None:
    coordinator = RecordingCoordinator()
    with pytest.raises(ValueError, match="requiredImageSources"):
        await _create(
            coordinator,
            {
                "mode": "enforce",
                "goal": "move",
                "success_criteria": ["moved"],
                "evidence_policy": {"required_kinds": ["rgb_image"]},
            },
        )


async def test_rgb_image_is_allowed_when_sources_are_configured() -> None:
    config = ForgeConfig()
    config.evidence.required_image_sources = ["front"]
    coordinator = RecordingCoordinator(config)
    await _create(
        coordinator,
        {
            "mode": "enforce",
            "goal": "move",
            "success_criteria": ["moved"],
            "evidence_policy": {"required_kinds": ["rgb_image"], "required_sources": ["front"]},
        },
    )
    assert coordinator.verification is not None
    assert coordinator.verification.evidence_policy.required_kinds == ["rgb_image"]

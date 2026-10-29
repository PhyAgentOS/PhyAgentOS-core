from __future__ import annotations

import json
from typing import Any

from PhyAgentOS.agent_legacy.tools.forge_task import (
    ForgeTaskBeginRevisionTool,
    ForgeTaskCancelTool,
    ForgeTaskCreateTool,
    ForgeTaskFinalizeTool,
    ForgeTaskGetTool,
    build_forge_task_tools,
)
from PhyAgentOS.agent_legacy.tools.registry import ToolRegistry
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.task import AgentTaskCoordinator, AgentTaskRecord, PlanRevision
from PhyAgentOS.verification.contracts import TaskVerificationContract


class StubCoordinator:
    def __init__(self, task: AgentTaskRecord) -> None:
        self.task = task

    async def create_task(self, **_: Any) -> AgentTaskRecord:
        return self.task

    def get_task(self, _task_id: str) -> AgentTaskRecord:
        return self.task

    def begin_revision(self, _task_id: str, *, reason: str) -> AgentTaskRecord:
        assert reason
        return self.task

    async def finalize_task(self, _task_id: str) -> AgentTaskRecord:
        return self.task

    async def cancel_task(self, _task_id: str, *, reason: str) -> AgentTaskRecord:
        assert reason
        return self.task


def _task() -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id="task_serialization",
        task_description="verify AgentTask tool responses",
        verification=TaskVerificationContract(mode="off"),
        revisions=[
            PlanRevision(
                revision_id="revision_serialization",
                number=1,
                reason="initial plan",
            )
        ],
        active_revision_id="revision_serialization",
    )


async def test_all_agent_task_tools_serialize_their_nested_record() -> None:
    coordinator = StubCoordinator(_task())
    create = ForgeTaskCreateTool(coordinator)  # type: ignore[arg-type]
    create.set_context("cli:test")

    results = [
        await create.execute(
            task_description="verify AgentTask tool responses",
            activation_id="activation_serialization",
            verification={"mode": "off"},
        ),
        await ForgeTaskGetTool(coordinator).execute(  # type: ignore[arg-type]
            "task_serialization"
        ),
        await ForgeTaskBeginRevisionTool(coordinator).execute(  # type: ignore[arg-type]
            "task_serialization", reason="retry"
        ),
        await ForgeTaskFinalizeTool(coordinator).execute(  # type: ignore[arg-type]
            "task_serialization"
        ),
        await ForgeTaskCancelTool(coordinator).execute(  # type: ignore[arg-type]
            "task_serialization", reason="cleanup"
        ),
    ]

    for result in results:
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["data"]["task_id"] == "task_serialization"
        assert payload["data"]["status"] == "executing"
        assert isinstance(payload["data"]["created_at"], str)
        assert isinstance(payload["data"]["revisions"][0]["created_at"], str)
        assert "terminal_at" not in payload["data"]


async def test_registry_create_get_and_cancel_match_persisted_state(tmp_path) -> None:
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(),
        client=object(),  # type: ignore[arg-type]
    )
    registry = ToolRegistry()
    for tool in build_forge_task_tools(coordinator):
        registry.register(tool)
    create = registry.get("forge_task_create")
    assert isinstance(create, ForgeTaskCreateTool)
    create.set_context("cli:test")

    created = json.loads(
        await registry.execute(
            "forge_task_create",
            {
                "task_description": "move the arm",
                "activation_id": "activation_serialization",
                "verification": {"mode": "off"},
            },
        )
    )
    task_id = created["data"]["task_id"]
    active = coordinator.store.active()
    assert created["ok"] is True
    assert active is not None
    assert active.task_id == task_id
    assert active.status.value == created["data"]["status"] == "executing"
    assert active.execution_records == []

    fetched = json.loads(await registry.execute("forge_task_get", {"task_id": task_id}))
    assert fetched["ok"] is True
    assert fetched["data"]["task_id"] == task_id
    assert fetched["data"]["status"] == "executing"

    cancelled = json.loads(
        await registry.execute(
            "forge_task_cancel",
            {"task_id": task_id, "reason": "test cleanup"},
        )
    )
    persisted = coordinator.store.get(task_id)
    assert cancelled["ok"] is True
    assert cancelled["data"]["status"] == "cancelled"
    assert persisted.status.value == "cancelled"
    assert coordinator.store.active() is None

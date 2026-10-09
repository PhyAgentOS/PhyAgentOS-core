"""Agent tools for explicit PAOS AgentTask lifecycle management."""

from __future__ import annotations

import inspect
import json
from typing import Any

from PhyAgentOS.agent.tools.base import Tool
from PhyAgentOS.forge.task import AgentTaskCoordinator, AgentTaskRecord
from PhyAgentOS.verification.contracts import TaskVerificationContract


def _task_response(task: AgentTaskRecord) -> str:
    return json.dumps(
        {
            "ok": True,
            "data": task.model_dump(mode="json", exclude_none=True),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ForgeTaskCreateTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator
        self.session_key: str | None = None

    def set_context(self, session_key: str) -> None:
        self.session_key = session_key

    @property
    def name(self) -> str:
        return "forge_task_create"

    @property
    def description(self) -> str:
        return (
            "Create the single active AgentTask before a task-bound Forge Tool sequence. "
            "This records planning and verification context but does not execute the robot."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_description": {"type": "string", "minLength": 1},
                "activation_id": {
                    "type": "string",
                    "pattern": "^activation_[a-z0-9]+$",
                    "description": "Primary activate_skill result from this session turn",
                },
                "verification": _verification_schema(),
            },
            "required": ["task_description", "activation_id", "verification"],
            "additionalProperties": False,
        }

    async def execute(
        self, task_description: str, activation_id: str, verification: dict[str, Any]
    ) -> str:
        task = self.coordinator.create_task(
            task_description=task_description,
            activation_id=activation_id,
            verification=TaskVerificationContract.model_validate(verification),
            origin_session_key=self.session_key,
        )
        if inspect.isawaitable(task):
            task = await task
        return _task_response(task)


class ForgeTaskGetTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_task_get"

    @property
    def description(self) -> str:
        return "Read persisted AgentTask, PlanRevision, Tool execution, evidence and verdict state."

    @property
    def parameters(self) -> dict[str, Any]:
        return _task_id_schema()

    async def execute(self, task_id: str) -> str:
        response = json.loads(_task_response(self.coordinator.get_task(task_id)))
        interaction = getattr(self.coordinator, "interaction", None)
        summary = interaction.summary(task_id) if interaction is not None else None
        if summary is not None:
            response["interaction"] = summary
        return json.dumps(response, ensure_ascii=False)


class ForgeTaskBeginRevisionTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_task_begin_revision"

    @property
    def description(self) -> str:
        return (
            "Append a new immutable PlanRevision to the same task after verification requests "
            "replanning."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        schema = _task_id_schema()
        schema["properties"]["reason"] = {"type": "string", "minLength": 1}
        schema["required"].append("reason")
        return schema

    async def execute(self, task_id: str, reason: str) -> str:
        return _task_response(self.coordinator.begin_revision(task_id, reason=reason))


class ForgeTaskFinalizeTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_task_finalize"

    @property
    def description(self) -> str:
        return (
            "Finalize a task after every bound Action is terminal. PAOS aggregates Tool facts "
            "and judges the user-level verification contract."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _task_id_schema()

    async def execute(self, task_id: str) -> str:
        return _task_response(await self.coordinator.finalize_task(task_id))


class ForgeTaskCancelTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_task_cancel"

    @property
    def description(self) -> str:
        return (
            "Request cancellation for every non-terminal Action bound to an AgentTask. "
            "Cancellation acceptance is not proof that motion stopped."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        schema = _task_id_schema()
        schema["properties"]["reason"] = {"type": "string", "minLength": 1}
        return schema

    async def execute(self, task_id: str, reason: str = "agent_requested") -> str:
        return _task_response(await self.coordinator.cancel_task(task_id, reason=reason))


def build_forge_task_tools(coordinator: AgentTaskCoordinator) -> list[Tool]:
    return [
        ForgeTaskCreateTool(coordinator),
        ForgeTaskGetTool(coordinator),
        ForgeTaskBeginRevisionTool(coordinator),
        ForgeTaskFinalizeTool(coordinator),
        ForgeTaskCancelTool(coordinator),
    ]


def _task_id_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"task_id": {"type": "string", "minLength": 1}},
        "required": ["task_id"],
        "additionalProperties": False,
    }


def _verification_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["off", "audit", "enforce", "recovery"],
            },
            "goal": {"type": "string"},
            "success_criteria": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "evidence_policy": {
                "type": "object",
                "properties": {
                    "profile": {"type": "string"},
                    "required_kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "required_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "minimum_association": {
                        "type": "string",
                        "enum": ["best_effort", "authoritative"],
                    },
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }


__all__ = [
    "ForgeTaskBeginRevisionTool",
    "ForgeTaskCancelTool",
    "ForgeTaskCreateTool",
    "ForgeTaskFinalizeTool",
    "ForgeTaskGetTool",
    "build_forge_task_tools",
]

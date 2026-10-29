"""Legacy AgentTask tool wrappers over the native Forge definitions.

The tool names, descriptions, schemas and operations live in
:mod:`PhyAgentOS.agent.capabilities.forge_task_tools`. This module keeps the
historical ``Tool`` class shapes, the ``ContextVar`` session context and the
JSON string return contract used by the old loop. No lifecycle logic is
duplicated here.
"""

from __future__ import annotations

import copy
import json
from contextvars import ContextVar
from typing import Any

from PhyAgentOS.agent.capabilities.forge_task_tools import (
    TASK_TOOL_DEFINITIONS,
    execute_task_tool,
)
from PhyAgentOS.agent_legacy.tools.base import Tool
from PhyAgentOS.forge.task import AgentTaskCoordinator

__all__ = [
    "ForgeTaskBeginRevisionTool",
    "ForgeTaskCancelTool",
    "ForgeTaskCreateTool",
    "ForgeTaskFinalizeTool",
    "ForgeTaskGetTool",
    "build_forge_task_tools",
]


def _definition(name: str):
    return next(item for item in TASK_TOOL_DEFINITIONS if item.name == name)


def _parameters(name: str) -> dict[str, Any]:
    return copy.deepcopy(dict(_definition(name).parameters))


def _dump(name: str, payload: dict[str, Any]) -> str:
    if name == "forge_task_get":
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class ForgeTaskCreateTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator
        self.session_key: ContextVar[str | None] = ContextVar(
            "forge_task_create_session_key", default=None
        )

    def set_context(self, session_key: str) -> None:
        self.session_key.set(session_key)

    @property
    def name(self) -> str:
        return _definition("forge_task_create").name

    @property
    def description(self) -> str:
        return _definition("forge_task_create").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_task_create")

    async def execute(
        self,
        task_description: str,
        activation_id: str,
        verification: dict[str, Any],
        *,
        origin_session_key: str | None = None,
    ) -> str:
        payload = await execute_task_tool(
            self.name,
            self.coordinator,
            {
                "task_description": task_description,
                "activation_id": activation_id,
                "verification": verification,
            },
            session_id=origin_session_key or self.session_key.get(),
        )
        return _dump(self.name, payload)


class ForgeTaskGetTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return _definition("forge_task_get").name

    @property
    def description(self) -> str:
        return _definition("forge_task_get").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_task_get")

    async def execute(self, task_id: str) -> str:
        payload = await execute_task_tool(
            self.name, self.coordinator, {"task_id": task_id}
        )
        return _dump(self.name, payload)


class ForgeTaskBeginRevisionTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return _definition("forge_task_begin_revision").name

    @property
    def description(self) -> str:
        return _definition("forge_task_begin_revision").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_task_begin_revision")

    async def execute(self, task_id: str, reason: str) -> str:
        payload = await execute_task_tool(
            self.name, self.coordinator, {"task_id": task_id, "reason": reason}
        )
        return _dump(self.name, payload)


class ForgeTaskFinalizeTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return _definition("forge_task_finalize").name

    @property
    def description(self) -> str:
        return _definition("forge_task_finalize").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_task_finalize")

    async def execute(self, task_id: str) -> str:
        payload = await execute_task_tool(
            self.name, self.coordinator, {"task_id": task_id}
        )
        return _dump(self.name, payload)


class ForgeTaskCancelTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return _definition("forge_task_cancel").name

    @property
    def description(self) -> str:
        return _definition("forge_task_cancel").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_task_cancel")

    async def execute(self, task_id: str, reason: str = "agent_requested") -> str:
        payload = await execute_task_tool(
            self.name,
            self.coordinator,
            {"task_id": task_id, "reason": reason},
        )
        return _dump(self.name, payload)


_TASK_TOOL_CLASSES = {
    "forge_task_create": ForgeTaskCreateTool,
    "forge_task_get": ForgeTaskGetTool,
    "forge_task_begin_revision": ForgeTaskBeginRevisionTool,
    "forge_task_finalize": ForgeTaskFinalizeTool,
    "forge_task_cancel": ForgeTaskCancelTool,
}


def build_forge_task_tools(coordinator: AgentTaskCoordinator) -> list[Tool]:
    return [
        _TASK_TOOL_CLASSES[definition.name](coordinator)
        for definition in TASK_TOOL_DEFINITIONS
    ]

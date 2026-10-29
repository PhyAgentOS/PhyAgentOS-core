"""Legacy Gateway Tool API wrappers over the native Forge definitions.

The tool names, descriptions, schemas and operations live in
:mod:`PhyAgentOS.agent.capabilities.forge_tool_api`. This module keeps the
historical ``Tool`` class shapes and the JSON string return contract used by
the old loop. Gateway call shapes and ownership checks are not duplicated here.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from PhyAgentOS.agent.capabilities.forge_tool_api import (
    TOOL_API_DEFINITIONS,
    execute_tool_api_tool,
)
from PhyAgentOS.agent_legacy.tools.base import Tool
from PhyAgentOS.forge.task import AgentTaskCoordinator
from PhyAgentOS.forge.tool_client import ForgeToolClient

__all__ = [
    "ForgeToolActionResultTool",
    "ForgeToolActionStatusTool",
    "ForgeToolCancelActionTool",
    "ForgeToolContextTool",
    "ForgeToolQueryTool",
    "ForgeToolSessionResultTool",
    "ForgeToolSessionStatusTool",
    "ForgeToolStartActionTool",
    "ForgeToolStartSessionTool",
    "ForgeToolStopSessionTool",
    "build_forge_tool_api_tools",
]


def _definition(name: str):
    return next(item for item in TOOL_API_DEFINITIONS if item.name == name)


def _parameters(name: str) -> dict[str, Any]:
    return copy.deepcopy(dict(_definition(name).parameters))


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class ForgeToolContextTool(Tool):
    """Read a Forge ToolSpec and its live readiness/context before invocation.

    Readiness, frame, and pose belong to the live Gateway state, so this tool
    never caches its response; every call must reflect the current runtime.
    """

    def __init__(self, client: ForgeToolClient) -> None:
        self.client = client
        self.coordinator: AgentTaskCoordinator | None = None

    @property
    def name(self) -> str:
        return _definition("forge_tool_context").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_context").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_tool_context")

    async def execute(self, tool_id: str) -> str:
        payload = await execute_tool_api_tool(
            self.name, self.client, self.coordinator, {"tool_id": tool_id}  # type: ignore[arg-type]
        )
        return _dump(payload)


class ForgeToolQueryTool(Tool):
    def __init__(self, client: ForgeToolClient, coordinator: AgentTaskCoordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return _definition("forge_tool_query").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_query").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_tool_query")

    async def execute(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        task_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        call_arguments: dict[str, Any] = {
            "tool_id": tool_id,
            "arguments": arguments,
        }
        if task_id:
            call_arguments["task_id"] = task_id
        if timeout_ms is not None:
            call_arguments["timeout_ms"] = timeout_ms
        payload = await execute_tool_api_tool(
            self.name, self.client, self.coordinator, call_arguments
        )
        return _dump(payload)


class ForgeToolStartActionTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator
        self.client: ForgeToolClient | None = None

    @property
    def name(self) -> str:
        return _definition("forge_tool_start_action").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_start_action").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_tool_start_action")

    async def execute(
        self,
        task_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        timeout_ms: int | None = None,
    ) -> str:
        call_arguments: dict[str, Any] = {
            "task_id": task_id,
            "tool_id": tool_id,
            "arguments": arguments,
        }
        if timeout_ms is not None:
            call_arguments["timeout_ms"] = timeout_ms
        payload = await execute_tool_api_tool(
            self.name, self.client, self.coordinator, call_arguments
        )
        return _dump(payload)


class _ActionReadTool(Tool):
    def __init__(self, client: ForgeToolClient, coordinator: AgentTaskCoordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters(self.name)

    async def execute(self, task_id: str, invocation_id: str) -> str:
        payload = await execute_tool_api_tool(
            self.name,
            self.client,
            self.coordinator,
            {"task_id": task_id, "invocation_id": invocation_id},
        )
        return _dump(payload)


class ForgeToolActionStatusTool(_ActionReadTool):
    @property
    def name(self) -> str:
        return _definition("forge_tool_action_status").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_action_status").description


class ForgeToolActionResultTool(_ActionReadTool):
    @property
    def name(self) -> str:
        return _definition("forge_tool_action_result").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_action_result").description


class ForgeToolCancelActionTool(Tool):
    def __init__(self, client: ForgeToolClient, coordinator: AgentTaskCoordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return _definition("forge_tool_cancel_action").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_cancel_action").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_tool_cancel_action")

    async def execute(self, task_id: str, invocation_id: str) -> str:
        payload = await execute_tool_api_tool(
            self.name,
            self.client,
            self.coordinator,
            {"task_id": task_id, "invocation_id": invocation_id},
        )
        return _dump(payload)


class ForgeToolStartSessionTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator
        self.client: ForgeToolClient | None = None

    @property
    def name(self) -> str:
        return _definition("forge_tool_start_session").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_start_session").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_tool_start_session")

    async def execute(
        self,
        task_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        ownership: str,
    ) -> str:
        payload = await execute_tool_api_tool(
            self.name,
            self.client,
            self.coordinator,
            {
                "task_id": task_id,
                "tool_id": tool_id,
                "arguments": arguments,
                "ownership": ownership,
            },
        )
        return _dump(payload)


class _SessionReadTool(Tool):
    def __init__(self, client: ForgeToolClient, coordinator: AgentTaskCoordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters(self.name)

    async def execute(self, task_id: str, invocation_id: str) -> str:
        payload = await execute_tool_api_tool(
            self.name,
            self.client,
            self.coordinator,
            {"task_id": task_id, "invocation_id": invocation_id},
        )
        return _dump(payload)


class ForgeToolSessionStatusTool(_SessionReadTool):
    @property
    def name(self) -> str:
        return _definition("forge_tool_session_status").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_session_status").description


class ForgeToolSessionResultTool(_SessionReadTool):
    @property
    def name(self) -> str:
        return _definition("forge_tool_session_result").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_session_result").description


class ForgeToolStopSessionTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator
        self.client: ForgeToolClient | None = None

    @property
    def name(self) -> str:
        return _definition("forge_tool_stop_session").name

    @property
    def description(self) -> str:
        return _definition("forge_tool_stop_session").description

    @property
    def parameters(self) -> dict[str, Any]:
        return _parameters("forge_tool_stop_session")

    async def execute(self, task_id: str, invocation_id: str) -> str:
        payload = await execute_tool_api_tool(
            self.name,
            self.client,
            self.coordinator,
            {"task_id": task_id, "invocation_id": invocation_id},
        )
        return _dump(payload)


_TOOL_API_CLASSES = {
    "forge_tool_context": ForgeToolContextTool,
    "forge_tool_query": ForgeToolQueryTool,
    "forge_tool_start_action": ForgeToolStartActionTool,
    "forge_tool_action_status": ForgeToolActionStatusTool,
    "forge_tool_action_result": ForgeToolActionResultTool,
    "forge_tool_cancel_action": ForgeToolCancelActionTool,
    "forge_tool_start_session": ForgeToolStartSessionTool,
    "forge_tool_session_status": ForgeToolSessionStatusTool,
    "forge_tool_session_result": ForgeToolSessionResultTool,
    "forge_tool_stop_session": ForgeToolStopSessionTool,
}


def build_forge_tool_api_tools(
    client: ForgeToolClient,
    *,
    invocation_ids: Any | None = None,
    coordinator: AgentTaskCoordinator | None = None,
) -> list[Tool]:
    """Build Query/Action/Session wrappers; all mutation requires a Coordinator."""
    del invocation_ids
    if coordinator is None:
        return [ForgeToolContextTool(client)]
    tools: list[Tool] = []
    coordinator_only = {
        ForgeToolStartActionTool,
        ForgeToolStartSessionTool,
        ForgeToolStopSessionTool,
    }
    for definition in TOOL_API_DEFINITIONS:
        tool_class = _TOOL_API_CLASSES[definition.name]
        if tool_class is ForgeToolContextTool:
            tools.append(tool_class(client))
        elif tool_class in coordinator_only:
            tools.append(tool_class(coordinator))
        else:
            tools.append(tool_class(client, coordinator))
    return tools

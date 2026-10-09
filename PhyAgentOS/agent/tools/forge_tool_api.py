"""Governed Agent wrappers for the Forge Gateway Tool API."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable
from uuid import uuid4

from PhyAgentOS.agent.tools.base import Tool
from PhyAgentOS.forge.task import AgentTaskCoordinator, AgentTaskError
from PhyAgentOS.forge.tool_client import (
    ForgeToolAPIError,
    ForgeToolAPITimeoutError,
    ForgeToolClient,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _call(operation: Callable[[], Awaitable[dict[str, Any]]]) -> str:
    try:
        return _json(await operation())
    except ForgeToolAPIError as exc:
        error: dict[str, Any] = {
            "type": "timeout" if isinstance(exc, ForgeToolAPITimeoutError) else "gateway_tool_api",
            "message": str(exc),
        }
        for name in ("status_code", "error_code", "retryable"):
            value = getattr(exc, name)
            if value is not None:
                error["code" if name == "error_code" else name] = value
        payload_data = exc.payload.get("data") if isinstance(exc.payload, dict) else None
        if isinstance(payload_data, dict):
            for identity in ("invocation_id", "attempt_id"):
                if isinstance(payload_data.get(identity), str):
                    error[identity] = payload_data[identity]
        if isinstance(exc, ForgeToolAPITimeoutError):
            error.update({"remote_state": "unknown", "stopped": False})
        return _json({"ok": False, "error": error})
    except AgentTaskError as exc:
        return _json({"ok": False, "error": {"type": "agent_task", "message": str(exc)}})
    except RuntimeError as exc:
        return _json({"ok": False, "error": {"type": "runtime", "message": str(exc)}})


class ForgeToolContextTool(Tool):
    def __init__(self, client: ForgeToolClient) -> None:
        self.client = client

    @property
    def name(self) -> str:
        return "forge_tool_context"

    @property
    def description(self) -> str:
        return "Read a Forge ToolSpec and its live readiness/context before invocation."

    @property
    def parameters(self) -> dict[str, Any]:
        return _single_schema("tool_id")

    async def execute(self, tool_id: str) -> str:
        async def describe() -> dict[str, Any]:
            spec, context = await asyncio.gather(
                self.client.get_tool(tool_id), self.client.get_tool_context(tool_id)
            )
            return {"ok": True, "data": {"tool": spec["data"], "context": context["data"]}}

        return await _call(describe)


class ForgeToolQueryTool(Tool):
    def __init__(self, client: ForgeToolClient, coordinator: AgentTaskCoordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_tool_query"

    @property
    def description(self) -> str:
        return (
            "Invoke a Gateway-declared read-only Query. Supply task_id while executing an "
            "AgentTask so the result is audited; omit it only for unbound diagnostics."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _invoke_schema(task_required=False, include_timeout=True)

    async def execute(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        task_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        if task_id:
            return await _call(
                lambda: self.coordinator.invoke_query(
                    task_id, tool_id, arguments, timeout_ms=timeout_ms
                )
            )
        if self.coordinator.interaction.restricted():
            return _json({"ok": False, "error": {"type": "interaction", "message": "unbound diagnostics are disabled for interaction"}})
        return await _call(
            lambda: self.client.invoke_query_tool(
                tool_id,
                arguments,
                caller_id=f"paos:diagnostic:{uuid4().hex[:20]}",
                timeout_ms=timeout_ms,
            )
        )


class ForgeToolStartActionTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_tool_start_action"

    @property
    def description(self) -> str:
        return "Start a task-bound Forge Action; admission is not completion or task success."

    @property
    def parameters(self) -> dict[str, Any]:
        return _invoke_schema(task_required=True, include_timeout=True)

    async def execute(
        self,
        task_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        timeout_ms: int | None = None,
    ) -> str:
        return await _call(
            lambda: self.coordinator.start_action(
                task_id, tool_id, arguments, timeout_ms=timeout_ms
            )
        )


class _ActionReadTool(Tool):
    operation = ""

    def __init__(self, client: ForgeToolClient, coordinator: AgentTaskCoordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    @property
    def parameters(self) -> dict[str, Any]:
        return _owned_invocation_schema()

    async def execute(self, task_id: str, invocation_id: str) -> str:
        async def read() -> dict[str, Any]:
            # Reject cross-task identifiers before disclosing Gateway state.
            self.coordinator.require_action_invocation(task_id, invocation_id)
            if self.operation == "status":
                response = await self.client.invocation_status(invocation_id)
            else:
                response = await self.client.invocation_result(invocation_id)
            self.coordinator.observe_action(task_id, invocation_id, response)
            return response

        return await _call(read)


class ForgeToolActionStatusTool(_ActionReadTool):
    operation = "status"

    @property
    def name(self) -> str:
        return "forge_tool_action_status"

    @property
    def description(self) -> str:
        return "Read and persist the authoritative phase of this task's Action invocation."


class ForgeToolActionResultTool(_ActionReadTool):
    operation = "result"

    @property
    def name(self) -> str:
        return "forge_tool_action_result"

    @property
    def description(self) -> str:
        return "Read and persist this task's Action result; pending and unknown are not success."


class ForgeToolCancelActionTool(Tool):
    def __init__(self, client: ForgeToolClient, coordinator: AgentTaskCoordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_tool_cancel_action"

    @property
    def description(self) -> str:
        return "Request cancellation of this task's Action; then poll until terminal or unknown."

    @property
    def parameters(self) -> dict[str, Any]:
        return _owned_invocation_schema()

    async def execute(self, task_id: str, invocation_id: str) -> str:
        async def cancel() -> dict[str, Any]:
            # Ownership is checked before the control request is sent.
            self.coordinator.require_action_invocation(task_id, invocation_id)
            response = await self.coordinator.cancel_action(task_id, invocation_id)
            return response

        return await _call(cancel)


class ForgeToolStartSessionTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_tool_start_session"

    @property
    def description(self) -> str:
        return "Start a first-class, no-deadline Forge Session with explicit ownership."

    @property
    def parameters(self) -> dict[str, Any]:
        schema = _invoke_schema(task_required=True, include_timeout=False)
        schema["properties"]["ownership"] = {"type": "string", "enum": ["task", "shared"]}
        schema["required"].append("ownership")
        return schema

    async def execute(
        self,
        task_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        ownership: str,
    ) -> str:
        return await _call(
            lambda: self.coordinator.start_session(
                task_id, tool_id, arguments, ownership=ownership  # type: ignore[arg-type]
            )
        )


class _SessionReadTool(Tool):
    operation = ""

    def __init__(self, client: ForgeToolClient, coordinator: AgentTaskCoordinator) -> None:
        self.client = client
        self.coordinator = coordinator

    @property
    def parameters(self) -> dict[str, Any]:
        return _owned_invocation_schema()

    async def execute(self, task_id: str, invocation_id: str) -> str:
        async def read() -> dict[str, Any]:
            # Reject cross-task identifiers before disclosing Gateway state.
            self.coordinator.require_session_invocation(task_id, invocation_id)
            if self.coordinator.interaction.managed(task_id):
                return await self.coordinator.interaction.read_invocation(task_id, invocation_id, "session", self.operation)
            response = (
                await self.client.invocation_status(invocation_id)
                if self.operation == "status"
                else await self.client.invocation_result(invocation_id)
            )
            self.coordinator.observe_session(task_id, invocation_id, response)
            return response

        return await _call(read)


class ForgeToolSessionStatusTool(_SessionReadTool):
    operation = "status"

    @property
    def name(self) -> str:
        return "forge_tool_session_status"

    @property
    def description(self) -> str:
        return "Read and persist the authoritative state of a task-referenced Session."


class ForgeToolSessionResultTool(_SessionReadTool):
    operation = "result"

    @property
    def name(self) -> str:
        return "forge_tool_session_result"

    @property
    def description(self) -> str:
        return "Read the result of a terminal task-referenced Forge Session."


class ForgeToolStopSessionTool(Tool):
    def __init__(self, coordinator: AgentTaskCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def name(self) -> str:
        return "forge_tool_stop_session"

    @property
    def description(self) -> str:
        return "Request stop for a task-owned Session; shared/runtime Sessions are protected."

    @property
    def parameters(self) -> dict[str, Any]:
        return _owned_invocation_schema()

    async def execute(self, task_id: str, invocation_id: str) -> str:
        return await _call(lambda: self.coordinator.stop_session(task_id, invocation_id))


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
    return [
        ForgeToolContextTool(client),
        ForgeToolQueryTool(client, coordinator),
        ForgeToolStartActionTool(coordinator),
        ForgeToolActionStatusTool(client, coordinator),
        ForgeToolActionResultTool(client, coordinator),
        ForgeToolCancelActionTool(client, coordinator),
        ForgeToolStartSessionTool(coordinator),
        ForgeToolSessionStatusTool(client, coordinator),
        ForgeToolSessionResultTool(client, coordinator),
        ForgeToolStopSessionTool(coordinator),
    ]


def _single_schema(name: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": "string", "minLength": 1}},
        "required": [name],
        "additionalProperties": False,
    }


def _owned_invocation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "invocation_id": {"type": "string", "minLength": 1},
        },
        "required": ["task_id", "invocation_id"],
        "additionalProperties": False,
    }


def _invoke_schema(*, task_required: bool, include_timeout: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "task_id": {"type": "string", "minLength": 1},
        "tool_id": {"type": "string", "minLength": 1},
        "arguments": {"type": "object"},
    }
    required = ["tool_id", "arguments"]
    if task_required:
        required.insert(0, "task_id")
    if include_timeout:
        properties["timeout_ms"] = {"type": "integer", "minimum": 1}
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


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

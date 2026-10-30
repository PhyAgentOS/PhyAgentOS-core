"""Native definitions and operations for the ten Forge Gateway Tool API tools.

This module is the single source for the Tool API tool names, descriptions,
JSON Schemas and operations. ``forge_capability`` builds Core registrations
from the definitions and calls the operations directly; the legacy wrappers in
:mod:`PhyAgentOS.agent_legacy.tools.forge_tool_api` only keep their historical
``Tool`` class shapes and serialize the returned mapping back to a JSON string.

Operations return the same structured mapping the old wrappers encoded. They
do not add a second physical path: every call still goes through
``ForgeToolClient -> Gateway Tool API -> ToolEndpoint``, and task/session
ownership stays enforced by :class:`AgentTaskCoordinator`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from PhyAgentOS.agent.core.types import ToolDefinition
from PhyAgentOS.forge.task import AgentTaskCoordinator, AgentTaskError
from PhyAgentOS.forge.tool_client import (
    ForgeToolAPIError,
    ForgeToolAPITimeoutError,
    ForgeToolClient,
)

__all__ = [
    "TOOL_API_DEFINITIONS",
    "execute_tool_api_tool",
]


async def _call(
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run one Gateway call, preserving the historical error payload fields."""
    try:
        return await operation()
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
        return {"ok": False, "error": error}
    except AgentTaskError as exc:
        return {"ok": False, "error": {"type": "agent_task", "message": str(exc)}}
    except RuntimeError as exc:
        return {"ok": False, "error": {"type": "runtime", "message": str(exc)}}


async def _context(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    del coordinator
    assert client is not None
    tool_id = arguments["tool_id"]

    async def describe() -> dict[str, Any]:
        spec, context = await asyncio.gather(
            client.get_tool(tool_id), client.get_tool_context(tool_id)
        )
        return {"ok": True, "data": {"tool": spec["data"], "context": context["data"]}}

    return await _call(describe)


async def _query(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    assert client is not None
    tool_id = arguments["tool_id"]
    query_arguments = arguments["arguments"]
    timeout_ms = arguments.get("timeout_ms")
    task_id = arguments.get("task_id")

    if task_id:
        return await _call(
            lambda: coordinator.invoke_query(
                task_id, tool_id, query_arguments, timeout_ms=timeout_ms
            )
        )
    if coordinator.interaction.restricted():
        return {
            "ok": False,
            "error": {
                "type": "interaction",
                "message": "unbound diagnostics are disabled for interaction",
            },
        }
    return await _call(
        lambda: client.invoke_query_tool(
            tool_id,
            query_arguments,
            caller_id=f"paos:diagnostic:{uuid4().hex[:20]}",
            timeout_ms=timeout_ms,
        )
    )


async def _start_action(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    del client
    return await _call(
        lambda: coordinator.start_action(
            arguments["task_id"],
            arguments["tool_id"],
            arguments["arguments"],
            timeout_ms=arguments.get("timeout_ms"),
        )
    )


async def _read_action(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
    operation: str,
) -> dict[str, Any]:
    assert client is not None
    task_id = arguments["task_id"]
    invocation_id = arguments["invocation_id"]

    async def read() -> dict[str, Any]:
        # Reject cross-task identifiers before disclosing Gateway state.
        coordinator.require_action_invocation(task_id, invocation_id)
        if operation == "status":
            response = await client.invocation_status(invocation_id)
        else:
            response = await client.invocation_result(invocation_id)
        coordinator.observe_action(task_id, invocation_id, response)
        return response

    return await _call(read)


async def _action_status(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return await _read_action(client, coordinator, arguments, "status")


async def _action_result(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return await _read_action(client, coordinator, arguments, "result")


async def _cancel_action(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    del client
    task_id = arguments["task_id"]
    invocation_id = arguments["invocation_id"]

    async def cancel() -> dict[str, Any]:
        # Ownership is checked before the control request is sent.
        coordinator.require_action_invocation(task_id, invocation_id)
        return await coordinator.cancel_action(task_id, invocation_id)

    return await _call(cancel)


async def _start_session(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    del client
    return await _call(
        lambda: coordinator.start_session(
            arguments["task_id"],
            arguments["tool_id"],
            arguments["arguments"],
            ownership=arguments["ownership"],
        )
    )


async def _read_session(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
    operation: str,
) -> dict[str, Any]:
    assert client is not None
    task_id = arguments["task_id"]
    invocation_id = arguments["invocation_id"]

    async def read() -> dict[str, Any]:
        # Reject cross-task identifiers before disclosing Gateway state.
        coordinator.require_session_invocation(task_id, invocation_id)
        if coordinator.interaction.managed(task_id):
            return await coordinator.interaction.read_invocation(
                task_id, invocation_id, "session", operation
            )
        response = (
            await client.invocation_status(invocation_id)
            if operation == "status"
            else await client.invocation_result(invocation_id)
        )
        coordinator.observe_session(task_id, invocation_id, response)
        return response

    return await _call(read)


async def _session_status(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return await _read_session(client, coordinator, arguments, "status")


async def _session_result(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return await _read_session(client, coordinator, arguments, "result")


async def _stop_session(
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    del client
    return await _call(
        lambda: coordinator.stop_session(
            arguments["task_id"], arguments["invocation_id"]
        )
    )


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
        properties["timeout_ms"] = {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Optional Gateway execution timeout in ms. Omit it when the Gateway's "
                "limit is unknown so the Gateway default applies; never substitute an "
                "HTTP client timeout."
            ),
        }
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_CONTEXT_DEFINITION = ToolDefinition(
    name="forge_tool_context",
    description="Read a Forge ToolSpec and its live readiness/context before invocation.",
    parameters=_single_schema("tool_id"),
)

_QUERY_DEFINITION = ToolDefinition(
    name="forge_tool_query",
    description=(
        "Invoke a Gateway-declared read-only Query. Supply task_id while executing an "
        "AgentTask so the result is audited; omit it only for unbound diagnostics. "
        "Omit timeout_ms unless the Gateway's limit is known."
    ),
    parameters=_invoke_schema(task_required=False, include_timeout=True),
)

_START_ACTION_DEFINITION = ToolDefinition(
    name="forge_tool_start_action",
    description=(
        "Start a task-bound Forge Action; admission is not completion or task success. "
        "Omit timeout_ms unless the Gateway's limit is known; the Gateway default applies."
    ),
    parameters=_invoke_schema(task_required=True, include_timeout=True),
)

_ACTION_STATUS_DEFINITION = ToolDefinition(
    name="forge_tool_action_status",
    description="Read and persist the authoritative phase of this task's Action invocation.",
    parameters=_owned_invocation_schema(),
)

_ACTION_RESULT_DEFINITION = ToolDefinition(
    name="forge_tool_action_result",
    description=(
        "Read and persist this task's Action result; pending and unknown are not success."
    ),
    parameters=_owned_invocation_schema(),
)

_CANCEL_ACTION_DEFINITION = ToolDefinition(
    name="forge_tool_cancel_action",
    description="Request cancellation of this task's Action; then poll until terminal or unknown.",
    parameters=_owned_invocation_schema(),
)

_START_SESSION_DEFINITION = ToolDefinition(
    name="forge_tool_start_session",
    description="Start a first-class, no-deadline Forge Session with explicit ownership.",
    parameters={
        **_invoke_schema(task_required=True, include_timeout=False),
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "tool_id": {"type": "string", "minLength": 1},
            "arguments": {"type": "object"},
            "ownership": {"type": "string", "enum": ["task", "shared"]},
        },
        "required": ["task_id", "tool_id", "arguments", "ownership"],
    },
)

_SESSION_STATUS_DEFINITION = ToolDefinition(
    name="forge_tool_session_status",
    description="Read and persist the authoritative state of a task-referenced Session.",
    parameters=_owned_invocation_schema(),
)

_SESSION_RESULT_DEFINITION = ToolDefinition(
    name="forge_tool_session_result",
    description="Read the result of a terminal task-referenced Forge Session.",
    parameters=_owned_invocation_schema(),
)

_STOP_SESSION_DEFINITION = ToolDefinition(
    name="forge_tool_stop_session",
    description="Request stop for a task-owned Session; shared/runtime Sessions are protected.",
    parameters=_owned_invocation_schema(),
)

_ToolOperation = Callable[..., Awaitable[dict[str, Any]]]

# Order is the registration order every caller must keep.
_TOOL_API_TOOLS: tuple[tuple[ToolDefinition, _ToolOperation], ...] = (
    (_CONTEXT_DEFINITION, _context),
    (_QUERY_DEFINITION, _query),
    (_START_ACTION_DEFINITION, _start_action),
    (_ACTION_STATUS_DEFINITION, _action_status),
    (_ACTION_RESULT_DEFINITION, _action_result),
    (_CANCEL_ACTION_DEFINITION, _cancel_action),
    (_START_SESSION_DEFINITION, _start_session),
    (_SESSION_STATUS_DEFINITION, _session_status),
    (_SESSION_RESULT_DEFINITION, _session_result),
    (_STOP_SESSION_DEFINITION, _stop_session),
)

TOOL_API_DEFINITIONS: tuple[ToolDefinition, ...] = tuple(
    definition for definition, _ in _TOOL_API_TOOLS
)

_TOOL_API_OPERATIONS = {
    definition.name: operation for definition, operation in _TOOL_API_TOOLS
}


async def execute_tool_api_tool(
    name: str,
    client: ForgeToolClient | None,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Run one Gateway Tool API operation and return the structured mapping."""
    operation = _TOOL_API_OPERATIONS.get(name)
    if operation is None:
        raise KeyError(f"unknown Forge Tool API tool {name!r}")
    return await operation(client, coordinator, arguments)

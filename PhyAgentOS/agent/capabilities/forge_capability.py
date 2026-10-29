"""Agent-side Forge capability over native adapter definitions.

Forge owns AgentTask, invocation, and remote execution state. This module
translates the core's generic tool contract into the native Forge operations
in :mod:`PhyAgentOS.agent.capabilities.forge_task_tools` and
:mod:`PhyAgentOS.agent.capabilities.forge_tool_api`, and maps their structured
responses to ``ToolOutcome`` values. It does not reimplement Forge lifecycle
rules: task creation, cancellation, ownership checks, and remote stop requests
stay in ``PhyAgentOS.forge.task``.

The adapter layer imports only the core contracts and ``PhyAgentOS.forge``; it
does not import :mod:`PhyAgentOS.agent_legacy`. The legacy tool classes keep
their public shapes but delegate to the same definitions and operations, so
the native and legacy paths cannot drift apart.

Outcome rules:
* a local API call that was accepted or is still pending is ``UNKNOWN``, not
  ``SUCCEEDED``; admission is not completion;
* a timeout or an unclear Gateway failure stays ``UNKNOWN`` and is never
  retried here;
* a task/session stop request is only ``SUCCEEDED`` when the response reports
  a terminal stopped/cancelled state. A request that was merely accepted is
  ``UNKNOWN`` as well;
* session identity comes from the explicit ``ExecutionContext``; the legacy
  ``ContextVar`` path is not used by this capability.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from PhyAgentOS.agent.core.types import (
    DomainRefs,
    ExecutionContext,
    ToolCall,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.forge.task import AgentTaskBusyError, AgentTaskCoordinator, AgentTaskRecord
from PhyAgentOS.forge.tool_client import ForgeToolClient

from .forge_task_tools import TASK_TOOL_DEFINITIONS, execute_task_tool
from .forge_tool_api import TOOL_API_DEFINITIONS, execute_tool_api_tool

__all__ = ["ForgeCapability"]

# A start call only means the Gateway admitted the request. The remote action
# or session has not finished, so the local tool call is not a success.
_ACCEPTED_START_TOOLS = frozenset(
    {"forge_tool_start_action", "forge_tool_start_session"}
)

# These tools ask Forge to stop remote work. A request is not proof of stop.
_STOP_REQUEST_TOOLS = frozenset(
    {"forge_tool_cancel_action", "forge_tool_stop_session"}
)

# These tools read authoritative remote invocation state and may return a
# pending, failed, or unknown phase.
_REMOTE_STATE_TOOLS = frozenset(
    {
        "forge_tool_action_status",
        "forge_tool_action_result",
        "forge_tool_session_status",
        "forge_tool_session_result",
    }
)

_SUCCESS_DOMAIN_STATUSES = frozenset({"succeeded", "completed", "success"})
_FAILED_DOMAIN_STATUSES = frozenset({"failed", "error"})
_CANCELLED_DOMAIN_STATUSES = frozenset({"cancelled", "canceled", "stopped"})
_PENDING_DOMAIN_STATUSES = frozenset(
    {
        "pending",
        "accepted",
        "running",
        "dispatching",
        "stopping",
        "requested",
        "cancel_requested",
        "cancelling",
        "canceling",
        "unknown",
        "awaiting_replan",
        "executing",
    }
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _domain_status(data: Mapping[str, Any]) -> str | None:
    """Return the response's own phase/status, without inventing one."""
    for key in ("phase", "state", "status", "cancel_status"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            status = value.strip().lower()
            if status == "available":
                result = data.get("result")
                if isinstance(result, Mapping):
                    nested = _domain_status(result)
                    if nested is not None:
                        return nested
            return status
    return None


def _split_payload(
    payload: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Split one structured operation response into data and error objects."""
    if not isinstance(payload, Mapping):
        return None, None
    if payload.get("ok") is False:
        error = payload.get("error")
        return None, error if isinstance(error, dict) else {}
    data = payload.get("data")
    return data if isinstance(data, dict) else dict(payload), None


def _domain_refs(
    data: Mapping[str, Any] | None,
    arguments: Mapping[str, Any],
    error: Mapping[str, Any] | None = None,
) -> DomainRefs:
    """Carry Forge identity through without interpreting it in the core."""

    def value(source: Mapping[str, Any] | None, *names: str) -> str | None:
        if source is None:
            return None
        for name in names:
            candidate = source.get(name)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    return DomainRefs(
        task_id=value(data, "task_id")
        or value(arguments, "task_id")
        or value(error, "task_id"),
        revision_id=value(data, "revision_id", "active_revision_id"),
        invocation_id=value(data, "invocation_id")
        or value(arguments, "invocation_id")
        or value(error, "invocation_id"),
        attempt_id=value(data, "attempt_id") or value(error, "attempt_id"),
    )


def _domain_error(data: Mapping[str, Any], status: str) -> ToolError:
    error = data.get("error")
    if isinstance(error, Mapping):
        message = error.get("message") or error.get("type")
        if isinstance(message, str) and message.strip():
            return ToolError("forge_remote_failed", message)
    return ToolError("forge_remote_failed", f"Forge reported {status!r}")


def _unknown_outcome(
    call: ToolCall,
    text: str,
    data: Mapping[str, Any] | None,
    arguments: Mapping[str, Any],
    *,
    domain_status: str | None = None,
    error_payload: Mapping[str, Any] | None = None,
) -> ToolOutcome:
    content: dict[str, Any] = {"data": dict(data) if data is not None else None}
    if domain_status is not None:
        content["domain_status"] = domain_status
    content["remote_state"] = "unknown"
    return ToolOutcome(
        tool_call_id=call.tool_call_id,
        tool_name=call.name,
        status=ToolStatus.UNKNOWN,
        text=text,
        content=content,
        domain_refs=_domain_refs(data, arguments, error_payload),
    )


def _success_outcome(
    call: ToolCall,
    data: Mapping[str, Any],
    arguments: Mapping[str, Any],
    *,
    text: str | None = None,
) -> ToolOutcome:
    payload = {"ok": True, "data": dict(data)}
    return ToolOutcome(
        tool_call_id=call.tool_call_id,
        tool_name=call.name,
        status=ToolStatus.SUCCEEDED,
        text=text or _json(payload),
        content={"data": dict(data)},
        domain_refs=_domain_refs(data, arguments),
    )


def _failed_outcome(
    call: ToolCall,
    code: str,
    message: str,
    *,
    status: ToolStatus = ToolStatus.FAILED,
    data: Mapping[str, Any] | None = None,
    arguments: Mapping[str, Any] | None = None,
    error_payload: Mapping[str, Any] | None = None,
    content: Mapping[str, Any] | None = None,
) -> ToolOutcome:
    body: dict[str, Any] = {"data": dict(data) if data is not None else None}
    if content:
        body.update(content)
    return ToolOutcome(
        tool_call_id=call.tool_call_id,
        tool_name=call.name,
        status=status,
        text=message,
        content=body,
        error=ToolError(code, message),
        domain_refs=_domain_refs(data, arguments or {}, error_payload),
    )


def _error_outcome(
    call: ToolCall,
    error: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> ToolOutcome:
    error_type = error.get("type")
    message = error.get("message")
    message = message if isinstance(message, str) and message.strip() else _json(error)
    code = error.get("code")

    if error_type == "timeout":
        return _unknown_outcome(
            call,
            f"Forge request timed out; remote state is unknown. {message}",
            None,
            arguments,
            error_payload=error,
        )
    if error_type == "gateway_tool_api":
        status_code = error.get("status_code")
        if (
            isinstance(status_code, int)
            and 400 <= status_code < 500
            and status_code not in {408, 429}
        ):
            return _failed_outcome(
                call,
                str(code) if isinstance(code, str) and code else "forge_gateway_rejected",
                message,
                status=ToolStatus.REJECTED,
                arguments=arguments,
                error_payload=error,
            )
        return _unknown_outcome(
            call,
            f"Forge Gateway failed without confirming the remote outcome. {message}",
            None,
            arguments,
            error_payload=error,
        )
    if error_type == "interaction":
        return _failed_outcome(
            call,
            "forge_interaction_denied",
            message,
            status=ToolStatus.REJECTED,
            arguments=arguments,
            error_payload=error,
        )
    if error_type == "agent_task":
        content: dict[str, Any] = {}
        blocking = error.get("blocking")
        if isinstance(blocking, list) and blocking:
            content["blocking"] = blocking
        if isinstance(error.get("task_id"), str):
            content["blocked_task_id"] = error["task_id"]
        if "invocation_id" in error:
            content["blocked_invocation_id"] = error["invocation_id"]
        if isinstance(error.get("status"), str):
            content["blocked_status"] = error["status"]
        return _failed_outcome(
            call,
            "forge_agent_task",
            message,
            arguments=arguments,
            error_payload=error,
            content=content or None,
        )
    if error_type == "runtime":
        return _failed_outcome(
            call,
            "forge_runtime",
            message,
            arguments=arguments,
            error_payload=error,
        )
    return _unknown_outcome(
        call,
        f"Forge returned an unrecognized error; remote state is unknown. {message}",
        None,
        arguments,
        error_payload=error,
    )


def _outcome_for_success(
    call: ToolCall,
    data: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> ToolOutcome:
    status = _domain_status(data)

    if call.name in _ACCEPTED_START_TOOLS:
        return _unknown_outcome(
            call,
            "Forge accepted the request, but remote completion is not confirmed. "
            + _json({"ok": True, "data": dict(data)}),
            data,
            arguments,
            domain_status=status or "accepted",
        )

    if call.name in _STOP_REQUEST_TOOLS:
        if status in _CANCELLED_DOMAIN_STATUSES:
            return _success_outcome(call, data, arguments)
        return _unknown_outcome(
            call,
            "Forge accepted the stop request, but remote work is not confirmed stopped. "
            + _json({"ok": True, "data": dict(data)}),
            data,
            arguments,
            domain_status=status or "requested",
        )

    if call.name in _REMOTE_STATE_TOOLS:
        if status in _SUCCESS_DOMAIN_STATUSES:
            return _success_outcome(call, data, arguments)
        if status in _FAILED_DOMAIN_STATUSES:
            error = _domain_error(data, status)
            return _failed_outcome(
                call,
                error.code,
                error.message,
                data=data,
                arguments=arguments,
            )
        if status in _CANCELLED_DOMAIN_STATUSES:
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.CANCELLED,
                text=f"Forge reports the invocation as {status}. "
                + _json({"ok": True, "data": dict(data)}),
                content={"data": dict(data), "domain_status": status},
                domain_refs=_domain_refs(data, arguments),
            )
        if status in _PENDING_DOMAIN_STATUSES:
            return _unknown_outcome(
                call,
                f"Forge reports {status!r}; this is not a completed result. "
                + _json({"ok": True, "data": dict(data)}),
                data,
                arguments,
                domain_status=status,
            )
        return _unknown_outcome(
            call,
            "Forge did not report an authoritative success status "
            f"({status or 'missing'}); this is not success. "
            + _json({"ok": True, "data": dict(data)}),
            data,
            arguments,
            domain_status=status,
        )

    if call.name == "forge_task_cancel":
        if status in _CANCELLED_DOMAIN_STATUSES:
            return _success_outcome(call, data, arguments)
        if status in _FAILED_DOMAIN_STATUSES:
            error = _domain_error(data, status)
            return _failed_outcome(
                call,
                error.code,
                error.message,
                data=data,
                arguments=arguments,
            )
        return _unknown_outcome(
            call,
            f"Forge task cancellation is {status or 'unconfirmed'}; remote work may still run. "
            + _json({"ok": True, "data": dict(data)}),
            data,
            arguments,
            domain_status=status,
        )

    if call.name == "forge_task_finalize":
        if status in _SUCCESS_DOMAIN_STATUSES:
            return _success_outcome(call, data, arguments)
        if status in _FAILED_DOMAIN_STATUSES:
            error = _domain_error(data, status)
            return _failed_outcome(
                call,
                error.code,
                error.message,
                data=data,
                arguments=arguments,
            )
        if status in _CANCELLED_DOMAIN_STATUSES:
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.CANCELLED,
                text=f"Forge finalized the task as {status}. "
                + _json({"ok": True, "data": dict(data)}),
                content={"data": dict(data), "domain_status": status},
                domain_refs=_domain_refs(data, arguments),
            )
        return _unknown_outcome(
            call,
            f"Forge finalized the task as {status or 'unconfirmed'}; it is not task success. "
            + _json({"ok": True, "data": dict(data)}),
            data,
            arguments,
            domain_status=status,
        )

    return _success_outcome(call, data, arguments)


def _outcome(
    call: ToolCall,
    payload: Mapping[str, Any] | None,
    arguments: Mapping[str, Any],
) -> ToolOutcome:
    data, error = _split_payload(payload)
    if error is not None:
        return _error_outcome(call, error, arguments)
    if data is None:
        return _unknown_outcome(
            call,
            "Forge returned a response this capability could not parse; "
            "remote state is unknown.",
            None,
            arguments,
        )
    return _outcome_for_success(call, data, arguments)


class ForgeCapability:
    """Expose the Forge AgentTask and Tool API groups to the agent core.

    ``coordinator`` owns the AgentTask store and remote execution rules;
    ``client`` is optional and adds the Gateway Tool API group when supplied.
    Both are already-built objects, matching the capability contract. The
    capability creates no clients, stores, or background tasks.
    """

    def __init__(
        self,
        coordinator: AgentTaskCoordinator,
        *,
        client: ForgeToolClient | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._client = client
        registrations: list[ToolRegistration] = [
            ToolRegistration(
                definition=definition,
                handler=self._task_handler(definition.name),
            )
            for definition in TASK_TOOL_DEFINITIONS
        ]
        if client is not None:
            registrations.extend(
                ToolRegistration(
                    definition=definition,
                    handler=self._tool_api_handler(definition.name),
                )
                for definition in TOOL_API_DEFINITIONS
            )
        self._registrations = tuple(registrations)

    @property
    def name(self) -> str:
        return "forge"

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return self._registrations

    def _task_handler(self, name: str):
        async def handle(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            arguments = call.arguments_dict()
            # Session identity is explicit on this path and cannot come from
            # the model's arguments.
            try:
                payload = await execute_task_tool(
                    name,
                    self._coordinator,
                    arguments,
                    session_id=context.session_id,
                )
            except AgentTaskBusyError as exc:
                payload = {
                    "ok": False,
                    "error": {
                        "type": "agent_task",
                        "code": "forge_agent_task",
                        "message": str(exc),
                        "task_id": exc.task_id,
                        "invocation_id": exc.invocation_id,
                        "status": exc.status,
                        "blocking": list(exc.blocking),
                    },
                }
            return _outcome(call, payload, arguments)

        return handle

    def _tool_api_handler(self, name: str):
        async def handle(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            arguments = call.arguments_dict()
            assert self._client is not None
            payload = await execute_tool_api_tool(
                name, self._client, self._coordinator, arguments
            )
            return _outcome(call, payload, arguments)

        return handle

    def prompt_context(self, context: ExecutionContext) -> str | None:
        """Return the authoritative latest task state for this session.

        The store is queried on every call; nothing is cached and nothing is
        invented when the session has no task.
        """
        record = self._coordinator.store.latest_for_session(context.session_id)
        if record is None:
            return None
        nonterminal = sum(
            1 for item in record.execution_records if not item.terminal
        )
        unknown = sum(
            1 for item in record.execution_records if item.status == "unknown"
        )
        return (
            f"Forge task {record.task_id}: status={record.status.value}; "
            f"active_revision={record.active_revision_id}; "
            f"cancellation_requested={record.cancellation_requested}; "
            f"nonterminal_invocations={nonterminal}; unknown_invocations={unknown}."
        )

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        """Ask Forge to cancel the latest task owned by this session.

        The lookup and stop request use Forge's own store/coordinator. A
        normal return only means the request was delivered; it does not claim
        that remote work stopped.
        """
        del context
        record: AgentTaskRecord | None = (
            self._coordinator.store.latest_for_session(session_id)
        )
        if record is None:
            return
        await self._coordinator.cancel_task(record.task_id, reason="agent_cancel")

    async def aclose(self) -> None:
        """Release nothing: the injected coordinator/client are externally owned."""
        return None

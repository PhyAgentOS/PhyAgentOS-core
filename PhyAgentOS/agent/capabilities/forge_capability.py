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
from collections.abc import Callable, Mapping
from typing import Any

from PhyAgentOS.agent.core.types import (
    DomainRefs,
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.forge.task import (
    AgentTaskBusyError,
    AgentTaskCoordinator,
    AgentTaskRecord,
    _stale_dispatch_unknown,
)
from PhyAgentOS.forge.tool_client import ForgeToolClient

from .forge_task_tools import (
    TASK_TOOL_DEFINITIONS,
    execute_task_tool,
    verification_schema,
)
from .forge_tool_api import TOOL_API_DEFINITIONS, execute_tool_api_tool

__all__ = ["ForgeCapability"]

# A start call only means the Gateway admitted the request. The remote action
# or session has not finished, so the local tool call is not a success.
_ACCEPTED_START_TOOLS = frozenset(
    {"forge_tool_start_action", "forge_tool_start_session"}
)

# Pure reads that do not write AgentTask state. They may overlap in one model
# step; every Action/Session control remains a sequential barrier.
_PARALLEL_SAFE_TOOLS = frozenset(
    {"forge_tool_context", "forge_tool_list", "forge_task_get"}
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


_RUN_ACTION_TOOL = "forge_task_run_action"

_STAGE_HIDE_BEFORE_ACTIVATION = frozenset(
    {"forge_task_create", "forge_task_begin_revision", "forge_task_finalize",
     "forge_tool_start_action", "forge_tool_start_session", _RUN_ACTION_TOOL}
)
_STAGE_HIDE_BEFORE_TASK = frozenset(
    {"forge_task_begin_revision", "forge_task_finalize",
     "forge_tool_start_action", "forge_tool_start_session"}
)
# The single-action orchestration creates its own task, so it must not be
# offered while this session already owns a non-terminal task.
_STAGE_HIDE_WHEN_TASK = frozenset({_RUN_ACTION_TOOL})

_RUN_ACTION_DEFINITION = ToolDefinition(
    name=_RUN_ACTION_TOOL,
    description=(
        "One-call orchestration for a simple task that needs exactly one Forge Action: "
        "create the AgentTask, read the live Tool context, start the Action and wait for "
        "a terminal result, then finalize the task. Use it only when the request needs a "
        "single Action and no replan; activation by activate_skill still comes first. "
        "The Action is started at most once. If the result is pending, unknown, failed, "
        "or cancelled, this tool reports that truthfully and never treats it as success. "
        "Use forge_task_create / forge_tool_start_action / forge_task_finalize for "
        "multi-action tasks, replanning, or any additional Query/Session work."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_description": {"type": "string", "minLength": 1},
            "activation_id": {
                "type": "string",
                "pattern": "^activation_[a-z0-9]+$",
                "description": "Primary activate_skill result from this session turn",
            },
            "verification": verification_schema(),
            "tool_id": {"type": "string", "minLength": 1},
            "arguments": {"type": "object"},
            "wait_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": 60_000,
                "description": (
                    "Optional total wait inside this call for a terminal Action result; "
                    "defaults to 60000. A timeout stays pending/unknown, not success."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional Gateway execution timeout in ms. Omit it when the Gateway's "
                    "limit is unknown so the Gateway default applies."
                ),
            },
        },
        "required": [
            "task_description",
            "activation_id",
            "verification",
            "tool_id",
            "arguments",
        ],
        "additionalProperties": False,
    },
)

_COMPACT_ACTION_KEYS = (
    "tool_id",
    "status",
    "invocation_id",
    "attempt_id",
    "error",
)


def _compact_task_result(
    data: Mapping[str, Any],
    tool_id: str,
    refs: DomainRefs,
) -> dict[str, Any]:
    """Return a small, truthful view of the finalized task and its Action."""
    records: list[Mapping[str, Any]] = []
    raw_records = data.get("execution_records")
    if isinstance(raw_records, list):
        records.extend(item for item in raw_records if isinstance(item, Mapping))
    revisions = data.get("revisions")
    if isinstance(revisions, list):
        for revision in revisions:
            if not isinstance(revision, Mapping):
                continue
            nested = revision.get("execution_records")
            if isinstance(nested, list):
                records.extend(
                    item for item in nested if isinstance(item, Mapping)
                )
    action: Mapping[str, Any] | None = None
    actions = [item for item in records if item.get("semantics") == "action"]
    matches = [item for item in actions if item.get("tool_id") == tool_id]
    chosen = matches or actions
    if chosen:
        action = chosen[-1]
    action_view: dict[str, Any] = {}
    if action is not None:
        for key in _COMPACT_ACTION_KEYS:
            if key in action:
                action_view[key] = action[key]
        if action.get("response") is not None:
            action_view["response"] = action["response"]
    if not action_view:
        action_view = {"tool_id": tool_id}
        if refs.invocation_id is not None:
            action_view["invocation_id"] = refs.invocation_id
        if refs.attempt_id is not None:
            action_view["attempt_id"] = refs.attempt_id

    result: dict[str, Any] = {
        "task_id": data.get("task_id") or refs.task_id,
        "status": data.get("status"),
        "action": action_view,
    }
    verdict = data.get("verdict")
    if isinstance(verdict, Mapping):
        result["verdict"] = {
            key: verdict[key]
            for key in ("verdict", "reason")
            if isinstance(verdict.get(key), str)
        }
    errors = data.get("evidence_errors")
    if isinstance(errors, list) and errors:
        result["evidence_errors"] = list(errors)
    return result


def _compact_domain_refs(
    compact: Mapping[str, Any], fallback: DomainRefs
) -> DomainRefs:
    """Keep identity from the compact result, falling back to earlier steps."""
    task_id = compact.get("task_id")
    action = compact.get("action")
    invocation_id = attempt_id = None
    if isinstance(action, Mapping):
        value = action.get("invocation_id")
        invocation_id = value if isinstance(value, str) else None
        value = action.get("attempt_id")
        attempt_id = value if isinstance(value, str) else None
    return DomainRefs(
        task_id=task_id if isinstance(task_id, str) else fallback.task_id,
        revision_id=fallback.revision_id,
        invocation_id=invocation_id or fallback.invocation_id,
        attempt_id=attempt_id or fallback.attempt_id,
    )


def _task_failure_detail(compact: Mapping[str, Any]) -> str:
    verdict = compact.get("verdict")
    if isinstance(verdict, Mapping):
        reason = verdict.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason
    errors = compact.get("evidence_errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors[:3])
    status = compact.get("status")
    return f"Forge task finalized as {status or 'not succeeded'}"


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
        self._stage_resolver: Callable[[ExecutionContext], str] | None = None
        # session_id -> (turn_id, task_id, terminal status). The barrier lasts
        # only for the turn whose finalize produced the terminal task; a later
        # turn starts with tools again.
        self._terminal_turns: dict[str, tuple[str, str, str]] = {}
        registrations: list[ToolRegistration] = [
            ToolRegistration(
                definition=definition,
                handler=self._task_handler(definition.name),
                parallel_safe=definition.name in _PARALLEL_SAFE_TOOLS,
            )
            for definition in TASK_TOOL_DEFINITIONS
        ]
        if client is not None:
            registrations.extend(
                ToolRegistration(
                    definition=definition,
                    handler=self._tool_api_handler(definition.name),
                    parallel_safe=definition.name in _PARALLEL_SAFE_TOOLS,
                )
                for definition in TOOL_API_DEFINITIONS
            )
            registrations.append(
                ToolRegistration(
                    definition=_RUN_ACTION_DEFINITION,
                    handler=self._run_action_handler(),
                )
            )
        self._registrations = tuple(registrations)

    @property
    def name(self) -> str:
        return "forge"

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return self._registrations

    def set_stage_resolver(self, resolver: Callable[[ExecutionContext], str] | None) -> None:
        """Install the entry's authoritative stage resolver.

        The resolver returns ``"none"`` (no primary activation), ``"activated"``
        or ``"task"`` (an AgentTask exists for this session). Leaving it unset
        keeps every Forge tool visible, which is the pre-existing behaviour for
        entries that do not wire Skill activation.
        """
        self._stage_resolver = resolver

    def stage(self, context: ExecutionContext) -> str:
        if self._stage_resolver is None:
            return "task"
        return self._stage_resolver(context)

    def hidden_tools(self, context: ExecutionContext) -> frozenset[str]:
        """Forge tool names the current stage must not show.

        Recovery tools (get/status/result/cancel) are never hidden. Without a
        primary activation only ``forge_task_create`` and the mutating task /
        action tools are hidden; before any AgentTask exists, the mutating task
        and action tools stay hidden but every read/query tool remains.

        After a terminal finalize, every Forge tool is hidden for the rest of
        that turn: the task is over, so the model must answer from recorded
        facts instead of exploring again. Composition also suppresses tools
        owned by other capabilities for the same turn.
        """
        if self._terminal_state(context) is not None:
            return frozenset(
                registration.definition.name for registration in self._registrations
            )
        if self._stage_resolver is None:
            # No activation wiring: keep the documented pre-existing behaviour
            # where every Forge tool is visible.
            return frozenset()
        stage = self.stage(context)
        if stage == "task":
            return _STAGE_HIDE_WHEN_TASK
        if stage == "activated":
            return _STAGE_HIDE_BEFORE_TASK
        return _STAGE_HIDE_BEFORE_ACTIVATION

    def suppress_tools(self, context: ExecutionContext) -> bool:
        """Whether this step must offer no tools at all.

        True only after ``forge_task_finalize`` returned and the corresponding
        AgentTask is definitively terminal in the store. A failed finalize, an
        unknown/pending result, or a non-terminal task never sets it.
        """
        return self._terminal_state(context) is not None

    def _terminal_state(
        self, context: ExecutionContext
    ) -> tuple[str, str, str] | None:
        """Return this turn's finalized (turn_id, task_id, status), if any."""
        state = self._terminal_turns.get(context.session_id)
        if state is None:
            return None
        if state[0] != context.turn_id:
            # A new turn is not bound by the earlier turn's terminal task.
            self._terminal_turns.pop(context.session_id, None)
            return None
        return state

    def _note_terminal_finalize(
        self,
        call: ToolCall,
        context: ExecutionContext,
        outcome: ToolOutcome,
        arguments: Mapping[str, Any],
    ) -> None:
        """Record a terminal task only when finalize really produced one.

        ``UNKNOWN``, ``REJECTED`` and still-running results are never evidence
        of a terminal task. A ``FAILED`` outcome is recorded too when the store
        says the task is terminal, because the task is over either way; the
        prompt reports the real status and never calls it success.
        """
        if call.name != "forge_task_finalize":
            return
        if outcome.status in {
            ToolStatus.UNKNOWN,
            ToolStatus.REJECTED,
            ToolStatus.RUNNING,
        }:
            return
        data = outcome.content.get("data") if outcome.content else None
        if not isinstance(data, Mapping):
            # A transport/handler failure carries no task state. It is not a
            # terminal finalize result even if an older record is terminal.
            return
        response_status = data.get("status")
        if not isinstance(response_status, str) or response_status not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            return
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            task_id = arguments.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        try:
            record = self._coordinator.get_task(task_id)
        except Exception:
            return
        if not getattr(record, "terminal", False):
            return
        status = getattr(record, "status", None)
        status_text = getattr(status, "value", None) or str(status)
        self._terminal_turns[context.session_id] = (
            context.turn_id,
            task_id,
            status_text,
        )

    def _stage_blocked(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome | None:
        if call.name not in self.hidden_tools(context):
            return None
        return _failed_outcome(
            call,
            "tool_stage_blocked",
            "Tool is not available at this stage. Activate a primary Skill first; "
            "mutating action tools additionally need an existing AgentTask. "
            "Read-only context/query/status/get/cancel tools remain available.",
            status=ToolStatus.REJECTED,
        )

    def _task_handler(self, name: str):
        async def handle(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            blocked = self._stage_blocked(call, context)
            if blocked is not None:
                return blocked
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
            outcome = _outcome(call, payload, arguments)
            self._note_terminal_finalize(call, context, outcome, arguments)
            return outcome

        return handle

    def _tool_api_handler(self, name: str):
        async def handle(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            blocked = self._stage_blocked(call, context)
            if blocked is not None:
                return blocked
            arguments = call.arguments_dict()
            assert self._client is not None
            payload = await execute_tool_api_tool(
                name, self._client, self._coordinator, arguments
            )
            return _outcome(call, payload, arguments)

        return handle

    @staticmethod
    def _sub_call(
        parent: ToolCall,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolCall:
        """Build the next internal call without inventing a second dispatch path.

        The orchestration calls the same capability handlers the model would
        call; only the audit identity is local to the composite call.
        """
        return ToolCall(
            tool_call_id=f"{parent.tool_call_id}:{name}",
            name=name,
            arguments=dict(arguments),
        )

    def _run_action_handler(self):
        async def handle(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            blocked = self._stage_blocked(call, context)
            if blocked is not None:
                return blocked
            return await self._run_single_action(
                call, context, call.arguments_dict()
            )

        return handle

    async def _run_single_action(
        self,
        call: ToolCall,
        context: ExecutionContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        """Run create -> context -> start (once) -> finalize through handlers.

        The Core loop cannot interleave its own cancellation check between the
        internal steps of one handler call, so this method checks the turn's
        cooperative token before every stage. In particular, no Action is
        dispatched after a cancellation, and an already-dispatched Action is
        never resent.
        """
        if context.cancellation.cancelled:
            return self._orchestration_cancelled(
                call, "task creation", completed_before="no task was created"
            )
        create_arguments = {
            "task_description": arguments["task_description"],
            "activation_id": arguments["activation_id"],
            "verification": arguments["verification"],
        }
        created = await self._task_handler("forge_task_create")(
            self._sub_call(call, "forge_task_create", create_arguments), context
        )
        if created.status is not ToolStatus.SUCCEEDED:
            return self._orchestration_failure(call, "task creation", created)
        data = self._outcome_data(created)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return self._orchestration_failure(
                call,
                "task creation",
                _failed_outcome(
                    call,
                    "forge_single_action_invalid_task",
                    "task creation returned no task_id; no Action was started",
                ),
            )
        if context.cancellation.cancelled:
            return self._orchestration_cancelled(
                call,
                "context/readiness check",
                task_id=task_id,
                completed_before="the Action was not started",
            )

        # Reuse the existing live context read. The authoritative readiness decision
        # stays in start_action's frozen-binding validation, so its rules are not
        # duplicated here; the Action is not dispatched if this read fails.
        ready = await self._tool_api_handler("forge_tool_context")(
            self._sub_call(call, "forge_tool_context", {"tool_id": arguments["tool_id"]}),
            context,
        )
        if ready.status is not ToolStatus.SUCCEEDED:
            # Readiness is not confirmed, so the Action is not dispatched. The
            # created task stays non-terminal exactly as the store records it.
            return self._orchestration_failure(
                call,
                "context/readiness check",
                ready,
                task_id=task_id,
                refs=DomainRefs(
                    task_id=task_id,
                    revision_id=created.domain_refs.revision_id,
                ),
            )
        if context.cancellation.cancelled:
            return self._orchestration_cancelled(
                call,
                "Action dispatch",
                task_id=task_id,
                completed_before="the Action was not started",
            )

        start_arguments: dict[str, Any] = {
            "task_id": task_id,
            "tool_id": arguments["tool_id"],
            "arguments": arguments["arguments"],
            "wait_ms": 60_000 if arguments.get("wait_ms") is None else arguments["wait_ms"],
        }
        if arguments.get("timeout_ms") is not None:
            start_arguments["timeout_ms"] = arguments["timeout_ms"]
        started = await self._tool_api_handler("forge_tool_start_action")(
            self._sub_call(call, "forge_tool_start_action", start_arguments), context
        )
        action_refs = DomainRefs(
            task_id=task_id,
            revision_id=created.domain_refs.revision_id,
            invocation_id=started.domain_refs.invocation_id,
            attempt_id=started.domain_refs.attempt_id,
        )
        if context.cancellation.cancelled:
            # The Action may already have been dispatched. Keep its identity,
            # never resend it, and never claim task success.
            return self._orchestration_cancelled(
                call,
                "Action dispatch",
                task_id=task_id,
                refs=action_refs,
                completed_before=(
                    "the Action was already dispatched at most once; do not resend it"
                ),
            )
        if started.status is ToolStatus.UNKNOWN:
            # Admission or a pending wait is not success and must not be
            # finalized. The model gets the real invocation identity instead.
            return self._orchestration_pending(
                call, "Action start", started, task_id, action_refs
            )
        if started.status is ToolStatus.REJECTED:
            return self._orchestration_failure(
                call, "Action start", started, task_id=task_id, refs=action_refs
            )
        if (
            started.status is ToolStatus.FAILED
            and started.domain_refs.invocation_id is None
        ):
            # A pre-dispatch failure (for example a binding/readiness error)
            # never reached the Gateway, so there is no execution to finalize.
            return self._orchestration_failure(
                call, "Action start", started, task_id=task_id, refs=action_refs
            )

        finalized = await self._task_handler("forge_task_finalize")(
            self._sub_call(call, "forge_task_finalize", {"task_id": task_id}), context
        )
        if context.cancellation.cancelled:
            return self._orchestration_cancelled(
                call,
                "task finalize",
                task_id=task_id,
                refs=action_refs,
                completed_before=(
                    "the Action was already dispatched at most once and was not resent"
                ),
            )
        if finalized.status is ToolStatus.UNKNOWN:
            return self._orchestration_pending(
                call, "task finalize", finalized, task_id, action_refs
            )
        final_data = self._outcome_data(finalized)
        if not final_data:
            return self._orchestration_failure(
                call, "task finalize", finalized, task_id=task_id, refs=action_refs
            )

        compact = _compact_task_result(
            final_data, arguments["tool_id"], action_refs
        )
        refs = _compact_domain_refs(compact, action_refs)
        if (
            finalized.status is ToolStatus.SUCCEEDED
            and started.status is ToolStatus.SUCCEEDED
        ):
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.SUCCEEDED,
                text=_json({"ok": True, "data": compact}),
                content={"data": compact},
                domain_refs=refs,
            )

        status = (
            finalized.status
            if finalized.status in {ToolStatus.FAILED, ToolStatus.CANCELLED}
            else started.status
        )
        if status is ToolStatus.SUCCEEDED:
            status = ToolStatus.FAILED
        detail = _task_failure_detail(compact)
        text = (
            f"Forge single-action task finalized as {compact.get('status') or status.value}; "
            "this is not success. " + _json({"ok": True, "data": compact})
        )
        error = (
            ToolError("forge_task_not_succeeded", detail)
            if status is ToolStatus.FAILED
            else None
        )
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=status,
            text=text,
            content={"data": compact, "phase": "finalize"},
            error=error,
            domain_refs=refs,
        )

    @staticmethod
    def _outcome_data(outcome: ToolOutcome) -> dict[str, Any]:
        content = outcome.content
        if not isinstance(content, Mapping):
            return {}
        data = content.get("data")
        return dict(data) if isinstance(data, Mapping) else {}

    def _orchestration_failure(
        self,
        call: ToolCall,
        phase: str,
        outcome: ToolOutcome,
        *,
        task_id: str | None = None,
        refs: DomainRefs | None = None,
    ) -> ToolOutcome:
        """Turn a failed sub-step into a non-success composite result."""
        status = outcome.status
        if status is ToolStatus.SUCCEEDED:
            status = ToolStatus.FAILED
        content: dict[str, Any] = {
            "phase": phase,
            "sub_status": outcome.status.value,
        }
        if task_id is not None:
            content["task_id"] = task_id
        if outcome.error is not None:
            content["sub_error"] = {
                "code": outcome.error.code,
                "message": outcome.error.message,
            }
        if outcome.content:
            content["sub_content"] = dict(outcome.content)
        text = (
            f"Forge single-action orchestration stopped during {phase}: "
            f"{outcome.text} No later step was run and no success is claimed."
        )
        error = (
            ToolError(
                outcome.error.code if outcome.error is not None else "forge_single_action",
                text,
            )
            if status in {ToolStatus.FAILED, ToolStatus.REJECTED}
            else None
        )
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=status,
            text=text,
            content=content,
            error=error,
            domain_refs=refs or outcome.domain_refs,
        )

    def _orchestration_cancelled(
        self,
        call: ToolCall,
        phase: str,
        *,
        task_id: str | None = None,
        refs: DomainRefs | None = None,
        completed_before: str | None = None,
    ) -> ToolOutcome:
        """Report cooperative local cancellation without claiming success.

        No further internal step was started at the point this is returned. If
        the Action had already been dispatched, ``refs`` keeps its invocation
        and attempt identity so the caller can reconcile it instead of resending.
        """
        details: dict[str, Any] = {"phase": phase}
        if task_id is not None:
            details["task_id"] = task_id
        if refs is not None:
            if refs.invocation_id is not None:
                details["invocation_id"] = refs.invocation_id
            if refs.attempt_id is not None:
                details["attempt_id"] = refs.attempt_id
        text = (
            f"Forge single-action orchestration was cancelled locally during {phase}. "
            f"{completed_before or 'no further step was started'}. No success is "
            "claimed. " + _json({"ok": True, "data": details})
        )
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.CANCELLED,
            text=text,
            content={"data": details, "phase": phase},
            error=ToolError("forge_single_action_cancelled", text),
            domain_refs=refs or DomainRefs(),
        )

    def _orchestration_pending(
        self,
        call: ToolCall,
        phase: str,
        outcome: ToolOutcome,
        task_id: str,
        refs: DomainRefs,
    ) -> ToolOutcome:
        """Report an admitted but non-terminal Action without retrying it."""
        pending = {
            "task_id": task_id,
            "invocation_id": refs.invocation_id,
            "attempt_id": refs.attempt_id,
        }
        domain_status = None
        if isinstance(outcome.content, Mapping):
            value = outcome.content.get("domain_status")
            if isinstance(value, str):
                domain_status = value
        if domain_status is not None:
            pending["domain_status"] = domain_status
        if phase == "task finalize" and domain_status == "awaiting_replan":
            text = (
                "Forge task finalize returned awaiting_replan; this single-action "
                "orchestration does not replan and did not claim success. Continue "
                "with forge_task_begin_revision and the individual task/action tools."
            )
        elif phase == "task finalize":
            text = (
                "Forge task finalize did not confirm a terminal task; no success is "
                "claimed and the Action was not resent. Inspect forge_task_get and "
                "follow the recorded task state. " + _json({"ok": True, "data": pending})
            )
        else:
            text = (
                f"Forge single-action orchestration stopped during {phase}: the remote "
                "state is unknown and the Action was started at most once. Do not "
                "resend it. " + _json({"ok": True, "data": pending})
            )
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.UNKNOWN,
            text=text,
            content={"data": pending, "phase": phase, "remote_state": "unknown"},
            domain_refs=refs,
        )

    def _current_task(self, session_id: str) -> AgentTaskRecord | None:
        """Return this session's newest non-terminal task, if the store can."""
        store = self._coordinator.store
        getter = getattr(store, "nonterminal_for_session", None)
        if callable(getter):
            return getter(session_id)
        record = store.latest_for_session(session_id)
        return None if record is None or record.terminal else record

    def has_active_task(self, session_id: str) -> bool:
        """Authoritative check: does this session own a non-terminal AgentTask?"""
        return self._current_task(session_id) is not None

    def prompt_context(self, context: ExecutionContext) -> str | None:
        """Return this session's current unfinished Forge task, if any.

        Only the newest non-terminal task for the calling session is considered,
        and only while it is not locally abandonable. A task whose only
        unfinished dispatch has no Gateway identity cannot be advanced locally,
        so it is not shown as the task to continue. Finished history and
        unresolved remote state from other sessions are facts in the store, not
        prompt material; the model can read them explicitly with
        ``forge_task_get`` when the user asks. Nothing is cached and nothing is
        invented.
        """
        terminal = self._terminal_state(context)
        if terminal is not None:
            _, task_id, status = terminal
            return (
                f"Forge task {task_id} is finalized with status={status}. "
                "This turn's tool use is complete: every tool is unavailable in "
                "this step. Reply to the user now from the recorded task, Action "
                "result, verdict, and evidence errors already returned. Do not "
                "take screenshots, run visual analysis, or search for evidence "
                "files. Claim success only if status=succeeded; otherwise state "
                "the failure, cancellation, or unknown remote state plainly."
            )
        record = self._current_task(context.session_id)
        if record is None or _stale_dispatch_unknown(record):
            if (
                record is None
                and self._stage_resolver is not None
                and self.stage(context) == "activated"
            ):
                return (
                    "For a simple task that needs exactly one Forge Action, prefer "
                    "forge_task_run_action: it creates the AgentTask, checks readiness, "
                    "starts that one Action, waits for a terminal result, and finalizes "
                    "in one call. Use the individual task/action tools for multiple "
                    "Actions, replanning, or extra Query/Session work."
                )
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

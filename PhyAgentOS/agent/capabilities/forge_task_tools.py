"""Native definitions and operations for the five Forge AgentTask tools.

This module is the single source for the task-tool names, descriptions, JSON
Schemas and operations. ``forge_capability`` builds Core registrations from
the definitions and calls the operations directly; the legacy wrappers in
:mod:`PhyAgentOS.agent_legacy.tools.forge_task` only keep their historical
``Tool`` class shapes and serialize the returned mapping back to a JSON string.

Operations return plain dictionaries shaped like the old tool responses
(``{"ok": True, "data": ...}`` or ``{"ok": False, "error": ...}``) so no
serialize/parse round trip is needed on the native path. They do not execute
the robot: every physical call still goes through
``ForgeToolClient -> Gateway Tool API -> ToolEndpoint``.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from PhyAgentOS.agent.core.types import ToolDefinition
from PhyAgentOS.forge.task import AgentTaskCoordinator, AgentTaskRecord
from PhyAgentOS.verification.contracts import TaskVerificationContract

__all__ = [
    "TASK_TOOL_DEFINITIONS",
    "execute_task_tool",
]

_SUPPORTED_EVIDENCE_KINDS = ("rgb_image", "robot_state")
_DEFAULT_EVIDENCE_KINDS = ["robot_state"]


def _prepare_verification(
    verification: dict[str, Any],
    *,
    image_sources: list[str],
) -> dict[str, Any]:
    """Reject evidence policies this Forge path cannot satisfy.

    A classic AgentTask Evidence Bundle is built from observation snapshots only:
    each phase can hold rgb_image artifacts (when the Gateway publishes images)
    and one robot_state artifact from /ws/state. Query and Action results live in
    the execution records, not in the bundle, so requiring them as artifact kinds
    can never complete.
    """
    prepared = dict(verification)
    raw_policy = prepared.get("evidence_policy")
    policy = dict(raw_policy) if isinstance(raw_policy, dict) else {}
    kinds = policy.get("required_kinds")
    if kinds is None:
        kinds = list(_DEFAULT_EVIDENCE_KINDS)
    unsupported = [
        str(kind) for kind in kinds if kind not in _SUPPORTED_EVIDENCE_KINDS
    ]
    if unsupported:
        raise ValueError(
            "unsupported evidence required_kinds: "
            + ", ".join(unsupported)
            + "; this Forge path records only "
            + ", ".join(_SUPPORTED_EVIDENCE_KINDS)
            + " artifacts (query/action results stay in execution records)"
        )
    association = policy.get("minimum_association", "best_effort")
    if association != "best_effort":
        raise ValueError(
            "minimum_association=authoritative is unavailable for AgentTask evidence; "
            "PAOS Forge collection records best-effort observation evidence"
        )
    if "rgb_image" in kinds and not image_sources:
        raise ValueError(
            "rgb_image evidence requires forge.evidence.requiredImageSources and a Gateway "
            "image stream; this runtime has neither. Use robot_state evidence instead."
        )
    policy["required_kinds"] = list(kinds)
    prepared["evidence_policy"] = policy
    return prepared


def _task_payload(task: AgentTaskRecord) -> dict[str, Any]:
    return {
        "ok": True,
        "data": task.model_dump(mode="json", exclude_none=True),
    }


def _image_sources(coordinator: AgentTaskCoordinator) -> list[str]:
    coordinator_config = getattr(coordinator, "config", None)
    evidence_config = getattr(coordinator_config, "evidence", None)
    if evidence_config is None:
        return []
    return list(evidence_config.required_image_sources)


async def _create(
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    verification = _prepare_verification(
        arguments["verification"],
        image_sources=_image_sources(coordinator),
    )
    task = coordinator.create_task(
        task_description=arguments["task_description"],
        activation_id=arguments["activation_id"],
        verification=TaskVerificationContract.model_validate(verification),
        origin_session_key=session_id,
    )
    if inspect.isawaitable(task):
        task = await task
    return _task_payload(task)


async def _get(
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    del session_id
    task_id = arguments["task_id"]
    payload = _task_payload(coordinator.get_task(task_id))
    interaction = getattr(coordinator, "interaction", None)
    summary = interaction.summary(task_id) if interaction is not None else None
    if summary is not None:
        payload["interaction"] = summary
    return payload


async def _begin_revision(
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    del session_id
    return _task_payload(
        coordinator.begin_revision(arguments["task_id"], reason=arguments["reason"])
    )


async def _finalize(
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    del session_id
    return _task_payload(await coordinator.finalize_task(arguments["task_id"]))


async def _cancel(
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    del session_id
    return _task_payload(
        await coordinator.cancel_task(
            arguments["task_id"],
            reason=arguments.get("reason", "agent_requested"),
        )
    )


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
                "description": (
                    "Observation evidence requirements. This Forge path can produce "
                    "rgb_image and robot_state artifacts. rgb_image also needs a Gateway "
                    "image stream and forge.evidence.requiredImageSources; robot_state "
                    "reads /ws/state. Defaults to robot_state."
                ),
                "properties": {
                    "profile": {"type": "string"},
                    "required_kinds": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(_SUPPORTED_EVIDENCE_KINDS),
                        },
                    },
                    "required_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "minimum_association": {
                        "type": "string",
                        "enum": ["best_effort"],
                        "description": (
                            "PAOS Forge observation evidence is best-effort; "
                            "authoritative association is not available here."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }


_CREATE_DEFINITION = ToolDefinition(
    name="forge_task_create",
    description=(
        "Create the single active AgentTask before a task-bound Forge Tool sequence. "
        "This records planning and verification context but does not execute the robot. "
        "Evidence is captured in two phases: before evidence when the first bound Action "
        "starts (confirm before_snapshot_ref and an empty evidence_errors via "
        "forge_task_get after that Action is admitted), and after evidence when "
        "forge_task_finalize runs after every Action is terminal. Keep observation "
        "sources live before the first Action; the agent cannot create evidence itself."
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
            "verification": _verification_schema(),
        },
        "required": ["task_description", "activation_id", "verification"],
        "additionalProperties": False,
    },
)

_GET_DEFINITION = ToolDefinition(
    name="forge_task_get",
    description=(
        "Read persisted AgentTask, PlanRevision, Tool execution, evidence and verdict state."
    ),
    parameters=_task_id_schema(),
)

_BEGIN_REVISION_DEFINITION = ToolDefinition(
    name="forge_task_begin_revision",
    description=(
        "Append a new immutable PlanRevision to the same task after verification requests "
        "replanning."
    ),
    parameters={
        **_task_id_schema(),
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["task_id", "reason"],
    },
)

_FINALIZE_DEFINITION = ToolDefinition(
    name="forge_task_finalize",
    description=(
        "Finalize a task after every bound Action is terminal. Before calling it, confirm "
        "with forge_task_get that before_snapshot_ref is present and evidence_errors is "
        "empty; finalize then captures after evidence and judges the user-level "
        "verification contract. If finalize reports an incomplete Evidence Bundle, use "
        "read-only tools to inspect the missing kinds; do not resubmit the same Action or "
        "replay finalize with unchanged evidence."
    ),
    parameters=_task_id_schema(),
)

_CANCEL_DEFINITION = ToolDefinition(
    name="forge_task_cancel",
    description=(
        "Request cancellation for every non-terminal Action bound to an AgentTask. "
        "Cancellation acceptance is not proof that motion stopped."
    ),
    parameters={
        **_task_id_schema(),
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        },
    },
)

_TaskOperation = Callable[..., Awaitable[dict[str, Any]]]

# Order is the registration order every caller must keep.
_TASK_TOOLS: tuple[tuple[ToolDefinition, _TaskOperation], ...] = (
    (_CREATE_DEFINITION, _create),
    (_GET_DEFINITION, _get),
    (_BEGIN_REVISION_DEFINITION, _begin_revision),
    (_FINALIZE_DEFINITION, _finalize),
    (_CANCEL_DEFINITION, _cancel),
)

TASK_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = tuple(
    definition for definition, _ in _TASK_TOOLS
)

_TASK_OPERATIONS = {definition.name: operation for definition, operation in _TASK_TOOLS}


async def execute_task_tool(
    name: str,
    coordinator: AgentTaskCoordinator,
    arguments: Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run one task operation and return the structured response mapping.

    ``session_id`` is the explicit caller identity. It is never read from the
    model arguments: any ``origin_session_key`` in ``arguments`` is discarded
    so a caller cannot override the session the tool was invoked for.
    """
    operation = _TASK_OPERATIONS.get(name)
    if operation is None:
        raise KeyError(f"unknown Forge task tool {name!r}")
    clean = dict(arguments)
    clean.pop("origin_session_key", None)
    return await operation(coordinator, clean, session_id=session_id)

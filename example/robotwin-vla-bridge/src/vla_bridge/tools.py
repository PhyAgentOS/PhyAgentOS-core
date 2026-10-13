"""Forge ToolEndpoint for the vla_bridge node: vla.set_mode / vla.get_status / vla.decide."""

from __future__ import annotations

import uuid

from forge_tool import (
    TOOL_ENDPOINT_PROTOCOL,
    ToolContext,
    ToolEndpointDescriptor,
    ToolEndpointError,
    ToolEndpointHandler,
    ToolError,
    ToolOperationDescriptor,
    ToolRequest,
    ToolResult,
)
from forge_tool.dora import DoraToolEndpointBinding

from .service import BridgeState, DecisionRejected

SET_MODE = "set_mode"
GET_STATUS = "get_status"
DECIDE = "decide"

_DECISION_ARGUMENTS = ("mode", "steps", "edit", "target", "reason", "assessment")


def _invalid(message: str, **details: object) -> ToolEndpointError:
    return ToolEndpointError(
        ToolError(code="VLA_INVALID_ARGUMENT", message=message, retryable=False, details=dict(details))
    )


class SetModeQuery:
    """vla.set_mode: flip the action mux and relay the policy lifecycle verb.

    自发透传（piper 档案 / S3 学生基线）：不记段预算、不检测 C2。检查点纪律走
    vla.decide。

    mode 词表由 state.set_mode 按**本档案的词汇**判定（legacy → policy/agent，
    decision → 四档 + 别名）——**不在这一层再挂一份白名单**：该层的白名单会与
    profile 的 ToolSpec enum 漂移（libero 的 spec 已开放 student/edit/eef/stop，
    节点若只认 policy/agent 就自相矛盾，S4a 实测踩过）。
    """

    def __init__(self, state: BridgeState, max_proprio_age_ms: int) -> None:
        self._state = state
        self._max_proprio_age_ms = max_proprio_age_ms

    async def query(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        arguments = request.arguments
        unexpected = sorted(set(arguments) - {"mode"})
        if unexpected:
            raise _invalid("set_mode accepts only ['mode']", unexpected_arguments=unexpected)
        mode = arguments.get("mode")
        if not isinstance(mode, str) or not mode:
            raise _invalid("set_mode requires a string 'mode'")
        try:
            outputs = self._state.set_mode(mode)
        except DecisionRejected as exc:
            raise ToolEndpointError(
                ToolError(code=exc.code, message=exc.message, retryable=True, details=exc.details)
            ) from exc
        except Exception as exc:  # noqa: BLE001 - transport failure of the lifecycle send
            return ToolResult(
                status="failed",
                error=ToolError(
                    code="VLA_POLICY_COMMAND_FAILED",
                    message=f"mode switched but policy command could not be sent: {exc}",
                    retryable=True,
                    details={},
                ),
            )
        return ToolResult(status="succeeded", outputs=outputs)


class DecideQuery:
    """vla.decide: the checkpoint decision (student|edit|eef|stop).

    执行前校验失败 → ToolEndpointError（可重试：agent 改好再发一次）；状态已改而
    命令发不出去 → ToolResult(status="failed")（执行期失败）。
    """

    def __init__(self, state: BridgeState, max_proprio_age_ms: int) -> None:
        self._state = state
        self._max_proprio_age_ms = max_proprio_age_ms

    async def query(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        arguments = request.arguments
        unexpected = sorted(set(arguments) - set(_DECISION_ARGUMENTS))
        if unexpected:
            raise _invalid(
                f"decide accepts only {list(_DECISION_ARGUMENTS)}",
                unexpected_arguments=unexpected,
            )
        mode = arguments.get("mode")
        if not isinstance(mode, str) or not mode:
            raise _invalid("decide requires a string 'mode'")
        reason = arguments.get("reason", "agent")
        if not isinstance(reason, str) or not reason:
            raise _invalid("reason must be a non-empty string")
        assessment = arguments.get("assessment")
        if assessment is not None and not isinstance(assessment, dict):
            raise _invalid("assessment must be an object")
        try:
            outputs = self._state.decide(
                mode=mode,
                steps=arguments.get("steps"),
                edit=arguments.get("edit"),
                target=arguments.get("target"),
                reason=reason,
                assessment=assessment,
            )
        except DecisionRejected as exc:
            self._state.bump("decisions_rejected")
            raise ToolEndpointError(
                ToolError(code=exc.code, message=exc.message, retryable=True, details=exc.details)
            ) from exc
        except Exception as exc:  # noqa: BLE001 - transport failure of the lifecycle send
            return ToolResult(
                status="failed",
                error=ToolError(
                    code="VLA_DECISION_FAILED",
                    message=f"decision applied but policy command could not be sent: {exc}",
                    retryable=True,
                    details={},
                ),
            )
        return ToolResult(status="succeeded", outputs=outputs)


class GetStatusQuery:
    """vla.get_status: mux mode, policy phase, freshness, segment/checkpoint, counters."""

    def __init__(self, state: BridgeState, max_proprio_age_ms: int) -> None:
        self._state = state
        self._max_proprio_age_ms = max_proprio_age_ms

    async def query(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        unexpected = sorted(set(request.arguments))
        if unexpected:
            raise _invalid("get_status takes no arguments", unexpected_arguments=unexpected)
        return ToolResult(status="succeeded", outputs=self._state.status(self._max_proprio_age_ms))


def create_bridge_descriptor(endpoint_id: str) -> ToolEndpointDescriptor:
    return ToolEndpointDescriptor(
        protocol_version=TOOL_ENDPOINT_PROTOCOL,
        endpoint_id=endpoint_id,
        operations=(
            ToolOperationDescriptor(name=SET_MODE, semantics="query", max_concurrency=1),
            ToolOperationDescriptor(name=GET_STATUS, semantics="query", max_concurrency=1),
            ToolOperationDescriptor(name=DECIDE, semantics="query", max_concurrency=1),
        ),
    )


def create_bridge_binding(
    state: BridgeState,
    *,
    endpoint_id: str,
    max_proprio_age_ms: int,
    endpoint_instance_id: str | None = None,
) -> DoraToolEndpointBinding:
    handler = ToolEndpointHandler(
        create_bridge_descriptor(endpoint_id),
        endpoint_instance_id=str(uuid.uuid4()) if endpoint_instance_id is None else endpoint_instance_id,
        operations={
            SET_MODE: SetModeQuery(state, max_proprio_age_ms),
            GET_STATUS: GetStatusQuery(state, max_proprio_age_ms),
            DECIDE: DecideQuery(state, max_proprio_age_ms),
        },
    )
    return DoraToolEndpointBinding(handler)


__all__ = [
    "DECIDE",
    "GET_STATUS",
    "SET_MODE",
    "create_bridge_binding",
    "create_bridge_descriptor",
]

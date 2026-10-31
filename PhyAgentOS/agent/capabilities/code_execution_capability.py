"""Core capability for running restricted tool-composition programs.

The capability exposes one tool (``execute_program``) whose program may call
only read-only tools that the author marked ``parallel_safe``. It never calls
a tool handler directly: every subcall goes through the executor bound for the
current turn, so policy checks and ``tool.intent`` / ``tool.dispatched`` /
``tool.settled`` events stay on the normal path.

A canonical capability is shared across sessions. The executor belongs to one
turn, so callers must build a per-turn clone with :meth:`for_turn` before
collecting registrations and then bind that clone's executor. Binding an
already-bound clone with a different executor raises instead of silently
overwriting another session's execution path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from PhyAgentOS.agent.core.types import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.tooling.code_execution import (
    CodeExecutionLimits,
    ProgramCancelledError,
    ProgramError,
    ProgramResult,
    ProgramToolResult,
    run_program,
)

__all__ = ["CodeExecutionCapability"]

_TOOL_PARAMETERS: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "A restricted Python-subset program. Use "
                "'await tools.call(name, arguments)', "
                "'await parallel([...])', print(...), and return a finite value."
            ),
            "minLength": 1,
        }
    },
    "required": ["code"],
    "additionalProperties": False,
}

_PROMPT = (
    "execute_program runs a restricted Python subset, not general Python. "
    "It supports assignments, containers, indexing, if, a bounded for loop, "
    "await tools.call(name, arguments), await parallel([...]) for read-only "
    "tool calls, print(...), and return. It has no imports, attribute access "
    "(except tools.call), file/process/network access, eval, or exec. Only "
    "tools marked read-only/parallel-safe are exposed, and each subcall still "
    "obeys the current tool policy."
)

_EXCLUDED_TOOL_PREFIXES = ("forge_",)
_EXCLUDED_TOOL_NAMES = frozenset(
    {"tool_search", "search_tools", "discover_tools", "execute_program"}
)

# These are the executor's durable-write failures. The loop driver stops the
# turn on these codes, so a program must not swallow them as an ordinary
# failed subcall and continue with more tool calls.
_PERSISTENCE_FAILURE_CODES = frozenset(
    {
        "tool_intent_write_failed",
        "tool_dispatched_write_failed",
        "tool_settled_write_failed",
    }
)


class CodeExecutionCapability:
    """Build per-turn restricted program capability over the core executor."""

    def __init__(
        self,
        *,
        tool_name: str = "execute_program",
        limits: CodeExecutionLimits | None = None,
        excluded_prefixes: Sequence[str] = _EXCLUDED_TOOL_PREFIXES,
        excluded_tools: Sequence[str] = _EXCLUDED_TOOL_NAMES,
    ) -> None:
        self._tool_name = tool_name
        self._limits = limits if limits is not None else CodeExecutionLimits()
        self._excluded_prefixes = tuple(excluded_prefixes)
        self._excluded_tools = frozenset(excluded_tools)
        self._executor: Any | None = None
        self._exposed: dict[str, ToolRegistration] = {}
        self._definition = ToolDefinition(
            name=tool_name,
            description=(
                "Run a short restricted program that composes read-only tool "
                "calls. Not general Python and not a sandbox of its own."
            ),
            parameters=_TOOL_PARAMETERS,
        )
        self._registration = ToolRegistration(
            definition=self._definition,
            handler=self._handle,
        )

    @property
    def name(self) -> str:
        return "code_execution"

    @property
    def is_bound(self) -> bool:
        return self._executor is not None

    def for_turn(self) -> CodeExecutionCapability:
        """Return an unbound clone for one turn's executor.

        Limits and exclusions are preserved; the executor and discovered tool
        map are intentionally not copied, because both belong to a single turn.
        """
        return CodeExecutionCapability(
            tool_name=self._tool_name,
            limits=self._limits,
            excluded_prefixes=self._excluded_prefixes,
            excluded_tools=self._excluded_tools,
        )

    def bind_executor(self, executor: Any) -> None:
        """Bind this per-turn capability to that turn's tool executor."""
        if self._executor is not None:
            if self._executor is executor:
                return
            raise RuntimeError(
                "code execution capability is already bound to another executor; "
                "use for_turn() to build a per-turn capability"
            )
        if not callable(getattr(executor, "execute", None)):
            raise TypeError("executor must expose an async execute(call, context)")
        registrations = getattr(executor, "registrations", None)
        if registrations is None:
            raise TypeError("executor must expose its registrations tuple")
        exposed: dict[str, ToolRegistration] = {}
        for registration in registrations:
            if not isinstance(registration, ToolRegistration):
                raise TypeError("executor.registrations must contain ToolRegistration")
            name = registration.definition.name
            if not self._is_exposed(registration):
                continue
            if name in exposed:
                raise ValueError(f"duplicate exposed tool registration for {name!r}")
            exposed[name] = registration
        self._exposed = exposed
        self._executor = executor

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return (self._registration,)

    def prompt_context(self, context: ExecutionContext) -> str | None:
        del context
        return _PROMPT

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        del session_id, context

    async def aclose(self) -> None:
        self._executor = None
        self._exposed = {}

    async def _handle(
        self, call: ToolCall, context: ExecutionContext
    ) -> ToolOutcome:
        if self._executor is None:
            return self._failed(
                call,
                "code_execution_not_bound",
                "code execution has no executor bound for this turn",
            )
        executor = self._executor
        if context.cancellation.cancelled:
            return self._cancelled(call)
        arguments = call.arguments_dict()
        source = arguments.get("code")
        if not isinstance(source, str) or not source.strip():
            return self._failed(
                call,
                "code_execution_invalid_program",
                "code must be a non-empty string",
            )

        async def dispatch(
            name: str, subcall_arguments: Mapping[str, Any], subcall_id: str
        ) -> ProgramToolResult:
            registration = self._exposed.get(name)
            if registration is None:
                return ProgramToolResult(
                    status=ToolStatus.REJECTED.value,
                    text="this tool is not exposed to the restricted program",
                    error_code="code_tool_not_exposed",
                    error_message=(
                        "this tool is not exposed to the restricted program"
                    ),
                )
            subcall = ToolCall(
                tool_call_id=subcall_id,
                name=name,
                arguments=subcall_arguments,
            )
            outcome = await executor.execute(subcall, context)
            error_code = outcome.error.code if outcome.error is not None else None
            if error_code in _PERSISTENCE_FAILURE_CODES:
                raise ProgramError(
                    error_code,
                    f"tool subcall {subcall_id!r} could not be recorded durably; "
                    "the program must stop",
                )
            return ProgramToolResult(
                status=outcome.status.value,
                text=outcome.text,
                content=outcome.content,
                error_code=error_code,
                error_message=(
                    outcome.error.message if outcome.error is not None else None
                ),
            )

        try:
            result = await run_program(
                source,
                is_cancelled=lambda: context.cancellation.cancelled,
                limits=self._limits,
                available_tools=frozenset(self._exposed),
                dispatch=dispatch,
                outer_call_id=call.tool_call_id,
            )
        except ProgramCancelledError:
            return self._cancelled(call)
        except ProgramError as exc:
            return self._failed(
                call,
                exc.code,
                f"restricted program stopped: {exc.message}",
            )
        except Exception as exc:  # noqa: BLE001 - bounded failure, no traceback
            return self._failed(
                call,
                "code_execution_error",
                f"restricted program failed with {type(exc).__name__}",
            )
        if context.cancellation.cancelled:
            return self._cancelled(call)
        return self._outcome(call, result)

    def _outcome(self, call: ToolCall, result: ProgramResult) -> ToolOutcome:
        lines: list[str] = []
        if result.printed:
            lines.append(result.printed)
        if result.has_return:
            lines.append(
                "result: "
                + json.dumps(result.returned, ensure_ascii=False, sort_keys=True)
            )
        if not lines:
            lines.append("(program completed with no output)")
        lines.append(self._summary(result))
        text = "\n".join(lines)
        content: dict[str, Any] = {
            "subcalls": [
                {
                    "tool_call_id": subcall.tool_call_id,
                    "tool": subcall.tool_name,
                    "status": subcall.status,
                    "error": subcall.error_code,
                }
                for subcall in result.subcalls
            ],
            "steps": result.steps,
            "unknown": result.had_unknown,
        }
        if result.has_return:
            content["returned"] = result.returned
        if result.had_unknown:
            message = (
                "program output depends on a tool call whose remote outcome is "
                "unknown; do not treat it as success or retry it automatically"
            )
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.FAILED,
                text=f"{message}. {text}",
                content=content,
                error=ToolError("program_dependency_unknown", message),
            )
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=text,
            content=content,
        )

    def _summary(self, result: ProgramResult) -> str:
        if not result.subcalls:
            return "tool calls: 0"
        counts: dict[str, int] = {}
        for subcall in result.subcalls:
            status = subcall.status
            counts[status] = counts.get(status, 0) + 1
        parts = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        return f"tool calls: {len(result.subcalls)} ({parts})"

    def _is_exposed(self, registration: ToolRegistration) -> bool:
        name = registration.definition.name
        if name == self._tool_name or name in self._excluded_tools:
            return False
        if any(name.startswith(prefix) for prefix in self._excluded_prefixes):
            return False
        # ``parallel_safe`` is the author's declaration that the tool is
        # read-only and safe to run concurrently. Anything else stays out of
        # the program environment even if the model names it explicitly.
        return registration.parallel_safe

    def _failed(self, call: ToolCall, code: str, message: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.FAILED,
            text=message,
            error=ToolError(code, message),
        )

    def _cancelled(self, call: ToolCall) -> ToolOutcome:
        message = "restricted program cancelled locally; no further subcall was started"
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.CANCELLED,
            text=message,
            error=ToolError("code_execution_cancelled", message),
        )

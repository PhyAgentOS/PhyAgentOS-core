"""Concrete tool executor for the new agent core.

The executor is the only writer of ``tool.intent``, ``tool.dispatched``, and
``tool.settled``. Every registered tool is treated as potentially
side-effecting:

* policy and argument checks happen before any event is written;
* ``tool.intent`` and ``tool.dispatched`` are appended before the handler is
  called, and a failed append leaves the call unexecuted;
* a local cancellation after dispatch is reported as ``unknown``; the handler
  keeps running in an executor-owned task and its late result is ignored;
* an external task cancellation is settled as ``unknown`` and then re-raised;
* no call is retried automatically.

``raw_arguments`` is checked only when the provider supplied it. A missing raw
payload is allowed (the adapter already parsed ``arguments``), but an empty,
truncated, or non-object payload is rejected before execution. When raw is
present it is authoritative: the handler receives a ``ToolCall`` snapshot whose
arguments are the validated raw payload, not the adapter's parsed mapping.

A settlement that cannot be persisted is returned as ``unknown`` with error
code ``tool_settled_write_failed``; the local status stays in ``content``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

import jsonschema

from .events import EventType, RuntimeEvent
from .protocols import SessionStore
from .types import (
    DomainRefs,
    ExecutionContext,
    OperationCancelledError,
    ToolCall,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)

__all__ = ["ToolExecutorImpl"]

_SETTLED_TEXT_LIMIT = 2000
_ERROR_TEXT_LIMIT = 1000
_TRUNCATION_MARKER = "...[truncated]"


async def _call_handler(
    registration: ToolRegistration,
    call: ToolCall,
    context: ExecutionContext,
) -> ToolOutcome:
    """Call the handler inside a task so cancellation can be observed."""
    return await registration.handler(call, context)


def _describe_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


def _truncate(text: str, limit: int = _SETTLED_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


def _error_payload(error: ToolError | None) -> dict[str, str] | None:
    if error is None:
        return None
    return {"code": error.code, "message": error.message}


def _domain_refs_payload(refs: DomainRefs) -> dict[str, str | None]:
    return {
        "task_id": refs.task_id,
        "revision_id": refs.revision_id,
        "invocation_id": refs.invocation_id,
        "attempt_id": refs.attempt_id,
    }


class ToolExecutorImpl:
    """Validate, authorize, dispatch, and settle one tool call.

    ``registrations`` is snapshotted at construction; a duplicate tool name is
    a wiring error and raises :class:`ValueError`. The injected store is used
    only through its async ``append`` method.

    Rejections and pre-dispatch cancellations are settled with their terminal
    status even though no ``tool.intent`` was written: the call never ran, but
    the model's request is still recorded. A failed ``tool.intent`` append is
    returned as ``failed`` without a settlement event, because nothing durable
    was opened to close.
    """

    def __init__(
        self,
        registrations: Iterable[ToolRegistration],
        store: SessionStore,
    ) -> None:
        self._store = store
        self._tools: dict[str, ToolRegistration] = {}
        self._handler_tasks: set[asyncio.Task[Any]] = set()
        for registration in registrations:
            if not isinstance(registration, ToolRegistration):
                raise TypeError("registrations must contain ToolRegistration instances")
            name = registration.definition.name
            if name in self._tools:
                raise ValueError(f"duplicate tool registration for {name!r}")
            self._tools[name] = registration

    @property
    def pending_handler_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Handler tasks left running after a local cancellation.

        The executor keeps these tasks in a referenced set, so they are never
        silently dropped; each task is removed only when it finishes, its late
        exception is consumed, and its late result is never written back as a
        settlement.
        """
        return tuple(self._handler_tasks)

    async def execute(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")

        registration = self._tools.get(call.name)
        if registration is None:
            rejection = self._rejected(
                call, "unknown_tool", f"unknown tool {call.name!r}"
            )
            return await self._settle(context, rejection)

        if not context.policy.allows(call.name):
            rejection = self._rejected(
                call, "tool_denied", f"policy denies tool {call.name!r}"
            )
            return await self._settle(context, rejection)

        arguments, rejection = self._prepare_arguments(call, registration)
        if rejection is not None:
            return await self._settle(context, rejection)

        # The handler gets a snapshot whose arguments are the ones that passed
        # validation (raw JSON is authoritative when present), not the possibly
        # stale mapping the adapter parsed earlier.
        handler_call = replace(call, arguments=arguments)

        if context.cancellation.cancelled:
            return await self._settle(context, self._cancelled(handler_call))

        try:
            await self._append(
                EventType.TOOL_INTENT, handler_call, context, arguments=arguments
            )
        except Exception as exc:
            return self._write_failed(
                handler_call, "tool.intent", "tool_intent_write_failed", exc
            )

        # The token may have fired while the intent write was in flight; do
        # not dispatch in that case, and never fake a successful dispatch.
        if context.cancellation.cancelled:
            return await self._settle(context, self._cancelled(handler_call))

        try:
            await self._append(EventType.TOOL_DISPATCHED, handler_call, context)
        except Exception as exc:
            outcome = self._write_failed(
                handler_call, "tool.dispatched", "tool_dispatched_write_failed", exc
            )
            return await self._settle(context, outcome)

        outcome = await self._invoke(registration, handler_call, context)
        if not outcome.is_terminal:
            return outcome
        return await self._settle(context, outcome)

    async def _append(
        self,
        event_type: EventType,
        call: ToolCall,
        context: ExecutionContext,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "tool_call_id": call.tool_call_id,
            "tool_name": call.name,
        }
        if arguments is not None:
            # Audit snapshot only; nothing here is written to a text log.
            payload["arguments"] = copy.deepcopy(dict(arguments))
        await self._store.append(
            RuntimeEvent(
                type=event_type,
                session_id=context.session_id,
                turn_id=context.turn_id,
                step_id=context.step_id,
                payload=payload,
            )
        )

    async def _settle(
        self, context: ExecutionContext, outcome: ToolOutcome
    ) -> ToolOutcome:
        if not outcome.is_terminal:
            return outcome
        try:
            await self._store.append(
                RuntimeEvent(
                    type=EventType.TOOL_SETTLED,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    step_id=context.step_id,
                    payload=self._settled_payload(outcome),
                )
            )
        except Exception as exc:
            return self._settlement_unknown(outcome, exc)
        return outcome

    async def _invoke(
        self,
        registration: ToolRegistration,
        call: ToolCall,
        context: ExecutionContext,
    ) -> ToolOutcome:
        try:
            handler_task = asyncio.create_task(
                _call_handler(registration, call, context),
                name=f"tool-handler:{call.tool_call_id}",
            )
        except Exception as exc:
            return self._handler_failed(call, exc)
        self._track(handler_task)

        # The handler task is deliberately never cancelled: a local stop cannot
        # prove that remote work stopped. The waiter only ends our wait.
        cancellation = context.cancellation
        waiter = asyncio.create_task(
            cancellation.wait(), name=f"tool-cancel:{call.tool_call_id}"
        )
        try:
            done, _ = await asyncio.wait(
                {handler_task, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # External task cancellation must reach the caller so the driver
            # stops the turn instead of starting another model call. Settle
            # what we know first (UNKNOWN, never success), then re-raise.
            unknown = self._unknown(
                call,
                "the executor was cancelled locally while waiting; "
                "the handler result is ignored",
            )
            try:
                await self._settle(context, unknown)
            except (asyncio.CancelledError, Exception):
                pass
            raise
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

        if handler_task not in done:
            return self._unknown(
                call,
                "cancelled locally after dispatch; the handler is still running "
                "in the background and its result is ignored",
            )

        try:
            result = handler_task.result()
        except (asyncio.CancelledError, OperationCancelledError):
            return self._unknown(
                call, "the handler reported local cancellation after dispatch"
            )
        except Exception as exc:
            return self._handler_failed(call, exc)
        return self._checked_outcome(call, result)

    def _prepare_arguments(
        self, call: ToolCall, registration: ToolRegistration
    ) -> tuple[dict[str, Any] | None, ToolOutcome | None]:
        raw = call.raw_arguments
        if raw is not None:
            if not raw.strip():
                return None, self._rejected(
                    call, "invalid_arguments", "raw arguments are empty"
                )
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError) as exc:
                return None, self._rejected(
                    call,
                    "invalid_arguments",
                    _truncate(
                        f"raw arguments are not complete JSON: {exc}",
                        _ERROR_TEXT_LIMIT,
                    ),
                )
            if not isinstance(parsed, dict):
                return None, self._rejected(
                    call, "invalid_arguments", "raw arguments must be a JSON object"
                )
            # The provider's raw JSON is authoritative when present: it is what
            # the model actually emitted, so validation and execution must use
            # it instead of a possibly repaired or stale parsed mapping.
            arguments = copy.deepcopy(parsed)
        else:
            arguments = call.arguments_dict()
        try:
            jsonschema.validate(
                instance=arguments,
                schema=dict(registration.definition.parameters),
            )
        except jsonschema.ValidationError as exc:
            location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
            message = _truncate(
                f"arguments do not match schema at {location}: {exc.message}",
                _ERROR_TEXT_LIMIT,
            )
            return None, self._rejected(call, "invalid_arguments", message)
        except jsonschema.SchemaError as exc:
            message = _truncate(
                f"tool schema is invalid: {exc.message}", _ERROR_TEXT_LIMIT
            )
            return None, self._rejected(call, "invalid_tool_schema", message)
        except Exception as exc:
            # A bad schema reference or an unknown schema type is a tool
            # definition problem; reject instead of crashing the step.
            message = _truncate(
                f"tool schema could not validate the arguments: "
                f"{_describe_exception(exc)}",
                _ERROR_TEXT_LIMIT,
            )
            return None, self._rejected(call, "invalid_tool_schema", message)
        return arguments, None

    def _checked_outcome(self, call: ToolCall, result: Any) -> ToolOutcome:
        if not isinstance(result, ToolOutcome):
            return self._failed(
                call,
                "invalid_tool_outcome",
                f"handler returned {type(result).__name__}, expected ToolOutcome",
            )
        if result.tool_call_id != call.tool_call_id or result.tool_name != call.name:
            return self._failed(
                call,
                "invalid_tool_outcome",
                "handler returned a ToolOutcome for a different tool call",
            )
        return result

    def _settled_payload(self, outcome: ToolOutcome) -> dict[str, Any]:
        return {
            "tool_call_id": outcome.tool_call_id,
            "tool_name": outcome.tool_name,
            "status": outcome.status.value,
            "text": _truncate(outcome.text),
            "error": _error_payload(outcome.error),
            "domain_refs": _domain_refs_payload(outcome.domain_refs),
        }

    def _rejected(self, call: ToolCall, code: str, message: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.REJECTED,
            text=message,
            error=ToolError(code, message),
        )

    def _failed(self, call: ToolCall, code: str, message: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.FAILED,
            text=message,
            error=ToolError(code, message),
        )

    def _cancelled(self, call: ToolCall) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.CANCELLED,
            text="cancelled locally before dispatch; the tool handler was not called",
        )

    def _unknown(self, call: ToolCall, reason: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.UNKNOWN,
            text=(f"{reason}; remote side effects are unknown. Do not retry automatically."),
        )

    def _write_failed(
        self, call: ToolCall, event_name: str, code: str, exc: BaseException
    ) -> ToolOutcome:
        message = (
            f"{event_name} could not be persisted ({_describe_exception(exc)}); "
            "the tool handler was not called. Do not retry automatically."
        )
        return self._failed(call, code, message)

    def _handler_failed(self, call: ToolCall, exc: BaseException) -> ToolOutcome:
        return self._failed(
            call,
            "tool_handler_error",
            f"tool handler raised {_describe_exception(exc)}",
        )

    def _settlement_unknown(
        self, outcome: ToolOutcome, exc: BaseException
    ) -> ToolOutcome:
        message = (
            f"tool.settled could not be persisted ({_describe_exception(exc)}); "
            f"the local status was {outcome.status.value!r} but the durable "
            "record is unknown. Do not retry automatically."
        )
        return ToolOutcome(
            tool_call_id=outcome.tool_call_id,
            tool_name=outcome.tool_name,
            status=ToolStatus.UNKNOWN,
            text=message,
            content={
                "settled": False,
                "local_status": outcome.status.value,
                "local_error": _error_payload(outcome.error),
            },
            error=ToolError("tool_settled_write_failed", message),
            domain_refs=outcome.domain_refs,
        )

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_finished)

    def _handler_finished(self, task: asyncio.Task[Any]) -> None:
        self._handler_tasks.discard(task)
        if not task.cancelled():
            task.exception()

"""Expose the existing cron service to the agent core as a native capability.

The tool keeps the legacy external contract: name ``cron`` with the same
``add``/``list``/``remove`` actions and the same argument names. The legacy
``CronTool`` still wraps :class:`PhyAgentOS.cron.service.CronService`; this
capability is the native Core adapter over that same service and store, so
there is exactly one scheduler and one job database.

Two entry shapes are supported without a second implementation:

* gateway/background (``routes`` supplied): the delivery target is the turn's
  frozen :class:`TurnBinding`, and a turn started by a cron job is marked
  ``scheduled`` so creating another job from it is refused. This is the legacy
  re-entrancy guard, owned by the capability instead of the model.
* CLI/manual (``routes=None``): a normal interactive turn may still create a
  job. There is no channel input on this path, so the target is the explicit
  ``default_target`` (``cli:direct``) recorded in the same job store the
  gateway reads. The capability never starts the scheduler; an unstarted
  :class:`CronService` only persists jobs, so a CLI process adds no timer.

The capability also never starts the service: who runs the scheduler is the
gateway process's decision.
"""

from __future__ import annotations

from typing import Any

from PhyAgentOS.agent.services.background_runtime import TurnRoutes
from PhyAgentOS.cron.service import CronService
from PhyAgentOS.cron.types import CronSchedule

from ..core.types import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)

CRON_TOOL = ToolDefinition(
    name="cron",
    description="Schedule reminders and recurring tasks. Actions: add, list, remove.",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "remove"],
                "description": "Action to perform",
            },
            "message": {"type": "string", "description": "Reminder message (for add)"},
            "every_seconds": {
                "type": "integer",
                "description": "Interval in seconds (for recurring tasks)",
            },
            "cron_expr": {
                "type": "string",
                "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
            },
            "tz": {
                "type": "string",
                "description": "IANA timezone for cron expressions (e.g. 'America/Vancouver')",
            },
            "at": {
                "type": "string",
                "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')",
            },
            "job_id": {"type": "string", "description": "Job ID (for remove)"},
        },
        "required": ["action"],
    },
)

REENTRANCY_ERROR = "Error: cannot schedule new jobs from within a cron job execution"


class CronCapability:
    """Native Core tools over the shared :class:`CronService`."""

    def __init__(
        self,
        service: CronService,
        routes: TurnRoutes | None = None,
        *,
        default_target: tuple[str, str] = ("cli", "direct"),
    ) -> None:
        self._service = service
        self._routes = routes
        self._default_target = default_target

    @property
    def name(self) -> str:
        return "cron"

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return (ToolRegistration(definition=CRON_TOOL, handler=self._handle),)

    def prompt_context(self, context: ExecutionContext) -> str | None:
        return None

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        # Jobs belong to the user's schedule, not to a session's turn. A local
        # cancel must not delete jobs the user asked to keep.
        del session_id, context

    async def aclose(self) -> None:
        # The CronService is owned and stopped by the process entry point.
        return None

    def _target(self, session_id: str) -> tuple[str, str] | None:
        """Return this turn's delivery target, or ``None`` when it has none.

        With turn routes, only the frozen binding of the running turn counts.
        Without them (CLI), the explicit default target is used.
        """
        if self._routes is None:
            return self._default_target
        binding = self._routes.get(session_id)
        if binding is None:
            return None
        return binding.channel, binding.chat_id

    def _is_scheduled_turn(self, session_id: str) -> bool:
        if self._routes is None:
            return False
        binding = self._routes.get(session_id)
        return bool(binding is not None and binding.scheduled)

    async def _handle(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        arguments = call.arguments_dict()
        action = arguments["action"]
        if action == "add":
            if self._is_scheduled_turn(context.session_id):
                return self._failed(call, "cron_reentrancy", REENTRANCY_ERROR)
            return self._add(call, arguments, self._target(context.session_id))
        if action == "list":
            return self._succeeded(call, self._list_jobs())
        if action == "remove":
            return self._remove(call, arguments)
        return self._failed(
            call, "cron_action", f"Unknown action: {action}"
        )

    def _add(
        self,
        call: ToolCall,
        arguments: dict[str, Any],
        target: tuple[str, str] | None,
    ) -> ToolOutcome:
        message = arguments.get("message") or ""
        if not message:
            return self._failed(call, "cron_job", "Error: message is required for add")
        channel, chat_id = target or ("", "")
        if not channel or not chat_id:
            return self._failed(
                call, "cron_job", "Error: no session context (channel/chat_id)"
            )
        every_seconds = arguments.get("every_seconds")
        cron_expr = arguments.get("cron_expr")
        tz = arguments.get("tz")
        at = arguments.get("at")
        if tz and not cron_expr:
            return self._failed(
                call, "cron_job", "Error: tz can only be used with cron_expr"
            )

        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=int(every_seconds) * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        elif at:
            from datetime import datetime

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return self._failed(
                    call,
                    "cron_job",
                    f"Error: invalid ISO datetime format '{at}'. "
                    "Expected format: YYYY-MM-DDTHH:MM:SS",
                )
            schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
            delete_after = True
        else:
            return self._failed(
                call,
                "cron_job",
                "Error: either every_seconds, cron_expr, or at is required",
            )

        try:
            job = self._service.add_job(
                name=message[:30],
                schedule=schedule,
                message=message,
                deliver=True,
                channel=channel,
                to=chat_id,
                delete_after_run=delete_after,
            )
        except ValueError as exc:
            return self._failed(call, "cron_job", f"Error: {exc}")
        return self._succeeded(call, f"Created job '{job.name}' (id: {job.id})")

    def _list_jobs(self) -> str:
        jobs = self._service.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove(self, call: ToolCall, arguments: dict[str, Any]) -> ToolOutcome:
        job_id = arguments.get("job_id")
        if not job_id:
            return self._failed(
                call, "cron_job", "Error: job_id is required for remove"
            )
        if self._service.remove_job(job_id):
            return self._succeeded(call, f"Removed job {job_id}")
        return self._failed(call, "cron_job", f"Job {job_id} not found")

    @staticmethod
    def _succeeded(call: ToolCall, text: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=text,
        )

    @staticmethod
    def _failed(call: ToolCall, code: str, text: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.FAILED,
            text=text,
            error=ToolError(code, text.removeprefix("Error: ")),
        )

"""Expose the general-purpose tools through native Core registrations.

The execution logic lives in :mod:`PhyAgentOS.tooling`, shared with the legacy
loop. This adapter only adds what the Core protocol requires: tool
definitions, per-session cancellation of owned shell/network work, and a
mapping from each tool's string result to a :class:`ToolOutcome`.

Result mapping keeps the legacy contract while making failure explicit: a
file/web result that starts with ``Error:`` (or carries a JSON ``error`` field
from ``web_fetch``) is a failed outcome, never a successful one. ``exec`` uses
its own exact mapping (tool-emitted error prefixes and a trailing nonzero
``Exit code:`` line), so command output is never reinterpreted. Oversized results
pass through :class:`ToolResultLimiter`, which saves the full text under
``.paos/tool_outputs`` and leaves the path in the truncated model text. The
core executor remains the only writer of tool lifecycle events.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
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
from PhyAgentOS.agent.services.job_manager import (
    JOB_CANCEL_TOOL,
    JOB_STATUS_TOOL,
    JOB_WAIT_TOOL,
    JobManager,
    JobNotify,
)
from PhyAgentOS.config.schema import Config
from PhyAgentOS.tooling.base import Tool
from PhyAgentOS.tooling.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from PhyAgentOS.tooling.scene_graph import SceneGraphQueryTool
from PhyAgentOS.tooling.shell import ExecTool, exec_failure_message
from PhyAgentOS.tooling.tool_results import ToolResultLimiter
from PhyAgentOS.tooling.web import WebFetchTool, WebSearchTool

__all__ = ["GeneralToolsCapability"]

_ERROR_PREFIX = "Error:"
_PROXY_PREFIX = "Proxy error:"


class GeneralToolsCapability:
    """Native Core tools over the shared general tool implementations."""

    def __init__(
        self,
        config: Config,
        *,
        job_notify: JobNotify | None = None,
        job_notify_factory: Callable[[ExecutionContext], JobNotify | None] | None = None,
    ) -> None:
        if job_notify is not None and not callable(job_notify):
            raise TypeError("job_notify must be callable or None")
        if job_notify_factory is not None and not callable(job_notify_factory):
            raise TypeError("job_notify_factory must be callable or None")
        self._job_notify_factory = job_notify_factory
        self._workspace = Path(config.workspace_path)
        restrict = config.tools.restrict_to_workspace
        self._restrict_to_workspace = restrict
        allowed_dir = self._workspace if restrict else None

        exec_tool = ExecTool(
            working_dir=str(self._workspace),
            timeout=config.tools.exec.timeout,
            restrict_to_workspace=restrict,
            path_append=config.tools.exec.path_append,
        )
        self._exec_tool = exec_tool
        self._tools: tuple[Tool, ...] = (
            ReadFileTool(workspace=self._workspace, allowed_dir=allowed_dir),
            WriteFileTool(workspace=self._workspace, allowed_dir=allowed_dir),
            EditFileTool(workspace=self._workspace, allowed_dir=allowed_dir),
            ListDirTool(workspace=self._workspace, allowed_dir=allowed_dir),
            exec_tool,
            WebSearchTool(
                api_key=config.tools.web.search.api_key or None,
                max_results=config.tools.web.search.max_results,
                proxy=config.tools.web.proxy,
            ),
            WebFetchTool(proxy=config.tools.web.proxy),
            SceneGraphQueryTool(workspace=self._workspace),
        )
        self._session_tools: tuple[Any, ...] = tuple(
            tool
            for tool in self._tools
            if callable(getattr(tool, "cancel_session", None))
            and callable(getattr(tool, "aclose", None))
        )
        self._limiter = ToolResultLimiter(self._workspace)
        self._jobs = JobManager(
            exec_tool, self._workspace, notify=job_notify
        )

    _JOB_DEFINITIONS = (
        ("job_status", JOB_STATUS_TOOL,
         "Check a background shell job started with exec background=true. "
         "Omit job_id to list this session's jobs. Never claims a running job "
         "is complete."),
        ("job_wait", JOB_WAIT_TOOL,
         "Wait up to timeout seconds for a background job and return its "
         "real state. Expiring while still running is not a job timeout."),
        ("job_cancel", JOB_CANCEL_TOOL,
         "Stop a background job this session owns and wait for its process "
         "group to be reaped."),
    )

    @property
    def name(self) -> str:
        return "general_tools"

    def tool_registrations(self) -> Sequence[ToolRegistration]:
        registrations: list[ToolRegistration] = []
        for tool in self._tools:
            definition = ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            registrations.append(
                ToolRegistration(
                    definition=definition,
                    handler=self._handler(tool),
                    parallel_safe=tool.parallel_safe,
                )
            )
        for name, parameters, description in self._JOB_DEFINITIONS:
            registrations.append(
                ToolRegistration(
                    definition=ToolDefinition(
                        name=name,
                        description=description,
                        parameters=parameters,
                    ),
                    handler=self._job_handler(name),
                    parallel_safe=name == "job_status",
                )
            )
        return tuple(registrations)

    def prompt_context(self, context: ExecutionContext) -> str | None:
        # General tool usage needs no per-turn text; the workspace layout is
        # already part of the system prompt.
        del context
        return None

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        """Stop shell and network work this capability started for a session.

        This is local cancellation only. It does not claim anything about
        physical execution or any other remote side effect.
        """
        del context
        try:
            await self._jobs.cancel_session(session_id)
        except Exception:
            # A job failure must not skip the other session-scoped tools.
            pass
        for tool in self._session_tools:
            try:
                await tool.cancel_session(session_id)
            except Exception:
                # One domain failing must not leave the others running.
                continue

    async def aclose(self) -> None:
        try:
            await self._jobs.aclose()
        except Exception:
            pass
        for tool in self._session_tools:
            try:
                await tool.aclose()
            except Exception:
                continue

    def _handler(self, tool: Tool) -> Any:
        async def handle(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            arguments = call.arguments_dict()
            # ``session_id`` belongs to the runtime, never to model arguments.
            arguments.pop("session_id", None)
            if call.name == "exec":
                background = arguments.pop("background", False) is True
                if self._restrict_to_workspace:
                    rejection = self._restrict_exec_working_dir(arguments)
                    if rejection is not None:
                        return self._outcome(call, rejection)
                if background:
                    per_job_notify: dict[str, Any] = {}
                    if self._job_notify_factory is not None:
                        try:
                            callback = self._job_notify_factory(context)
                            if callback is not None and not callable(callback):
                                raise TypeError(
                                    "job_notify_factory returned a non-callable"
                                )
                        except Exception as exc:  # noqa: BLE001 - reject before start
                            return self._outcome(
                                call,
                                json.dumps(
                                    {
                                        "error": (
                                            "could not prepare the background job "
                                            f"callback: {type(exc).__name__}: {exc}"
                                        ),
                                        "state": "rejected",
                                    },
                                    ensure_ascii=False,
                                ),
                                classify_as="background_job",
                            )
                        # An explicit None disables this job's notification.
                        per_job_notify["notify"] = callback
                    result = await self._jobs.start(
                        command=arguments.get("command", ""),
                        working_dir=arguments.get("working_dir"),
                        timeout=arguments.get("timeout"),
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        **per_job_notify,
                    )
                    # The manager returns JSON; a start rejection must not be
                    # mistaken for command output that merely contains "error".
                    return self._outcome(call, result, classify_as="background_job")
            result = await tool.execute(session_id=context.session_id, **arguments)
            return self._outcome(call, result)

        return handle

    def _job_handler(self, name: str) -> Any:
        async def handle(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            arguments = call.arguments_dict()
            arguments.pop("session_id", None)
            result = await self._jobs.handle(
                name,
                arguments,
                session_id=context.session_id,
                turn_id=context.turn_id,
            )
            return self._outcome(call, result)

        return handle

    def _restrict_exec_working_dir(self, arguments: dict[str, Any]) -> str | None:
        """Reject an exec cwd outside the workspace before any spawn.

        The shared tool only checks the command text against the supplied cwd;
        the native capability additionally resolves the model's ``working_dir``
        against the workspace (not the process cwd) and normalizes it, so a
        relative path cannot silently escape.
        """
        raw = arguments.get("working_dir")
        if not raw:
            return None
        path = Path(str(raw)).expanduser()
        candidate = path if path.is_absolute() else self._workspace / path
        try:
            resolved = candidate.resolve()
            workspace = self._workspace.resolve()
        except OSError:
            resolved = candidate.absolute()
            workspace = self._workspace.absolute()
        if not resolved.is_relative_to(workspace):
            return (
                "Error: Command blocked by safety guard "
                "(working dir outside workspace)"
            )
        arguments["working_dir"] = str(resolved)
        return None

    def _outcome(
        self, call: ToolCall, result: str, *, classify_as: str | None = None
    ) -> ToolOutcome:
        text = result if isinstance(result, str) else str(result)
        failed, message = self._classify(classify_as or call.name, text)
        limited = self._limiter.limit(call.tool_call_id, text)
        if failed:
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.FAILED,
                text=limited,
                error=ToolError(f"{call.name}_failed", message or text),
            )
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=limited,
        )

    @staticmethod
    def _classify(tool_name: str, text: str) -> tuple[bool, str]:
        if tool_name == "exec":
            message = exec_failure_message(text)
            return (message is not None), (message or "")

        stripped = text.lstrip()
        if stripped.startswith(_ERROR_PREFIX):
            message = stripped[len(_ERROR_PREFIX):].strip()
            return True, message or stripped
        if stripped.startswith(_PROXY_PREFIX):
            return True, stripped
        if stripped.startswith("{"):
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, str) and error.strip():
                    return True, error
                if error is not None:
                    return True, json.dumps(error, ensure_ascii=False)
        return False, ""

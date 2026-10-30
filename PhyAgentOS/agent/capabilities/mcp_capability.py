"""Native Core capability over configured MCP servers.

Discovery happens once, in :meth:`MCPCapability.open`, because the Core
assembles a fixed tool registry from ``tool_registrations()``. Entry points
must therefore await ``open()`` before asking the composition to gather
registrations.

Each configured server gets an owning task that enters and exits its MCP
transport and session in the same asyncio task; see
:mod:`PhyAgentOS.tooling.mcp`. Tool calls run in per-call tasks tracked by
session, so cancellation only affects the session that asked for it.

Outcome rules:

* a timeout or cancellation after dispatch is ``UNKNOWN`` (or ``CANCELLED``
  for a local cancellation) and is never retried automatically;
* an MCP ``isError`` result is ``FAILED``;
* a call attempted while the server is unavailable is ``FAILED`` because it
  was never dispatched;
* a remote error after dispatch is ``UNKNOWN``: the effect may have happened.

When a workspace is supplied, result and error text pass through
:class:`PhyAgentOS.tooling.ToolResultLimiter`. The full body is saved under
``.paos/tool_outputs`` and the model-facing text keeps the saved path; the
outcome status and remote identity are never changed by that truncation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from PhyAgentOS.agent.core.types import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.tooling.base import SessionTaskRegistry
from PhyAgentOS.tooling.mcp import (
    MCPConnectionManager,
    MCPNotConnectedError,
    MCPRemoteCancelledError,
    MCPServerConnection,
    MCPToolTimeoutError,
    render_tool_result,
    result_is_error,
)
from PhyAgentOS.tooling.tool_results import ToolResultLimiter

__all__ = ["MCPCapability"]


class MCPCapability:
    """Discover configured MCP tools and expose them as Core registrations."""

    def __init__(
        self, servers: Mapping[str, Any], *, workspace: Path | None = None
    ) -> None:
        self._manager = MCPConnectionManager(servers)
        self._sessions = SessionTaskRegistry()
        self._limiter = (
            ToolResultLimiter(workspace) if workspace is not None else None
        )
        self._registrations: tuple[ToolRegistration, ...] = ()
        self._opened = False
        self._closed = False
        self._open_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def failures(self) -> Mapping[str, BaseException]:
        """Servers that failed to initialize, keyed by configured name."""
        return self._manager.failures

    async def open(self) -> None:
        """Connect every configured server and freeze discovered tools."""
        async with self._open_lock:
            if self._closed:
                raise RuntimeError("MCP capability is closed")
            if self._opened:
                return
            connections = await self._manager.open()
            self._registrations = self._build_registrations(connections)
            self._opened = True

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        """Return the fixed registrations built by :meth:`open`."""
        return self._registrations

    def prompt_context(self, context: ExecutionContext) -> str | None:
        return None

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        """Cancel only this session's in-flight MCP tool calls."""
        del context
        await self._sessions.cancel_session(session_id)

    async def aclose(self) -> None:
        """Cancel tracked calls and close every server exactly once."""
        async with self._open_lock:
            if self._closed:
                return
            self._closed = True
            await self._sessions.aclose()
            await self._manager.aclose()

    def _build_registrations(
        self, connections: tuple[MCPServerConnection, ...]
    ) -> tuple[ToolRegistration, ...]:
        registrations: list[ToolRegistration] = []
        seen: set[str] = set()
        for connection in connections:
            timeout = connection.tool_timeout
            for tool_def in connection.tools:
                remote_name = getattr(tool_def, "name", None)
                if not isinstance(remote_name, str) or not remote_name:
                    logger.warning(
                        "MCP server '{}': skipping a tool without a name",
                        connection.name,
                    )
                    continue
                exposed_name = f"mcp_{connection.name}_{remote_name}"
                if exposed_name in seen:
                    logger.warning(
                        "MCP tool name collision for '{}'; keeping the first server",
                        exposed_name,
                    )
                    continue
                definition = self._definition(exposed_name, tool_def)
                if definition is None:
                    logger.warning(
                        "MCP server '{}': skipping tool '{}' with an invalid schema",
                        connection.name,
                        remote_name,
                    )
                    continue
                seen.add(exposed_name)
                registrations.append(
                    ToolRegistration(
                        definition=definition,
                        handler=self._handler(connection, remote_name, timeout),
                    )
                )
        return tuple(registrations)

    @staticmethod
    def _definition(exposed_name: str, tool_def: Any) -> ToolDefinition | None:
        description = getattr(tool_def, "description", "") or getattr(
            tool_def, "name", ""
        )
        raw_schema = getattr(tool_def, "inputSchema", None)
        schema = dict(raw_schema) if isinstance(raw_schema, Mapping) else {}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        try:
            return ToolDefinition(
                name=exposed_name,
                description=str(description),
                parameters=schema,
            )
        except (TypeError, ValueError):
            return None

    def _handler(
        self,
        connection: MCPServerConnection,
        remote_name: str,
        timeout: float | None,
    ) -> Any:
        async def dispatch(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            return await self._dispatch(
                connection, remote_name, timeout, call, context
            )

        return dispatch

    async def _dispatch(
        self,
        connection: MCPServerConnection,
        remote_name: str,
        timeout: float | None,
        call: ToolCall,
        context: ExecutionContext,
    ) -> ToolOutcome:
        if context.cancellation.cancelled:
            return self._outcome(
                call,
                ToolStatus.CANCELLED,
                f"MCP tool '{call.name}' was not dispatched: turn cancelled locally",
                "mcp_cancelled",
            )
        with self._sessions.scope(context.session_id):
            try:
                result = await connection.call_tool(
                    remote_name, call.arguments_dict(), timeout
                )
            except MCPToolTimeoutError:
                message = (
                    f"MCP tool '{call.name}' timed out after {timeout}s; "
                    "the remote result is unknown. Do not retry automatically."
                )
                return self._outcome(
                    call, ToolStatus.UNKNOWN, message, "mcp_timeout"
                )
            except asyncio.CancelledError:
                message = (
                    f"MCP tool '{call.name}' was cancelled locally; "
                    "remote side effects are unconfirmed."
                )
                return self._outcome(
                    call, ToolStatus.CANCELLED, message, "mcp_cancelled"
                )
            except MCPRemoteCancelledError:
                message = (
                    f"MCP tool '{call.name}' was cancelled by the server or SDK; "
                    "the remote result is unknown. Do not retry automatically."
                )
                return self._outcome(
                    call,
                    ToolStatus.UNKNOWN,
                    message,
                    "mcp_remote_cancelled",
                )
            except MCPNotConnectedError:
                message = f"MCP tool '{call.name}' was not dispatched: server unavailable"
                return self._outcome(
                    call, ToolStatus.FAILED, message, "mcp_not_connected"
                )
            except Exception as exc:
                # The request may already have reached the server. A generic
                # failure is therefore unknown, not a clean local failure, and
                # the capability never replays it.
                message = (
                    f"MCP tool '{call.name}' failed after dispatch with "
                    f"{type(exc).__name__}; the remote result is unknown. "
                    "Do not retry automatically."
                )
                return self._outcome(
                    call, ToolStatus.UNKNOWN, message, "mcp_call_failed"
                )

        text = render_tool_result(result)
        if result_is_error(result):
            limited = self._limit(call, text)
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.FAILED,
                text=limited,
                error=ToolError("mcp_tool_error", limited),
            )
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=self._limit(call, text),
            content={"server": connection.name, "tool": remote_name},
        )

    def _limit(self, call: ToolCall, text: str) -> str:
        """Bound model-facing text; the full body is saved under the workspace."""
        if self._limiter is None:
            return text
        return self._limiter.limit(call.tool_call_id, text)

    def _outcome(
        self, call: ToolCall, status: ToolStatus, message: str, code: str
    ) -> ToolOutcome:
        limited = self._limit(call, message)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=status,
            text=limited,
            error=ToolError(code, limited),
        )

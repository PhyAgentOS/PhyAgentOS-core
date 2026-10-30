"""Shared MCP client plumbing for legacy tools and the native Core capability.

The MCP SDK opens anyio cancel scopes for every transport and session. Those
scopes must be exited by the same asyncio task that entered them, so each
configured server gets one owning task: it enters the transport, the
``ClientSession`` and initialization in that task, publishes the discovered
tools, waits for shutdown, and then unwinds the same ``AsyncExitStack``. The
capability never opens a stack in one task and closes it in another.

One connection serves every agent session. Individual tool calls run in their
own task so cancelling one session cannot cancel another session's call or
the shared server connection.

This module owns transport selection for both paths, so stdio, SSE and
streamable HTTP keep one implementation. It does not import
``PhyAgentOS.agent_legacy`` and it never logs headers, environment values,
credentials or full endpoint URLs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

import httpx
from loguru import logger

from PhyAgentOS.tooling.base import Tool

__all__ = [
    "ClientSession",
    "MCPConfigurationError",
    "MCPConnectionError",
    "MCPConnectionManager",
    "MCPNotConnectedError",
    "MCPProtocolError",
    "MCPRemoteCancelledError",
    "MCPServerConnection",
    "MCPToolTimeoutError",
    "MCPToolWrapper",
    "call_mcp_tool",
    "connect_mcp_servers",
    "discover_server_tools",
    "enter_mcp_transport",
    "render_tool_result",
    "result_is_error",
    "stdio_client",
]


def ClientSession(*args: Any, **kwargs: Any) -> Any:  # noqa: N802
    """Lazy proxy so importing this module never imports the MCP SDK clients."""
    from mcp import ClientSession

    return ClientSession(*args, **kwargs)


def StdioServerParameters(*args: Any, **kwargs: Any) -> Any:  # noqa: N802
    """Lazy proxy for ``mcp.StdioServerParameters``."""
    from mcp import StdioServerParameters

    return StdioServerParameters(*args, **kwargs)


def stdio_client(params: Any) -> Any:
    """Lazy proxy for ``mcp.client.stdio.stdio_client``."""
    from mcp.client.stdio import stdio_client

    return stdio_client(params)


def sse_client(*args: Any, **kwargs: Any) -> Any:
    """Lazy proxy for ``mcp.client.sse.sse_client``."""
    from mcp.client.sse import sse_client

    return sse_client(*args, **kwargs)


def streamable_http_client(*args: Any, **kwargs: Any) -> Any:
    """Lazy proxy for ``mcp.client.streamable_http.streamable_http_client``."""
    from mcp.client.streamable_http import streamable_http_client

    return streamable_http_client(*args, **kwargs)


class MCPError(Exception):
    """Base class for shared MCP client failures."""


class MCPConfigurationError(MCPError):
    """The server configuration cannot select a transport."""


class MCPProtocolError(MCPError):
    """A server returned a malformed or non-advancing tool list."""


class MCPConnectionError(MCPError):
    """The server connection is not usable."""


class MCPNotConnectedError(MCPConnectionError):
    """A call was attempted before the server finished connecting."""


class MCPToolTimeoutError(MCPError):
    """The local wait expired; the remote effect is unknown."""


class MCPRemoteCancelledError(MCPError):
    """The remote side cancelled or the SDK reported cancellation."""


def _transport_type(config: Any) -> str:
    configured = str(getattr(config, "type", "") or "")
    if configured:
        return configured
    if getattr(config, "command", ""):
        return "stdio"
    url = str(getattr(config, "url", "") or "")
    if url:
        return "sse" if url.rstrip("/").endswith("/sse") else "streamableHttp"
    raise MCPConfigurationError("no command or url configured")


async def enter_mcp_transport(
    stack: AsyncExitStack, config: Any
) -> tuple[Any, Any]:
    """Enter the configured transport on ``stack`` and return its streams.

    The caller must run this and the eventual ``stack.aclose()`` in the same
    asyncio task. HTTP headers stay inside the HTTP client; they are never
    rendered into a log line.
    """
    transport_type = _transport_type(config)

    if transport_type == "stdio":
        params = StdioServerParameters(
            command=getattr(config, "command", ""),
            args=list(getattr(config, "args", []) or []),
            env=dict(getattr(config, "env", {}) or {}) or None,
        )
        return await stack.enter_async_context(stdio_client(params))

    if transport_type == "sse":
        configured_headers = dict(getattr(config, "headers", {}) or {})

        def httpx_client_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            merged_headers = {**configured_headers, **(headers or {})}
            return httpx.AsyncClient(
                headers=merged_headers or None,
                follow_redirects=True,
                timeout=timeout,
                auth=auth,
            )

        return await stack.enter_async_context(
            sse_client(
                str(getattr(config, "url", "")),
                httpx_client_factory=httpx_client_factory,
            )
        )

    if transport_type == "streamableHttp":
        # An explicit client keeps the transport from inheriting httpx's short
        # default read timeout and preempting the configured tool timeout.
        http_client = await stack.enter_async_context(
            httpx.AsyncClient(
                headers=dict(getattr(config, "headers", {}) or {}) or None,
                follow_redirects=True,
                timeout=None,
            )
        )
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(
                str(getattr(config, "url", "")), http_client=http_client
            )
        )
        return read, write

    raise MCPConfigurationError(f"unknown transport type {transport_type!r}")


async def discover_server_tools(session: Any) -> list[Any]:
    """Return every tool from ``tools/list``, following pagination cursors."""
    tools: list[Any] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        if cursor is None:
            result = await session.list_tools()
        else:
            result = await session.list_tools(cursor)
        tools.extend(getattr(result, "tools", ()) or ())
        next_cursor = getattr(result, "nextCursor", None)
        if not next_cursor:
            return tools
        if next_cursor in seen_cursors:
            raise MCPProtocolError("tools/list repeated a pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def result_is_error(result: Any) -> bool:
    """Whether the server reported the tool-level failure flag."""
    return bool(getattr(result, "isError", False))


def render_tool_result(result: Any) -> str:
    """Render MCP content blocks without inventing a successful empty result."""
    parts: list[str] = []
    for block in getattr(result, "content", ()) or ():
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            parts.append(str(block))
    if not parts:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            with contextlib.suppress(TypeError, ValueError):
                parts.append(json.dumps(structured, ensure_ascii=False))
    return "\n".join(parts) or "(no output)"


async def _cancel_and_absorb(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(BaseException):
        await task


async def call_mcp_tool(
    session: Any,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    timeout: float | None,
) -> Any:
    """Call one MCP tool exactly once and raise a typed failure on trouble.

    The call is dispatched in a child task. A timeout, local cancellation or
    remote cancellation therefore ends only this call; it never cancels the
    shared session or another session's call. The caller must treat timeout
    and cancellation as ``unknown`` remote state and must not replay the call.
    """
    call = asyncio.create_task(
        session.call_tool(tool_name, arguments=dict(arguments or {})),
        name=f"mcp-call:{tool_name}",
    )
    try:
        if timeout is not None and float(timeout) > 0:
            return await asyncio.wait_for(call, timeout=float(timeout))
        return await call
    except asyncio.TimeoutError as exc:
        await _cancel_and_absorb(call)
        raise MCPToolTimeoutError(
            f"MCP tool call timed out after {timeout}s"
        ) from exc
    except asyncio.CancelledError:
        await _cancel_and_absorb(call)
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        raise MCPRemoteCancelledError("MCP tool call was cancelled remotely") from None


class MCPServerConnection:
    """One configured server plus the task that owns its MCP resources."""

    def __init__(self, name: str, config: Any) -> None:
        self.name = name
        self.config = config
        self.tools: tuple[Any, ...] = ()
        self._session: Any = None
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[tuple[Any, ...]] | None = None
        self._stopping: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._closed = False

    @property
    def tool_timeout(self) -> float | None:
        value = getattr(self.config, "tool_timeout", 30)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 30.0

    async def start(self) -> None:
        """Start the owning task and wait for initialization and discovery."""
        if self._task is not None:
            return
        if self._closed:
            raise MCPConnectionError(f"MCP server {self.name!r} is closed")
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name=f"mcp-server:{self.name}"
        )
        try:
            self.tools = await self._ready
        except BaseException:
            current = asyncio.current_task()
            if (
                current is not None
                and current.cancelling()
                and not self._task.done()
            ):
                self._task.cancel()
            await self._wait_task()
            raise

    async def _wait_task(self) -> None:
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        assert self._ready is not None
        assert self._stopping is not None
        try:
            async with AsyncExitStack() as stack:
                read, write = await enter_mcp_transport(stack, self.config)
                session = await stack.enter_async_context(ClientSession(read, write))
                self._session = session
                await session.initialize()
                discovered = await discover_server_tools(session)
                self.tools = tuple(discovered)
                if not self._ready.done():
                    self._ready.set_result(self.tools)
                await self._stopping.wait()
        except asyncio.CancelledError:
            self._error = MCPConnectionError(
                f"MCP server {self.name!r} initialization was cancelled"
            )
            if not self._ready.done():
                self._ready.set_exception(self._error)
            raise
        except BaseException as exc:
            self._error = exc
            if not self._ready.done():
                self._ready.set_exception(exc)
            else:
                # The connection broke after discovery. Log only the server
                # name and exception type: messages can contain endpoint or
                # header material.
                logger.warning(
                    "MCP server '{}' stopped after initialization: {}",
                    self.name,
                    type(exc).__name__,
                )
        finally:
            self._session = None
            if not self._ready.done():
                self._ready.set_exception(
                    MCPConnectionError(
                        f"MCP server {self.name!r} closed before initialization"
                    )
                )

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        timeout: float | None = None,
    ) -> Any:
        session = self._session
        if session is None:
            raise MCPNotConnectedError(f"MCP server {self.name!r} is not connected")
        return await call_mcp_tool(
            session,
            tool_name,
            arguments,
            self.tool_timeout if timeout is None else timeout,
        )

    async def aclose(self) -> None:
        """Stop the owning task once and let it close its own resources.

        If initialization is still blocked, setting the stop event would never
        be observed; cancel the owner task instead so its own ``AsyncExitStack``
        unwinds promptly in that same task.
        """
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            if self._ready is not None and not self._ready.done():
                task.cancel()
            elif self._stopping is not None:
                self._stopping.set()
        await self._wait_task()


class MCPConnectionManager:
    """Open all configured servers and keep their discovered tools fixed."""

    def __init__(self, servers: Mapping[str, Any]) -> None:
        self._servers = dict(servers)
        self._connections: dict[str, MCPServerConnection] = {}
        self._usable: list[MCPServerConnection] = []
        self._failures: dict[str, BaseException] = {}
        self._opened = False
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def connections(self) -> tuple[MCPServerConnection, ...]:
        return tuple(self._connections.values())

    @property
    def failures(self) -> Mapping[str, BaseException]:
        return dict(self._failures)

    async def open(self) -> tuple[MCPServerConnection, ...]:
        """Resolve every configured server; failed servers are not exposed."""
        async with self._lock:
            if self._closed:
                raise MCPConnectionError("MCP connection manager is closed")
            if self._opened:
                return tuple(self._usable)
            self._opened = True
            self._connections = {
                name: MCPServerConnection(name, config)
                for name, config in self._servers.items()
            }
            try:
                results = await asyncio.gather(
                    *(connection.start() for connection in self._connections.values()),
                    return_exceptions=True,
                )
            except BaseException:
                self._closed = True
                await asyncio.gather(
                    *(connection.aclose() for connection in self._connections.values()),
                    return_exceptions=True,
                )
                raise

            usable: list[MCPServerConnection] = []
            for name, result in zip(self._connections, results, strict=True):
                connection = self._connections[name]
                if isinstance(result, BaseException):
                    self._failures[name] = result
                    await connection.aclose()
                    logger.warning(
                        "MCP server '{}' failed to initialize: {}",
                        name,
                        type(result).__name__,
                    )
                else:
                    usable.append(connection)
                    logger.debug(
                        "MCP server '{}': connected, {} tools discovered",
                        name,
                        len(connection.tools),
                    )
            self._usable = usable
            return tuple(usable)

    async def aclose(self) -> None:
        """Close every server once; safe to call more than once."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = tuple(self._connections.values())
            if connections:
                await asyncio.gather(
                    *(connection.aclose() for connection in connections),
                    return_exceptions=True,
                )


class MCPToolWrapper(Tool):
    """Legacy ``Tool`` adapter over one MCP tool.

    The native Core path uses :class:`MCPConnectionManager` directly. This
    wrapper keeps the historical ``session`` constructor and string results
    for the legacy agent loop and its tests.
    """

    def __init__(
        self,
        session: Any,
        server_name: str,
        tool_def: Any,
        tool_timeout: int = 30,
    ) -> None:
        self._session = session
        self._original_name = tool_def.name
        self._name = f"mcp_{server_name}_{tool_def.name}"
        self._description = tool_def.description or tool_def.name
        self._parameters = tool_def.inputSchema or {
            "type": "object",
            "properties": {},
        }
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await call_mcp_tool(
                self._session,
                self._original_name,
                kwargs,
                self._tool_timeout,
            )
        except MCPToolTimeoutError:
            logger.warning(
                "MCP tool '{}' timed out after {}s",
                self._name,
                self._tool_timeout,
            )
            return f"(MCP tool call timed out after {self._tool_timeout}s)"
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            logger.warning("MCP tool '{}' was cancelled", self._name)
            return "(MCP tool call was cancelled)"
        except MCPRemoteCancelledError:
            logger.warning("MCP tool '{}' was cancelled", self._name)
            return "(MCP tool call was cancelled)"
        except Exception as exc:
            logger.warning(
                "MCP tool '{}' failed with {}",
                self._name,
                type(exc).__name__,
            )
            return f"(MCP tool call failed: {type(exc).__name__})"

        text = render_tool_result(result)
        if result_is_error(result):
            return f"Error: MCP tool '{self._name}' reported: {text}"
        return text


async def connect_mcp_servers(
    mcp_servers: dict[str, Any], registry: Any, stack: AsyncExitStack
) -> None:
    """Connect configured servers and register legacy ``Tool`` wrappers.

    The caller owns ``stack``, so this keeps the historical legacy lifecycle.
    A server that cannot connect is skipped. Headers, environment values and
    endpoint URLs are never logged.
    """
    for name, config in mcp_servers.items():
        try:
            read, write = await enter_mcp_transport(stack, config)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = await discover_server_tools(session)
            for tool_def in tools:
                wrapper = MCPToolWrapper(
                    session,
                    name,
                    tool_def,
                    tool_timeout=getattr(config, "tool_timeout", 30),
                )
                registry.register(wrapper)
                logger.debug(
                    "MCP: registered tool '{}' from server '{}'",
                    wrapper.name,
                    name,
                )
            logger.info(
                "MCP server '{}': connected, {} tools registered",
                name,
                len(tools),
            )
        except Exception as exc:
            logger.error(
                "MCP server '{}': failed to connect: {}",
                name,
                type(exc).__name__,
            )

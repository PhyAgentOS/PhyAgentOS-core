"""Compatibility shim for the legacy MCP tool import path.

The implementation lives in :mod:`PhyAgentOS.tooling.mcp`. This module keeps
``MCPToolWrapper`` and ``connect_mcp_servers`` importable from the historical
``PhyAgentOS.agent_legacy.tools.mcp`` path and re-exports the same objects, so
the legacy loop and its tests exercise the shared implementation.
"""

from __future__ import annotations

from PhyAgentOS.tooling.mcp import (
    ClientSession,
    MCPToolWrapper,
    StdioServerParameters,
    connect_mcp_servers,
    sse_client,
    stdio_client,
    streamable_http_client,
)

__all__ = [
    "ClientSession",
    "MCPToolWrapper",
    "StdioServerParameters",
    "connect_mcp_servers",
    "sse_client",
    "stdio_client",
    "streamable_http_client",
]

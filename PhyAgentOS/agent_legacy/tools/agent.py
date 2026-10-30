"""Compatibility shim for the moved agent-mode tool.

The implementation lives in :mod:`PhyAgentOS.tooling.agent_mode`; this module
keeps the historical import path and class name used by the legacy loop.
"""

from PhyAgentOS.tooling.agent_mode import AgentModeTool

__all__ = ["AgentModeTool"]

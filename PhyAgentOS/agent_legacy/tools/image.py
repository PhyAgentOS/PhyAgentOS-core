"""Compatibility shim for the moved image tool.

The implementation lives in :mod:`PhyAgentOS.tooling.image`; this module keeps
the historical import path, class name, constructor, and ``ContextVar`` routing
methods used by the legacy loop and existing tests.
"""

from PhyAgentOS.tooling.image import ImageCallContext, ImageTool

__all__ = ["ImageCallContext", "ImageTool"]

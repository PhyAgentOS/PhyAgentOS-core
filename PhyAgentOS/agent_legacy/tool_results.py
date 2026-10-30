"""Compatibility alias for the moved tool result limiter.

The implementation lives in :mod:`PhyAgentOS.tooling.tool_results`. Replacing
this module entry with the implementation keeps every historical import,
constant, and monkeypatch path pointing at the same object.
"""

import sys

from PhyAgentOS.tooling import tool_results as _impl

sys.modules[__name__] = _impl

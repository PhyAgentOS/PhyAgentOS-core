"""Compatibility alias for the moved shell tool.

The implementation lives in :mod:`PhyAgentOS.tooling.shell`. Replacing this
module entry with the implementation keeps every historical import and
monkeypatch path pointing at the same object.
"""

import sys

from PhyAgentOS.tooling import shell as _impl

sys.modules[__name__] = _impl

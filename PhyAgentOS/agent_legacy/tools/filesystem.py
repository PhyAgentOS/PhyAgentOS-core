"""Compatibility alias for the moved filesystem tools.

The implementation lives in :mod:`PhyAgentOS.tooling.filesystem`. Replacing
this module entry with the implementation keeps every historical import,
private helper, and monkeypatch path pointing at the same object.
"""

import sys

from PhyAgentOS.tooling import filesystem as _impl

sys.modules[__name__] = _impl

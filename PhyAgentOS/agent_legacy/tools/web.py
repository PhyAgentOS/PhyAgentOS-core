"""Compatibility alias for the moved web tools.

The implementation lives in :mod:`PhyAgentOS.tooling.web`. Replacing this
module entry with the implementation keeps every historical import, constant,
private helper, and monkeypatch path pointing at the same object.
"""

import sys

from PhyAgentOS.tooling import web as _impl

sys.modules[__name__] = _impl

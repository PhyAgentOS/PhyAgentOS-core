"""Compatibility alias for the moved general tool base class.

The implementation lives in :mod:`PhyAgentOS.tooling.base`. Replacing this
module entry with the implementation keeps every historical import, private
helper, and monkeypatch path pointing at the same object.
"""

import sys

from PhyAgentOS.tooling import base as _impl

sys.modules[__name__] = _impl

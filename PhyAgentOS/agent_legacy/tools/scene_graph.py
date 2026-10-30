"""Compatibility alias for the moved scene graph tool.

The implementation lives in :mod:`PhyAgentOS.tooling.scene_graph`. Replacing
this module entry with the implementation keeps every historical import and
monkeypatch path pointing at the same object.
"""

import sys

from PhyAgentOS.tooling import scene_graph as _impl

sys.modules[__name__] = _impl

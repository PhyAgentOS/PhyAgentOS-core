"""Compatibility alias for :mod:`PhyAgentOS.agent.services.forge_verifier`.

Sharing the module keeps legacy imports and monkeypatched module symbols working.
"""

import sys

from PhyAgentOS.agent.services import forge_verifier

sys.modules[__name__] = forge_verifier

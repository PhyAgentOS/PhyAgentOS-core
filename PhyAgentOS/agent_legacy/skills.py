"""Compatibility alias for :mod:`PhyAgentOS.agent.services.skill_documents`.

Sharing the module keeps legacy imports and monkeypatched module symbols working.
"""

import sys

from PhyAgentOS.agent.services import skill_documents

sys.modules[__name__] = skill_documents

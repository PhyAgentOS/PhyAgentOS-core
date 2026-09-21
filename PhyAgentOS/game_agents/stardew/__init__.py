"""General game hierarchy, hosted by the existing PhyAgentOS runtime."""

from PhyAgentOS.game_agents.stardew.runtime import (
    GeneralGameSkillRuntime,
    register_general_game,
)

__all__ = ["GeneralGameSkillRuntime", "register_general_game"]

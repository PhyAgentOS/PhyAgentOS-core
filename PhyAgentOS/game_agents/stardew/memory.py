"""Freeze role memory per session; record evidenced candidates after execution."""

from __future__ import annotations

import json
from pathlib import Path

from PhyAgentOS.agent.memory import MemoryStore
from PhyAgentOS.game_agents.stardew.models import MemoryCandidate, Role


class GameMemory:
    """Use Core's storage API without creating another memory service.

    MEMORY.md contains curated knowledge. New model lessons go to HISTORY.md
    as unverified candidates, not directly into the next session's instructions.
    """

    def __init__(self, workspace: Path) -> None:
        self.stores = {
            role: MemoryStore(workspace / "game_agent" / role) for role in ("planner", "actor")
        }

    def snapshot(self) -> dict[str, str]:
        return {role: store.read_long_term() for role, store in self.stores.items()}

    def record(self, session_id: str, role: Role, candidates: list[MemoryCandidate]) -> None:
        if not candidates:
            return
        if any(item.role != role for item in candidates):
            raise ValueError("candidate role does not match memory scope")
        entry = {
            "session_id": session_id,
            "status": "unverified",
            "candidates": [item.model_dump() for item in candidates],
        }
        self.stores[role].append_history(json.dumps(entry, ensure_ascii=False))

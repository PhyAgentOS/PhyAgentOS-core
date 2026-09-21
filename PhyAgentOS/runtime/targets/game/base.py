"""Base class for all game rollout targets.

Provides shared lifecycle boilerplate so new game targets (Minecraft,
Stardew Valley, etc.) only need to implement build/reset/observe/step/close.
"""

from __future__ import annotations

from typing import Any

from PhyAgentOS.runtime.targets.local.base import BaseLocalTarget


class BaseGameTarget(BaseLocalTarget):
    """Shared base for game targets — supplies defaults for the session
    lifecycle methods that are identical across games.
    """

    def describe(self) -> dict[str, Any]:
        return {"type": self._game_type(), "actions": self._supported_actions()}

    def _game_type(self) -> str:
        return self.__class__.__name__

    def _supported_actions(self) -> list[str]:
        return sorted(getattr(self, "_current_action_types", frozenset()))

    def configure_session(self, session_ctx: dict[str, Any]) -> dict[str, Any]:
        return {"configured": True, "session_id": session_ctx.get("session_id")}

    def start_session(self, session_ctx: dict[str, Any]) -> dict[str, Any]:
        self._last_status = {}
        self.reset_step_counter()
        obs = self.observe()
        return obs

    def reset_step_counter(self) -> None:
        ...

    def action_chunk(self, executable_action_chunk: dict[str, Any]) -> dict[str, Any]:
        actions = executable_action_chunk.get("actions", [executable_action_chunk])
        if isinstance(actions, dict):
            actions = [actions]
        last = {"obs": self.observe(), "done": False, "info": {}}
        for act in actions:
            last = self.step(act)
            if last.get("done"):
                break
        self._last_status = {
            **last,
            "executed_steps": getattr(self, "_step_idx", 0),
            "success": bool(last.get("info", {}).get("success")),
            "done": bool(last.get("done")),
        }
        return self._last_status

    def execution_status(self) -> dict[str, Any]:
        return getattr(self, "_last_status", {"status": "idle"})

    def reset(self, session_ctx: dict[str, Any]) -> dict[str, Any]:
        self._last_status = {}
        self.reset_step_counter()
        return self.observe()

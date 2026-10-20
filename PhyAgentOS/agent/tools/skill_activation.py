"""Explicit, auditable activation of one registered Agent Skill."""

from __future__ import annotations

import inspect
from contextvars import ContextVar
from typing import Any

from PhyAgentOS.agent.experience.activation import SkillActivationManager
from PhyAgentOS.agent.tools.base import Tool


class ActivateSkillTool(Tool):
    def __init__(self, manager: SkillActivationManager) -> None:
        self.manager = manager
        self.session_key: ContextVar[str] = ContextVar(
            "activate_skill_session_key", default="cli:direct"
        )

    def set_context(self, session_key: str) -> None:
        self.session_key.set(session_key)

    @property
    def name(self) -> str:
        return "activate_skill"

    @property
    def description(self) -> str:
        return (
            "Activate a registered workflow Skill before executing a matching task. Returns the "
            "Skill instructions and only its applicable task-scoped lessons, and records an "
            "auditable task-to-Skill binding. Use one primary Skill and optional supporting Skills."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "description": "Exact Skill name from the registered Skills summary",
                },
                "role": {
                    "type": "string",
                    "enum": ["primary", "supporting"],
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    async def execute(self, name: str, role: str = "primary") -> str:
        result = self.manager.activate(
            session_key=self.session_key.get(),
            name=name,
            role=role,
        )
        if inspect.isawaitable(result):
            result = await result
        activation, content, lessons = result
        return self.manager.dump_activation_result(activation, content, lessons)

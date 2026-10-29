"""Expose Skill documents and explicit activation through native Core tools."""

from __future__ import annotations

import json

from PhyAgentOS.agent.core.types import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.agent.services.turn_activation import TurnSkillActivationManager

ACTIVATE_SKILL = ToolDefinition(
    name="activate_skill",
    description=(
        "Read and activate a registered Skill for this turn before using its workflow. "
        "Returns the full document and a fresh activation_id for forge_task_create. "
        "Use one primary Skill and optional supporting Skills. Activation does not start a runtime."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
            "role": {"type": "string", "enum": ["primary", "supporting"]},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)


class SkillAccessCapability:
    def __init__(self, activations: TurnSkillActivationManager) -> None:
        self.activations = activations

    @property
    def name(self) -> str:
        return "skill_access"

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return (ToolRegistration(definition=ACTIVATE_SKILL, handler=self._activate),)

    async def _activate(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        arguments = call.arguments_dict()
        try:
            activation, content = await self.activations.activate(
                session_key=context.session_id,
                turn_id=context.turn_id,
                name=arguments["name"],
                role=arguments.get("role", "primary"),
            )
        except (ValueError, OSError, RuntimeError) as exc:
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.FAILED,
                text=str(exc),
                error=ToolError("skill_activation_failed", str(exc)),
            )
        payload = {"ok": True, "activation": activation.model_dump(mode="json"), "skill": content}
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=json.dumps(payload, ensure_ascii=False),
            content=payload,
        )

    def prompt_context(self, context: ExecutionContext) -> str:
        self.activations.begin_turn(context.session_id, context.turn_id)
        current = self.activations.current(context.session_id, context.turn_id)
        documents = self.activations.documents
        parts = [
            "Skills: call activate_skill before using a matching workflow. "
            "Only activation_ids returned in this turn are valid; historical ids are not authorization. "
            "For physical work activate a primary Skill, then create an AgentTask. "
            "Do not ask the user to supply activation ids. "
            "A running Skill runtime is separate from a document activation.",
            documents.build_skills_summary(),
            "Current turn activations: " + json.dumps(
                [a.model_dump(mode="json") for a in current], ensure_ascii=False
            ),
        ]
        always = documents.get_always_skills()
        if always:
            parts.append(documents.load_skills_for_context(always))
        return "\n\n".join(part for part in parts if part)

    async def cancel(self, session_id: str, context: ExecutionContext | None) -> None:
        # Forge owns remote cancellation. Document activation has no remote effect.
        pass

    async def aclose(self) -> None:
        self.activations.clear()

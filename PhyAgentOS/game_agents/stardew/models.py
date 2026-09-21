"""Planner/Actor decisions and execution receipts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from PhyAgentOS.runtime.schemas.task_plan import ActionSpec

Role = Literal["planner", "actor"]


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannerDecision(DecisionModel):
    decision: Literal["new_phase", "continue_phase", "replan", "finish"]
    goal: str = Field(default="", max_length=500)
    reason: str = Field(min_length=1, max_length=1000)
    max_rounds: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def require_goal(self) -> PlannerDecision:
        if self.decision in {"new_phase", "replan"} and not self.goal.strip():
            raise ValueError("new_phase and replan require a goal")
        return self


class GameAction(ActionSpec):
    """Core's type/params action, rejecting unvalidated alternate payloads."""

    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1)


class ActorDecision(DecisionModel):
    decision: Literal["execute", "yield", "replan"]
    intent: str = Field(min_length=1, max_length=500)
    action: GameAction | None = None

    @model_validator(mode="after")
    def require_one_primitive(self) -> ActorDecision:
        if self.decision == "execute":
            if self.action is None:
                raise ValueError("execute requires one typed action")
        elif self.action is not None:
            raise ValueError("control decisions cannot carry an action")
        return self


class MemoryCandidate(DecisionModel):
    role: Role
    lesson: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=16)


class MemoryUpdate(DecisionModel):
    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=8)


@dataclass
class Phase:
    id: str
    goal: str
    max_rounds: int


@dataclass
class RoundReceipt:
    id: str
    intent: str
    action: dict[str, Any] | None
    before: dict[str, Any]
    after: dict[str, Any]
    feedback: dict[str, Any]

    def planner_view(self) -> dict[str, Any]:
        """Expose observed changes, never primitive payloads or Actor memory."""
        return {
            "id": self.id,
            "intent": self.intent,
            "changes": {
                key: {"before": self.before.get(key), "after": self.after.get(key)}
                for key in self.before.keys() | self.after.keys()
                if self.before.get(key) != self.after.get(key)
            },
            "feedback": self.feedback,
        }


@dataclass
class LoopReceipt:
    id: str
    phase: Phase
    rounds: list[RoundReceipt] = field(default_factory=list)
    end_reason: str = "round_limit"
    end_detail: str = ""

    def planner_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase_id": self.phase.id,
            "goal": self.phase.goal,
            "end_reason": self.end_reason,
            "end_detail": self.end_detail,
            "rounds": [item.planner_view() for item in self.rounds],
        }

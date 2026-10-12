"""Data contracts for isolated, round-based benchmark evaluation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL = "benchmark_contract_v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Case(StrictModel):
    case_key: str = Field(min_length=1, max_length=160)
    environment: dict = Field(default_factory=dict)


class BenchmarkPlan(StrictModel):
    version: Literal["benchmark_plan_v1"] = "benchmark_plan_v1"
    cases: list[Case] = Field(min_length=1, max_length=1000)
    rounds: int = Field(default=1, ge=1, le=100)
    parallelism: int = Field(default=1, ge=1, le=64)
    max_decisions: int = Field(default=32, ge=1, le=10000)
    episode_timeout_s: int = Field(default=300, ge=1, le=86400)
    model_timeout_s: int = Field(default=60, ge=1, le=600)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({c.case_key for c in self.cases}) != len(self.cases):
            raise ValueError("case_key must be unique within a manifest")
        return self


class LessonCandidate(StrictModel):
    workflow_key: str = Field(min_length=1, max_length=160)
    principle: str = Field(min_length=1, max_length=1000)
    applies_when: list[str] = Field(min_length=1)
    does_not_apply_when: list[str] = Field(min_length=1)
    supporting_subtasks: list[str] = Field(min_length=2)


class EpisodeSnapshot(StrictModel):
    task_id: str
    run_id: str
    subtask_id: str
    attempt_id: str
    worker_id: str
    world_epoch: str
    binding_digest: str
    observation_version: int = Field(ge=0)
    observation: dict
    state: Literal["ready", "running", "terminal", "unknown"]
    outcome: Literal["success", "failure", "cancelled", "infra_error"] | None = None
    native_score: float | int | None = None
    stopped: bool = False
    history_digest: str

    @model_validator(mode="after")
    def terminal_outcome(self):
        if self.state == "terminal" and self.outcome is None:
            raise ValueError("terminal episode requires authoritative outcome")
        return self

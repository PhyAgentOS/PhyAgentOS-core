"""Versioned contracts for task-level experience and Skill evolution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExperienceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillActivation(ExperienceModel):
    version: Literal["skill_activation_v2"] = "skill_activation_v2"
    activation_id: str
    skill_name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    role: Literal["primary", "supporting"] = "primary"
    source: Literal["workspace", "installed", "builtin"]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_version: str | None = None
    binding_candidate_id: str | None = None
    activated_at: datetime = Field(default_factory=utc_now)


class WorkflowTraceItem(ExperienceModel):
    name: str = Field(min_length=1)
    input_keys: list[str] = Field(default_factory=list)


class LineageOutcome(ExperienceModel):
    session_ref: str
    task_ref: str | None = None
    revision_ref: str | None = None
    invocation_ref: str | None = None
    attempt_ref: str | None = None
    action_semantics: str = Field(min_length=1)
    input_keys: list[str] = Field(default_factory=list)
    execution_status: str | None = None
    semantic_verdict: Literal[
        "success", "failure", "replan_required", "inconclusive"
    ] | None = None
    reason: str = ""
    verifier_lesson: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class TaskOutcomeEnvelope(ExperienceModel):
    version: Literal["task_outcome_envelope_v1"] = "task_outcome_envelope_v1"
    task_id: str
    root_task_id: str
    source: str = Field(default="forge", min_length=1, max_length=64)
    goal: str
    success_criteria: list[str] = Field(default_factory=list)
    final_verdict: Literal[
        "success", "failure", "replan_required", "inconclusive"
    ] | None = None
    criteria_statuses: dict[str, Literal["satisfied", "unsatisfied", "unknown"]] = (
        Field(default_factory=dict)
    )
    lineage: list[LineageOutcome] = Field(default_factory=list)
    record_refs: list[str] = Field(default_factory=list)
    agent_task_ref: str | None = None
    tool_invocation_refs: list[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=utc_now)

    @property
    def learnable(self) -> bool:
        return self.successful or self.final_verdict in {"failure", "replan_required"}

    @property
    def successful(self) -> bool:
        return self.final_verdict == "success" and bool(self.criteria_statuses) and all(
            status == "satisfied" for status in self.criteria_statuses.values()
        )

    @property
    def has_failed_attempt(self) -> bool:
        return any(
            item.semantic_verdict in {"failure", "replan_required"}
            for item in self.lineage
        )


class TaskEpisode(ExperienceModel):
    version: Literal["task_episode_v1"] = "task_episode_v1"
    episode_id: str
    root_task_id: str
    source: str = Field(default="forge", min_length=1, max_length=64)
    task_summary: str
    goal: str
    success_criteria: list[str] = Field(default_factory=list)
    skill_activations: list[SkillActivation] = Field(default_factory=list)
    primary_skill_binding_id: str | None = None
    primary_skill_version: str | None = None
    skill_document_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    workflow_trace: list[WorkflowTraceItem] = Field(default_factory=list)
    outcome: TaskOutcomeEnvelope
    agent_task_ref: str | None = None
    tool_invocation_refs: list[str] = Field(default_factory=list)
    processing_status: Literal[
        "pending", "processed", "skipped", "failed"
    ] = "pending"
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: datetime | None = None

    @property
    def primary_skill(self) -> str | None:
        for activation in self.skill_activations:
            if activation.role == "primary":
                return activation.skill_name
        return None


class LessonProposal(ExperienceModel):
    skill_name: str | None = None
    workflow_key: str = Field(min_length=1, max_length=160)
    applies_when: list[str] = Field(min_length=1)
    does_not_apply_when: list[str] = Field(min_length=1)
    failure_mode: str = Field(min_length=1, max_length=600)
    recommendation: str = Field(min_length=1, max_length=1000)
    severity: Literal["advisory", "important"] = "advisory"
    supersedes_lesson_ids: list[str] = Field(default_factory=list)

    @field_validator("applies_when", "does_not_apply_when")
    @classmethod
    def nonempty_scope(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("lesson scope items must be non-empty")
        return normalized


class LessonEligibility(ExperienceModel):
    decision: Literal["related", "unrelated", "uncertain"]
    reason: Literal[
        "workflow_related",
        "task_unsatisfiable",
        "verifier_limit",
        "evidence_limit",
        "external_or_infrastructure",
        "user_constraint",
        "unknown",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def consistent_reason(self) -> "LessonEligibility":
        if self.decision == "related" and self.reason != "workflow_related":
            raise ValueError("related Lesson eligibility requires workflow_related")
        if self.decision != "related" and self.reason == "workflow_related":
            raise ValueError("workflow_related eligibility must be related")
        return self


class FailureObservationProposal(ExperienceModel):
    eligibility: LessonEligibility
    skill_name: str | None = None
    workflow_key: str | None = Field(default=None, max_length=160)
    matched_cluster_id: str | None = None
    pattern_key: str | None = Field(
        default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    pattern_summary: str | None = Field(default=None, max_length=600)
    applies_when: list[str] = Field(default_factory=list)
    does_not_apply_when: list[str] = Field(default_factory=list)
    recovery_principle: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_related_pattern(self) -> "FailureObservationProposal":
        if self.eligibility.decision != "related":
            return self
        required = (
            self.workflow_key,
            self.pattern_key,
            self.pattern_summary,
            self.recovery_principle,
        )
        if any(not item for item in required):
            raise ValueError("related failure observation requires a normalized pattern")
        if not self.applies_when or not self.does_not_apply_when:
            raise ValueError("related failure observation requires scoped applicability")
        return self


class FailureObservation(ExperienceModel):
    version: Literal["failure_observation_v1"] = "failure_observation_v1"
    observation_id: str
    episode_id: str
    root_task_id: str
    skill_name: str | None = None
    skill_version_spec: str | None = None
    workflow_key: str = Field(min_length=1, max_length=160)
    cluster_id: str
    pattern_key: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    pattern_summary: str = Field(min_length=1, max_length=600)
    applies_when: list[str] = Field(min_length=1)
    does_not_apply_when: list[str] = Field(min_length=1)
    recovery_principle: str = Field(min_length=1, max_length=1000)
    created_at: datetime = Field(default_factory=utc_now)


class LessonAbstractionValidation(ExperienceModel):
    version: Literal["lesson_abstraction_validation_v1"] = (
        "lesson_abstraction_validation_v1"
    )
    reusable: bool
    contains_specific_answer: bool
    unsupported_literals: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=1000)


class LessonCluster(ExperienceModel):
    version: Literal["lesson_cluster_v1"] = "lesson_cluster_v1"
    cluster_id: str
    skill_name: str | None = None
    skill_version_spec: str | None = None
    workflow_key: str = Field(min_length=1, max_length=160)
    pattern_key: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    canonical_pattern: str = Field(min_length=1, max_length=600)
    applies_when: list[str] = Field(min_length=1)
    does_not_apply_when: list[str] = Field(min_length=1)
    recovery_principles: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    supporting_root_task_ids: list[str] = Field(default_factory=list)
    status: Literal["collecting", "blocked", "activated"] = "collecting"
    draft: LessonProposal | None = None
    validation: LessonAbstractionValidation | None = None
    validation_errors: list[str] = Field(default_factory=list)
    lesson_id: str | None = None
    migration_seed_lesson_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    activated_at: datetime | None = None


class SkillWorkflowProposal(ExperienceModel):
    operation: Literal["create", "update"]
    skill_name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    workflow_key: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1024)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(min_length=1)
    verification_checkpoints: list[str] = Field(min_length=1)
    recovery_guidance: list[str] = Field(default_factory=list)
    applicability_boundaries: list[str] = Field(min_length=1)

    @field_validator(
        "preconditions",
        "steps",
        "verification_checkpoints",
        "recovery_guidance",
        "applicability_boundaries",
    )
    @classmethod
    def normalize_items(cls, values: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("workflow items must be non-empty")
        return normalized


class ExperienceAssessment(ExperienceModel):
    version: Literal["experience_assessment_v1"] = "experience_assessment_v1"
    outcome: Literal["success", "failure", "mixed", "ignored"]
    reusable: bool
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=2000)
    skill_candidate: SkillWorkflowProposal | None = None
    failure_observations: list[FailureObservationProposal] = Field(default_factory=list)
    contradicted_lesson_ids: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_candidate(self) -> "ExperienceAssessment":
        if self.skill_candidate is not None and (
            not self.reusable or self.outcome not in {"success", "mixed"}
        ):
            raise ValueError("Skill candidate requires a reusable successful outcome")
        if self.failure_observations and self.outcome not in {"failure", "mixed"}:
            raise ValueError("failure observations require a failure or mixed outcome")
        return self


class ScopedLesson(ExperienceModel):
    version: Literal["scoped_lesson_v1"] = "scoped_lesson_v1"
    lesson_id: str
    skill_name: str | None = None
    skill_version_spec: str | None = None
    workflow_key: str
    applies_when: list[str]
    does_not_apply_when: list[str]
    failure_mode: str
    recommendation: str
    severity: Literal["advisory", "important"] = "advisory"
    status: Literal["active", "inactive", "superseded", "retired"] = "active"
    source_episode_ids: list[str] = Field(default_factory=list)
    counterexample_episode_ids: list[str] = Field(default_factory=list)
    supersedes_lesson_ids: list[str] = Field(default_factory=list)
    superseded_by_lesson_id: str | None = None
    cluster_id: str | None = None
    supporting_episode_ids: list[str] = Field(default_factory=list)
    observation_count: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillCandidate(ExperienceModel):
    version: Literal["skill_candidate_v1"] = "skill_candidate_v1"
    candidate_id: str
    proposal: SkillWorkflowProposal
    supporting_episode_ids: list[str] = Field(default_factory=list)
    status: Literal[
        "collecting", "blocked", "promoted", "rejected"
    ] = "collecting"
    blocked_by_lesson_ids: list[str] = Field(default_factory=list)
    target_revision: int = Field(default=1, ge=1)
    validation_errors: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    promoted_at: datetime | None = None

"""Guarded Lesson lifecycle and Skill candidate promotion."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

import yaml

from PhyAgentOS.agent_legacy.experience.contracts import (
    ExperienceAssessment,
    FailureObservation,
    FailureObservationProposal,
    LessonAbstractionValidation,
    LessonCluster,
    LessonProposal,
    ScopedLesson,
    SkillCandidate,
    SkillWorkflowProposal,
    TaskEpisode,
    utc_now,
)
from PhyAgentOS.agent_legacy.experience.store import ExperienceStore
from PhyAgentOS.agent_legacy.skills import SkillsLoader
from PhyAgentOS.utils.atomic_file import atomic_write_text

_MANAGED_START = "<!-- paos:learned-workflow:start -->"
_MANAGED_END = "<!-- paos:learned-workflow:end -->"
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BANNED_CONTENT = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"(?<![\w.])/(?:etc|home|root|data|var|opt|srv|tmp|Users)(?:/[^\s`]+)+"),
    re.compile(r"\b[A-Za-z]:\\(?:[^\s`]+\\)*[^\s`]+"),
    re.compile(r"\b(?:command|session)_[0-9a-f]{6,}\b", re.IGNORECASE),
    re.compile(r"\b(?:api[_ -]?key|password|secret|access[_ -]?token)\b", re.IGNORECASE),
    re.compile(r"\b(?:bypass|disable|skip)\b.{0,40}\b(?:forge|verif(?:y|ier|ication))\b", re.IGNORECASE),
    re.compile(r"(?:绕过|禁用|跳过).{0,20}(?:forge|验证|校验)", re.IGNORECASE),
    re.compile(r"action[_ -]?manifest\s*[:=\[{]", re.IGNORECASE),
    re.compile(r"action_type\s*[:=]", re.IGNORECASE),
    re.compile(
        r"\b(?:ignore|disregard)\b.{0,60}\b(?:system|developer|previous)\b.{0,30}"
        r"\b(?:instruction|prompt)s?\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:忽略|无视).{0,30}(?:系统|开发者|先前).{0,20}(?:指令|提示词)"),
    re.compile(r"<\s*/?\s*(?:script|system|developer)\b", re.IGNORECASE),
)
_BANNED_SPECIFIC_LESSON = (
    re.compile(r"\b(?:answer is|correct answer is|choose option|select option)\b", re.IGNORECASE),
    re.compile(r"(?:答案是|正确答案|选择选项|选项为|设置为具体值)"),
    re.compile(r"\boption\s+[A-Z0-9]\b", re.IGNORECASE),
    re.compile(r"选项\s*[A-Z0-9甲乙丙丁]", re.IGNORECASE),
    re.compile(
        r"(?:\(|\[)\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?(?:\s*,\s*-?\d+(?:\.\d+)?)?\s*(?:\)|\])"
    ),
    re.compile(r"\b(?:x|y|z|latitude|longitude)\s*[:=]\s*-?\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\b(?:set|assign)\b.{0,40}\bto\s+-?\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])"),
)


class SkillEvolutionError(ValueError):
    pass


class SkillEvolutionManager:
    def __init__(
        self,
        *,
        workspace: str | Path,
        store: ExperienceStore,
        min_successful_episodes: int = 3,
        min_lesson_episodes: int = 3,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.store = store
        self.min_successful_episodes = max(1, int(min_successful_episodes))
        self.min_lesson_episodes = max(1, int(min_lesson_episodes))
        self.skills = SkillsLoader(self.workspace)
        self.revisions_dir = self.workspace / ".paos" / "evolution" / "revisions"

    def apply(self, episode: TaskEpisode, assessment: ExperienceAssessment) -> set[str]:
        self._validate_assessment_binding(episode, assessment)
        touched_skills: set[str] = set()
        scheduled_clusters: set[str] = set()
        if episode.outcome.has_failed_attempt:
            if not assessment.failure_observations:
                self.store.record_event(
                    "lesson_eligibility_rejected",
                    episode.episode_id,
                    {"decision": "uncertain", "reason": "no_observation"},
                )
            for proposal in assessment.failure_observations:
                eligibility = proposal.eligibility
                if (
                    eligibility.decision != "related"
                    or eligibility.reason != "workflow_related"
                ):
                    self.store.record_event(
                        "lesson_eligibility_rejected",
                        episode.episode_id,
                        {
                            "decision": eligibility.decision,
                            "reason": eligibility.reason,
                            "confidence": eligibility.confidence,
                        },
                    )
                    continue
                try:
                    observation, cluster = self._observation_from_proposal(
                        episode, proposal
                    )
                    self._validate_observation_content(observation)
                except SkillEvolutionError as exc:
                    self.store.record_event(
                        "validation_rejected",
                        episode.episode_id,
                        {"artifact": "failure_observation", "summary": str(exc)},
                    )
                    continue
                cluster, inserted_support = self.store.add_observation(
                    observation, cluster
                )
                if cluster.skill_name:
                    touched_skills.add(cluster.skill_name)
                if cluster.status == "activated":
                    self._attach_support_to_active_lesson(cluster, observation)
                elif (
                    inserted_support
                    and len(cluster.supporting_root_task_ids)
                    >= self.min_lesson_episodes
                ):
                    self.store.enqueue_cluster_job(cluster.cluster_id)
                    scheduled_clusters.add(cluster.cluster_id)

        if episode.outcome.successful:
            for lesson_id in assessment.contradicted_lesson_ids:
                lesson = self._add_counterexample(lesson_id, episode.episode_id)
                if lesson and lesson.skill_name:
                    touched_skills.add(lesson.skill_name)

            if assessment.reusable and assessment.skill_candidate is not None:
                candidate = self._support_candidate(
                    episode, assessment.skill_candidate, assessment.conflicts
                )
                if len(candidate.supporting_episode_ids) >= self.min_successful_episodes:
                    self._promote_if_ready(candidate)

        for skill_name in touched_skills:
            self.write_lesson_projection(skill_name)
        return scheduled_clusters

    def _validate_assessment_binding(
        self, episode: TaskEpisode, assessment: ExperienceAssessment
    ) -> None:
        activated = {item.skill_name for item in episode.skill_activations}
        primary = episode.primary_skill
        candidate = assessment.skill_candidate
        if candidate is not None:
            if candidate.operation == "update" and candidate.skill_name != primary:
                raise SkillEvolutionError("an update candidate must target the activated primary Skill")
            if candidate.operation == "create" and primary is not None:
                raise SkillEvolutionError("a task with a primary Skill cannot create a replacement Skill")
        for proposal in assessment.failure_observations:
            if (
                proposal.eligibility.decision == "related"
                and proposal.skill_name is not None
                and proposal.skill_name not in activated
            ):
                raise SkillEvolutionError("observation target must be an activated Skill")
        active_ids = {item.lesson_id for item in self.store.list_lessons(status="active")}
        if set(assessment.contradicted_lesson_ids) - active_ids:
            raise SkillEvolutionError("assessment contradicted an unknown or inactive lesson")

    def _observation_from_proposal(
        self, episode: TaskEpisode, proposal: FailureObservationProposal
    ) -> tuple[FailureObservation, LessonCluster]:
        target = proposal.skill_name or episode.primary_skill
        target_activation = next(
            (item for item in episode.skill_activations if item.skill_name == target),
            None,
        )
        version_spec = (
            f"=={target_activation.skill_version}"
            if target_activation is not None and target_activation.skill_version
            else None
        )
        workflow_key = proposal.workflow_key or ""
        pattern_key = proposal.pattern_key or ""
        if proposal.matched_cluster_id:
            cluster = self.store.get_cluster(proposal.matched_cluster_id)
            if cluster is None:
                raise SkillEvolutionError("matched Lesson cluster does not exist")
            if (
                cluster.skill_name != target
                or cluster.workflow_key != workflow_key
                or cluster.skill_version_spec != version_spec
            ):
                raise SkillEvolutionError("matched Lesson cluster has a different scope")
        else:
            digest = hashlib.sha256(
                f"{target or 'unbound'}\n{version_spec or '*'}\n{workflow_key}\n{pattern_key}".encode("utf-8")
            ).hexdigest()[:20]
            cluster = LessonCluster(
                cluster_id=f"lesson_cluster_{digest}",
                skill_name=target,
                skill_version_spec=version_spec,
                workflow_key=workflow_key,
                pattern_key=pattern_key,
                canonical_pattern=proposal.pattern_summary or "",
                applies_when=proposal.applies_when,
                does_not_apply_when=proposal.does_not_apply_when,
                recovery_principles=[proposal.recovery_principle or ""],
            )
        observation_id = "failure_observation_" + hashlib.sha256(
            f"{episode.episode_id}\n{cluster.cluster_id}".encode("utf-8")
        ).hexdigest()[:20]
        observation = FailureObservation(
            observation_id=observation_id,
            episode_id=episode.episode_id,
            root_task_id=episode.root_task_id,
            skill_name=target,
            skill_version_spec=version_spec,
            workflow_key=workflow_key,
            cluster_id=cluster.cluster_id,
            pattern_key=pattern_key,
            pattern_summary=proposal.pattern_summary or "",
            applies_when=proposal.applies_when,
            does_not_apply_when=proposal.does_not_apply_when,
            recovery_principle=proposal.recovery_principle or "",
        )
        return observation, cluster

    def _attach_support_to_active_lesson(
        self, cluster: LessonCluster, observation: FailureObservation
    ) -> None:
        if not cluster.lesson_id:
            return

        def attach(lesson: ScopedLesson) -> None:
            lesson.source_episode_ids = list(
                dict.fromkeys(lesson.source_episode_ids + [observation.episode_id])
            )
            lesson.supporting_episode_ids = list(
                dict.fromkeys(
                    lesson.supporting_episode_ids + [observation.episode_id]
                )
            )
            lesson.observation_count = len(lesson.supporting_episode_ids)

        self.store.update_lesson(
            cluster.lesson_id, attach, event_type="lesson_observed"
        )

    def validate_cluster_draft(
        self, cluster: LessonCluster, draft: LessonProposal
    ) -> None:
        if draft.skill_name != cluster.skill_name:
            raise SkillEvolutionError("Lesson draft must target the cluster Skill")
        if draft.workflow_key != cluster.workflow_key:
            raise SkillEvolutionError("Lesson draft must target the cluster workflow")
        self._validate_lesson_content(draft)
        text = "\n".join(
            [
                *draft.applies_when,
                *draft.does_not_apply_when,
                draft.failure_mode,
                draft.recommendation,
            ]
        )
        for pattern in _BANNED_SPECIFIC_LESSON:
            if pattern.search(text):
                raise SkillEvolutionError(
                    f"generated Lesson contains a task-specific answer: {pattern.pattern}"
                )

    def block_cluster(
        self,
        cluster: LessonCluster,
        errors: list[str],
        *,
        draft: LessonProposal | None = None,
        validation: LessonAbstractionValidation | None = None,
    ) -> None:
        cluster.status = "blocked"
        cluster.draft = draft
        cluster.validation = validation
        cluster.validation_errors = errors
        self.store.update_cluster(cluster, event_type="lesson_cluster_blocked")
        if cluster.skill_name:
            self.write_lesson_projection(cluster.skill_name)

    def activate_cluster(
        self,
        cluster: LessonCluster,
        draft: LessonProposal,
        validation: LessonAbstractionValidation,
    ) -> ScopedLesson:
        self.validate_cluster_draft(cluster, draft)
        if (
            not validation.reusable
            or validation.contains_specific_answer
            or validation.unsupported_literals
            or validation.confidence < 0.8
        ):
            raise SkillEvolutionError("Lesson abstraction validation rejected the draft")
        observations = self.store.list_observations(cluster.cluster_id)
        episode_ids = list(dict.fromkeys(item.episode_id for item in observations))
        lesson_id = "lesson_" + hashlib.sha256(
            cluster.cluster_id.encode("utf-8")
        ).hexdigest()[:20]
        if lesson_id in draft.supersedes_lesson_ids:
            raise SkillEvolutionError("a Lesson cannot supersede itself")
        lesson = self.store.upsert_lesson(
            self._build_cluster_lesson(cluster, draft, lesson_id, episode_ids)
        )
        self._supersede_lessons(lesson)
        cluster.status = "activated"
        cluster.draft = draft
        cluster.validation = validation
        cluster.validation_errors = []
        cluster.lesson_id = lesson.lesson_id
        cluster.activated_at = utc_now()
        self.store.update_cluster(cluster, event_type="lesson_activated")
        if cluster.skill_name:
            self.write_lesson_projection(cluster.skill_name)
        return lesson

    def _build_cluster_lesson(
        self,
        cluster: LessonCluster,
        draft: LessonProposal,
        lesson_id: str,
        episode_ids: list[str],
    ) -> ScopedLesson:
        lesson = ScopedLesson(
            lesson_id=lesson_id,
            skill_name=cluster.skill_name,
            skill_version_spec=cluster.skill_version_spec,
            workflow_key=cluster.workflow_key,
            applies_when=draft.applies_when,
            does_not_apply_when=draft.does_not_apply_when,
            failure_mode=draft.failure_mode,
            recommendation=draft.recommendation,
            severity=draft.severity,
            source_episode_ids=episode_ids,
            supporting_episode_ids=episode_ids,
            supersedes_lesson_ids=draft.supersedes_lesson_ids,
            cluster_id=cluster.cluster_id,
            observation_count=max(1, len(episode_ids)),
        )
        self._validate_supersede_targets(lesson)
        return lesson

    def _validate_supersede_targets(self, replacement: ScopedLesson) -> None:
        for lesson_id in replacement.supersedes_lesson_ids:
            current = self.store.get_lesson(lesson_id)
            if (
                current is None
                or current.status != "active"
                or current.skill_name != replacement.skill_name
                or current.workflow_key != replacement.workflow_key
            ):
                raise SkillEvolutionError(
                    "a replacement Lesson must supersede an active Lesson in the same scope"
                )

    def _supersede_lessons(self, replacement: ScopedLesson) -> set[str]:
        self._validate_supersede_targets(replacement)
        touched: set[str] = set()
        for lesson_id in replacement.supersedes_lesson_ids:
            def supersede(current: ScopedLesson) -> None:
                current.status = "superseded"
                current.superseded_by_lesson_id = replacement.lesson_id

            previous = self.store.update_lesson(
                lesson_id, supersede, event_type="lesson_superseded"
            )
            if previous and previous.skill_name:
                touched.add(previous.skill_name)
        return touched

    def _add_counterexample(self, lesson_id: str, episode_id: str) -> ScopedLesson | None:
        def mutate(lesson: ScopedLesson) -> None:
            lesson.counterexample_episode_ids = list(
                dict.fromkeys(lesson.counterexample_episode_ids + [episode_id])
            )
            if len(lesson.counterexample_episode_ids) >= self.min_successful_episodes:
                lesson.status = "retired"

        lesson = self.store.update_lesson(
            lesson_id,
            mutate,
            event_type="lesson_retired" if self.min_successful_episodes == 1 else "lesson_counterexample",
        )
        if lesson and lesson.status == "retired":
            self.store.record_event("lesson_retired", lesson.lesson_id)
            if lesson.cluster_id:
                cluster = self.store.get_cluster(lesson.cluster_id)
                if cluster is not None:
                    cluster.status = "collecting"
                    cluster.draft = None
                    cluster.validation = None
                    cluster.validation_errors = []
                    self.store.update_cluster(
                        cluster, event_type="lesson_cluster_reopened"
                    )
        return lesson

    def _support_candidate(
        self,
        episode: TaskEpisode,
        proposal: SkillWorkflowProposal,
        conflicts: list[str],
    ) -> SkillCandidate:
        self._bind_matching_unbound_lessons(
            proposal.skill_name, proposal.workflow_key
        )
        base = hashlib.sha256(
            f"{proposal.skill_name}\n{proposal.workflow_key}".encode("utf-8")
        ).hexdigest()[:20]
        related = [
            item
            for item in self.store.list_candidates()
            if item.proposal.skill_name == proposal.skill_name
            and item.proposal.workflow_key == proposal.workflow_key
        ]
        collecting = next(
            (item for item in related if item.status in {"collecting", "blocked"}), None
        )
        if collecting is None:
            revision = max((item.target_revision for item in related), default=0) + 1
            collecting = SkillCandidate(
                candidate_id=f"candidate_{base}_r{revision}",
                proposal=proposal,
                supporting_episode_ids=[episode.episode_id],
                target_revision=revision,
            )
        else:
            collecting.proposal = proposal
            collecting.supporting_episode_ids = list(
                dict.fromkeys(collecting.supporting_episode_ids + [episode.episode_id])
            )

        blockers = [
            lesson.lesson_id
            for lesson in self.store.list_lessons(status="active")
            if lesson.workflow_key == proposal.workflow_key
            and lesson.skill_name in {None, proposal.skill_name}
        ]
        collecting.blocked_by_lesson_ids = blockers
        collecting.validation_errors = [
            "assessment_conflict_"
            + hashlib.sha256(item.encode("utf-8")).hexdigest()[:16]
            for item in conflicts
        ]
        collecting.status = (
            "blocked" if blockers or collecting.validation_errors else "collecting"
        )
        return self.store.upsert_candidate(collecting)

    def _bind_matching_unbound_lessons(
        self, skill_name: str, workflow_key: str
    ) -> None:
        """Bind only an exact workflow match proposed by the reflection contract."""
        bound = False
        for lesson in self.store.list_lessons(status="active"):
            if lesson.skill_name is not None or lesson.workflow_key != workflow_key:
                continue

            def bind(current: ScopedLesson) -> None:
                current.skill_name = skill_name

            self.store.update_lesson(
                lesson.lesson_id, bind, event_type="lesson_bound_to_skill"
            )
            if lesson.cluster_id:
                cluster = self.store.get_cluster(lesson.cluster_id)
                if cluster is not None and cluster.skill_name is None:
                    cluster.skill_name = skill_name
                    self.store.update_cluster(
                        cluster, event_type="lesson_cluster_bound_to_skill"
                    )
            bound = True
        self._bind_unbound_clusters(skill_name, workflow_key)
        if bound or any(
            item.skill_name == skill_name and item.workflow_key == workflow_key
            for item in self.store.list_clusters()
        ):
            self.write_lesson_projection(skill_name)

    def _bind_unbound_clusters(self, skill_name: str, workflow_key: str) -> None:
        for cluster in self.store.list_clusters():
            if cluster.skill_name is not None or cluster.workflow_key != workflow_key:
                continue
            cluster.skill_name = skill_name
            self.store.update_cluster(
                cluster, event_type="lesson_cluster_bound_to_skill"
            )

    def _promote_if_ready(self, candidate: SkillCandidate) -> None:
        active_blockers = [
            lesson_id
            for lesson_id in candidate.blocked_by_lesson_ids
            if (lesson := self.store.get_lesson(lesson_id)) is not None
            and lesson.status == "active"
        ]
        if active_blockers:
            candidate.status = "blocked"
            candidate.blocked_by_lesson_ids = active_blockers
            self.store.update_candidate(candidate, event_type="candidate_blocked")
            return
        if candidate.validation_errors:
            candidate.status = "blocked"
            self.store.update_candidate(candidate, event_type="candidate_blocked")
            return
        try:
            self._write_skill(candidate)
        except Exception as exc:
            candidate.status = "blocked"
            candidate.validation_errors = [
                str(exc) if isinstance(exc, SkillEvolutionError) else type(exc).__name__
            ]
            self.store.update_candidate(candidate, event_type="validation_rejected")
            return
        self._bind_unbound_lessons(
            candidate.proposal.skill_name, candidate.proposal.workflow_key
        )
        candidate.status = "promoted"
        candidate.validation_errors = []
        candidate.promoted_at = utc_now()
        self.store.update_candidate(candidate, event_type="candidate_promoted")

    def _bind_unbound_lessons(self, skill_name: str, workflow_key: str) -> None:
        for lesson in self.store.list_lessons(status="active"):
            if lesson.skill_name is not None or lesson.workflow_key != workflow_key:
                continue

            def bind(current: ScopedLesson) -> None:
                current.skill_name = skill_name

            self.store.update_lesson(
                lesson.lesson_id, bind, event_type="lesson_bound_to_skill"
            )
            if lesson.cluster_id:
                cluster = self.store.get_cluster(lesson.cluster_id)
                if cluster is not None and cluster.skill_name is None:
                    cluster.skill_name = skill_name
                    self.store.update_cluster(
                        cluster, event_type="lesson_cluster_bound_to_skill"
                    )
        self._bind_unbound_clusters(skill_name, workflow_key)
        self.write_lesson_projection(skill_name)

    def _write_skill(self, candidate: SkillCandidate) -> None:
        proposal = candidate.proposal
        self._validate_proposal_content(proposal)
        info = self.skills.resolve_skill(proposal.skill_name, require_available=False)
        workspace_path = self.workspace / "skills" / proposal.skill_name / "SKILL.md"
        original: str | None = None
        builtin_baseline: str | None = None
        if workspace_path.exists():
            original = workspace_path.read_text(encoding="utf-8")
            base = original
        elif info is not None:
            base = Path(info["path"]).read_text(encoding="utf-8")
            if info["source"] == "builtin":
                builtin_baseline = base
                base = self._force_not_always(base)
        else:
            if proposal.operation != "create":
                raise SkillEvolutionError("update target Skill does not exist")
            base = self._new_skill_base(proposal)

        managed = self._managed_block(proposal, candidate.target_revision)
        updated = self._replace_managed_block(base, managed)
        self._validate_skill_document(updated, proposal.skill_name)

        revision_dir = self.revisions_dir / proposal.skill_name
        if workspace_path.exists():
            digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]
            atomic_write_text(
                revision_dir / f"r{candidate.target_revision - 1}-{digest}.md", base
            )
        elif builtin_baseline is not None:
            digest = hashlib.sha256(builtin_baseline.encode("utf-8")).hexdigest()[:12]
            atomic_write_text(
                revision_dir / f"baseline-builtin-{digest}.md", builtin_baseline
            )
            self.store.record_event(
                "builtin_baseline_archived",
                candidate.candidate_id,
                {"skill": proposal.skill_name, "baseline_digest": digest},
            )

        try:
            atomic_write_text(workspace_path, updated)
            loaded = SkillsLoader(self.workspace).resolve_skill(
                proposal.skill_name, require_available=False
            )
            if loaded is None or Path(loaded["path"]).resolve() != workspace_path.resolve():
                raise SkillEvolutionError("promoted Skill cannot be reloaded from workspace")
            self.write_lesson_projection(proposal.skill_name)
        except BaseException:
            if original is not None:
                atomic_write_text(workspace_path, original)
            else:
                workspace_path.unlink(missing_ok=True)
            self.store.record_event("revision_rolled_back", candidate.candidate_id)
            raise

    @staticmethod
    def _new_skill_base(proposal: SkillWorkflowProposal) -> str:
        escaped = proposal.description.replace("\n", " ").replace('"', "'")
        return (
            "---\n"
            f"name: {proposal.skill_name}\n"
            f'description: "{escaped}"\n'
            "always: false\n"
            'metadata: {"PhyAgentOS":{"always":false,"available":true,"evolved":true}}\n'
            "---\n\n"
            f"# {proposal.skill_name}\n"
        )

    @staticmethod
    def _force_not_always(content: str) -> str:
        """Make a copied built-in evolvable without turning it into an always-on workflow."""
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillEvolutionError("built-in Skill is missing YAML frontmatter")
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise SkillEvolutionError("built-in Skill frontmatter is not closed") from exc
        metadata = yaml.safe_load("\n".join(lines[1:end]))
        if not isinstance(metadata, dict):
            raise SkillEvolutionError("built-in Skill frontmatter must be an object")
        metadata["always"] = False
        nested = metadata.get("metadata")
        if isinstance(nested, dict):
            paos = nested.get("PhyAgentOS")
            if isinstance(paos, dict):
                paos["always"] = False
        frontmatter = yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).rstrip()
        return "---\n" + frontmatter + "\n---\n" + "\n".join(lines[end + 1 :])

    @staticmethod
    def _managed_block(proposal: SkillWorkflowProposal, revision: int) -> str:
        def section(title: str, items: Iterable[str]) -> str:
            values = list(items)
            if not values:
                return ""
            return f"\n### {title}\n\n" + "\n".join(f"{i}. {item}" for i, item in enumerate(values, 1)) + "\n"

        text = f"{_MANAGED_START}\n\n## Learned Workflow (revision {revision})\n"
        text += f"\n### Trigger\n\n{proposal.description}\n"
        text += section("Preconditions", proposal.preconditions)
        text += section("Workflow", proposal.steps)
        text += section("Verification Checkpoints", proposal.verification_checkpoints)
        text += section("Recovery", proposal.recovery_guidance)
        text += section("Applicability Boundaries", proposal.applicability_boundaries)
        text += (
            "\nDiscover live robot capabilities with `forge_tool_context` and execute only through "
            "registered Forge tools. Treat Gateway completion as an execution fact and use the "
            "task verification verdict for success.\n\n"
        )
        return text + _MANAGED_END

    @staticmethod
    def _replace_managed_block(base: str, managed: str) -> str:
        if _MANAGED_START in base or _MANAGED_END in base:
            pattern = re.compile(
                re.escape(_MANAGED_START) + r"[\s\S]*?" + re.escape(_MANAGED_END)
            )
            if not pattern.search(base):
                raise SkillEvolutionError("Skill has an incomplete PAOS managed block")
            return pattern.sub(managed, base, count=1).rstrip() + "\n"
        return base.rstrip() + "\n\n" + managed + "\n"

    @staticmethod
    def _validate_proposal_content(proposal: SkillWorkflowProposal) -> None:
        if not _SKILL_NAME.fullmatch(proposal.skill_name):
            raise SkillEvolutionError("invalid generated Skill name")
        text = "\n".join(
            [
                proposal.description,
                *proposal.preconditions,
                *proposal.steps,
                *proposal.verification_checkpoints,
                *proposal.recovery_guidance,
                *proposal.applicability_boundaries,
            ]
        )
        for pattern in _BANNED_CONTENT:
            if pattern.search(text):
                raise SkillEvolutionError(
                    f"generated Skill violates content policy: {pattern.pattern}"
                )

    @staticmethod
    def _validate_lesson_content(proposal) -> None:
        text = "\n".join(
            [
                proposal.workflow_key,
                *proposal.applies_when,
                *proposal.does_not_apply_when,
                proposal.failure_mode,
                proposal.recommendation,
            ]
        )
        for pattern in _BANNED_CONTENT:
            if pattern.search(text):
                raise SkillEvolutionError(
                    f"generated Lesson violates content policy: {pattern.pattern}"
                )

    @staticmethod
    def _validate_observation_content(observation: FailureObservation) -> None:
        safety_text = "\n".join(
            [
                observation.workflow_key,
                observation.pattern_key,
                observation.pattern_summary,
                *observation.applies_when,
                *observation.does_not_apply_when,
                observation.recovery_principle,
            ]
        )
        abstract_text = "\n".join(
            [
                observation.pattern_summary,
                *observation.applies_when,
                *observation.does_not_apply_when,
                observation.recovery_principle,
            ]
        )
        for pattern in _BANNED_CONTENT:
            if pattern.search(safety_text):
                raise SkillEvolutionError(
                    f"FailureObservation violates abstraction policy: {pattern.pattern}"
                )
        for pattern in _BANNED_SPECIFIC_LESSON:
            if pattern.search(abstract_text):
                raise SkillEvolutionError(
                    f"FailureObservation violates abstraction policy: {pattern.pattern}"
                )

    def migrate_active_lessons(self) -> set[str]:
        """Downgrade pre-cluster active Lessons and seed auditable cluster support."""
        if self.store.metadata("lesson_cluster_migration_v1") == "1":
            return set()
        scheduled: set[str] = set()
        touched: set[str] = set()
        for lesson in self.store.list_lessons(status="active"):
            if lesson.cluster_id:
                continue
            pattern_key = "migrated-" + hashlib.sha256(
                lesson.failure_mode.encode("utf-8")
            ).hexdigest()[:12]
            cluster_id = "lesson_cluster_" + hashlib.sha256(
                f"{lesson.skill_name or 'unbound'}\n{lesson.workflow_key}\n{pattern_key}".encode(
                    "utf-8"
                )
            ).hexdigest()[:20]
            cluster = self.store.upsert_cluster(
                LessonCluster(
                    cluster_id=cluster_id,
                    skill_name=lesson.skill_name,
                    skill_version_spec=lesson.skill_version_spec,
                    workflow_key=lesson.workflow_key,
                    pattern_key=pattern_key,
                    canonical_pattern=lesson.failure_mode,
                    applies_when=lesson.applies_when,
                    does_not_apply_when=lesson.does_not_apply_when,
                    recovery_principles=[lesson.recommendation],
                    migration_seed_lesson_id=lesson.lesson_id,
                )
            )
            for source_episode_id in lesson.source_episode_ids:
                episode = self.store.get_episode(source_episode_id)
                if episode is None:
                    continue
                observation = FailureObservation(
                    observation_id="failure_observation_"
                    + hashlib.sha256(
                        f"{episode.episode_id}\n{cluster.cluster_id}".encode("utf-8")
                    ).hexdigest()[:20],
                    episode_id=episode.episode_id,
                    root_task_id=episode.root_task_id,
                    skill_name=lesson.skill_name,
                    skill_version_spec=lesson.skill_version_spec,
                    workflow_key=lesson.workflow_key,
                    cluster_id=cluster.cluster_id,
                    pattern_key=pattern_key,
                    pattern_summary=lesson.failure_mode,
                    applies_when=lesson.applies_when,
                    does_not_apply_when=lesson.does_not_apply_when,
                    recovery_principle=lesson.recommendation,
                )
                cluster, _ = self.store.add_observation(observation, cluster)

            def deactivate(current: ScopedLesson) -> None:
                current.status = "inactive"

            self.store.update_lesson(
                lesson.lesson_id,
                deactivate,
                event_type="lesson_migrated_to_cluster",
            )
            if lesson.skill_name:
                touched.add(lesson.skill_name)
            if len(cluster.supporting_root_task_ids) >= self.min_lesson_episodes:
                self.store.enqueue_cluster_job(cluster.cluster_id)
                scheduled.add(cluster.cluster_id)
        self.store.set_metadata("lesson_cluster_migration_v1", "1")
        for skill_name in touched:
            self.write_lesson_projection(skill_name)
        return scheduled

    @staticmethod
    def _validate_skill_document(content: str, expected_name: str) -> None:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillEvolutionError("Skill is missing YAML frontmatter")
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise SkillEvolutionError("Skill frontmatter is not closed") from exc
        metadata = yaml.safe_load("\n".join(lines[1:end]))
        if not isinstance(metadata, dict):
            raise SkillEvolutionError("Skill frontmatter must be an object")
        if metadata.get("name") != expected_name:
            raise SkillEvolutionError("Skill frontmatter name does not match its directory")
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip():
            raise SkillEvolutionError("Skill description is required")
        if len(description) > 1024 or "<" in description or ">" in description:
            raise SkillEvolutionError("Skill description is invalid")
        if metadata.get("always") is True:
            raise SkillEvolutionError("evolved Skills cannot be always-on")
        if content.count(_MANAGED_START) != 1 or content.count(_MANAGED_END) != 1:
            raise SkillEvolutionError("Skill must contain exactly one PAOS managed block")

    def write_lesson_projection(self, skill_name: str) -> None:
        lessons = self.store.list_lessons(skill_name=skill_name)
        clusters = self.store.list_clusters(skill_name=skill_name)
        path = self.workspace / "skills" / skill_name / "references" / "LESSONS.md"
        lines = [
            "# Skill Lessons",
            "",
            "Generated from the PAOS experience ledger. Do not use these as global constraints.",
        ]
        for status in ("active", "superseded", "retired", "inactive"):
            selected = [item for item in lessons if item.status == status]
            if not selected:
                continue
            lines.extend(["", f"## {status.title()}", ""])
            for lesson in selected:
                lines.extend(
                    [
                        f"### {lesson.lesson_id}",
                        "",
                        f"- Applies when: {'; '.join(lesson.applies_when)}",
                        f"- Does not apply when: {'; '.join(lesson.does_not_apply_when)}",
                        f"- Failure mode: {lesson.failure_mode}",
                        f"- Recommendation: {lesson.recommendation}",
                        f"- Observations: {lesson.observation_count}",
                        "",
                    ]
                )
        for status in ("collecting", "blocked"):
            selected_clusters = [item for item in clusters if item.status == status]
            if not selected_clusters:
                continue
            lines.extend(["", f"## {status.title()} Clusters", ""])
            for cluster in selected_clusters:
                lines.extend(
                    [
                        f"### {cluster.cluster_id}",
                        "",
                        f"- Pattern: {cluster.canonical_pattern}",
                        f"- Workflow: {cluster.workflow_key}",
                        "- Independent support: "
                        f"{len(cluster.supporting_root_task_ids)}/{self.min_lesson_episodes}",
                        f"- Validation: {'; '.join(cluster.validation_errors) or 'pending'}",
                        "",
                    ]
                )
        atomic_write_text(path, "\n".join(lines).rstrip() + "\n")

"""Online binding and asynchronous task-level experience orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from PhyAgentOS.agent.experience.activation import SkillActivationManager
from PhyAgentOS.agent.experience.analyzer import ExperienceAnalyzer
from PhyAgentOS.agent.experience.contracts import (
    ScopedLesson,
    SkillActivation,
    TaskEpisode,
    WorkflowTraceItem,
    utc_now,
)
from PhyAgentOS.agent.experience.evolution import SkillEvolutionError, SkillEvolutionManager
from PhyAgentOS.agent.experience.redaction import redact_text
from PhyAgentOS.agent.experience.source import (
    AgentTaskOutcomeSource,
    ForgeTaskOutcomeSource,
)
from PhyAgentOS.agent.experience.store import ExperienceStore


class ExperienceCoordinator:
    def __init__(
        self,
        *,
        workspace: str | Path,
        analyzer: ExperienceAnalyzer,
        forge_orchestrator=None,
        task_coordinator=None,
        runtime_availability_provider: Callable[[str], bool] | None = None,
        binding_resolver=None,
        min_successful_episodes: int = 3,
        min_lesson_episodes: int = 3,
        max_lessons_per_skill: int = 8,
        max_calls: int = 20,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.store = ExperienceStore(self.workspace)
        self.activation = SkillActivationManager(
            workspace=self.workspace,
            store=self.store,
            max_lessons_per_skill=max_lessons_per_skill,
            runtime_availability_provider=runtime_availability_provider,
            binding_resolver=binding_resolver,
        )
        self.analyzer = analyzer
        self.evolution = SkillEvolutionManager(
            workspace=self.workspace,
            store=self.store,
            min_successful_episodes=min_successful_episodes,
            min_lesson_episodes=min_lesson_episodes,
        )
        if task_coordinator is not None:
            self.outcome_source = AgentTaskOutcomeSource(task_coordinator)
        elif forge_orchestrator is not None:
            self.outcome_source = ForgeTaskOutcomeSource(forge_orchestrator)
        else:
            self.outcome_source = None
        self.max_calls = max(0, int(max_calls))
        self.calls = 0
        self._tasks: dict[str, asyncio.Task] = {}
        self._cluster_tasks: dict[str, asyncio.Task] = {}
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._import_legacy_lessons()
            self.evolution.migrate_active_lessons()
            for root_task_id in self.store.pending_jobs():
                self._schedule_job(root_task_id)
            for cluster_id in self.store.pending_cluster_jobs():
                self._schedule_cluster_job(cluster_id)
        except Exception as exc:
            logger.warning(
                "Experience startup failed open: error_type={}", type(exc).__name__
            )

    def stop(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._cluster_tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._cluster_tasks.clear()

    def begin_turn(self, session_key: str, task_summary: str) -> None:
        self.activation.begin_turn(session_key, task_summary)

    def record_tool(self, session_key: str, name: str, arguments: Any) -> None:
        self.activation.record_tool(session_key, name, arguments)

    def bind_forge_task(
        self,
        root_task_id: str,
        *,
        session_key: str,
        forge_binding: Any | None = None,
    ) -> None:
        try:
            snapshot = self.activation.snapshot(session_key)
            if forge_binding is not None:
                snapshot["forge_skill_binding"] = forge_binding.model_dump(mode="json")
            self.store.save_binding(root_task_id, snapshot)
        except Exception as exc:
            logger.warning(
                "Experience binding failed open for {}: error_type={}",
                root_task_id,
                type(exc).__name__,
            )

    def verification_lessons_for_root(self, root_task_id: str) -> str:
        """Return the scoped Lessons frozen when the root Forge task was bound."""
        try:
            binding = self.store.get_binding(root_task_id) or {}
            lessons = binding.get("verification_lessons", [])
            if not isinstance(lessons, list):
                return "[]"
            safe_lessons = [item for item in lessons if isinstance(item, dict)]
            return json.dumps(safe_lessons, ensure_ascii=False)
        except Exception as exc:
            logger.warning(
                "Verification Lesson lookup failed open for {}: error_type={}",
                root_task_id,
                type(exc).__name__,
            )
            return "[]"

    def schedule_forge_completion(self, task_ref: str) -> None:
        """Persist an episode synchronously, then schedule reflection without awaiting it."""
        if self.outcome_source is None:
            return
        try:
            outcome = self.outcome_source.build(task_ref)
            if not outcome.learnable:
                self.store.record_event(
                    "outcome_ignored",
                    outcome.root_task_id,
                    {
                        "source": outcome.source,
                        "verdict": outcome.final_verdict or "unavailable",
                    },
                )
                return
            binding = self.store.get_binding(outcome.root_task_id) or {}
            activations = [
                SkillActivation.model_validate(item)
                for item in binding.get("skill_activations", [])
            ]
            trace = [
                WorkflowTraceItem.model_validate(item)
                for item in binding.get("workflow_trace", [])
            ]
            episode = TaskEpisode(
                episode_id="episode_"
                + hashlib.sha256(outcome.root_task_id.encode("utf-8")).hexdigest()[:20],
                root_task_id=outcome.root_task_id,
                source=outcome.source,
                task_summary=redact_text(
                    (binding.get("task_summary") or outcome.goal).strip()
                ),
                goal=outcome.goal,
                success_criteria=outcome.success_criteria,
                skill_activations=activations,
                primary_skill_binding_id=(
                    binding.get("forge_skill_binding", {}).get("binding_id")
                    if isinstance(binding.get("forge_skill_binding"), dict)
                    else None
                ),
                primary_skill_version=(
                    binding.get("forge_skill_binding", {}).get("skill_version")
                    if isinstance(binding.get("forge_skill_binding"), dict)
                    else None
                ),
                skill_document_sha256=(
                    # The episode must be attributed to the document the agent
                    # actually read (workflow overlay first), not the installed
                    # bundle document when the two differ.
                    (
                        binding.get("forge_skill_binding", {}).get("workflow_document_sha256")
                        or binding.get("forge_skill_binding", {}).get("skill_document_sha256")
                    )
                    if isinstance(binding.get("forge_skill_binding"), dict)
                    else None
                ),
                workflow_trace=trace,
                outcome=outcome,
                agent_task_ref=outcome.agent_task_ref,
                tool_invocation_refs=outcome.tool_invocation_refs,
                processing_status="pending",
            )
            created = self.store.create_episode(episode, enqueue=True)
            if created:
                self._schedule_job(outcome.root_task_id)
        except Exception as exc:
            logger.warning(
                "Experience capture failed open for {}: error_type={}",
                task_ref,
                type(exc).__name__,
            )

    def _schedule_job(self, root_task_id: str) -> None:
        current = self._tasks.get(root_task_id)
        if current is not None and not current.done():
            return
        try:
            task = asyncio.create_task(
                self._process_job(root_task_id),
                name=f"experience-{root_task_id}",
            )
        except RuntimeError:
            return
        self._tasks[root_task_id] = task
        task.add_done_callback(lambda done, root=root_task_id: self._job_done(root, done))

    def _job_done(self, root_task_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(root_task_id) is task:
            self._tasks.pop(root_task_id, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    async def _process_job(self, root_task_id: str) -> None:
        if not self._claim_evolution_call():
            self.store.defer_job(root_task_id)
            self.store.record_event("evolution_budget_deferred", root_task_id)
            return
        if not self.store.start_job(root_task_id):
            return
        try:
            episode = self.store.get_episode_by_root(root_task_id)
            assessment = await self.analyzer.assess(
                episode,
                candidates=self.store.list_candidates(active_only=True),
                lessons=self.store.list_lessons(status="active"),
                clusters=self.store.list_clusters(),
                skill_catalog=self.activation.skills.build_evolution_catalog(
                    {item.skill_name for item in episode.skill_activations}
                ),
            )
            self.store.record_event(
                "assessment_completed",
                episode.episode_id,
                {"outcome": assessment.outcome, "reusable": assessment.reusable},
            )
            scheduled_clusters = self.evolution.apply(episode, assessment)
            episode.processing_status = "processed"
            episode.processed_at = utc_now()
            self.store.update_episode(episode)
            self.store.finish_job(root_task_id)
            for cluster_id in scheduled_clusters:
                self._schedule_cluster_job(cluster_id)
        except asyncio.CancelledError:
            self.store.fail_job(root_task_id, "evolution task cancelled", retry=True)
            raise
        except Exception as exc:
            attempts = self.store.job_attempts(root_task_id)
            retry = attempts < 3
            error_type = type(exc).__name__
            self.store.fail_job(root_task_id, error_type, retry=retry)
            logger.warning(
                "Experience evolution failed open for {}: error_type={}",
                root_task_id,
                error_type,
            )
            if retry:
                await asyncio.sleep(1.0)
                self._tasks.pop(root_task_id, None)
                self._schedule_job(root_task_id)

    def _claim_evolution_call(self) -> bool:
        if self.max_calls and self.calls >= self.max_calls:
            return False
        self.calls += 1
        return True

    def _schedule_cluster_job(self, cluster_id: str) -> None:
        current = self._cluster_tasks.get(cluster_id)
        if current is not None and not current.done():
            return
        try:
            task = asyncio.create_task(
                self._process_cluster_job(cluster_id),
                name=f"lesson-cluster-{cluster_id}",
            )
        except RuntimeError:
            return
        self._cluster_tasks[cluster_id] = task
        task.add_done_callback(
            lambda done, cid=cluster_id: self._cluster_job_done(cid, done)
        )

    def _cluster_job_done(self, cluster_id: str, task: asyncio.Task) -> None:
        if self._cluster_tasks.get(cluster_id) is task:
            self._cluster_tasks.pop(cluster_id, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    async def _process_cluster_job(self, cluster_id: str) -> None:
        if not self.store.start_cluster_job(cluster_id):
            return
        try:
            cluster = self.store.get_cluster(cluster_id)
            if cluster is None:
                self.store.finish_cluster_job(cluster_id)
                return
            if cluster.status == "activated":
                self.store.finish_cluster_job(cluster_id)
                return
            observations = self.store.list_observations(cluster_id)
            if len(cluster.supporting_root_task_ids) < self.evolution.min_lesson_episodes:
                self.store.finish_cluster_job(cluster_id)
                return
            draft = cluster.draft
            if draft is None:
                if not self._claim_evolution_call():
                    self.store.defer_cluster_job(cluster_id)
                    self.store.record_event("evolution_budget_deferred", cluster_id)
                    return
                draft = await self.analyzer.synthesize_lesson(cluster, observations)
                cluster.draft = draft
                self.store.update_cluster(
                    cluster, event_type="lesson_synthesis_completed"
                )
            try:
                self.evolution.validate_cluster_draft(cluster, draft)
            except SkillEvolutionError as exc:
                self.evolution.block_cluster(cluster, [str(exc)], draft=draft)
                self.store.finish_cluster_job(cluster_id)
                return
            if not self._claim_evolution_call():
                self.store.defer_cluster_job(cluster_id)
                self.store.record_event("evolution_budget_deferred", cluster_id)
                return
            validation = await self.analyzer.validate_lesson_abstraction(
                cluster, observations, draft
            )
            try:
                self.evolution.activate_cluster(cluster, draft, validation)
            except SkillEvolutionError as exc:
                self.evolution.block_cluster(
                    cluster,
                    [str(exc)],
                    draft=draft,
                    validation=validation,
                )
            self.store.finish_cluster_job(cluster_id)
        except asyncio.CancelledError:
            self.store.fail_cluster_job(
                cluster_id, "cluster evolution cancelled", retry=True
            )
            raise
        except Exception as exc:
            attempts = self.store.cluster_job_attempts(cluster_id)
            retry = attempts < 3
            error_type = type(exc).__name__
            self.store.fail_cluster_job(cluster_id, error_type, retry=retry)
            logger.warning(
                "Lesson cluster evolution failed open for {}: error_type={}",
                cluster_id,
                error_type,
            )
            if retry:
                await asyncio.sleep(1.0)
                self._cluster_tasks.pop(cluster_id, None)
                self._schedule_cluster_job(cluster_id)

    def _import_legacy_lessons(self) -> None:
        if self.store.metadata("legacy_lessons_imported") == "1":
            return
        path = self.workspace / "LESSONS.md"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            matches = re.findall(r"^- Lesson:\s*(.+)$", text, flags=re.MULTILINE)
            for summary in matches:
                digest = hashlib.sha256(summary.strip().encode("utf-8")).hexdigest()[:20]
                self.store.upsert_lesson(
                    ScopedLesson(
                        lesson_id=f"legacy_{digest}",
                        workflow_key="legacy-unbound",
                        applies_when=["Legacy applicability is unknown"],
                        does_not_apply_when=["Until explicitly bound to a matching Skill"],
                        failure_mode=summary.strip(),
                        recommendation=summary.strip(),
                        status="inactive",
                        source_episode_ids=["legacy-import"],
                    )
                )
        self.store.set_metadata("legacy_lessons_imported", "1")

#!/usr/bin/env python3
"""Drive PAOS's real experience + evolution pipeline from one real RoboTwin run.

Why the outcome arrives through a TaskOutcomeSource
---------------------------------------------------
The robotwin profile cannot produce a Forge verdict: its gateway.yaml declares
`joint_order: []` / `image_input_ids: []`, so the Gateway publishes no /ws/images
or /ws/state stream, ForgeObservationCollector captures no snapshot, the Evidence
Bundle stays incomplete and `final_verdict` stays None. Every episode is then
dropped as `outcome_ignored` (131 such tasks, 226 events, 0 episodes before this
work). The documented seam for that case is the TaskOutcomeSource protocol
(docs/zh/05-agent-experience-and-skill-evolution.md section 10); this script
implements it with the benchmark's own result JSON as the verdict.

Evidence used (all real, nothing synthesized)
---------------------------------------------
* session transcript  -> replayed into the SkillActivationManager exactly like
  the agent loop does (begin_turn -> record_tool per call -> activate ->
  bind_forge_task), so the episode carries the real workflow trace;
* session tool results -> per-call lineage plus a mechanical supervision summary;
* benchmark result JSON -> terminal verdict of the episode.

Two kinds of success criterion are registered, both evaluated from the run's own
records:
1. task outcome   - the benchmark result JSON reports success_rate == 1;
2. process evidence - whether the supervision plan actually covered the
   episode's stall phase. This is reported as a diagnostic metric, not folded
   into benchmark success: a successful episode must remain successful even if
   no C2 checkpoint was needed.

Everything after that is unmodified PAOS: the real ExperienceCoordinator, the
real SkillEvolutionManager and the real model-backed reflection (RobustAnalyzer
only adds schema repair on top of the upstream analyzer).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PhyAgentOS.agent.experience.contracts import TaskOutcomeEnvelope
from PhyAgentOS.agent.experience.coordinator import ExperienceCoordinator
from PhyAgentOS.agent.experience.redaction import redact_text
from PhyAgentOS.cli.commands import _make_provider
from PhyAgentOS.config.loader import load_config
from robotwin_evidence import (
    as_dict,
    benchmark_succeeded,
    require_valid_episode,
    build_lineage,
    load_tool_calls,
    session_task_summary,
    terminal_lineage,
    tool_outputs,
    verification_contract,
)
from robust_analyzer import RobustAnalyzer
from supervision_summary import summary_lineage

DEFAULT_GOAL = (
    "Run exactly one supervised RoboTwin episode for {task} via pi0.5 with checkpoint "
    "supervision, and report the benchmark outcome truthfully."
)
DEFAULT_CRITERION = "the benchmark result JSON for this single episode reports success_rate == 1"
STALL_CRITERION = (
    "supervision covers the episode's stall phase: at least one checkpoint decision is taken "
    "while the checkpoint reason is no_progress (C2)"
)


def stall_phase_covered(calls) -> bool:
    """True when the run spent at least one decision on a no_progress checkpoint."""
    for call in calls:
        if call.name != "forge_tool_query" or call.inner_tool != "vla.decide":
            continue
        closed = as_dict(tool_outputs(call).get("checkpoint_closed"))
        if str(closed.get("trigger") or "").upper() == "C2":
            return True
        if str(closed.get("reason") or "") == "no_progress":
            return True
    return False


class RoboTwinOutcomeSource:
    """TaskOutcomeSource backed by one real run: session trace + benchmark result."""

    def __init__(self, calls, result: dict, task_name: str) -> None:
        self.calls = calls
        self.result = result
        self.task_name = task_name

    def build(self, task_ref: str) -> TaskOutcomeEnvelope:
        goal, criteria, association = verification_contract(self.calls)
        goal = goal or DEFAULT_GOAL.format(task=self.task_name)
        criteria = list(criteria) or [DEFAULT_CRITERION]
        outcome_ok = benchmark_succeeded(self.result)
        statuses = {
            redact_text(item): ("satisfied" if outcome_ok else "unsatisfied")
            for item in criteria
        }
        succeeded = outcome_ok
        lineage = build_lineage(self.calls)
        aggregate = summary_lineage(self.calls)
        if aggregate is not None:
            lineage.append(aggregate)
        lineage.append(terminal_lineage(self.result, task_name=self.task_name))
        return TaskOutcomeEnvelope(
            task_id=task_ref,
            root_task_id=task_ref,
            source="robotwin_benchmark",
            goal=redact_text(goal),
            success_criteria=[redact_text(item) for item in criteria],
            final_verdict="success" if succeeded else "failure",
            criteria_statuses=statuses,
            lineage=lineage,
            record_refs=[f"robotwin:{redact_text(self.task_name)}"]
            + ([f"evidence-association:{association}"] if association else []),
        )


async def drain(coordinator: ExperienceCoordinator, timeout: float = 900.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        tasks = list(coordinator._tasks.values()) + list(coordinator._cluster_tasks.values())
        if not any(not task.done() for task in tasks):
            await asyncio.sleep(0.5)
            tasks = list(coordinator._tasks.values()) + list(coordinator._cluster_tasks.values())
            if not any(not task.done() for task in tasks):
                return
        await asyncio.sleep(0.5)


def print_ledger(coordinator: ExperienceCoordinator, skill: str) -> None:
    print("[drive] --- ledger ---")
    for cluster in coordinator.store.list_clusters():
        print(
            f"[drive]   cluster {cluster.cluster_id} skill={cluster.skill_name} "
            f"workflow={cluster.workflow_key} pattern={cluster.pattern_key} "
            f"status={cluster.status} support={len(cluster.supporting_root_task_ids)} "
            f"obs={len(cluster.observation_ids)} errors={len(cluster.validation_errors)}"
        )
    for lesson in coordinator.store.list_lessons(skill_name=skill):
        print(f"[drive]   lesson {lesson.lesson_id} status={lesson.status} severity={lesson.severity}")
        print(f"[drive]     workflow_key: {lesson.workflow_key}")
        print(f"[drive]     applies_when: {'; '.join(lesson.applies_when)}")
        print(f"[drive]     does_not_apply_when: {'; '.join(lesson.does_not_apply_when)}")
        print(f"[drive]     failure_mode: {lesson.failure_mode}")
        print(f"[drive]     recommendation: {lesson.recommendation}")
    print(f"[drive]   pending_jobs={coordinator.store.pending_jobs()}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.path.expanduser("~/.PhyAgentOS/config.rsi.json"))
    parser.add_argument("--session", required=True, help="agent session .jsonl of the run")
    parser.add_argument("--result", required=True, help="benchmark result JSON of the run")
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--root", required=True, help="root task id for this episode")
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--skill", default="robotwin-benchmark-3")
    parser.add_argument("--report-out", default="")
    args = parser.parse_args()

    calls = load_tool_calls(args.session)
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    require_valid_episode(result)
    task_summary = session_task_summary(args.session)
    if not task_summary:
        raise SystemExit(f"session {args.session} carries no user prompt")

    cfg = load_config(Path(args.config))
    evolution = cfg.agents.evolution
    provider = _make_provider(cfg)
    model = evolution.model or cfg.agents.defaults.model

    print(f"[drive] workspace={cfg.workspace_path}")
    print(f"[drive] session={args.session}")
    print(f"[drive] replayed tool calls={len(calls)}")
    print(f"[drive] evolution enabled={evolution.enabled} minLessonEpisodes={evolution.min_lesson_episodes}")
    print(f"[drive] reflection model={model}")
    print(f"[drive] bench status={result.get('status')} success_rate={result.get('success_rate')} "
          f"num_steps={result.get('num_steps')}")
    print(f"[drive] stall-phase criterion satisfied={stall_phase_covered(calls)}")

    coordinator = ExperienceCoordinator(
        workspace=cfg.workspace_path,
        analyzer=RobustAnalyzer(provider=provider, model=model, timeout_s=300.0),
        min_successful_episodes=evolution.min_successful_episodes,
        min_lesson_episodes=evolution.min_lesson_episodes,
        max_lessons_per_skill=evolution.max_lessons_per_skill,
        max_calls=evolution.max_evolution_calls_per_run,
    )
    coordinator.outcome_source = RoboTwinOutcomeSource(calls, result, args.task_name)
    await coordinator.start()

    coordinator.begin_turn(args.session_key, task_summary)
    activation = None
    lessons = []
    for call in calls:
        coordinator.record_tool(args.session_key, call.name, call.arguments)
        if call.name == "activate_skill":
            activation, _document, lessons = coordinator.activation.activate(
                session_key=args.session_key,
                name=str(call.arguments.get("name") or args.skill),
                role=str(call.arguments.get("role") or "primary"),
            )
            print(f"[drive] activate_skill({call.arguments.get('name')}) at call "
                  f"#{call.index} -> lessons={len(lessons)}")
    if activation is None:
        activation, _document, lessons = coordinator.activation.activate(
            session_key=args.session_key, name=args.skill, role="primary"
        )
    print(f"[drive] lessons visible at turn start: {len(lessons)}")
    coordinator.bind_forge_task(args.root, session_key=args.session_key)
    coordinator.schedule_forge_completion(args.root)
    await drain(coordinator)

    print_ledger(coordinator, args.skill)

    workspace = Path(cfg.workspace_path)
    print("[drive] --- LESSONS.md projection ---")
    for candidate in (
        workspace / "skills" / args.skill / "references" / "LESSONS.md",
        workspace / "LESSONS.md",
    ):
        if candidate.is_file():
            body = candidate.read_text(encoding="utf-8")
            print(f"[drive] {candidate} ({len(body)} bytes)")
            print(body[:3000])
        else:
            print(f"[drive] {candidate} (missing)")

    probe_key = f"{args.session_key}:next-turn"
    coordinator.begin_turn(probe_key, task_summary)
    _activation, _document, next_lessons = coordinator.activation.activate(
        session_key=probe_key, name=args.skill, role="primary"
    )
    print(f"[drive] --- next-turn retrieval (same prompt, fresh session) -> {len(next_lessons)} lesson(s) ---")
    for lesson in next_lessons:
        print(f"[drive]   {lesson.lesson_id} severity={lesson.severity} "
              f"failure_mode={lesson.failure_mode}")
        print(f"[drive]     applies_when: {'; '.join(lesson.applies_when)}")
        print(f"[drive]     recommendation: {lesson.recommendation}")

    if args.report_out:
        report = {
            "session": str(args.session),
            "result_file": str(args.result),
            "task_name": args.task_name,
            "root_task_id": args.root,
            "tool_calls": len(calls),
            "stall_phase_covered": stall_phase_covered(calls),
            "bench": {
                "status": result.get("status"),
                "success_rate": result.get("success_rate"),
                "num_steps": result.get("num_steps"),
            },
            "lessons": [lesson.model_dump(mode="json") for lesson in next_lessons],
            "clusters": [cluster.model_dump(mode="json") for cluster in coordinator.store.list_clusters()],
            "all_lessons": [
                lesson.model_dump(mode="json") for lesson in coordinator.store.list_lessons()
            ],
        }
        Path(args.report_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[drive] report written to {args.report_out}")

    coordinator.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Experiment-local lessons; case support never changes global root thresholds."""

import asyncio

from PhyAgentOS.agent.experience.contracts import LessonAbstractionValidation
from PhyAgentOS.forge.benchmark.contracts import LessonCandidate
from PhyAgentOS.forge.interaction.contracts import digest


async def reflect_round(service, task_id, round_id):
    store, deployment = service.store, service.deployment
    current = store.get("round", round_id)
    if current.get("reflection_complete"):
        return current["next_lessons"]
    existing = current["lessons"]
    children = [c for c in store.list("subtask", task_id) if c["round_id"] == round_id]
    if not children or any(not c["settled"] for c in children):
        raise RuntimeError("round barrier requires all episodes settled")
    next_lessons = list(existing)
    if deployment.reflect and deployment.validate_lesson:
        for attempt in range(2):
            key = round_id + f":reflection:{attempt}"
            if store.get("reflection", key):
                continue  # A crashed model attempt consumes budget too.
            store.put("reflection", key, task_id, {"id": key, "status": "started"}, create=True)
            try:
                raw = await asyncio.wait_for(
                    deployment.reflect(
                        {
                            "episodes": children,
                            "lessons": existing,
                            "scope": "experiment_local",
                        }
                    ),
                    timeout=service.store.binding(task_id)["plan"]["model_timeout_s"],
                )
                if not isinstance(raw, list) or len(raw) > 8:
                    raise ValueError("reflection must return at most eight lesson candidates")
                approved = []
                for item in raw:
                    candidate = LessonCandidate.model_validate(item)
                    support = [
                        c
                        for c in children
                        if c["subtask_id"] in candidate.supporting_subtasks
                        and c["outcome"] == "failure"
                        and c.get("history_digest") == digest(c.get("history"))
                    ]
                    if (
                        set(candidate.supporting_subtasks) != {c["subtask_id"] for c in support}
                        or len({c["case"]["case_key"] for c in support}) < 2
                    ):
                        continue
                    validation = LessonAbstractionValidation.model_validate(
                        await asyncio.wait_for(
                            deployment.validate_lesson(candidate.model_dump(), support),
                            timeout=service.store.binding(task_id)["plan"]["model_timeout_s"],
                        )
                    )
                    if (
                        not validation.reusable
                        or validation.contains_specific_answer
                        or validation.unsupported_literals
                        or validation.confidence < 0.8
                    ):
                        continue
                    lesson = candidate.model_dump()
                    lesson["supporting_case_keys"] = sorted(
                        {c["case"]["case_key"] for c in support}
                    )
                    lesson["validation"] = validation.model_dump(mode="json")
                    lesson["lesson_id"] = digest(
                        {
                            k: lesson[k]
                            for k in (
                                "workflow_key",
                                "principle",
                                "applies_when",
                                "does_not_apply_when",
                            )
                        }
                    )
                    approved.append(lesson)
                by_id = {v["lesson_id"]: v for v in existing}
                for lesson in sorted(approved, key=lambda v: v["lesson_id"]):
                    by_id.setdefault(lesson["lesson_id"], lesson)
                next_lessons = [by_id[k] for k in sorted(by_id)]
                store.update("reflection", key, status="completed", approved=approved)
                break
            except Exception as exc:
                store.update("reflection", key, status="failed", error=type(exc).__name__)
    store.update(
        "round",
        round_id,
        reflection_complete=True,
        next_lessons=next_lessons,
        reflection_status="updated" if next_lessons != existing else "no_update",
    )
    return next_lessons

"""Deterministic completeness verification, independent of model finish claims."""

import hashlib
import json
from collections import Counter

from PhyAgentOS.forge.interaction.contracts import digest
from PhyAgentOS.utils.atomic_file import atomic_write_bytes, atomic_write_text
from PhyAgentOS.verification.contracts import (
    CriterionVerdict,
    EvidenceArtifact,
    EvidenceBundle,
    EvidenceQuality,
    VerificationAttempt,
    VerificationVerdict,
    utc_now,
)


def reconciled_intent(intent):
    proof = intent.get("reconciliation")
    if not proof or not intent.get("receipt"):
        return False
    from PhyAgentOS.forge.interaction.client import invocation_outputs

    return (
        proof.get("gateway_status") == "succeeded"
        and proof.get("receipt_digest") == digest(intent["receipt"])
        and invocation_outputs(proof["result_response"]) == intent["receipt"]
    )


def unknowns_resolved(service, task_id):
    intents = service.store.list("intent", task_id)
    task = service.coordinator.get_task(task_id)
    return all(
        record.semantics == "action"
        and any(
            record.arguments == intent["arguments"] and reconciled_intent(intent)
            for intent in intents
        )
        for record in task.execution_records
        if record.status == "unknown"
    )


def checked_report(service, task_id):
    store = service.store
    run, binding = store.get("run", task_id), store.binding(task_id)
    service.assert_settled(task_id)
    if not run or digest(run.get("final_root")) != run.get("final_root_digest"):
        raise RuntimeError("missing or changed root evidence")
    task = service.coordinator.get_task(task_id)
    if (
        run["binding_digest"] != binding["binding_digest"]
        or binding["binding_id"] != task.primary_skill_binding.binding_id
    ):
        raise RuntimeError("benchmark evidence binding mismatch")
    plan = binding["plan"]
    children = store.list("subtask", task_id)
    rounds = store.list("round", task_id)
    if len(rounds) != plan["rounds"] or len(children) != len(plan["cases"]) * plan["rounds"]:
        raise RuntimeError("benchmark manifest coverage incomplete")
    reports = []
    for round_state in rounds:
        group = [c for c in children if c["round_id"] == round_state["round_id"]]
        if (
            round_state["state"] != "complete"
            or not round_state["reflection_complete"]
            or sorted(c["case"]["case_key"] for c in group)
            != sorted(c["case_key"] for c in plan["cases"])
        ):
            raise RuntimeError("round manifest incomplete")
        for child in group:
            history = child.get("history")
            if (
                not child["settled"]
                or not history
                or digest(history) != child.get("history_digest")
                or child["lessons_digest"] != digest(round_state["lessons"])
            ):
                raise RuntimeError("missing or corrupt episode trajectory")
            prior = -1
            for snapshot in history:
                if any(
                    snapshot.get(k) != child[k]
                    for k in (
                        "task_id",
                        "run_id",
                        "subtask_id",
                        "attempt_id",
                        "worker_id",
                        "world_epoch",
                        "binding_digest",
                    )
                ):
                    raise RuntimeError("trajectory association mismatch")
                if snapshot["observation_version"] < prior:
                    raise RuntimeError("trajectory rollback")
                prior = snapshot["observation_version"]
            if (
                history[-1]["outcome"] != child["outcome"]
                or not history[-1]["stopped"]
                or history[-1]["state"] != "terminal"
            ):
                raise RuntimeError("terminal evidence mismatch")
            intents = [
                i
                for i in store.list("intent", task_id)
                if i["arguments"].get("subtask_id") == child["subtask_id"]
            ]
            if not intents or any(
                not i["receipt"]
                or i["digest"] != digest(i["arguments"])
                or i["receipt"].get("payload_digest") != i["digest"]
                for i in intents
            ):
                raise RuntimeError("incomplete invocation receipt evidence")
            from .client import SCOPE_KEYS

            for intent in intents:
                if intent["receipt"].get("request_id") != intent["request_id"] or any(
                    intent["receipt"].get(k) != child[k] for k in SCOPE_KEYS
                ):
                    raise RuntimeError("receipt association mismatch")
            observed = [
                snap
                for snap in store.list("snapshot", task_id)
                if snap["subtask_id"] == child["subtask_id"]
            ]
            if any(
                {k: v for k, v in snap.items() if k != "history_digest"} not in history
                for snap in observed
            ):
                raise RuntimeError("trajectory omitted an observed state")
            # Every ordinary dispatch must match a real Coordinator intention.
            for intent in intents:
                if intent["arguments"]["operation"] == "episode.stop":
                    continue
                if not any(
                    r.arguments == intent["arguments"]
                    and (
                        (r.invocation_id == intent["invocation_id"] and r.status == "succeeded")
                        or (r.status == "unknown" and reconciled_intent(intent))
                    )
                    for r in task.execution_records
                ):
                    raise RuntimeError("missing original Coordinator invocation fact")
        counts = Counter(c["outcome"] for c in group)
        child_ids = {c["subtask_id"] for c in group}
        steps = [
            step for step in store.list("decision", task_id) if step["subtask_id"] in child_ids
        ]
        step_ids = {step["step_id"] for step in steps}
        attempts = [
            attempt for attempt in store.list("model", task_id) if attempt["step_id"] in step_ids
        ]
        usages = [attempt.get("usage") for attempt in attempts]
        tokens = (
            sum(usage["total_tokens"] for usage in usages)
            if all(
                isinstance(usage, dict) and type(usage.get("total_tokens")) is int
                for usage in usages
            )
            else None
        )
        reports.append(
            {
                "round": round_state["number"],
                "denominator": len(plan["cases"]),
                "counts": {
                    k: counts[k]
                    for k in ("success", "failure", "cancelled", "infra_error", "unknown")
                },
                "coverage": (counts["success"] + counts["failure"]) / len(plan["cases"]),
                "accounted_episodes": len(group),
                "decision_steps": len(steps),
                "provider_total_tokens": tokens,
                # One decision callback may make several provider requests (e.g. JSON repair).
                # Absent provider accounting stays unknown, rather than assuming one request.
                "provider_calls": (
                    sum(usage["provider_calls"] for usage in usages)
                    if all(isinstance(usage, dict)
                           and type(usage.get("provider_calls")) is int
                           and usage["provider_calls"] >= 0 for usage in usages)
                    else None
                ),
                "success_rate": counts["success"] / len(plan["cases"]),
                "native_scores": [c.get("native_score") for c in group],
                "model_calls": sum(c["model_calls"] for c in group),
                "elapsed_s": sum(c.get("finished_at", 0) - c.get("started_at", 0) for c in group),
                "lessons_digest": round_state["lessons_digest"],
                "lesson_exposures": len(group) * len(round_state["lessons"]),
                "reflection_status": round_state["reflection_status"],
            }
        )
    return {
        "version": "benchmark_report_v1",
        "task_id": task_id,
        "run_id": run["run_id"],
        "binding_digest": binding["binding_digest"],
        "plan_digest": digest(plan),
        "evaluation_complete": True,
        "environment_all_successful": all(c["outcome"] == "success" for c in children),
        "rounds": reports,
        "episodes": children,
        "model_attempts": store.list("model", task_id),
        "final_lessons": rounds[-1]["next_lessons"],
    }


def write_evidence(service, task_id):
    report = checked_report(service, task_id)
    coordinator = service.coordinator
    root = coordinator.workspace / ".paos" / "benchmark" / "evidence" / task_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / "report.json"
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    atomic_write_bytes(path, raw)
    artifact = EvidenceArtifact(
        artifact_id="benchmark_" + task_id,
        phase="after",
        kind="benchmark_report",
        source_id="benchmark_executor",
        received_at=utc_now(),
        media_type="application/json",
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        uri=path.relative_to(coordinator.workspace).as_posix(),
    )
    bundle = EvidenceBundle(
        bundle_id="benchmark_" + task_id,
        session_id=task_id,
        command_id="agent_task",
        artifacts=[artifact],
        quality=EvidenceQuality(
            complete=True,
            association_quality="authoritative",
            capture_authority="benchmark_adapter",
        ),
    )
    bundle_path = root / "bundle.json"
    atomic_write_text(bundle_path, bundle.model_dump_json())

    def mutate(task):
        task.evidence_bundle_ref = bundle_path.relative_to(coordinator.workspace).as_posix()
        task.after_snapshot_ref = artifact.uri

    coordinator.store.update(
        task_id,
        mutate,
        event_type="benchmark_evidence_captured",
        payload={"report_sha256": artifact.sha256},
    )
    return report


def verify(service, task_id):
    report = checked_report(service, task_id)
    task = service.coordinator.get_task(task_id)
    return (
        VerificationVerdict(
            verdict="success",
            criteria=[
                CriterionVerdict(
                    criterion=c, status="satisfied", evidence_refs=["benchmark_" + task_id]
                )
                for c in task.verification.success_criteria
            ],
            evidence_refs=["benchmark_" + task_id],
            reason="Complete authoritative evaluation; environmental outcomes are reported separately",
            lesson="Evaluation completeness does not establish environmental workflow success",
        ),
        report,
        VerificationAttempt(attempt_id="benchmark_verify_" + task_id, verdict="success"),
    )

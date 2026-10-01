"""Convert checked interaction facts into the existing evidence bundle format."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from PhyAgentOS.utils.atomic_file import atomic_write_bytes, atomic_write_text
from PhyAgentOS.verification.contracts import (
    EvidenceArtifact,
    EvidenceBundle,
    EvidenceCaptureWindow,
    EvidenceQuality,
    utc_now,
)

from .contracts import digest


def checked_trace(service, run):
    evidence = service.store.export_evidence(run["interaction_run_id"])
    task = service.coordinator.get_task(run["task_id"])
    saved = service.store.binding(task.task_id)
    if (
        saved is None
        or saved["binding_id"] != task.primary_skill_binding.binding_id
        or run["start_arguments"]["binding_digest"] != saved["binding_digest"]
        or not run.get("settled")
        or digest(run["last_snapshot"]) != run.get("settled_snapshot_digest")
    ):
        raise RuntimeError("interaction evidence binding or settlement is invalid")
    history = run.get("verified_history")
    if not history or digest(history) != run.get("verified_history_digest"):
        raise RuntimeError("verified executor history is missing or changed")
    step_ids = {s["step_id"] for s in evidence["steps"]}
    records = [
        r
        for r in task.execution_records
        if r.revision_id == run["revision_id"] and r.semantics == "action"
    ]
    if {r.invocation_id for r in records} != {s["invocation_id"] for s in evidence["submissions"]}:
        raise RuntimeError("submission/Coordinator lineage incomplete")
    if any(s["payload"]["step_id"] not in step_ids for s in evidence["submissions"]):
        raise RuntimeError("submitted step evidence missing")
    snapshots = [e for e in evidence["events"] if e["kind"] == "executor_snapshot"]
    if not snapshots:
        raise RuntimeError("interaction snapshot evidence missing")
    prior = -1
    for event in snapshots:
        snapshot = event["data"]
        if any(
            snapshot.get(k) != run["start_arguments"][k]
            for k in (
                "task_id",
                "revision_id",
                "binding_digest",
                "interaction_run_id",
                "world_id",
                "runtime_instance_id",
                "gateway_identity",
            )
        ):
            raise RuntimeError("cross-run evidence")
        cursor = snapshot.get("executor_cursor")
        if type(cursor) is not int or cursor < prior:
            raise RuntimeError("executor cursor rollback or malformed cursor")
        prior = cursor
    for submission in evidence["submissions"]:
        receipt = submission["receipt"]
        payload = submission["payload"]
        expected = digest({k: v for k, v in payload.items() if k != "runner_epoch"})
        if (
            not receipt
            or submission["payload_digest"] != expected
            or receipt.get("payload_digest") != expected
            or any(
                receipt.get(k) != payload[k]
                for k in ("interaction_run_id", "step_id", "submission_id")
            )
        ):
            raise RuntimeError("submission evidence does not match original intent")
    for step in evidence["steps"]:
        if step["status"] in {"succeeded", "failed", "cancelled"} and not step.get(
            "executor_evidence"
        ):
            raise RuntimeError("step has no executor evidence")
    return evidence, snapshots


def write_task_evidence(coordinator, task_id):
    service = coordinator.interaction
    service.assert_settled(task_id)
    task = coordinator.get_task(task_id)
    runs = service.store.runs(task_id)
    artifacts = []
    first_at = None
    terminal_at = None
    root = coordinator.workspace / ".paos" / "interaction" / "evidence" / task_id
    root.mkdir(parents=True, exist_ok=True)
    for run in runs:
        evidence, snapshots = checked_trace(service, run)
        initial = snapshots[0]
        final = snapshots[-1]
        before = datetime.fromtimestamp(initial["created_at"], timezone.utc)
        terminal = datetime.fromtimestamp(run["settled_at"], timezone.utc)
        first_at = min(first_at, before) if first_at else before
        terminal_at = max(terminal_at, terminal) if terminal_at else terminal
        # Store data and source receipt time; do not pretend observations are robot captures.
        for phase, kind, value in [
            ("before", "interaction_observation", initial["data"]),
            ("after", "interaction_observation", final["data"]),
            ("during", "interaction_trace", evidence),
        ]:
            artifact_id = f"{run['interaction_run_id']}_{phase}"
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            path = root / (artifact_id + ".json")
            atomic_write_bytes(path, raw)
            artifacts.append(
                EvidenceArtifact(
                    artifact_id=artifact_id,
                    phase=phase,
                    kind=kind,
                    source_id="interaction_executor",
                    received_at=utc_now(),
                    media_type="application/json",
                    sha256=hashlib.sha256(raw).hexdigest(),
                    byte_size=len(raw),
                    uri=path.relative_to(coordinator.workspace).as_posix(),
                )
            )
    bundle = EvidenceBundle(
        bundle_id="interaction_" + task_id,
        session_id=task_id,
        command_id="agent_task",
        gateway_instance_id=task.primary_skill_binding.gateway_identity,
        capture_window=EvidenceCaptureWindow(
            before_command_at=first_at, command_terminal_at=terminal_at, after_command_at=utc_now()
        ),
        artifacts=artifacts,
        quality=EvidenceQuality(
            complete=True,
            association_quality="authoritative",
            capture_authority="governed_interaction_adapter",
        ),
    )
    path = root / "bundle.json"
    atomic_write_text(path, bundle.model_dump_json())

    def set_bundle(current):
        current.evidence_bundle_ref = path.relative_to(coordinator.workspace).as_posix()
        current.before_snapshot_ref = artifacts[0].uri
        current.after_snapshot_ref = artifacts[-2].uri

    coordinator.store.update(
        task_id,
        set_bundle,
        event_type="interaction_evidence_captured",
        payload={
            "bundle_id": bundle.bundle_id,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    )

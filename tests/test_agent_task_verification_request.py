from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest

from PhyAgentOS.forge.task import AgentTaskRecord, PlanRevision, ToolExecutionRecord
from PhyAgentOS.verification.contracts import (
    EvidenceArtifact,
    EvidenceBundle,
    EvidenceCaptureWindow,
    EvidenceQuality,
    TaskVerificationContract,
    VerificationEvidencePolicy,
    utc_now,
)
from PhyAgentOS.verification.request_builder import (
    VerificationEvidenceError,
    VerificationRequestBuilder,
)


def _strict_agent_task_fixture(
    workspace: Path,
) -> tuple[VerificationRequestBuilder, AgentTaskRecord, Path, Path]:
    artifact_dir = workspace / "artifacts" / "agent_tasks" / "task_strict" / "evidence"
    artifact_dir.mkdir(parents=True)
    now = utc_now()
    artifacts: list[EvidenceArtifact] = []
    tamper_target = artifact_dir / "after_front.png"
    for phase, image_data, state in (
        ("before", b"\x89PNG\r\n\x1a\nbefore", {"joint": 0.0}),
        ("after", b"\x89PNG\r\n\x1a\nafter!", {"joint": 1.0}),
    ):
        image_path = artifact_dir / f"{phase}_front.png"
        image_path.write_bytes(image_data)
        artifacts.append(
            EvidenceArtifact(
                artifact_id=f"artifact_{phase}_image",
                phase=phase,
                kind="rgb_image",
                source_id="front",
                sequence=1 if phase == "before" else 2,
                received_at=now,
                media_type="image/png",
                sha256=hashlib.sha256(image_data).hexdigest(),
                byte_size=len(image_data),
                uri=str(image_path.relative_to(workspace)),
            )
        )
        state_path = artifact_dir / f"{phase}_state.json"
        state_data = json.dumps(state, separators=(",", ":")).encode()
        state_path.write_bytes(state_data)
        artifacts.append(
            EvidenceArtifact(
                artifact_id=f"artifact_{phase}_state",
                phase=phase,
                kind="robot_state",
                source_id="ws/state",
                received_at=now,
                media_type="application/json",
                sha256=hashlib.sha256(state_data).hexdigest(),
                byte_size=len(state_data),
                uri=str(state_path.relative_to(workspace)),
            )
        )

    bundle = EvidenceBundle(
        bundle_id="bundle_strict",
        session_id="task_strict",
        command_id="agent_task",
        capture_window=EvidenceCaptureWindow(
            before_command_at=now,
            command_terminal_at=now + timedelta(seconds=1),
            after_command_at=now + timedelta(seconds=2),
        ),
        artifacts=artifacts,
        quality=EvidenceQuality(complete=True),
    )
    bundle_path = artifact_dir.parent / "evidence_bundle.json"
    bundle_path.write_text(
        json.dumps(bundle.model_dump(mode="json")),
        encoding="utf-8",
    )
    revision = PlanRevision(
        revision_id="revision_1",
        number=1,
        reason="initial plan",
        execution_records=[
            ToolExecutionRecord(
                record_id="record_1",
                revision_id="revision_1",
                tool_id="demo.query",
                semantics="query",
                caller_id="paos:task_strict:revision_1:record_1",
                status="succeeded",
                response={"ok": True, "data": {"value": 1}},
                evidence_refs=["tool:record_1"],
            )
        ],
    )
    task = AgentTaskRecord(
        task_id="task_strict",
        task_description="verify the final robot state",
        verification=TaskVerificationContract(
            mode="enforce",
            goal="Reach the requested robot state.",
            success_criteria=["The final joint value is one."],
            constraints=["Do not infer success from Tool completion alone."],
            evidence_policy=VerificationEvidencePolicy(
                required_kinds=["rgb_image", "robot_state"],
                required_sources=["front"],
            ),
        ),
        revisions=[revision],
        active_revision_id=revision.revision_id,
        evidence_bundle_ref=str(bundle_path.relative_to(workspace)),
    )
    return VerificationRequestBuilder(workspace), task, tamper_target, bundle_path


def test_agent_task_request_uses_strict_evidence_and_complete_context(tmp_path: Path) -> None:
    builder, task, _, _ = _strict_agent_task_fixture(tmp_path)

    request = builder.build_agent_task(
        task,
        events=[{"event_type": "query_finished"}],
        lessons='[{"lesson":"advisory only"}]',
    )

    context = json.loads(request.content[0]["text"].split("\n\n", 1)[1])
    assert request.valid_evidence_refs == frozenset(
        {
            "artifact_before_image",
            "artifact_after_image",
            "artifact_before_state",
            "artifact_after_state",
            "tool:record_1",
        }
    )
    assert context["tool_execution_records"][0]["record_id"] == "record_1"
    assert context["gateway_terminal_results"][0]["status"] == "succeeded"
    assert context["structured_evidence"]["artifact_after_state"] == {"joint": 1.0}
    assert context["constraints"] == [
        "Do not infer success from Tool completion alone."
    ]
    assert context["lessons"] == '[{"lesson":"advisory only"}]'


def test_agent_task_request_accepts_multiple_execution_evidence_refs(tmp_path: Path) -> None:
    builder, task, _, _ = _strict_agent_task_fixture(tmp_path)
    task.revisions[0].execution_records[0].evidence_refs.append(
        "gateway-result:query-1"
    )

    request = builder.build_agent_task(task, events=[], lessons="[]")

    assert "tool:record_1" in request.valid_evidence_refs
    assert "gateway-result:query-1" in request.valid_evidence_refs


def test_agent_task_request_rejects_tampered_artifact(tmp_path: Path) -> None:
    builder, task, tamper_target, _ = _strict_agent_task_fixture(tmp_path)
    original = tamper_target.read_bytes()
    tamper_target.write_bytes(original[:-1] + b"?")

    with pytest.raises(VerificationEvidenceError, match="digest mismatch"):
        builder.build_agent_task(task, events=[], lessons="[]")


def test_agent_task_request_rejects_incomplete_bundle(tmp_path: Path) -> None:
    builder, task, _, bundle_path = _strict_agent_task_fixture(tmp_path)
    bundle = EvidenceBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    bundle.quality.complete = False
    bundle.quality.missing_requirements = ["after:robot_state"]
    bundle_path.write_text(
        json.dumps(bundle.model_dump(mode="json")),
        encoding="utf-8",
    )

    with pytest.raises(VerificationEvidenceError, match="incomplete"):
        builder.build_agent_task(task, events=[], lessons="[]")

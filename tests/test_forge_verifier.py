from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PhyAgentOS.agent.session_verifier import ForgeTaskVerifier
from PhyAgentOS.verification.contracts import (
    EvidenceArtifact,
    EvidenceBundle,
    EvidenceQuality,
    utc_now,
)
from PhyAgentOS.verification.request_builder import VerificationRequest
from PhyAgentOS.verification.service import FORGE_TASK_PROMPT


class ForgeVerifierTests(unittest.TestCase):
    def test_lesson_policy_never_treats_lessons_as_evidence(self) -> None:
        self.assertIn("non-authoritative workflow advisories", FORGE_TASK_PROMPT)
        self.assertIn("never prove that a criterion", FORGE_TASK_PROMPT)
        self.assertIn("a Lesson or Lesson ID as an evidence reference", FORGE_TASK_PROMPT)

    def test_retention_deletes_entity_but_preserves_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            artifact_path = workspace / "artifacts" / "forge" / "s" / "evidence.png"
            artifact_path.parent.mkdir(parents=True)
            data = b"\x89PNG\r\n\x1a\nretention"
            artifact_path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            artifact = EvidenceArtifact(
                artifact_id="artifact_one",
                phase="before",
                kind="rgb_image",
                source_id="front",
                sequence=1,
                received_at=utc_now(),
                media_type="image/png",
                sha256=digest,
                byte_size=len(data),
                uri=str(artifact_path.relative_to(workspace)),
            )
            evidence = EvidenceBundle(
                bundle_id="bundle_one",
                session_id="s",
                command_id="c",
                artifacts=[artifact],
                quality=EvidenceQuality(complete=True),
            )
            bundle_path = artifact_path.parent / "evidence_bundle.json"
            bundle_path.write_text(
                json.dumps(evidence.model_dump(mode="json")), encoding="utf-8"
            )
            request = VerificationRequest(
                content=[],
                artifact_paths=(bundle_path, artifact_path),
                valid_evidence_refs=frozenset({artifact.artifact_id}),
                evidence=evidence,
            )
            verifier = ForgeTaskVerifier(
                workspace=workspace,
                provider=object(),
                model="test",
                evidence_retention="none",
            )
            result = verifier.apply_retention(request, final_status="failed")
            retained_bundle = EvidenceBundle.model_validate_json(
                bundle_path.read_text(encoding="utf-8")
            )

            self.assertEqual(result["status"], "deleted")
            self.assertFalse(artifact_path.exists())
            self.assertEqual(retained_bundle.artifacts[0].sha256, digest)
            self.assertFalse(retained_bundle.artifacts[0].retained)
            self.assertIsNotNone(retained_bundle.artifacts[0].deleted_at)


if __name__ == "__main__":
    unittest.main()

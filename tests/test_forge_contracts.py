from __future__ import annotations

import unittest

from pydantic import ValidationError

from PhyAgentOS.config.schema import Config
from PhyAgentOS.verification.contracts import (
    CriterionVerdict,
    EvidenceArtifact,
    ForgeTaskRequest,
    TaskVerificationContract,
    VerificationVerdict,
    utc_now,
)


class ForgeContractTests(unittest.TestCase):
    def test_non_off_verification_requires_goal_and_criteria(self) -> None:
        with self.assertRaisesRegex(ValidationError, "verification goal is required"):
            TaskVerificationContract(mode="enforce")
        contract = TaskVerificationContract(
            mode="recovery",
            goal="place the cup upright",
            success_criteria=["the cup is upright"],
        )
        self.assertEqual(contract.mode, "recovery")

    def test_forge_task_has_no_external_identity_or_legacy_hints(self) -> None:
        request = ForgeTaskRequest(task_description="pick the cup", action_type="grasp")
        self.assertNotIn("session_id", type(request).model_fields)
        self.assertNotIn("command_id", type(request).model_fields)
        self.assertEqual(
            set(type(request).model_fields),
            {
                "version",
                "task_description",
                "action_type",
                "inputs",
                "verification",
                "execution_timeout_s",
                "source",
            },
        )

    def test_legacy_runtime_config_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "legacy `runtime` configuration"):
            Config.model_validate({"runtime": {"enabled": True}})

    def test_embodiment_profiles_have_no_execution_adapter_field(self) -> None:
        config = Config.model_validate(
            {
                "embodiments": {
                    "mode": "fleet",
                    "instances": [
                        {"robotId": "piper", "workspace": "/tmp/piper-knowledge"}
                    ],
                }
            }
        )
        instance = config.embodiments.instances[0]
        self.assertEqual(instance.profile_name, None)
        with self.assertRaises(ValidationError):
            Config.model_validate(
                {
                    "embodiments": {
                        "mode": "fleet",
                        "instances": [
                            {
                                "robotId": "piper",
                                "workspace": "/tmp/piper-knowledge",
                                "driver": "old-adapter",
                            }
                        ],
                    }
                }
            )

    def test_artifact_uri_must_be_workspace_relative(self) -> None:
        values = {
            "artifact_id": "a",
            "phase": "before",
            "kind": "rgb_image",
            "source_id": "front",
            "received_at": utc_now(),
            "media_type": "image/png",
            "sha256": "0" * 64,
            "byte_size": 1,
        }
        with self.assertRaises(ValidationError):
            EvidenceArtifact(**values, uri="../escape.png")
        artifact = EvidenceArtifact(**values, uri="artifacts/forge/a/front.png")
        self.assertTrue(artifact.retained)

    def test_inputs_must_be_json_and_verdict_must_match_criteria(self) -> None:
        with self.assertRaisesRegex(ValidationError, "finite JSON"):
            ForgeTaskRequest(
                task_description="move", action_type="move", inputs={"speed": float("nan")}
            )
        with self.assertRaisesRegex(ValidationError, "every criterion"):
            VerificationVerdict(
                verdict="success",
                criteria=[CriterionVerdict(criterion="done", status="unknown")],
                reason="uncertain",
                lesson="collect better evidence",
            )


if __name__ == "__main__":
    unittest.main()

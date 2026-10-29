"""Independent Forge verifier imports and legacy module compatibility."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from PhyAgentOS.agent.services import forge_verifier
from PhyAgentOS.agent.services.forge_verifier import (
    ForgeTaskVerifier,
    VerificationBudgetError,
    VerificationVerdictError,
)

REPO_ROOT = Path(__file__).parents[1]


def test_forge_verifier_import_does_not_load_agent_legacy() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from PhyAgentOS.agent.services.forge_verifier import ForgeTaskVerifier\n"
            "assert ForgeTaskVerifier.__module__ == 'PhyAgentOS.agent.services.forge_verifier'\n"
            "assert not any(name == 'PhyAgentOS.agent_legacy' or "
            "name.startswith('PhyAgentOS.agent_legacy.') for name in sys.modules)\n",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_legacy_verifier_exports_same_module_class_and_exceptions() -> None:
    from PhyAgentOS.agent_legacy import session_verifier

    assert session_verifier is forge_verifier
    assert session_verifier.ForgeTaskVerifier is ForgeTaskVerifier
    assert session_verifier.VerificationBudgetError is VerificationBudgetError
    assert session_verifier.VerificationVerdictError is VerificationVerdictError


async def test_legacy_service_patch_applies_to_new_verifier_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PhyAgentOS.agent_legacy import session_verifier

    content = [{"type": "text", "text": "Check the object."}]
    verdict_data = {
        "verdict": "success",
        "criteria": [
            {
                "criterion": "object reached target",
                "status": "satisfied",
                "evidence_refs": ["artifact_one"],
            }
        ],
        "evidence_refs": ["artifact_one"],
        "reason": "The object is at the target.",
        "lesson": "Inspect the final observation.",
    }
    calls: list[object] = []
    service = SimpleNamespace(
        start=lambda: calls.append("start"),
        verify_task=lambda value: calls.append(value) or verdict_data,
    )
    service_options: dict = {}

    def create_service(**kwargs):
        service_options.update(kwargs)
        return service

    monkeypatch.setattr(session_verifier, "VerificationServiceProcess", create_service)
    verifier = ForgeTaskVerifier(
        workspace=tmp_path,
        provider=object(),
        model="test",
        session_secret="test-secret",
        max_calls=1,
    )
    assert verifier.service is service
    assert service_options["engine"] is verifier.engine
    assert service_options["session_secret"] == "test-secret"

    verdict, attempt = await verifier._verify_content(
        content=content,
        expected_criteria=["object reached target"],
        valid_evidence_refs={"artifact_one"},
        source="tool",
        mode="review",
    )
    assert verdict.verdict == "success"
    assert (attempt.source, attempt.mode, attempt.verdict) == ("tool", "review", "success")
    assert verifier.calls == 1
    assert calls == ["start", content]

    with pytest.raises(session_verifier.VerificationBudgetError, match="budget exhausted"):
        await verifier._verify_content(
            content=content,
            expected_criteria=["object reached target"],
            valid_evidence_refs={"artifact_one"},
        )
    assert calls == ["start", content]


def test_new_verifier_validation_raises_legacy_exception() -> None:
    from PhyAgentOS.agent_legacy.session_verifier import VerificationVerdictError

    verdict = forge_verifier.VerificationVerdict(
        verdict="success",
        criteria=[
            {
                "criterion": "object reached target",
                "status": "satisfied",
                "evidence_refs": ["unknown"],
            }
        ],
        reason="The object is at the target.",
        lesson="Inspect the final observation.",
    )
    with pytest.raises(VerificationVerdictError, match="unknown evidence: unknown"):
        ForgeTaskVerifier._validate_generic_verdict(
            expected_criteria=["object reached target"],
            valid_evidence_refs={"artifact_one"},
            verdict=verdict,
        )

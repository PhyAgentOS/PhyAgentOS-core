"""Agent-owned semantic verifier for Forge task records."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from PhyAgentOS.providers.base import LLMProvider
from PhyAgentOS.utils.atomic_file import atomic_write_text
from PhyAgentOS.verification.contracts import (
    ForgeSessionRecord,
    VerificationAttempt,
    VerificationVerdict,
    utc_now,
)
from PhyAgentOS.verification.engine import VerificationEngine
from PhyAgentOS.verification.request_builder import (
    VerificationRequest,
    VerificationRequestBuilder,
)
from PhyAgentOS.verification.service import VerificationServiceProcess

if TYPE_CHECKING:
    from PhyAgentOS.forge.task import AgentTaskRecord


class VerificationVerdictError(ValueError):
    pass


class VerificationBudgetError(RuntimeError):
    pass


class ForgeTaskVerifier:
    def __init__(
        self,
        *,
        workspace: str | Path,
        provider: LLMProvider,
        model: str,
        evidence_retention: Literal["all", "failed", "none"] = "none",
        timeout_s: float = 180.0,
        service_host: str = "127.0.0.1",
        service_port: int = 8100,
        session_secret: str | None = None,
        service_provider_spec: dict[str, Any] | None = None,
        max_calls: int = 50,
        write_legacy_lessons: bool = True,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.engine = VerificationEngine(provider=provider, model=model, timeout_s=timeout_s)
        self.evidence_retention = evidence_retention
        self.max_calls = max(0, int(max_calls))
        self.write_legacy_lessons = bool(write_legacy_lessons)
        self.calls = 0
        self.request_builder = VerificationRequestBuilder(self.workspace)
        self.service = VerificationServiceProcess(
            engine=self.engine,
            host=service_host,
            port=service_port,
            session_secret=session_secret or uuid4().hex,
            provider_spec=service_provider_spec,
        )
        self._lesson_lock = threading.Lock()

    async def start(self) -> None:
        await asyncio.to_thread(self.service.start)

    def stop(self) -> None:
        self.service.stop()

    async def verify(
        self,
        record: ForgeSessionRecord,
        *,
        history: list[dict[str, Any]],
        lessons: str,
        source: Literal["auto", "tool"] = "auto",
        mode: Literal["apply", "review"] = "apply",
    ) -> tuple[VerificationVerdict, VerificationRequest, VerificationAttempt]:
        request = self.request_builder.build(record, history=history, lessons=lessons)
        verdict, attempt = await self._verify_content(
            content=request.content,
            expected_criteria=record.request.verification.success_criteria,
            valid_evidence_refs=set(request.valid_evidence_refs),
            source=source,
            mode=mode,
        )
        return verdict, request, attempt

    async def verify_agent_task(
        self,
        task: AgentTaskRecord,
        *,
        events: list[dict[str, Any]],
        lessons: str,
        source: Literal["auto", "tool"] = "auto",
        mode: Literal["apply", "review"] = "apply",
    ) -> tuple[VerificationVerdict, VerificationRequest, VerificationAttempt]:
        request = self.request_builder.build_agent_task(
            task,
            events=events,
            lessons=lessons,
        )
        verdict, attempt = await self._verify_content(
            content=request.content,
            expected_criteria=task.verification.success_criteria,
            valid_evidence_refs=set(request.valid_evidence_refs),
            source=source,
            mode=mode,
        )
        return verdict, request, attempt

    async def _verify_content(
        self,
        *,
        content: list[dict[str, Any]],
        expected_criteria: list[str],
        valid_evidence_refs: set[str],
        source: Literal["auto", "tool"] = "auto",
        mode: Literal["apply", "review"] = "apply",
    ) -> tuple[VerificationVerdict, VerificationAttempt]:
        """Run semantic verification only after a strict request has been built."""
        if self.max_calls and self.calls >= self.max_calls:
            raise VerificationBudgetError(
                f"verifier call budget exhausted ({self.max_calls})"
            )
        self.calls += 1
        data = await asyncio.to_thread(self._start_and_verify, content)
        try:
            verdict = VerificationVerdict.model_validate(data)
        except Exception as exc:
            raise VerificationVerdictError(str(exc)) from exc
        self._validate_generic_verdict(
            expected_criteria=expected_criteria,
            valid_evidence_refs=valid_evidence_refs,
            verdict=verdict,
        )
        return verdict, VerificationAttempt(
            attempt_id=f"verification_{uuid4().hex[:12]}",
            source=source,
            mode=mode,
            verdict=verdict.verdict,
        )

    def _start_and_verify(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        self.service.start()
        return self.service.verify_task(content)

    @staticmethod
    def _validate_verdict(
        record: ForgeSessionRecord,
        request: VerificationRequest,
        verdict: VerificationVerdict,
    ) -> None:
        ForgeTaskVerifier._validate_generic_verdict(
            expected_criteria=record.request.verification.success_criteria,
            valid_evidence_refs=set(request.valid_evidence_refs),
            verdict=verdict,
        )

    @staticmethod
    def _validate_generic_verdict(
        *,
        expected_criteria: list[str],
        valid_evidence_refs: set[str],
        verdict: VerificationVerdict,
    ) -> None:
        expected = expected_criteria
        actual = [item.criterion for item in verdict.criteria]
        if len(actual) != len(expected) or set(actual) != set(expected):
            raise VerificationVerdictError(
                "verifier must return exactly one result for each success criterion"
            )
        refs = set(verdict.evidence_refs)
        for criterion in verdict.criteria:
            refs.update(criterion.evidence_refs)
        unknown = refs - valid_evidence_refs
        if unknown:
            raise VerificationVerdictError(
                "verifier referenced unknown evidence: " + ", ".join(sorted(unknown))
            )

    def apply_retention(
        self,
        request: VerificationRequest,
        *,
        final_status: str,
    ) -> dict[str, Any]:
        should_delete = self.evidence_retention == "none" or (
            self.evidence_retention == "failed" and final_status == "succeeded"
        )
        deleted: list[str] = []
        errors: list[dict[str, str]] = []
        evidence = request.evidence.model_copy(deep=True)
        if should_delete:
            deleted_at = utc_now()
            for artifact in evidence.artifacts:
                path = (self.workspace / artifact.uri).resolve()
                try:
                    if not path.is_relative_to(self.workspace):
                        raise ValueError("artifact escapes workspace")
                    path.unlink(missing_ok=True)
                    artifact.retained = False
                    artifact.deleted_at = deleted_at
                    deleted.append(artifact.uri)
                except Exception as exc:
                    errors.append({"path": artifact.uri, "error": str(exc)})
            bundle_path = request.artifact_paths[0]
            atomic_write_text(
                bundle_path,
                json.dumps(
                    evidence.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
        return {
            "policy": self.evidence_retention,
            "status": "partial" if errors else "deleted" if should_delete else "retained",
            "updated_at": utc_now().isoformat(),
            "deleted_paths": deleted,
            "errors": errors,
        }

    def write_verification_result(self, record: ForgeSessionRecord) -> str:
        path = self.workspace / "artifacts" / "forge" / record.session_id / "verification_result.json"
        payload = {
            "version": "forge_verification_result_v1",
            "session_id": record.session_id,
            "status": record.status.value,
            "execution_identity": (
                {
                    "session_id": record.execution.session_id,
                    "command_id": record.execution.command_id,
                    "status": record.execution.status,
                }
                if record.execution is not None
                else None
            ),
            "verification": record.verification.model_dump(mode="json"),
            "updated_at": utc_now().isoformat(),
        }
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return str(path.relative_to(self.workspace))

    def write_lesson(
        self,
        record: ForgeSessionRecord,
        *,
        summary: str,
        phase: str,
        error_code: str | None = None,
    ) -> None:
        if not self.write_legacy_lessons:
            return
        path = self.workspace / "LESSONS.md"
        entry = (
            f"\n\n## {utc_now().isoformat()} — {record.session_id}\n\n"
            f"- Phase: `{phase}`\n"
            f"- Action: `{record.request.action_type}`\n"
            f"- Error: `{error_code or 'none'}`\n"
            f"- Lesson: {summary.strip()}\n"
        )
        with self._lesson_lock:
            current = path.read_text(encoding="utf-8") if path.exists() else "# Lessons Learned\n"
            atomic_write_text(path, current.rstrip() + entry)

"""Task outcome source adapters for the experience subsystem."""

from __future__ import annotations

from typing import Protocol

from PhyAgentOS.agent.experience.contracts import LineageOutcome, TaskOutcomeEnvelope
from PhyAgentOS.agent.experience.redaction import opaque_ref, redact_text


class TaskOutcomeSource(Protocol):
    def build(self, task_ref: str) -> TaskOutcomeEnvelope: ...


class ForgeTaskOutcomeSource:
    """Build a redacted task-level outcome from one persisted Forge lineage."""

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    def build(self, task_ref: str) -> TaskOutcomeEnvelope:
        terminal = self.orchestrator.get_session(task_ref)
        lineage = self.orchestrator.store.lineage(terminal.root_session_id)
        final = lineage[-1]
        final_verdict = final.verification.verdict
        criteria_statuses = (
            {item.criterion: item.status for item in final_verdict.criteria}
            if final_verdict is not None
            else {}
        )
        items: list[LineageOutcome] = []
        for record in lineage:
            verdict = record.verification.verdict
            refs: list[str] = []
            if verdict is not None:
                refs.extend(verdict.evidence_refs)
                for criterion in verdict.criteria:
                    refs.extend(criterion.evidence_refs)
            items.append(
                LineageOutcome(
                    session_ref=record.session_id,
                    action_semantics=record.request.action_type,
                    input_keys=sorted(record.request.inputs.keys()),
                    execution_status=(
                        record.execution.status if record.execution is not None else None
                    ),
                    semantic_verdict=verdict.verdict if verdict is not None else None,
                    reason=redact_text(verdict.reason) if verdict is not None else "",
                    verifier_lesson=(
                        redact_text(verdict.lesson) if verdict is not None else ""
                    ),
                    evidence_refs=list(
                        dict.fromkeys(opaque_ref(item) for item in refs)
                    ),
                )
            )
        contract = final.request.verification
        return TaskOutcomeEnvelope(
            task_id=final.session_id,
            root_task_id=final.root_session_id,
            goal=redact_text(contract.goal or final.request.task_description),
            success_criteria=[redact_text(item) for item in contract.success_criteria],
            final_verdict=final_verdict.verdict if final_verdict is not None else None,
            criteria_statuses={
                redact_text(criterion): status
                for criterion, status in criteria_statuses.items()
            },
            lineage=items,
            record_refs=[f"forge:{item.session_ref}" for item in items],
            completed_at=final.terminal_at or final.updated_at,
        )


class AgentTaskOutcomeSource:
    """Build a redacted experience outcome from a Tool API AgentTask aggregate."""

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    def build(self, task_ref: str) -> TaskOutcomeEnvelope:
        task = self.coordinator.get_task(task_ref)
        verdict = task.verdict
        criteria_statuses = (
            {item.criterion: item.status for item in verdict.criteria}
            if verdict is not None
            else {}
        )
        revision_verdicts = {
            revision.revision_id: revision.verdict for revision in task.revisions
        }
        revision_last_records = {
            revision.execution_records[-1].record_id
            for revision in task.revisions
            if revision.execution_records
        }
        lineage: list[LineageOutcome] = []
        for record in task.execution_records:
            revision_verdict = (
                revision_verdicts.get(record.revision_id)
                if record.record_id in revision_last_records
                else None
            )
            lineage.append(
                LineageOutcome(
                    session_ref=record.record_id,
                    task_ref=opaque_ref(task.task_id),
                    revision_ref=opaque_ref(record.revision_id),
                    invocation_ref=(
                        opaque_ref(record.invocation_id)
                        if record.invocation_id is not None
                        else None
                    ),
                    attempt_ref=(
                        opaque_ref(record.attempt_id)
                        if record.attempt_id is not None
                        else None
                    ),
                    action_semantics=record.tool_id,
                    input_keys=sorted(record.arguments),
                    execution_status=record.status,
                    semantic_verdict=(
                        revision_verdict.verdict
                        if revision_verdict is not None
                        else None
                    ),
                    reason=(
                        redact_text(revision_verdict.reason)
                        if revision_verdict is not None
                        else ""
                    ),
                    verifier_lesson=(
                        redact_text(revision_verdict.lesson)
                        if revision_verdict is not None
                        else ""
                    ),
                    evidence_refs=list(
                        dict.fromkeys(
                            opaque_ref(item) for item in record.evidence_refs
                        )
                    ),
                )
            )
        inferred_verdict = None
        if verdict is not None:
            inferred_verdict = verdict.verdict
        elif task.verification.mode == "off" and task.status.value == "succeeded":
            inferred_verdict = "success"
            criteria_statuses = {
                item: "satisfied" for item in task.verification.success_criteria
            }
        elif task.verification.mode == "off" and task.status.value == "failed":
            inferred_verdict = "failure"
            criteria_statuses = {
                item: "unknown" for item in task.verification.success_criteria
            }
        interaction_refs = []
        service = getattr(self.coordinator, "interaction", None)
        if service is not None and service.managed(task.task_id):
            from PhyAgentOS.forge.interaction.evidence import checked_trace
            for run in service.store.runs(task.task_id):
                checked_trace(service, run)
                interaction_refs.append(opaque_ref("interaction:" + run["interaction_run_id"]))
        return TaskOutcomeEnvelope(
            task_id=task.task_id,
            root_task_id=task.task_id,
            goal=redact_text(task.verification.goal or task.task_description),
            success_criteria=[
                redact_text(item) for item in task.verification.success_criteria
            ],
            final_verdict=inferred_verdict,
            criteria_statuses={
                redact_text(criterion): status
                for criterion, status in criteria_statuses.items()
            },
            lineage=lineage,
            record_refs=[f"agent-task:{item.session_ref}" for item in lineage] + interaction_refs,
            agent_task_ref=opaque_ref(task.task_id),
            tool_invocation_refs=[
                opaque_ref(item.invocation_id)
                for item in task.execution_records
                if item.invocation_id is not None
            ],
            completed_at=task.terminal_at or task.updated_at,
        )

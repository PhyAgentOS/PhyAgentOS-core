"""Recover runtime references using read-only, identity-checked Gateway facts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.skill_runtime.locking import SkillOperationLock
from PhyAgentOS.skill_runtime.state import RuntimeStateStore
from PhyAgentOS.verification.contracts import utc_now

if TYPE_CHECKING:
    from PhyAgentOS.forge.task import AgentTaskStore

REMOTE_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "stopped"})


@dataclass
class ReconciliationReport:
    released_invocations: list[str] = field(default_factory=list)
    released_sessions: list[str] = field(default_factory=list)
    released_bindings: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


async def reconcile_runtime_references(
    skill_name: str,
    *,
    states: RuntimeStateStore,
    tasks: AgentTaskStore,
    client: ForgeToolClient | None = None,
    timeout_s: float = 10.0,
) -> ReconciliationReport:
    """Recover retained references; report leftovers instead of raising.

    Recovery runs before agent startup and before a non-forced shutdown, so a
    busy Skill lock, a missing state file, or an unreadable task journal must
    not abort the caller's own operation. Every failure is reported as an
    unresolved reason instead.
    """
    report = ReconciliationReport()
    try:
        with SkillOperationLock(states.root, skill_name):
            await _reconcile_locked(
                skill_name,
                states=states,
                tasks=tasks,
                client=client,
                timeout_s=timeout_s,
                report=report,
            )
    except Exception as exc:  # noqa: BLE001 - recovery must not block start/stop
        report.unresolved.append(f"{type(exc).__name__}: {exc}")
    return report


async def _reconcile_locked(
    skill_name: str,
    *,
    states: RuntimeStateStore,
    tasks: AgentTaskStore,
    client: ForgeToolClient | None,
    timeout_s: float,
    report: ReconciliationReport,
) -> None:
    """Reconcile while the Skill operation lock is held.

    Include terminal tasks; never infer remote termination from owner exit.
    The lock serializes this with start/stop/switch. Keep the task's verdict
    unchanged: recovery repairs execution facts, not task success.
    """
    from PhyAgentOS.forge.task import _tool_status

    state = states.load(skill_name)
    if state is None:
        report.unresolved.append(f"{skill_name}: no runtime state")
        return
    bindings = set(state.active_task_bindings)
    invocations, sessions = set(state.active_invocations), set(state.active_sessions)
    candidates = tasks.bound_tasks(bindings)
    active = tasks.active()
    if (
        active is not None
        and active.primary_skill_binding is not None
        and all(task.task_id != active.task_id for task in candidates)
    ):
        candidates.append(active)
    if not (bindings or invocations or sessions or candidates):
        return
    owned_client = client is None
    client = client or ForgeToolClient(state.gateway_url, timeout_s=min(timeout_s, 3.0))

    def change_reference(field_name: str, reference: str, *, add: bool = False) -> None:
        current = states.load(skill_name)
        if current is None or (
            current.runtime_instance_id != state.runtime_instance_id
            or current.gateway_identity != state.gateway_identity
            or current.gateway_url != state.gateway_url
        ):
            raise ValueError("Runtime changed during reconciliation")
        # Reload and remove only this confirmed identity. Unrelated references
        # added since recovery began must survive the update.
        items = set(getattr(current, field_name))
        remaining = tuple(sorted(items | {reference} if add else items - {reference}))
        states.save(
            current.with_status(
                current.status,
                error=current.last_error,
                **{field_name: remaining},
            )
        )

    try:
        async with asyncio.timeout(timeout_s):
            snapshot = await client.list_tools()
            data = snapshot.get("data", {})
            identity = data.get("gateway_identity") or data.get("gateway_id")
            if not isinstance(identity, str) or not identity:
                identity = (
                    "gateway_"
                    + hashlib.sha256(
                        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()[:20]
                )
            if not state.gateway_identity or identity != state.gateway_identity:
                raise ValueError("Gateway identity changed; references were retained")
            seen_invocations: set[str] = set()
            seen_bindings: set[str] = set()
            for task in candidates:
                binding = task.primary_skill_binding
                assert binding is not None
                seen_bindings.add(binding.binding_id)
                if (
                    binding.runtime_instance_id != state.runtime_instance_id
                    or binding.gateway_identity != state.gateway_identity
                    or binding.gateway_url != state.gateway_url
                    or binding.skill_name != state.skill_name
                    or binding.runtime_profile != state.profile
                ):
                    report.unresolved.append(f"{task.task_id}: Runtime binding changed")
                    continue
                if tasks.has_event(task.task_id, "interaction_bound"):
                    report.unresolved.append(
                        f"{task.task_id}: interaction supervisor recovery required"
                    )
                    continue
                if not task.terminal:
                    change_reference("active_task_bindings", binding.binding_id, add=True)
                    bindings.add(binding.binding_id)
                for record in task.execution_records:
                    if record.semantics == "query":
                        continue
                    if not record.invocation_id:
                        if record.status not in REMOTE_TERMINAL:

                            def mark_unknown(current, record_id=record.record_id):
                                for item in current.execution_records:
                                    if item.record_id == record_id:
                                        item.status = "unknown"
                                        item.updated_at = utc_now()
                                        item.error = {
                                            "type": "RecoveryUnknown",
                                            "message": "dispatch intent has no invocation identity; request was not resent",
                                        }

                            tasks.update(
                                task.task_id, mark_unknown, event_type="execution_recovery_unknown"
                            )
                            report.unresolved.append(
                                f"{record.record_id}: no remote invocation identity; retained"
                            )
                        continue
                    invocation_id = record.invocation_id
                    tracker = sessions if record.semantics == "session" else invocations
                    field_name = (
                        "active_sessions" if record.semantics == "session" else "active_invocations"
                    )
                    if not task.terminal and record.status not in REMOTE_TERMINAL:
                        change_reference(field_name, invocation_id, add=True)
                        tracker.add(invocation_id)
                    if invocation_id not in tracker and record.status != "unknown":
                        continue
                    seen_invocations.add(invocation_id)
                    observation_received = False
                    try:
                        response = await client.invocation_status(invocation_id)
                        returned_id = response.get("data", {}).get("invocation_id", invocation_id)
                        if returned_id != invocation_id:
                            raise ValueError("Gateway returned a different invocation identity")
                        status = _tool_status(response, default="unknown")
                        observation_received = True

                        def mutate(
                            current, record_id=record.record_id, facts=response, phase=status
                        ):
                            for item in current.execution_records:
                                if item.record_id == record_id:
                                    item.status = phase
                                    item.response = facts
                                    item.updated_at = utc_now()

                        tasks.update(
                            task.task_id,
                            mutate,
                            event_type="execution_reconciled",
                            payload={"invocation_id": invocation_id, "phase": status},
                        )
                        if status not in REMOTE_TERMINAL:
                            report.unresolved.append(f"{invocation_id}: remote state is {status}")
                            continue
                        released = (
                            report.released_sessions
                            if record.semantics == "session"
                            else report.released_invocations
                        )
                        change_reference(field_name, invocation_id)
                        released.append(invocation_id)
                    except Exception as exc:
                        if (
                            not observation_received
                            and not task.terminal
                            and record.status not in REMOTE_TERMINAL
                        ):

                            def observation_unknown(current, record_id=record.record_id, error=exc):
                                for item in current.execution_records:
                                    if item.record_id == record_id:
                                        item.status = "unknown"
                                        item.updated_at = utc_now()
                                        item.error = {
                                            "type": type(error).__name__,
                                            "message": str(error),
                                        }

                            tasks.update(
                                task.task_id,
                                observation_unknown,
                                event_type="execution_recovery_unknown",
                            )
                        report.unresolved.append(f"{invocation_id}: {type(exc).__name__}: {exc}")
                current_task = tasks.get(task.task_id)
                if current_task.terminal and all(
                    item.semantics == "query" or item.status in REMOTE_TERMINAL
                    for item in current_task.execution_records
                ):
                    change_reference("active_task_bindings", binding.binding_id)
                    report.released_bindings.append(binding.binding_id)
                elif not current_task.terminal:
                    report.unresolved.append(
                        f"{task.task_id}: task is still {current_task.status.value}; binding retained"
                    )
            for reference in (invocations | sessions) - seen_invocations:
                report.unresolved.append(f"{reference}: no matching recoverable execution record")
            for reference in bindings - seen_bindings:
                report.unresolved.append(f"{reference}: no matching task in this workspace")
    except Exception as exc:
        report.unresolved.append(f"{type(exc).__name__}: {exc}")
    finally:
        if owned_client:
            await client.close()

"""Opt-in agent interaction loop; no dependency on the legacy TaskCoordinator.

The model proposes, the executor owns execution, and an independent root verifier
checks the user's goal. This module never treats transport acceptance as success.
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from PhyAgentOS.forge.interaction.client import (
    GatewayUnknownError,
    InteractionClient,
    InteractionTransportError,
)
from PhyAgentOS.forge.interaction.contracts import START_SCHEMA, digest, validate, validate_receipt
from PhyAgentOS.forge.interaction.runner_store import RunnerStore

TERMINAL_SESSION = {"completed", "succeeded", "failed", "cancelled", "stopped"}
TERMINAL_STEP = {
    "succeeded",
    "failed",
    "cancelled",
    "rejected",
    "discarded",
    "wait",
    "finish_candidate",
    "cannot_proceed",
}
IDENTITY_KEYS = ("binding_digest", "runtime_instance_id", "gateway_identity", "world_id")


@dataclass(frozen=True)
class RunnerLimits:
    max_model_calls: int = 80
    max_attempts_per_snapshot: int = 2
    max_total_tokens: int = 100_000
    max_tokens_per_call: int = 2048
    model_timeout_s: float = 30
    io_timeout_s: float = 10
    poll_interval_s: float = 0.1
    stop_timeout_s: float = 5
    stop_poll_limit: int = 20
    max_idle_polls: int = 100
    start_timeout_s: float = 15
    max_no_progress_decisions: int = 20

    def __post_init__(self) -> None:
        if (
            any(getattr(self, k) <= 0 for k in self.__dataclass_fields__ if k != "poll_interval_s")
            or self.poll_interval_s < 0
        ):
            raise ValueError("Runner limits must be positive")


def decision_schema(proposal_schema: dict, *, allow_usage: bool = False) -> dict:
    usage = {
        "type": "object",
        "properties": {
            "total_tokens": {"type": "integer", "minimum": 0},
            "prompt_tokens": {"type": "integer", "minimum": 0},
            "completion_tokens": {"type": "integer", "minimum": 0},
        },
        "required": ["total_tokens"],
        "additionalProperties": False,
    }
    alternatives = []
    for kind in ("proposal", "wait", "finish_candidate", "cannot_proceed"):
        properties = {"kind": {"const": kind}, "reason": {"type": "string", "maxLength": 2048}}
        if allow_usage:
            properties["usage"] = usage
            usage["properties"].update(
                {
                    "input_tokens": {"type": "integer", "minimum": 0},
                    "output_tokens": {"type": "integer", "minimum": 0},
                }
            )
        required = ["kind"]
        if kind == "proposal":
            properties["proposal"] = copy.deepcopy(proposal_schema)
            required.append("proposal")
        alternatives.append(
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        )
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "oneOf": alternatives}


class AgentInteractionRunner:
    """One local owner, bounded polling, no automatic resubmission or hot takeover.

    ``decide`` receives a fresh data-only context, never this client or credentials.
    A callback is trusted host code; isolating an actual model's tools/process is a
    deployment responsibility. ``verifier`` is independent of that callback.
    ``identity_probe`` should read the active PAOS binding when embedded in PAOS.
    """

    def __init__(
        self,
        *,
        store: RunnerStore,
        client: InteractionClient,
        decide: Callable[[dict], Awaitable[dict]],
        goal: Any,
        proposal_schema: dict,
        start_arguments: dict,
        limits: RunnerLimits | None = None,
        identity_probe: Callable[[], Awaitable[dict]] | None = None,
    ):
        Draft202012Validator.check_schema(proposal_schema)
        validate(start_arguments, START_SCHEMA)
        self.store, self.client = store, client
        self.decide = decide
        self.goal = copy.deepcopy(goal)
        self.proposal_schema = copy.deepcopy(proposal_schema)
        self.start_arguments = copy.deepcopy(start_arguments)
        self.run_id = start_arguments["interaction_run_id"]
        self.limits = limits or RunnerLimits()
        self.identity_probe = identity_probe
        self._decision_schema = decision_schema(proposal_schema)
        self._decision_validator = Draft202012Validator(
            decision_schema(proposal_schema, allow_usage=True)
        )
        self._tick_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._cancel_event = asyncio.Event()
        self._idle_polls = 0
        self._attached = False

    def close(self) -> None:
        if self.store.owns(self):
            self.store.release_owner(self)

    def _own(self) -> None:
        self.store.acquire_owner(self)
        observe = getattr(self.client, "observe_invocations", None)
        if callable(observe):
            observe(self._record_transport_identity)

    def _record_transport_identity(
        self, operation: str, arguments: dict, invocation_id: str
    ) -> None:
        if arguments.get("interaction_run_id") != self.run_id:
            raise ValueError("Transport identity belongs to a different run")
        if operation == "run":
            current = self.state["session_invocation_id"]
            if current and current != invocation_id:
                raise ValueError("Original Session identity cannot be replaced")
            with self.store.transaction():
                self.store.update_run(self.run_id, session_invocation_id=invocation_id)
                self.store.event(
                    self.run_id, "start_invocation_registered", {"invocation_id": invocation_id}
                )
        elif operation == "submit_decision":
            self.store.update_submission(
                arguments["submission_id"], invocation_id=invocation_id, gateway_status="registered"
            )

    @property
    def state(self) -> dict:
        value = self.store.get_run(self.run_id)
        if value is None:
            raise RuntimeError("Runner has not started")
        return value

    async def _io(self, awaitable: Awaitable[dict]) -> dict:
        return await asyncio.wait_for(awaitable, timeout=self.limits.io_timeout_s)

    def _query(self, view: str = "snapshot", **extra: Any) -> dict:
        args = {
            "protocol": self.start_arguments["protocol"],
            "task_id": self.start_arguments["task_id"],
            "revision_id": self.start_arguments["revision_id"],
            "interaction_run_id": self.run_id,
            "view": view,
            **extra,
        }
        if view == "run" and "start_id" in args:
            args.pop("interaction_run_id")
        session = self.state["session_invocation_id"]
        if session:
            args["session_invocation_id"] = session
        return args

    def _block(self, reason: str, *, unknown: bool = False) -> dict:
        changes = {"state": "blocked", "reason": reason}
        if unknown:
            changes["gateway_uncertain"] = True
        with self.store.transaction():
            self.store.event(self.run_id, "blocked", {"reason": reason, "gateway_unknown": unknown})
            return self.store.update_run(self.run_id, **changes)

    async def _check_binding(self) -> bool:
        if self.identity_probe is None:
            self._block("live_binding_probe_required")
            return False
        try:
            actual = await self._io(self.identity_probe())
        except Exception:
            self._block("active_binding_unavailable")
            return False
        if any(actual.get(key) != self.start_arguments.get(key) for key in IDENTITY_KEYS[:3]):
            self._block("active_binding_changed")
            return False
        return True

    async def start(self) -> dict:
        async with self._tick_lock:
            return await self._start()

    async def _start(self) -> dict:
        self._own()
        existed = self.store.get_run(self.run_id) is not None
        self.store.create_run(self.start_arguments, self.goal, self.proposal_schema)
        if existed:
            return await self._recover()
        self._attached = True
        if not await self._check_binding():
            return self.state
        if self.state["cancelled"]:
            return self.store.update_run(
                self.run_id, state="cancelled", reason="cancelled_before_start"
            )
        try:
            self.store.update_run(self.run_id, state="starting")
            result = await self._io(self.client.start(copy.deepcopy(self.start_arguments)))
            session = result.get("session_invocation_id", result.get("invocation_id"))
            if not isinstance(session, str) or not session:
                raise InteractionTransportError("Start lacks original Session identity")
            self.store.update_run(self.run_id, session_invocation_id=session, state="monitoring")
            self.store.event(self.run_id, "start_transport", result)
            if "observation" in result:
                self._accept_snapshot(result)
        except Exception as exc:
            self.store.update_run(
                self.run_id,
                state="reconciling",
                recovery_required=True,
                reason="start_delivery_uncertain",
            )
            self.store.event(self.run_id, "start_transport_error", {"type": type(exc).__name__})
            await self._recover()
        if self.state["cancelled"] and self.state["session_invocation_id"]:
            return await self._stop_and_check("cancelled_during_start")
        return self.state

    def _snapshot_key(self, snapshot: dict) -> str:
        observation = snapshot["observation"]
        return digest(
            [
                self.run_id,
                observation["world_epoch"],
                snapshot["control_version"],
                observation["ref"],
            ]
        )

    def _accept_snapshot(self, snapshot: dict) -> bool:
        run = self.state
        if not isinstance(snapshot, dict):
            self._block("malformed_snapshot")
            return False
        expected = {
            **{k: self.start_arguments[k] for k in IDENTITY_KEYS},
            "interaction_run_id": self.run_id,
            "task_id": run["task_id"],
            "revision_id": run["revision_id"],
            "session_invocation_id": run["session_invocation_id"],
        }
        if any(snapshot.get(k) != value for k, value in expected.items()):
            self._block("snapshot_binding_or_ownership_changed")
            return False
        observation = snapshot.get("observation")
        if (
            not isinstance(observation, dict)
            or not isinstance(observation.get("ref"), str)
            or not isinstance(observation.get("world_epoch"), str)
            or type(snapshot.get("executor_cursor")) is not int
            or snapshot["executor_cursor"] < 0
            or type(snapshot.get("control_version")) is not int
            or type(snapshot.get("runner_epoch")) is not int
            or not isinstance(snapshot.get("executor_boot_id"), str)
            or not isinstance(observation.get("data"), dict)
            or not isinstance(snapshot.get("interaction"), dict)
            or type(snapshot["interaction"].get("can_submit")) is not bool
            or (
                snapshot.get("inflight_execution") is not None
                and not isinstance(snapshot["inflight_execution"], dict)
            )
            or (
                snapshot.get("last_step") is not None
                and not isinstance(snapshot["last_step"], dict)
            )
        ):
            self._block("malformed_snapshot")
            return False
        prior = run.get("last_snapshot")
        if prior and any(
            snapshot.get(k) != prior.get(k) for k in ("executor_boot_id", "runner_epoch")
        ):
            self._block("executor_or_control_identity_changed")
            return False
        if prior and observation["world_epoch"] != prior["observation"]["world_epoch"]:
            self._block("world_epoch_changed_requires_reconciliation")
            return False
        if prior and snapshot["control_version"] < prior["control_version"]:
            self._block("snapshot_version_rollback")
            return False
        # Cursor and corresponding snapshot evidence commit in the same transaction.
        with self.store.transaction():
            progress_key = digest(observation.get("data", {}))
            progress = {}
            if progress_key != run.get("progress_key"):
                progress = {
                    "progress_key": progress_key,
                    "last_progress_at": time.time(),
                    "no_progress_decisions": 0,
                }
            self.store.update_run(
                self.run_id,
                last_snapshot=snapshot,
                executor_cursor=snapshot.get("executor_cursor"),
                **progress,
            )
            evidence_snapshot = copy.deepcopy(snapshot)
            evidence_snapshot["observation"].pop("observed_at", None)
            evidence_prior = copy.deepcopy(prior)
            if evidence_prior:
                evidence_prior["observation"].pop("observed_at", None)
            if evidence_prior != evidence_snapshot:
                self.store.event(self.run_id, "executor_snapshot", snapshot)
            step = snapshot.get("last_step")
            if isinstance(step, dict) and step.get("step_id"):
                try:
                    local = self.store.get_step(step["step_id"])
                except KeyError:
                    self._block("untracked_executor_step")
                    return False
                if local and local["interaction_run_id"] == self.run_id:
                    status = step.get("status")
                    if status in {
                        "succeeded",
                        "failed",
                        "cancelled",
                        "unknown",
                        "executing",
                        "reconciling",
                    }:
                        changes = dict(
                            status=status,
                            execution_refs=step.get(
                                "execution_refs",
                                [step["execution_id"]]
                                if step.get("execution_id")
                                else local["execution_refs"],
                            ),
                            result_ref=step.get("result_ref"),
                            executor_evidence=step,
                            budget_usage={
                                **local["budget_usage"],
                                **{
                                    k: v
                                    for k, v in step.get("budget_usage", {}).items()
                                    if k in {"actions", "decisions"}
                                },
                            },
                            finished_at=(local.get("finished_at") or time.time())
                            if status in TERMINAL_STEP
                            else None,
                        )
                        if any(local.get(k) != v for k, v in changes.items()):
                            self.store.update_step(local["step_id"], **changes)
        return True

    async def _read_state(self) -> tuple[dict, dict] | None:
        if not await self._check_binding():
            return None
        session = self.state["session_invocation_id"]
        if not session:
            self._block("original_session_unknown")
            return None
        try:
            status = await self._io(self.client.status(session))
            if not isinstance(status, dict):
                raise InteractionTransportError("Malformed Session status")
            self.store.update_run(self.run_id, gateway_status=status)
            if status.get("status", status.get("phase")) == "unknown":
                raise GatewayUnknownError("Session status unknown", invocation_id=session)
            snapshot = await self._io(self.client.snapshot(self._query()))
        except Exception as exc:
            self._block(
                "session_or_snapshot_unavailable", unknown=isinstance(exc, GatewayUnknownError)
            )
            return None
        if (
            isinstance(snapshot, dict)
            and snapshot.get("status") == "unknown"
            and snapshot.get("reason") == "run_not_found"
            and self.state["last_snapshot"] is None
        ):
            if time.time() - self.state["created_at"] < self.limits.start_timeout_s:
                self.store.update_run(
                    self.run_id, state="starting", reason="waiting_for_start_acceptance"
                )
            else:
                self._block("start_acceptance_not_confirmed")
            return None
        if not self._accept_snapshot(snapshot):
            return None
        if self.state["state"] == "starting":
            self.store.update_run(self.run_id, state="monitoring", reason=None)
        if "status" not in status and "phase" in status:
            status = {**status, "status": status["phase"]}
        return status, snapshot

    def _record_receipt(self, submission: dict, receipt: dict) -> bool:
        try:
            validate_receipt(receipt)
        except Exception:
            self._block("malformed_business_receipt")
            return False
        payload = submission["payload"]
        if (
            not isinstance(receipt, dict)
            or type(receipt.get("accepted")) is not bool
            or any(
                receipt.get(k) != payload[k]
                for k in ("interaction_run_id", "step_id", "submission_id")
            )
            or receipt.get("payload_digest") != submission["payload_digest"]
        ):
            self._block("receipt_does_not_match_persisted_intent")
            return False
        transport = receipt.get("transport_invocation_id") or submission.get("invocation_id")
        if submission.get("invocation_id") and transport != submission["invocation_id"]:
            self._block("submission_transport_identity_changed")
            return False
        local = self.store.get_step(payload["step_id"])
        changes = {"submission_invocation_id": transport}
        if local["status"] not in TERMINAL_STEP and not local.get("executor_evidence"):
            changes.update(
                status="accepted" if receipt["accepted"] else "rejected",
                execution_refs=[receipt["execution_id"]] if receipt.get("execution_id") else [],
                local_verdict=receipt.get("reason"),
            )
        with self.store.transaction():
            self.store.update_submission(
                payload["submission_id"], status="receipt", receipt=receipt, invocation_id=transport
            )
            self.store.update_step(payload["step_id"], **changes)
        return True

    async def recover(self) -> dict:
        """Read-only reconciliation; a continuous original run can resume monitoring.

        No start/submit/stop is resent. Resume requires the original live binding,
        Session, executor, world and durable delivery facts to agree.
        """
        async with self._tick_lock:
            return await self._recover()

    async def _recover(self) -> dict:
        self._own()
        self._attached = True
        self.store.create_run(self.start_arguments, self.goal, self.proposal_schema)
        if self.state["state"] in {"finished", "cancelled"}:
            return self.state
        self.store.update_run(self.run_id, recovery_required=True, state="reconciling")
        if not await self._check_binding():
            return self.state
        try:
            found = await self._io(
                self.client.snapshot(self._query("run", start_id=self.start_arguments["start_id"]))
            )
            if found.get("status") != "found":
                return self._block("start_not_proven_by_durable_run_record")
            if found.get("start_payload_digest") != self.state["start_payload_digest"]:
                return self._block("start_intent_digest_mismatch")
            snapshot = found.get("run", {})
            session = snapshot.get("session_invocation_id")
            existing = self.state["session_invocation_id"]
            if not session or (existing and session != existing):
                return self._block("original_session_identity_mismatch")
            self.store.update_run(self.run_id, session_invocation_id=session)
            if not self._accept_snapshot(snapshot):
                return self.state
            for submission in self.store.submissions(self.run_id):
                if submission["receipt"] is None:
                    result = await self._io(
                        self.client.snapshot(
                            self._query("submission", submission_id=submission["submission_id"])
                        )
                    )
                    if (
                        result.get("status") != "found"
                        or result.get("payload_digest") != submission["payload_digest"]
                        or not self._record_receipt(submission, result.get("receipt", {}))
                    ):
                        return self._block("submission_delivery_unresolved")
                    if submission.get("invocation_id") is None:
                        return self._block("lost_submission_transport_identity")
                elif not self._record_receipt(submission, submission["receipt"]):
                    return self.state
                if submission.get("invocation_id"):
                    transport = await self._io(self.client.status(submission["invocation_id"]))
                    if transport.get("status", transport.get("phase")) == "unknown":
                        raise GatewayUnknownError("Submission invocation unknown")
                    if transport.get("status", transport.get("phase")) not in {
                        "completed",
                        "succeeded",
                    }:
                        return self._block("submission_transport_not_complete")
            observed = await self._read_state()
            if observed is None:
                return self.state
            status, snapshot = observed
            if (
                snapshot.get("interaction_phase") in {"unknown", "reconciling"}
                or (snapshot.get("inflight_execution") or {}).get("status")
                in {"unknown", "reconciling"}
                or self.state["gateway_uncertain"]
            ):
                return self._block("recovery_has_unresolved_execution")
            for step in self.store.steps(self.run_id):
                if step["status"] == "proposed":
                    for attempt in self.store.model_attempts(step["step_id"]):
                        if attempt["status"] == "running":
                            self.store.finish_model_attempt(
                                attempt["attempt_id"],
                                "interrupted",
                                None,
                                error="agent_process_interrupted",
                            )
                    if (
                        step["snapshot_key"] != self._snapshot_key(snapshot)
                        or status.get("status") in TERMINAL_SESSION
                        or self.state["cancelled"]
                    ):
                        self.store.update_step(
                            step["step_id"],
                            status="discarded",
                            local_verdict="stale_after_recovery",
                            finished_at=time.time(),
                        )
        except Exception as exc:
            return self._block(
                "recovery_evidence_unavailable", unknown=isinstance(exc, GatewayUnknownError)
            )
        self.store.event(self.run_id, "reconciliation_read_only", {"automatic_resubmit": False})
        if self.state["state"] != "blocked":
            next_state = (
                "needs_verification" if status.get("status") in TERMINAL_SESSION else "monitoring"
            )
            self.store.update_run(
                self.run_id,
                state=next_state,
                recovery_required=False,
                reason="original_run_reconciled",
            )
        return self.state

    async def tick(self) -> dict:
        self._own()
        if not self._attached:
            await self.recover()
        async with self._tick_lock:
            run = self.state
            if (
                run["state"] in {"finished", "cancelled", "blocked"}
                or run["recovery_required"]
                or run["gateway_uncertain"]
            ):
                return run
            if run["cancelled"] or self._cancel_event.is_set():
                return await self._stop_and_check("cancelled")
            observed = await self._read_state()
            if observed is None:
                return self.state
            status, snapshot = observed
            phase = snapshot.get("interaction_phase")
            if phase in {"unknown", "reconciling"}:
                return self._block("executor_has_unresolved_execution")
            if status.get("status") in TERMINAL_SESSION:
                self.store.update_run(self.run_id, state="needs_verification")
                return self.state
            budget = self.start_arguments.get("budget", {})
            if time.time() - run["created_at"] >= budget.get("wall_time_s", float("inf")):
                return await self._stop_and_check("wall_time_budget_exhausted")
            if (
                time.time() - self.state.get("last_progress_at", run["created_at"])
                >= budget.get("idle_time_s", float("inf"))
                or self.state.get("no_progress_decisions", 0)
                >= self.limits.max_no_progress_decisions
            ):
                return await self._stop_and_check("no_progress_budget_exhausted")
            if phase in {"needs_stop", "quiescent", "stopped", "completed"}:
                return await self._stop_and_check(
                    snapshot.get("completion_reason") or "executor_quiescent"
                )
            if snapshot.get("inflight_execution") or not snapshot.get("interaction", {}).get(
                "can_submit"
            ):
                return self.state
            if any(
                s["status"] in {"accepted", "executing", "unknown", "reconciling"}
                for s in self.store.steps(self.run_id)
            ):
                return self._block("prior_execution_missing_terminal_evidence")
            key = self._snapshot_key(snapshot)
            step = self.store.step_for_snapshot(self.run_id, key)
            if step and step["status"] != "proposed":
                self._idle_polls += 1
                if self._idle_polls >= self.limits.max_idle_polls:
                    return await self._stop_and_check("idle_budget_exhausted")
                return self.state
            if step is None:
                self._idle_polls = 0
                step = self.store.create_step(
                    self.run_id, "step_" + uuid.uuid4().hex, key, snapshot
                )
                self.store.update_run(
                    self.run_id,
                    no_progress_decisions=self.state.get("no_progress_decisions", 0) + 1,
                )
            attempts = self.store.model_attempts(step["step_id"])
            if any(a["status"] == "running" for a in attempts):
                return self._block("interrupted_model_attempt_requires_reconciliation")
            run = self.state
            if (
                len(attempts) >= self.limits.max_attempts_per_snapshot
                or run["model_calls"] >= self.limits.max_model_calls
                or run["charged_tokens"] + self.limits.max_tokens_per_call
                > self.limits.max_total_tokens
            ):
                self.store.update_step(
                    step["step_id"], status="cannot_proceed", local_verdict="model_budget_exhausted"
                )
                return await self._stop_and_check("model_budget_exhausted")
            attempt = self.store.begin_model_attempt(
                step["step_id"], self.limits.max_tokens_per_call
            )
            context = {
                "goal": copy.deepcopy(self.goal),
                "observation": copy.deepcopy(snapshot["observation"]),
                "proposal_schema": copy.deepcopy(self.proposal_schema),
                "decision_schema": copy.deepcopy(self._decision_schema),
            }
            decision = None
            try:
                decision = await asyncio.wait_for(self.decide(context), self.limits.model_timeout_s)
                self._decision_validator.validate(decision)
            except asyncio.CancelledError:
                self.store.finish_model_attempt(attempt, "interrupted", None, error="cancelled")
                self.store.update_step(
                    step["step_id"], status="discarded", local_verdict="cancelled"
                )
                raise
            except Exception as exc:
                usage = (
                    decision.get("usage")
                    if isinstance(decision, dict)
                    else getattr(exc, "usage", None)
                )
                self.store.finish_model_attempt(
                    attempt,
                    "invalid" if isinstance(exc, ValidationError) else "failed",
                    usage,
                    error=type(exc).__name__,
                )
                return self.state
            self.store.finish_model_attempt(attempt, "completed", decision.get("usage"))
            if self._cancel_event.is_set() or self.state["cancelled"]:
                self.store.update_step(
                    step["step_id"], status="discarded", local_verdict="cancelled_during_inference"
                )
                return self.state
            fresh = await self._read_state()
            if fresh is None:
                self.store.update_step(
                    step["step_id"],
                    status="discarded",
                    local_verdict="binding_or_snapshot_unavailable",
                )
                return self.state
            fresh_status, fresh_snapshot = fresh
            if (
                self._snapshot_key(fresh_snapshot) != key
                or fresh_snapshot.get("inflight_execution")
                or not fresh_snapshot.get("interaction", {}).get("can_submit")
                or fresh_status.get("status") in TERMINAL_SESSION
                or self._cancel_event.is_set()
                or self.state["cancelled"]
            ):
                self.store.update_step(
                    step["step_id"], status="discarded", local_verdict="stale_after_inference"
                )
                return self.state
            if self.state["charged_tokens"] > self.limits.max_total_tokens:
                self.store.update_step(
                    step["step_id"],
                    status="discarded",
                    local_verdict="model_token_budget_exhausted",
                )
                return await self._stop_and_check("model_token_budget_exhausted")
            if time.time() - run["created_at"] >= budget.get("wall_time_s", float("inf")):
                self.store.update_step(
                    step["step_id"], status="discarded", local_verdict="wall_time_budget_exhausted"
                )
                return await self._stop_and_check("wall_time_budget_exhausted")
            kind = decision["kind"]
            if kind != "proposal":
                self.store.update_step(
                    step["step_id"], status=kind, local_verdict=decision.get("reason")
                )
                if kind == "wait":
                    return self.state
                return await self._stop_and_check(kind)
            payload = {
                "protocol": self.start_arguments["protocol"],
                "task_id": run["task_id"],
                "revision_id": run["revision_id"],
                "interaction_run_id": self.run_id,
                "session_invocation_id": run["session_invocation_id"],
                "step_id": step["step_id"],
                "submission_id": "submission_" + uuid.uuid4().hex,
                "runner_epoch": fresh_snapshot["runner_epoch"],
                "expected_control_version": fresh_snapshot["control_version"],
                "based_on_observation": fresh_snapshot["observation"]["ref"],
                "world_epoch": fresh_snapshot["observation"]["world_epoch"],
                "proposal": decision["proposal"],
            }
            intent = self.store.submission_intent(step["step_id"], payload)
            try:
                receipt = await self._io(self.client.submit(copy.deepcopy(payload)))
                self._record_receipt(intent, receipt)
            except Exception as exc:
                saved = next(
                    s
                    for s in self.store.submissions(self.run_id)
                    if s["submission_id"] == payload["submission_id"]
                )
                self.store.update_submission(
                    payload["submission_id"],
                    status="uncertain",
                    invocation_id=getattr(exc, "invocation_id", None) or saved.get("invocation_id"),
                    gateway_status=getattr(exc, "gateway_status", None)
                    or saved.get("gateway_status"),
                )
                self.store.update_step(step["step_id"], status="reconciling")
                self.store.update_run(
                    self.run_id,
                    state="reconciling",
                    recovery_required=True,
                    gateway_uncertain=isinstance(exc, GatewayUnknownError),
                )
                await self._recover()
            return self.state

    async def _stop_and_check(self, reason: str) -> dict:
        async with self._stop_lock:
            self.store.update_run(self.run_id, state="needs_stop", completion_reason=reason)
            session = self.state["session_invocation_id"]
            if not session:
                return self._block("cannot_stop_original_session")
            if not self.state["stop_requested"]:
                self.store.update_run(self.run_id, stop_requested=True)
                self.store.event(self.run_id, "stop_intent", {"session_invocation_id": session})
                try:
                    response = await asyncio.wait_for(
                        self.client.stop(session),
                        timeout=min(self.limits.io_timeout_s, self.limits.stop_timeout_s),
                    )
                    self.store.event(self.run_id, "stop_response", response)
                except Exception as exc:
                    self.store.event(
                        self.run_id, "stop_transport_error", {"type": type(exc).__name__}
                    )
                    if isinstance(exc, GatewayUnknownError):
                        self.store.update_run(self.run_id, gateway_uncertain=True)
            deadline = time.monotonic() + self.limits.stop_timeout_s
            for _ in range(self.limits.stop_poll_limit):
                if time.monotonic() >= deadline:
                    break
                try:
                    observed = await asyncio.wait_for(
                        self._read_state(), timeout=max(0.001, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    break
                if observed:
                    status, snapshot = observed
                    if (
                        status.get("status") in TERMINAL_SESSION
                        and snapshot.get("interaction_phase")
                        not in {"unknown", "reconciling", "executing"}
                        and snapshot.get("inflight_execution") is None
                        and not self.state["gateway_uncertain"]
                    ):
                        return self.store.update_run(self.run_id, state="needs_verification")
                await asyncio.sleep(
                    min(self.limits.poll_interval_s, max(0, deadline - time.monotonic()))
                )
            return self._block("stop_not_confirmed_within_budget")

    async def cancel(self) -> dict:
        self._own()
        self._cancel_event.set()
        self.store.create_run(self.start_arguments, self.goal, self.proposal_schema)
        self.store.update_run(self.run_id, cancelled=True)
        self.store.event(self.run_id, "user_cancelled", {})
        for step in self.store.steps(self.run_id):
            if step["status"] == "proposed":
                self.store.update_step(
                    step["step_id"], status="discarded", local_verdict="cancelled"
                )
        if self.state["state"] == "start_intent":
            return self.store.update_run(
                self.run_id, state="cancelled", reason="cancelled_before_start"
            )
        return await self._stop_and_check("cancelled")

    def _finalization_blocker(self, observed: tuple[dict, dict] | None) -> str | None:
        run = self.state
        if not observed:
            return "missing_current_evidence"
        if run["gateway_uncertain"] or run["recovery_required"] or run["state"] == "blocked":
            return "unresolved_recovery_or_gateway_state"
        status, snapshot = observed
        if status.get("status") not in TERMINAL_SESSION:
            return "session_not_terminal"
        if snapshot.get("inflight_execution") is not None or snapshot.get("interaction_phase") in {
            "unknown",
            "reconciling",
            "executing",
            "ready",
        }:
            return "physical_execution_not_quiescent"
        if any(s["receipt"] is None for s in self.store.submissions(self.run_id)):
            return "unresolved_submission"
        if any(s["status"] not in TERMINAL_STEP for s in self.store.steps(self.run_id)):
            return "unresolved_decision_step"
        if run["cancelled"]:
            return "user_cancelled"
        return None

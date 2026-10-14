"""One decision stream per environmental episode, with durable no-resend intents."""

import asyncio
import sqlite3
import time
from uuid import uuid4

from jsonschema import validate

from PhyAgentOS.forge.benchmark.client import SCOPE_KEYS
from PhyAgentOS.forge.benchmark.contracts import EpisodeSnapshot
from PhyAgentOS.forge.interaction.contracts import digest


class EpisodeRunner:
    def __init__(self, supervisor, client, child_id):
        self.supervisor, self.client = supervisor, client
        self.store, self.service = client.store, client.service
        self.child_id = child_id
        self.cap = client.child_capability(self.child)
        self.plan = client.binding["plan"]
        self.deployment = self.service.deployment

    @property
    def child(self):
        return self.store.get("subtask", self.child_id)

    def update(self, **changes):
        return self.store.update("subtask", self.child_id, **changes)

    def args(self, **extra):
        return {**{k: self.child[k] for k in SCOPE_KEYS}, **extra}

    async def query(self, view="episode", **extra):
        return await asyncio.wait_for(
            self.client.query(self.cap, self.args(view=view, **extra)),
            self.deployment.control_timeout_s,
        )

    async def snapshot(self):
        raw = await self.query()
        snapshot = EpisodeSnapshot.model_validate(raw).model_dump()
        child = self.child
        if any(snapshot[k] != child[k] for k in SCOPE_KEYS):
            raise RuntimeError("cross-episode snapshot")
        prior = child.get("snapshot")
        if prior:
            if snapshot["observation_version"] < prior["observation_version"]:
                raise RuntimeError("observation version rollback")
            if (
                snapshot["observation_version"] == prior["observation_version"]
                and snapshot["observation"] != prior["observation"]
            ):
                raise RuntimeError("observation changed without version advance")
        self.update(snapshot=snapshot)
        key = (
            self.child_id + ":" + str(snapshot["observation_version"]) + ":" + digest(snapshot)[:16]
        )
        if not self.store.get("snapshot", key):
            self.store.put("snapshot", key, self.client.task_id, snapshot, create=True)
        return snapshot

    async def submit(self, operation, *, cleanup=False, **extra):
        request_id = (
            self.child_id + ":" + operation + ":" + str(extra.get("observation_version", 0))
        )
        return await asyncio.wait_for(
            self.client.submit(
                self.cap,
                self.args(operation=operation, request_id=request_id, **extra),
                cleanup=cleanup,
            ),
            self.deployment.control_timeout_s,
        )

    async def run(self):
        try:
            child = self.child
            await self.submit(
                "episode.start", case=child["case"], lessons_digest=child["lessons_digest"]
            )
            self.update(state="running")
            started = time.monotonic()
            while time.monotonic() - started < self.plan["episode_timeout_s"]:
                self.service.assert_dispatch(self.client.task_id)
                snapshot = await self.snapshot()
                if snapshot["state"] == "terminal":
                    break
                if snapshot["state"] == "unknown":
                    raise RuntimeError("executor episode is unknown")
                if snapshot["state"] != "ready":
                    await asyncio.sleep(self.deployment.poll_interval_s)
                    continue
                key = self.child_id + ":decision:" + str(snapshot["observation_version"])
                if self.store.get("decision", key):
                    # Even invalid output never causes repeat inference for this observation.
                    await asyncio.sleep(self.deployment.poll_interval_s)
                    continue
                child = self.child
                if child["model_calls"] >= self.plan["max_decisions"]:
                    self.update(reason="decision_budget_exhausted")
                    break
                self.store.put(
                    "decision",
                    key,
                    self.client.task_id,
                    {
                        "step_id": key,
                        "subtask_id": self.child_id,
                        "observation": snapshot,
                        "snapshot_digest": digest(snapshot),
                        "status": "inferring",
                        "decision": None,
                    },
                    create=True,
                )
                self.update(model_calls=child["model_calls"] + 1)
                context = self.context(snapshot)
                attempt_id = "model_" + uuid4().hex
                self.store.put(
                    "model",
                    attempt_id,
                    self.client.task_id,
                    {
                        "attempt_id": attempt_id,
                        "step_id": key,
                        "status": "started",
                        "lessons_digest": child["lessons_digest"],
                        "episodic_experience_digest": digest(context.get("episodic_experience", [])),
                        "started_at": time.time(),
                    },
                    create=True,
                )
                try:
                    decision = await asyncio.wait_for(
                        self.deployment.decide(context), self.plan["model_timeout_s"]
                    )
                    self.validate_decision(decision)
                    self.store.update(
                        "model",
                        attempt_id,
                        status="completed",
                        usage=decision.get("usage"),
                        finished_at=time.time(),
                    )
                except asyncio.CancelledError as exc:
                    self.store.update(
                        "model", attempt_id, status="cancelled", finished_at=time.time(),
                        usage=getattr(exc, "usage", None)
                    )
                    self.store.update("decision", key, status="discarded")
                    raise
                except Exception as exc:
                    self.store.update(
                        "model",
                        attempt_id,
                        status="failed",
                        error=type(exc).__name__,
                        finished_at=time.time(),
                        usage=getattr(exc, "usage", None),
                    )
                    raise
                self.service.assert_dispatch(self.client.task_id)
                await self.service.validate(self.client.task_id, self.client.runtime)
                latest = await self.snapshot()
                if digest(latest) != digest(snapshot):
                    self.store.update("decision", key, status="discarded", decision=decision)
                    continue
                self.store.update("decision", key, status="decided", decision=decision)
                if decision["kind"] == "finish_candidate":
                    break
                await self.submit(
                    "decision.submit",
                    observation_version=snapshot["observation_version"],
                    observation_digest=digest(snapshot["observation"]),
                    step_id=key,
                    proposal=decision["proposal"],
                )
                self.store.update("decision", key, status="submitted")
            else:
                self.update(reason="episode_timeout")
        except asyncio.CancelledError:
            self.update(cancelled=True, reason="runner_cancelled")
            raise
        except Exception as exc:
            if isinstance(exc, (OSError, sqlite3.Error)):
                # Storage is shared by all workers. Even a partially writable journal
                # must not allow another child to continue admitting physical work.
                self.service.closing = True
                self.store.update(
                    "run",
                    self.client.task_id,
                    admission_closed=True,
                    reconciliation_required=True,
                    reason="shared_storage_failure",
                )
            self.update(state="unknown", reason=f"{type(exc).__name__}: {exc}")
        finally:
            await self.stop_and_check()

    def context(self, snapshot):
        child = self.child
        round_state = self.store.get("round", child["round_id"])
        lessons = round_state["lessons"]
        if digest(lessons) != child["lessons_digest"]:
            raise RuntimeError("round lessons changed")
        context = {
            "goal": child["case"],
            "observation": snapshot["observation"],
            "lessons": lessons,
            "lessons_digest": child["lessons_digest"],
            "proposal_schema": self.client.binding["extension"]["proposal_schema"],
            "decision_schema": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "proposal"},
                            "proposal": self.client.binding["extension"]["proposal_schema"],
                        },
                        "required": ["kind", "proposal"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "finish_candidate"},
                            "reason": {"type": "string", "maxLength": 1000},
                        },
                        "required": ["kind", "reason"],
                        "additionalProperties": False,
                    },
                ]
            },
        }
        if hasattr(self.deployment, "experience_for"):
            context["episodic_experience"] = self.deployment.experience_for(child["case"])
            context["episodic_experience_digest"] = digest(context["episodic_experience"])
        artifacts = snapshot["observation"].get("artifacts", [])
        if artifacts:
            resolver = self.deployment.artifacts
            if resolver is None:
                raise RuntimeError("observation media has no trusted resolver")
            context["media"] = [resolver.resolve(self.args(), snapshot, ref) for ref in artifacts]
        return context

    def validate_decision(self, decision):
        if not isinstance(decision, dict):
            raise ValueError("decision must be structured JSON")
        schema = self.context(self.child["snapshot"])["decision_schema"]
        validate({k: v for k, v in decision.items() if k != "usage"}, schema)

    async def stop_and_check(self):
        child = self.child
        if child["settled"]:
            return True
        self.update(needs_stop=True)
        try:
            key = self.child_id + ":episode.stop:0"
            if not self.store.get("intent", key):
                await self.submit("episode.stop", cleanup=True)
            for _ in range(3):
                # Unknown POSTs are reconciled only by scoped receipt queries.
                for intent in self.store.list("intent", self.client.task_id):
                    if intent["arguments"].get("subtask_id") != self.child_id or intent["receipt"]:
                        continue
                    result = await self.query("receipt", request_id=intent["request_id"])
                    if result.get("status") == "found":
                        self.client.accept_receipt(intent["request_id"], result["receipt"])
                snapshot = await self.snapshot()
                if snapshot["stopped"] and snapshot["state"] == "terminal":
                    self.update(needs_stop=False, reconciliation_required=True)
                intents = [
                    i
                    for i in self.store.list("intent", self.client.task_id)
                    if i["arguments"].get("subtask_id") == self.child_id
                ]
                if (
                    snapshot["stopped"]
                    and snapshot["state"] == "terminal"
                    and all(i["receipt"] for i in intents)
                ):
                    task = self.service.coordinator.get_task(self.client.task_id)
                    for intent in intents:
                        if any(
                            record.status == "unknown" and record.arguments == intent["arguments"]
                            for record in task.execution_records
                        ):
                            await asyncio.wait_for(
                                self.client.reconcile_invocation(intent["request_id"]),
                                self.deployment.control_timeout_s,
                            )
                    history = await self.query("history")
                    records = history.get("records")
                    final = {k: v for k, v in snapshot.items() if k != "history_digest"}
                    if (
                        history.get("complete") is not True
                        or not isinstance(records, list)
                        or not records
                        or records[-1] != final
                        or digest(records) != snapshot["history_digest"]
                    ):
                        raise RuntimeError("missing or incomplete authoritative trajectory")
                    for record in records:
                        if any(record.get(k) != child[k] for k in SCOPE_KEYS):
                            raise RuntimeError("cross-episode trajectory")
                    self.update(
                        state="terminal",
                        settled=True,
                        needs_stop=False,
                        reconciliation_required=False,
                        outcome=snapshot["outcome"],
                        native_score=snapshot["native_score"],
                        history=records,
                        history_digest=digest(records),
                        finished_at=time.time(),
                    )
                    return True
                await asyncio.sleep(self.deployment.poll_interval_s)
        except Exception as exc:
            self.update(state="unknown", reconciliation_reason=f"{type(exc).__name__}: {exc}",
                        reason=self.child.get("reason") or f"reconciliation: {type(exc).__name__}: {exc}")
        return False

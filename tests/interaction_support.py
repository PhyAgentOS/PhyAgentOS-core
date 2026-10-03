"""Durable three-operation fake, with no network, models or game dependency."""

import copy
import json
import sqlite3

import httpx
import yaml

from PhyAgentOS.agent.experience.activation import SkillActivationManager
from PhyAgentOS.agent.experience.store import ExperienceStore
from PhyAgentOS.agent.interaction.runner import RunnerLimits
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.binding import ForgeSkillBindingResolver
from PhyAgentOS.forge.interaction.contracts import digest
from PhyAgentOS.forge.interaction.service import InProcessInteractionDeployment
from PhyAgentOS.forge.task import AgentTaskCoordinator
from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.skill_runtime.installer import SkillInstaller
from PhyAgentOS.skill_runtime.integration import (
    ActiveRuntimeRegistry,
    ActiveSkillRuntime,
    DynamicForgeToolClient,
)
from PhyAgentOS.skill_runtime.state import RuntimeStateStore
from PhyAgentOS.verification.contracts import (
    CriterionVerdict,
    TaskVerificationContract,
    VerificationAttempt,
    VerificationEvidencePolicy,
    VerificationVerdict,
)
from PhyAgentOS.verification.request_builder import VerificationRequestBuilder
from scripts.package_skill import package

SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"const": "advance"},
        "arguments": {
            "type": "object",
            "properties": {"distance": {"type": "integer", "minimum": 1, "maximum": 1}},
            "required": ["distance"],
            "additionalProperties": False,
        },
    },
    "required": ["operation", "arguments"],
    "additionalProperties": False,
}
SIDECAR = {
    "protocol": "interaction_contract_v1",
    "session_tool": "grid.run",
    "snapshot_tool": "grid.snapshot",
    "submission_tool": "grid.submit",
    "proposal_schema_file": "proposal.json",
    "snapshot_views": ["snapshot", "submission", "history", "run"],
    "event_channel": {
        "mode": "polling",
        "event_type": "progress",
        "business_kind": "interaction_changed",
    },
    "execution_policy": {
        "max_active_sessions_per_task": 1,
        "max_inflight_decisions_per_run": 1,
        "runner_mode": "single_owner_no_hot_takeover",
        "completion_policy": "quiesce_then_stop",
    },
    "access_policy": "private_control_plane",
}


def source_bundle(root):
    bundle = root / "grid"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\nname: grid\ndescription: fake grid\n---\nReach position 2."
    )
    (bundle / "flow.yaml").write_text("nodes: []\n")
    (bundle / "skill.yaml").write_text(
        yaml.safe_dump(
            {
                "manifest_version": 2,
                "name": "grid",
                "version": "1.0.0",
                "description": "fake grid",
                "skill_document": "SKILL.md",
                "gateway_url": "http://fake.invalid",
                "required_tools": ["grid.run", "grid.snapshot", "grid.submit"],
                "profiles": {"local": {"dataflow": "flow.yaml"}},
            }
        )
    )
    (bundle / "interaction.yaml").write_text(yaml.safe_dump(SIDECAR))
    (bundle / "proposal.json").write_text(json.dumps(SCHEMA))
    return bundle


class FakeGrid:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT);"
            "CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, data TEXT);"
            "CREATE TABLE IF NOT EXISTS events (cursor INTEGER PRIMARY KEY, data TEXT);"
        )
        self.calls = []
        self.invocations = {}
        self.start_timeout = self.submit_timeout = self.stop_timeout = False
        self.unknown = self.missing = False
        self.stop_calls = 0
        self.specs = {
            f"grid.{op}": {
                "tool_id": f"grid.{op}",
                "endpoint_id": "grid",
                "operation": op,
                "semantics": semantics,
                "input_schema": {"type": "object"},
            }
            for op, semantics in [("run", "session"), ("snapshot", "query"), ("submit", "action")]
        }

    def load(self):
        row = self.db.execute("SELECT data FROM runs").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, run):
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?)",
            (run["interaction_run_id"], json.dumps(run)),
        )
        cursor = run["snapshot"]["executor_cursor"]
        self.db.execute(
            "INSERT OR IGNORE INTO events VALUES (?, ?)",
            (
                cursor,
                json.dumps(
                    {"cursor": cursor, "kind": "state", "data": copy.deepcopy(run["snapshot"])}
                ),
            ),
        )
        self.db.commit()

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        args = body.get("arguments", {}) if isinstance(body, dict) else {}
        self.calls.append((request.method, path, copy.deepcopy(args)))

        def response(data, code=200):
            return httpx.Response(code, json={"ok": True, "data": data})

        if path.startswith("/tools/") and request.method == "GET":
            tool = path.split("/")[2]
            return response(
                {"ready": True, "binding_error": None}
                if path.endswith("/context")
                else self.specs[tool]
            )
        if path == "/tools/grid.run:invoke":
            if self.load():
                raise AssertionError("unexpected second Session POST")
            run = copy.deepcopy(args)
            run["start_payload_digest"] = digest(args)
            run["snapshot"] = {
                **args,
                "session_invocation_id": "session_1",
                "executor_boot_id": "boot_1",
                "runner_epoch": 1,
                "control_version": 1,
                "executor_cursor": 1,
                "interaction_phase": "ready",
                "observation": {"ref": "obs_0", "world_epoch": "world_1", "data": {"position": 0}},
                "interaction": {"can_submit": True},
                "inflight_execution": None,
                "last_step": None,
            }
            self.save(run)
            self.invocations["session_1"] = "running"
            if self.start_timeout:
                raise httpx.ReadTimeout("lost start receipt", request=request)
            return response({"invocation_id": "session_1", "attempt_id": "start_attempt"}, 202)
        if path == "/tools/grid/snapshot:invoke":
            run = self.load()
            if run is None:
                output = {"status": "unknown", "reason": "run_not_found"}
            elif args["view"] == "run":
                output = {
                    "status": "found",
                    "run": run["snapshot"],
                    "start_payload_digest": run["start_payload_digest"],
                }
            elif args["view"] == "submission":
                row = self.db.execute(
                    "SELECT data FROM receipts WHERE id=?", (args["submission_id"],)
                ).fetchone()
                receipt = json.loads(row[0]) if row else None
                output = (
                    {
                        "status": "found",
                        "receipt": receipt,
                        "payload_digest": receipt["payload_digest"],
                    }
                    if receipt
                    else {"status": "absent_confirmed"}
                )
            elif args["view"] == "history":
                events = [
                    json.loads(r[0])
                    for r in self.db.execute(
                        "SELECT data FROM events WHERE cursor>? ORDER BY cursor LIMIT ?",
                        (args.get("after_executor_cursor", 0), args.get("limit", 100)),
                    )
                ]
                output = {
                    "events": events,
                    "next_executor_cursor": events[-1]["cursor"]
                    if events
                    else args.get("after_executor_cursor", 0),
                    "retention_watermark": 0,
                    "gap": False,
                }
            else:
                output = run["snapshot"]
            return response({"response": {"result": {"status": "succeeded", "outputs": output}}})
        if path == "/tools/grid.submit:invoke":
            run = self.load()
            snapshot = run["snapshot"]
            payload_digest = digest({k: v for k, v in args.items() if k != "runner_epoch"})
            assert args["expected_control_version"] == snapshot["control_version"]
            assert snapshot["interaction"]["can_submit"]
            position = snapshot["observation"]["data"]["position"] + 1
            invocation = f"action_{position}"
            receipt = {
                "accepted": True,
                "interaction_run_id": args["interaction_run_id"],
                "submission_id": args["submission_id"],
                "step_id": args["step_id"],
                "control_version": snapshot["control_version"] + 1,
                "execution_status": "pending",
                "execution_id": f"execution_{position}",
                "payload_digest": payload_digest,
                "reason": None,
                "retryable": False,
            }
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?)", (args["submission_id"], json.dumps(receipt))
            )
            snapshot.update(
                control_version=snapshot["control_version"] + 1,
                executor_cursor=snapshot["executor_cursor"] + 1,
                observation={
                    "ref": f"obs_{position}",
                    "world_epoch": "world_1",
                    "data": {"position": position},
                },
                last_step={
                    "step_id": args["step_id"],
                    "status": "unknown" if self.unknown else "succeeded",
                    "execution_id": f"execution_{position}",
                    "result_ref": f"result_{position}",
                },
            )
            if self.unknown:
                snapshot.update(
                    interaction_phase="reconciling",
                    inflight_execution={"status": "unknown"},
                    interaction={"can_submit": False},
                )
            self.save(run)
            self.invocations[invocation] = "succeeded"
            if self.submit_timeout:
                raise httpx.ReadTimeout("lost submit receipt", request=request)
            return response(
                {"invocation_id": invocation, "attempt_id": "attempt_" + invocation}, 202
            )
        if path.startswith("/invocations/"):
            invocation = path.split("/")[2]
            if self.missing or invocation not in self.invocations:
                return httpx.Response(404, json={"ok": False, "error": {"message": "missing"}})
            if path.endswith("/stop"):
                self.stop_calls += 1
                if self.stop_timeout:
                    raise httpx.ReadTimeout("stop lost", request=request)
                run = self.load()
                run["snapshot"]["interaction"]["can_submit"] = False
                if not self.unknown:
                    self.invocations[invocation] = "stopped"
                    run["snapshot"]["interaction_phase"] = "stopped"
                    run["snapshot"]["control_version"] += 1
                    run["snapshot"]["executor_cursor"] += 1
                    self.save(run)
                return response({"status": "requested"}, 202)
            if path.endswith("/result"):
                row = self.db.execute(
                    "SELECT data FROM receipts ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                return response(
                    {
                        "status": "available",
                        "result": {"status": "succeeded", "outputs": json.loads(row[0])},
                    }
                )
            return response(
                {
                    "invocation_id": invocation,
                    "phase": self.invocations[invocation],
                    "status": self.invocations[invocation],
                }
            )
        raise AssertionError(path)


class GridVerifier:
    def __init__(self, workspace):
        self.workspace, self.calls = workspace, 0

    async def verify_agent_task(self, task, *, events, lessons, **kwargs):
        request = VerificationRequestBuilder(self.workspace).build_agent_task(
            task, events=events, lessons=lessons
        )
        after = [a for a in request.evidence.artifacts if a.phase == "after"][-1]
        snapshot = json.loads((self.workspace / after.uri).read_text())
        success = snapshot["observation"]["data"]["position"] == 2
        self.calls += 1
        verdict = VerificationVerdict(
            verdict="success" if success else "failure",
            criteria=[
                CriterionVerdict(
                    criterion="position is 2",
                    status="satisfied" if success else "unsatisfied",
                    evidence_refs=[after.artifact_id],
                )
            ],
            evidence_refs=[after.artifact_id],
            reason="independent grid position check",
            lesson="check final state",
        )
        return verdict, request, VerificationAttempt(attempt_id="verify_1")


async def system(tmp_path, monkeypatch, *, decide=None, enabled=True, long_lived=True):
    source = source_bundle(tmp_path / "source")
    installed = tmp_path / "skills"
    SkillInstaller(installed, state_store=RuntimeStateStore(tmp_path / "runtime-state")).install(
        package(source, tmp_path / "archives")
    )
    monkeypatch.setattr("PhyAgentOS.agent.skills.get_config_path", lambda: tmp_path / "config.json")
    grid = FakeGrid(tmp_path / "executor.sqlite3")
    client = ForgeToolClient("http://fake.invalid", transport=httpx.MockTransport(grid))
    runtime = ActiveSkillRuntime(
        "grid",
        "1.0.0",
        "local",
        "runtime_1",
        "http://fake.invalid",
        "gateway_1",
        client,
        set(),
        set(),
        set(),
    )
    registry = ActiveRuntimeRegistry(runtime)
    resolver = ForgeSkillBindingResolver(registry, catalog=SkillCatalog(installed))
    workspace = tmp_path / "workspace"
    activation = SkillActivationManager(
        workspace=workspace,
        store=ExperienceStore(workspace),
        runtime_availability_provider=lambda name: True,
        binding_resolver=resolver,
    )
    activation.begin_turn("cli:test", "reach position 2")
    activated, _, _ = await activation.activate(session_key="cli:test", name="grid", role="primary")
    coordinator = AgentTaskCoordinator(
        workspace=workspace,
        config=ForgeConfig(interaction={"enabled": enabled}, poll_interval_s=0.1),
        client=DynamicForgeToolClient(registry),
        binding_resolver=resolver,
        activation_manager=activation,
        verifier=GridVerifier(workspace),
        runtime_invocation_ids=runtime.invocation_ids,
        runtime_session_ids=runtime.session_ids,
        runtime_task_binding_ids=runtime.task_binding_ids,
    )
    task = await coordinator.create_task(
        task_description="reach position 2",
        activation_id=activated.activation_id,
        origin_session_key="cli:test",
        verification=TaskVerificationContract(
            mode="enforce",
            goal="reach 2",
            success_criteria=["position is 2"],
            evidence_policy=VerificationEvidencePolicy(
                required_kinds=["interaction_observation"], minimum_association="authoritative"
            ),
        ),
    )

    async def default_decide(context):
        if context["observation"]["data"]["position"] >= 2:
            return {"kind": "finish_candidate", "reason": "check target"}
        return {
            "kind": "proposal",
            "proposal": {"operation": "advance", "arguments": {"distance": 1}},
        }

    supervisor = coordinator.interaction.enable_in_process(
        InProcessInteractionDeployment(
            worlds={"world": {"decisions": 8, "actions": 8, "wall_time_s": 30, "idle_time_s": 10}},
            decide=decide or default_decide,
            limits=RunnerLimits(poll_interval_s=0.001, stop_timeout_s=0.02, stop_poll_limit=2),
        ),
        long_lived=long_lived,
    )
    return coordinator, task, grid, supervisor, registry

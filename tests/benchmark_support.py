"""Deterministic isolated workers using the real Forge HTTP/Coordinator path."""

import copy
import json

import httpx
import yaml
from interaction_support import source_bundle as grid_bundle

from PhyAgentOS.agent.experience.activation import SkillActivationManager
from PhyAgentOS.agent.experience.store import ExperienceStore
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.benchmark.service import InProcessBenchmarkDeployment
from PhyAgentOS.forge.binding import ForgeSkillBindingResolver
from PhyAgentOS.forge.interaction.contracts import digest
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
from PhyAgentOS.verification.contracts import TaskVerificationContract
from scripts.package_skill import package

KEYS = (
    "task_id",
    "run_id",
    "subtask_id",
    "attempt_id",
    "worker_id",
    "world_epoch",
    "binding_digest",
)


class FakeBenchmark:
    def __init__(self):
        self.root = None
        self.episodes, self.receipts, self.invocations, self.history = {}, {}, {}, {}
        self.calls = []
        self.lose_start = self.lose_submit = self.stop_timeout = self.missing = False
        self.active, self.max_active = set(), 0
        self.specs = {
            f"grid.{op}": {
                "tool_id": f"grid.{op}",
                "endpoint_id": "grid",
                "operation": op,
                "semantics": semantic,
                "input_schema": {"type": "object"},
            }
            for op, semantic in [("run", "session"), ("snapshot", "query"), ("submit", "action")]
        }

    def save(self, snapshot):
        key = snapshot["subtask_id"]
        self.history.setdefault(key, []).append(
            {k: copy.deepcopy(v) for k, v in snapshot.items() if k != "history_digest"}
        )
        snapshot["history_digest"] = digest(self.history[key])
        self.episodes[key] = snapshot

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        args = body.get("arguments", {})
        self.calls.append((request.method, path, copy.deepcopy(args)))

        def response(data, code=200):
            return httpx.Response(code, json={"ok": True, "data": data})

        if path.startswith("/tools/") and request.method == "GET":
            return response(
                {"ready": True, "binding_error": None}
                if path.endswith("/context")
                else self.specs[path.split("/")[2]]
            )
        if path == "/tools/grid.run:invoke":
            assert self.root is None
            self.root = {
                **args,
                "start_digest": digest(args),
                "session_invocation_id": "root-session",
                "stopped": False,
            }
            self.invocations["root-session"] = "running"
            if self.lose_start:
                raise httpx.ReadTimeout("lost root reply")
            return response({"invocation_id": "root-session", "attempt_id": "root-attempt"}, 202)
        if path == "/tools/grid/snapshot:invoke":
            if args["view"] == "root":
                output = self.root
            elif args["view"] == "episode":
                output = self.episodes[args["subtask_id"]]
            elif args["view"] == "history":
                output = {"complete": True, "records": self.history[args["subtask_id"]]}
            else:
                receipt = self.receipts.get(args["request_id"])
                output = (
                    {"status": "found", "receipt": receipt} if receipt else {"status": "unknown"}
                )
            return response({"response": {"result": {"status": "succeeded", "outputs": output}}})
        if path == "/tools/grid.submit:invoke":
            op, key = args["operation"], args["subtask_id"]
            assert args["request_id"] not in self.receipts
            invocation = "invocation-" + str(len(self.receipts))
            if op == "episode.start":
                self.active.add(key)
                self.max_active = max(self.max_active, len(self.active))
                snapshot = {
                    **{k: args[k] for k in KEYS},
                    "observation_version": 0,
                    "observation": {"position": 0},
                    "state": "ready",
                    "outcome": None,
                    "native_score": None,
                    "stopped": False,
                }
            else:
                snapshot = copy.deepcopy(self.episodes[key])
                if op == "decision.submit":
                    assert snapshot["observation_version"] == args["observation_version"]
                    assert digest(snapshot["observation"]) == args["observation_digest"]
                    snapshot.update(
                        observation_version=snapshot["observation_version"] + 1,
                        observation={
                            "position": snapshot["observation"]["position"]
                            + args["proposal"]["distance"]
                        },
                    )
                    if snapshot["observation"]["position"] >= 2:
                        snapshot.update(state="terminal", outcome="success", native_score=1)
                    elif args["proposal"]["distance"] == 0:
                        snapshot.update(state="terminal", outcome="failure", native_score=0)
                elif op == "episode.stop":
                    if self.stop_timeout:
                        raise httpx.ReadTimeout("stop timeout")
                    snapshot.update(
                        stopped=True, state="terminal", outcome=snapshot["outcome"] or "cancelled"
                    )
                    self.active.discard(key)
                else:
                    raise AssertionError(op)
            self.save(snapshot)
            receipt = {
                **{k: args[k] for k in KEYS},
                "request_id": args["request_id"],
                "transport_invocation_id": invocation,
                "payload_digest": digest(args),
                "accepted": True,
            }
            self.receipts[args["request_id"]] = receipt
            self.invocations[invocation] = receipt
            if self.lose_submit and op == "decision.submit":
                raise httpx.ReadTimeout("lost action reply")
            return response(
                {"invocation_id": invocation, "attempt_id": "attempt-" + invocation}, 202
            )
        if path.startswith("/invocations/"):
            invocation = path.split("/")[2]
            if self.missing:
                return httpx.Response(404, json={"ok": False, "error": {"message": "missing"}})
            if path.endswith("/stop"):
                if self.stop_timeout:
                    raise httpx.ReadTimeout("stop timeout")
                self.root["stopped"] = True
                self.invocations[invocation] = "stopped"
                return response({"status": "requested"}, 202)
            if path.endswith("/result"):
                return response(
                    {
                        "status": "available",
                        "result": {"status": "succeeded", "outputs": self.invocations[invocation]},
                    }
                )
            return response(
                {
                    "invocation_id": invocation,
                    "status": self.invocations[invocation]
                    if invocation == "root-session"
                    else "succeeded",
                    "phase": self.invocations[invocation]
                    if invocation == "root-session"
                    else "succeeded",
                }
            )
        raise AssertionError(path)


async def system(
    tmp_path, monkeypatch, *, decide=None, rounds=2, reflect=True, profile_tools=False, private=False
):
    bundle = grid_bundle(tmp_path / "source")
    if private:
        manifest = yaml.safe_load((bundle / "skill.yaml").read_text())
        manifest["gateway_url"] = "http://benchmark.private"
        (bundle / "skill.yaml").write_text(yaml.safe_dump(manifest))
    (bundle / "interaction.yaml").unlink()
    (bundle / "benchmark.yaml").write_text(
        yaml.safe_dump(
            {
                "protocol": "benchmark_contract_v1",
                "session_tool": "grid.run",
                "snapshot_tool": "grid.snapshot",
                "submission_tool": "grid.submit",
                "proposal_schema_file": "proposal.json",
                "snapshot_views": ["root", "episode", "receipt", "history"],
                "access_policy": "private_control_plane",
            }
        )
    )
    (bundle / "proposal.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"distance": {"type": "integer", "minimum": 0, "maximum": 1}},
                "required": ["distance"],
                "additionalProperties": False,
            }
        )
    )
    if profile_tools:
        manifest = yaml.safe_load((bundle / "skill.yaml").read_text())
        manifest["profiles"]["local"]["required_tools"] = manifest["required_tools"]
        manifest["required_tools"] = ["grid.run"]
        (bundle / "skill.yaml").write_text(yaml.safe_dump(manifest))
    installed = tmp_path / "skills"
    SkillInstaller(installed, state_store=RuntimeStateStore(tmp_path / "runtime-state")).install(
        package(bundle, tmp_path / "archives")
    )
    monkeypatch.setattr("PhyAgentOS.agent.skills.get_config_path", lambda: tmp_path / "config.json")
    fake = FakeBenchmark()
    base_url = "http://benchmark.private" if private else "http://fake.invalid"
    if private:
        from PhyAgentOS.forge.benchmark.private import PrivateBenchmarkTransport

        class FixtureAdapter:
            async def handle(self, request):
                return fake(request)

        transport = PrivateBenchmarkTransport(FixtureAdapter())
    else:
        transport = httpx.MockTransport(fake)
    client = ForgeToolClient(base_url, transport=transport)
    runtime = ActiveSkillRuntime(
        "grid",
        "1.0.0",
        "local",
        "runtime_1",
        base_url,
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
    activation.begin_turn("cli:test", "benchmark")
    activated, _, _ = await activation.activate(session_key="cli:test", name="grid", role="primary")
    coordinator = AgentTaskCoordinator(
        workspace=workspace,
        config=ForgeConfig(benchmark={"enabled": True}),
        client=DynamicForgeToolClient(registry),
        binding_resolver=resolver,
        activation_manager=activation,
        runtime_invocation_ids=runtime.invocation_ids,
        runtime_session_ids=runtime.session_ids,
        runtime_task_binding_ids=runtime.task_binding_ids,
    )
    task = await coordinator.create_task(
        task_description="benchmark",
        activation_id=activated.activation_id,
        origin_session_key="cli:test",
        benchmark_plan={
            "cases": [{"case_key": key, "environment": {"arm": "none"} if private else {}} for key in ("a", "b")],
            "rounds": rounds,
            "parallelism": 2,
        },
        verification=TaskVerificationContract(
            mode="enforce",
            goal="complete evaluation",
            success_criteria=["evaluation_complete"],
            evidence_policy={
                "profile": "benchmark_evaluation",
                "required_kinds": ["benchmark_report"],
            },
        ),
    )

    async def default_decide(context):
        import asyncio

        await asyncio.sleep(0.01)
        return {"kind": "proposal", "proposal": {"distance": 1 if context["lessons"] else 0}}

    async def reflection(context):
        support = [c["subtask_id"] for c in context["episodes"] if c["outcome"] == "failure"]
        return (
            [
                {
                    "workflow_key": "advance",
                    "principle": "Advance incrementally and verify progress",
                    "applies_when": ["target not reached"],
                    "does_not_apply_when": ["already terminal"],
                    "supporting_subtasks": support,
                }
            ]
            if len(support) >= 2
            else []
        )

    async def validation(candidate, support):
        return {
            "reusable": True,
            "contains_specific_answer": False,
            "unsupported_literals": [],
            "confidence": 1,
            "rationale": "workflow principle supported by two isolated failures",
        }

    if private:
        from PhyAgentOS.forge.benchmark.private import PrivateBenchmarkDeployment
        supervisor = coordinator.benchmark.enable_private_adapter(PrivateBenchmarkDeployment(
            transport=transport, workers=["worker-a", "worker-b"], decide=decide or default_decide,
            artifacts=None, control_timeout_s=0.2))
        return coordinator, task, fake, supervisor, registry
    supervisor = coordinator.benchmark.enable_in_process(
        InProcessBenchmarkDeployment(
            workers=["worker-a", "worker-b"],
            decide=decide or default_decide,
            reflect=reflection if reflect else None,
            validate_lesson=validation,
            control_timeout_s=0.2,
        )
    )
    return coordinator, task, fake, supervisor, registry

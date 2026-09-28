from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from PhyAgentOS.agent.experience.activation import SkillActivationManager
from PhyAgentOS.agent.experience.store import ExperienceStore
from PhyAgentOS.agent.tools.forge_tool_api import (
    ForgeToolActionStatusTool,
    ForgeToolSessionStatusTool,
)
from PhyAgentOS.config.schema import ForgeConfig, ForgeEvidenceConfig
from PhyAgentOS.forge.binding import ForgeSkillBindingResolver
from PhyAgentOS.forge.task import (
    AgentTaskCoordinator,
    AgentTaskError,
    AgentTaskStatus,
)
from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.skill_runtime.integration import (
    ActiveRuntimeRegistry,
    ActiveSkillRuntime,
    SkillRuntimeController,
)
from PhyAgentOS.skill_runtime.manager import RuntimeManager, RuntimeStatusReport
from PhyAgentOS.skill_runtime.state import RuntimeState, RuntimeStateStore, StateError
from PhyAgentOS.verification.contracts import (
    TaskVerificationContract,
    VerificationEvidencePolicy,
)
from scripts.package_skill import package


class FakeGatewayClient:
    def __init__(self) -> None:
        self.specs = {
            "demo.query": {
                "tool_id": "demo.query",
                "endpoint_id": "demo.endpoint",
                "operation": "read",
                "semantics": "query",
                "input_schema": {"type": "object"},
            },
            "demo.action": {
                "tool_id": "demo.action",
                "endpoint_id": "demo.endpoint",
                "operation": "act",
                "semantics": "action",
                "input_schema": {"type": "object"},
            },
            "demo.session": {
                "tool_id": "demo.session",
                "endpoint_id": "demo.endpoint",
                "operation": "serve",
                "semantics": "session",
                "input_schema": {"type": "object"},
            },
        }
        self.action_calls: list[dict[str, Any]] = []
        self.session_calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self.stop_calls: list[str] = []

    async def get_tool(self, tool_id: str) -> dict[str, Any]:
        return {"ok": True, "data": copy.deepcopy(self.specs[tool_id])}

    async def get_tool_context(self, tool_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "data": {"tool_id": tool_id, "ready": True, "binding_error": None},
        }

    async def invoke_query_tool(
        self, tool_id: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "data": {"tool_id": tool_id, "arguments": arguments, "caller_id": kwargs["caller_id"]},
        }

    async def invoke_action(
        self, tool_id: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.action_calls.append(
            {"tool_id": tool_id, "arguments": arguments, "caller_id": kwargs["caller_id"]}
        )
        number = len(self.action_calls)
        return {
            "ok": True,
            "data": {"invocation_id": f"action-{number}", "attempt_id": f"attempt-{number}"},
        }

    async def start_session(
        self, tool_id: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.session_calls.append(
            {"tool_id": tool_id, "arguments": arguments, "caller_id": kwargs["caller_id"]}
        )
        return {"ok": True, "data": {"invocation_id": f"session-{len(self.session_calls)}"}}

    async def invocation_status(self, invocation_id: str) -> dict[str, Any]:
        self.status_calls.append(invocation_id)
        return {"ok": True, "data": {"phase": "succeeded"}}

    async def stop_session(self, invocation_id: str) -> dict[str, Any]:
        self.stop_calls.append(invocation_id)
        return {
            "ok": True,
            "data": {"invocation_id": invocation_id, "stop_status": "accepted"},
        }


def _write_skill(root: Path) -> Path:
    bundle = root / "example-forge"
    profile = bundle / "profiles" / "local"
    profile.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\n"
        "name: example-forge\n"
        "description: Synthetic governed Forge workflow.\n"
        'metadata: {"PhyAgentOS":{"always":false,"requires":{"runtime":["example-forge"]}}}\n'
        "---\n\n# Example Forge workflow\n",
        encoding="utf-8",
    )
    (profile / "dataflow.yaml").write_text("nodes: []\n", encoding="utf-8")
    manifest = {
        "manifest_version": 2,
        "name": "example-forge",
        "version": "1.2.3",
        "description": "Synthetic governed Forge workflow.",
        "skill_document": "SKILL.md",
        "gateway_url": "http://127.0.0.1:9",
        "required_tools": ["demo.query", "demo.action", "demo.session"],
        "profiles": {"local": {"dataflow": "profiles/local/dataflow.yaml"}},
    }
    (bundle / "skill.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    return bundle


def _write_runtime_skill(root: Path, name: str, gateway_url: str) -> Path:
    bundle = root / name
    profile = bundle / "profiles" / "local"
    profile.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Synthetic Runtime fixture.\n---\n",
        encoding="utf-8",
    )
    (profile / "dataflow.yaml").write_text("nodes: []\n", encoding="utf-8")
    (bundle / "skill.yaml").write_text(
        yaml.safe_dump(
            {
                "manifest_version": 2,
                "name": name,
                "version": "1.0.0",
                "description": "Synthetic Runtime fixture.",
                "skill_document": "SKILL.md",
                "gateway_url": gateway_url,
                "required_tools": ["demo.query"],
                "profiles": {"local": {"dataflow": "profiles/local/dataflow.yaml"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return bundle


async def _bound_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    installed = tmp_path / "skills"
    _write_skill(installed)
    monkeypatch.setattr(
        "PhyAgentOS.agent.skills.get_config_path", lambda: tmp_path / "config.json"
    )
    client = FakeGatewayClient()
    invocations: set[str] = set()
    sessions: set[str] = set()
    task_bindings: set[str] = set()
    runtime = ActiveSkillRuntime(
        skill_name="example-forge",
        skill_version="1.2.3",
        profile="local",
        runtime_instance_id="runtime-test-1",
        gateway_url="http://127.0.0.1:9",
        gateway_identity="gateway-test-1",
        client=client,  # type: ignore[arg-type]
        invocation_ids=invocations,
        session_ids=sessions,
        task_binding_ids=task_bindings,
    )
    registry = ActiveRuntimeRegistry(runtime)
    resolver = ForgeSkillBindingResolver(registry, catalog=SkillCatalog(installed))
    workspace = tmp_path / "workspace"
    activation = SkillActivationManager(
        workspace=workspace,
        store=ExperienceStore(workspace),
        runtime_availability_provider=lambda name: name == "example-forge",
        binding_resolver=resolver,
    )
    activation.begin_turn("cli:test", "run the example workflow")
    activated, _, _ = await activation.activate(
        session_key="cli:test", name="example-forge", role="primary"
    )
    coordinator = AgentTaskCoordinator(
        workspace=workspace,
        config=ForgeConfig(
            evidence=ForgeEvidenceConfig(
                required_image_sources=[],
                capture_timeout_s=0.01,
                post_capture_timeout_s=0.01,
                connection_timeout_s=0.01,
            )
        ),
        client=client,  # type: ignore[arg-type]
        binding_resolver=resolver,
        activation_manager=activation,
        runtime_invocation_ids=invocations,
        runtime_session_ids=sessions,
        runtime_task_binding_ids=task_bindings,
    )

    async def no_capture(_task_id: str) -> None:
        return None

    monkeypatch.setattr(coordinator, "_capture_before", no_capture)
    monkeypatch.setattr(coordinator, "_capture_after", no_capture)
    task = await coordinator.create_task(
        task_description="run the example workflow",
        verification=TaskVerificationContract(
            evidence_policy=VerificationEvidencePolicy(required_kinds=[])
        ),
        activation_id=activated.activation_id,
        origin_session_key="cli:test",
    )
    return coordinator, task, client, invocations, sessions, task_bindings


async def test_binding_freezes_runtime_and_rejects_toolspec_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, task, client, _, _, task_bindings = await _bound_system(
        tmp_path, monkeypatch
    )

    binding = task.primary_skill_binding
    assert binding is not None
    assert binding.skill_version == "1.2.3"
    assert binding.runtime_instance_id == "runtime-test-1"
    assert binding.gateway_identity == "gateway-test-1"
    assert task.active_revision.skill_binding_id == binding.binding_id
    assert task_bindings == {binding.binding_id}

    query = await coordinator.invoke_query(task.task_id, "demo.query", {"key": "value"})
    assert query["data"]["caller_id"].startswith(f"paos:{task.task_id}:")

    original = copy.deepcopy(client.specs["demo.action"])
    client.specs["demo.action"]["input_schema"] = {"type": "object", "required": ["changed"]}
    with pytest.raises(AgentTaskError, match="changed after AgentTask binding"):
        await coordinator.start_action(task.task_id, "demo.action", {})
    assert client.action_calls == []
    client.specs["demo.action"] = original


async def test_session_ownership_and_terminal_binding_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, task, client, _, sessions, task_bindings = await _bound_system(
        tmp_path, monkeypatch
    )
    shared = await coordinator.start_session(
        task.task_id, "demo.session", {"mode": "shared"}, ownership="shared"
    )
    owned = await coordinator.start_session(
        task.task_id, "demo.session", {"mode": "task"}, ownership="task"
    )
    shared_id = shared["data"]["invocation_id"]
    owned_id = owned["data"]["invocation_id"]
    assert sessions == {shared_id, owned_id}

    with pytest.raises(AgentTaskError, match="shared-owned"):
        await coordinator.stop_session(task.task_id, shared_id)
    with pytest.raises(AgentTaskError, match="non-terminal"):
        await coordinator.finalize_task(task.task_id)

    await coordinator.stop_session(task.task_id, owned_id)
    coordinator.observe_session(
        task.task_id, owned_id, {"ok": True, "data": {"phase": "stopped"}}
    )
    final = await coordinator.finalize_task(task.task_id)
    assert final.status == AgentTaskStatus.SUCCEEDED
    assert sessions == {shared_id}
    assert client.stop_calls == [owned_id]
    assert task_bindings == set()


async def test_unknown_action_retains_runtime_guards_and_is_never_redispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, task, client, invocations, _, task_bindings = await _bound_system(
        tmp_path, monkeypatch
    )
    accepted = await coordinator.start_action(task.task_id, "demo.action", {"value": 1})
    invocation_id = accepted["data"]["invocation_id"]
    coordinator.observe_action(
        task.task_id, invocation_id, {"ok": True, "data": {"phase": "unknown"}}
    )
    final = await coordinator.finalize_task(task.task_id)

    assert final.status == AgentTaskStatus.FAILED
    assert len(client.action_calls) == 1
    assert invocations == {invocation_id}
    assert final.primary_skill_binding is not None
    assert task_bindings == {final.primary_skill_binding.binding_id}


async def test_restart_reconciliation_only_gets_persisted_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, task, client, invocations, sessions, task_bindings = await _bound_system(
        tmp_path, monkeypatch
    )
    accepted = await coordinator.start_action(task.task_id, "demo.action", {"value": 2})
    invocation_id = accepted["data"]["invocation_id"]

    restarted = AgentTaskCoordinator(
        workspace=coordinator.workspace,
        config=coordinator.config,
        client=client,  # type: ignore[arg-type]
        binding_resolver=coordinator.binding_resolver,
        activation_manager=coordinator.activation_manager,
        runtime_invocation_ids=invocations,
        runtime_session_ids=sessions,
        runtime_task_binding_ids=task_bindings,
        store=coordinator.store,
    )
    recovered = await restarted.reconcile_nonterminal()

    assert recovered is not None
    assert len(client.action_calls) == 1
    assert client.status_calls == [invocation_id]
    assert restarted.get_task(task.task_id).execution_records[0].status == "succeeded"


async def test_invocation_reads_check_task_ownership_before_gateway_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, task, client, _, _, _ = await _bound_system(tmp_path, monkeypatch)
    action_tool = ForgeToolActionStatusTool(client, coordinator)  # type: ignore[arg-type]
    session_tool = ForgeToolSessionStatusTool(client, coordinator)  # type: ignore[arg-type]

    action_error = json.loads(await action_tool.execute(task.task_id, "not-owned-action"))
    session_error = json.loads(await session_tool.execute(task.task_id, "not-owned-session"))

    assert action_error["error"]["type"] == "agent_task"
    assert session_error["error"]["type"] == "agent_task"
    assert client.status_calls == []


@dataclass
class _FakeRuntimeManager:
    catalog: SkillCatalog
    states: dict[str, RuntimeState]
    fail_start: str | None = None
    not_ready: str | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def start(self, skill_name: str, profile: str) -> RuntimeState:
        self.calls.append(("start", skill_name, profile))
        if skill_name == self.fail_start:
            raise RuntimeError("synthetic target startup failure")
        manifest = self.catalog.get(skill_name)
        state = RuntimeState(
            skill_name=skill_name,
            profile=profile,
            status="running",
            flow_name=f"paos-{skill_name}-{profile}",
            gateway_url=manifest.gateway_url,
            gateway_identity=f"gateway-{skill_name}",
        )
        self.states[skill_name] = state
        return state

    def status(self, skill_name: str) -> RuntimeStatusReport:
        self.calls.append(("status", skill_name))
        state = self.states[skill_name]
        manifest = self.catalog.get(skill_name)
        ready = skill_name != self.not_ready
        return RuntimeStatusReport(
            state=state,
            flow_running=ready,
            gateway_ready=ready,
            tool_contexts={tool_id: ready for tool_id in manifest.required_tools},
        )

    def stop(self, skill_name: str, *, force: bool = False) -> RuntimeState:
        self.calls.append(("stop", skill_name, force))
        return self.states[skill_name].with_status("stopped")


async def test_runtime_switch_rolls_back_when_same_gateway_target_fails(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "skills"
    gateway_url = "http://127.0.0.1:19090"
    _write_runtime_skill(installed, "old-skill", gateway_url)
    _write_runtime_skill(installed, "target-skill", gateway_url)
    catalog = SkillCatalog(installed)
    old_state = RuntimeState(
        skill_name="old-skill",
        profile="local",
        status="running",
        flow_name="paos-old-skill-local",
        gateway_url=gateway_url,
        gateway_identity="gateway-old-skill",
    )
    manager = _FakeRuntimeManager(
        catalog=catalog,
        states={"old-skill": old_state},
        fail_start="target-skill",
    )
    registry = ActiveRuntimeRegistry(
        ActiveSkillRuntime(
            skill_name="old-skill",
            skill_version="1.0.0",
            profile="local",
            runtime_instance_id=old_state.runtime_instance_id,
            gateway_url=gateway_url,
            gateway_identity=old_state.gateway_identity,
            client=FakeGatewayClient(),  # type: ignore[arg-type]
            invocation_ids=set(),
            session_ids=set(),
            task_binding_ids=set(),
        )
    )
    controller = SkillRuntimeController(
        registry,
        manager=manager,  # type: ignore[arg-type]
        catalog=catalog,
        state_store=RuntimeStateStore(tmp_path / "states"),
    )

    with pytest.raises(RuntimeError, match="synthetic target startup failure"):
        controller.switch("target-skill", "local")

    assert manager.calls == [
        ("stop", "old-skill", False),
        ("start", "target-skill", "local"),
        ("start", "old-skill", "local"),
        ("status", "old-skill"),
    ]
    restored = registry.current()
    assert restored is not None
    assert restored.skill_name == "old-skill"
    await restored.client.close()


async def test_runtime_registry_atomically_follows_persisted_switch(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "skills"
    _write_runtime_skill(installed, "runtime-skill", "http://127.0.0.1:19091")
    states = RuntimeStateStore(tmp_path / "states")
    first = RuntimeState(
        skill_name="runtime-skill",
        profile="local",
        status="running",
        flow_name="paos-runtime-skill-local",
        gateway_url="http://127.0.0.1:19091",
        gateway_identity="gateway-one",
    )
    states.save(first)
    registry = ActiveRuntimeRegistry(
        catalog=SkillCatalog(installed), state_store=states, auto_refresh=True
    )

    initial = registry.current()
    assert initial is not None
    second = RuntimeState(
        skill_name="runtime-skill",
        profile="local",
        status="running",
        flow_name="paos-runtime-skill-local",
        gateway_url="http://127.0.0.1:19091",
        gateway_identity="gateway-two",
    )
    states.save(second)
    refreshed = registry.current()

    assert refreshed is not None
    assert refreshed.runtime_instance_id == second.runtime_instance_id
    assert refreshed.gateway_identity == "gateway-two"
    assert refreshed.client is not initial.client
    for client in registry.clients_for_close():
        await client.close()


async def test_runtime_switch_cleans_unready_target_before_same_gateway_rollback(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "skills"
    gateway_url = "http://127.0.0.1:19093"
    _write_runtime_skill(installed, "old-skill", gateway_url)
    _write_runtime_skill(installed, "target-skill", gateway_url)
    catalog = SkillCatalog(installed)
    old_state = RuntimeState(
        skill_name="old-skill",
        profile="local",
        status="running",
        flow_name="paos-old-skill-local",
        gateway_url=gateway_url,
        gateway_identity="gateway-old-skill",
    )
    manager = _FakeRuntimeManager(
        catalog=catalog,
        states={"old-skill": old_state},
        not_ready="target-skill",
    )
    registry = ActiveRuntimeRegistry(
        ActiveSkillRuntime(
            skill_name="old-skill",
            skill_version="1.0.0",
            profile="local",
            runtime_instance_id=old_state.runtime_instance_id,
            gateway_url=gateway_url,
            gateway_identity=old_state.gateway_identity,
            client=FakeGatewayClient(),  # type: ignore[arg-type]
            invocation_ids=set(),
            session_ids=set(),
            task_binding_ids=set(),
        )
    )
    controller = SkillRuntimeController(
        registry,
        manager=manager,  # type: ignore[arg-type]
        catalog=catalog,
        state_store=RuntimeStateStore(tmp_path / "states"),
    )

    with pytest.raises(RuntimeError, match="did not become ready"):
        controller.switch("target-skill", "local")

    assert manager.calls == [
        ("stop", "old-skill", False),
        ("start", "target-skill", "local"),
        ("status", "target-skill"),
        ("stop", "target-skill", True),
        ("start", "old-skill", "local"),
        ("status", "old-skill"),
    ]
    restored = registry.current()
    assert restored is not None and restored.skill_name == "old-skill"
    await restored.client.close()


def test_skill_packaging_is_deterministic_and_excludes_build_junk(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path / "source")
    (skill / "notes.txt").write_text("stable payload\n", encoding="utf-8")
    junk = skill / "node_modules" / "dependency"
    junk.mkdir(parents=True)
    (junk / "ignored.js").write_text("ignored", encoding="utf-8")

    first = package(skill, tmp_path / "out-one")
    second = package(skill, tmp_path / "out-two")

    assert first.read_bytes() == second.read_bytes()


def test_force_stop_attempts_remote_controls_and_audits_unknown_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = RuntimeStateStore(tmp_path / "states")
    states.save(
        RuntimeState(
            skill_name="runtime-skill",
            profile="local",
            status="running",
            flow_name="paos-runtime-skill-local",
            gateway_url="http://127.0.0.1:19092",
            active_invocations=("action-one",),
            active_sessions=("session-one",),
            active_task_bindings=("binding-one",),
        )
    )

    class Catalog:
        @staticmethod
        def get(_name: str) -> Any:
            return SimpleNamespace(gateway_url="http://127.0.0.1:19092")

    manager = RuntimeManager(
        catalog=Catalog(),  # type: ignore[arg-type]
        state_store=states,
        runtime_root=tmp_path / "runtime",
        logs_root=tmp_path / "logs",
    )
    controls: list[tuple[str, str, str]] = []

    def control(url: str, *, reference: str, operation: str) -> dict[str, Any]:
        controls.append((url, reference, operation))
        return {
            "reference": reference,
            "operation": operation,
            "outcome": "accepted",
            "terminal_proven": False,
        }

    monkeypatch.setattr(manager, "_request_gateway_control", control)
    monkeypatch.setattr(manager, "_stop_flow", lambda *_args, **_kwargs: None)

    stopped = manager.stop("runtime-skill", force=True)

    assert controls == [
        (
            "http://127.0.0.1:19092/invocations/action-one/cancel",
            "action-one",
            "cancel",
        ),
        (
            "http://127.0.0.1:19092/invocations/session-one/stop",
            "session-one",
            "stop",
        ),
    ]
    assert stopped.status == "stopped"
    assert not stopped.active_invocations
    assert not stopped.active_sessions
    assert not stopped.active_task_bindings
    event = stopped.audit_events[-1]
    assert event["event"] == "force_stop_with_active_references"
    assert event["references"] == ["action-one", "binding-one", "session-one"]
    assert all(item["terminal_proven"] is False for item in event["control_attempts"])


def test_runtime_state_v2_rejects_old_or_incomplete_records() -> None:
    state = RuntimeState(
        skill_name="runtime-skill",
        profile="local",
        status="running",
        flow_name="paos-runtime-skill-local",
        gateway_url="http://127.0.0.1:19094",
    ).to_dict()
    state["state_version"] = 1
    with pytest.raises(StateError, match="state_version must be 2"):
        RuntimeState.from_dict(state)

    state["state_version"] = 2
    del state["runtime_instance_id"]
    with pytest.raises(StateError, match="missing field.*runtime_instance_id"):
        RuntimeState.from_dict(state)

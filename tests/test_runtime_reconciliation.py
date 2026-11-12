from dataclasses import replace

import httpx
import pytest
from test_forge_agenttask_integration_v023 import _bound_system

from PhyAgentOS.forge.reconciliation import reconcile_runtime_references
from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.skill_runtime.state import RuntimeState, RuntimeStateStore


@pytest.mark.parametrize(
    "boundary",
    ["replacement", "busy", "valid", "running", "unknown", "unreachable", "journal_error"],
)
async def test_agent_recovery_uses_one_locked_identity_checked_path(
    tmp_path, monkeypatch, boundary
):
    from PhyAgentOS.forge.task import AgentTaskCoordinator
    from PhyAgentOS.skill_runtime.integration import (
        ActiveSkillRuntime,
        DynamicForgeToolClient,
        DynamicRuntimeSet,
        PersistentInvocationSet,
        PersistentSessionSet,
        PersistentTaskBindingSet,
    )
    from PhyAgentOS.skill_runtime.locking import SkillOperationLock

    coordinator, task, _, _, _, _ = await _bound_system(tmp_path, monkeypatch)
    accepted = await coordinator.start_action(task.task_id, "demo.action", {})
    invocation = accepted["data"]["invocation_id"]
    coordinator.observe_action(task.task_id, invocation, {"ok": True, "data": {"phase": "unknown"}})
    binding = task.primary_skill_binding
    states = RuntimeStateStore(tmp_path / "states")
    state = RuntimeState(
        skill_name=binding.skill_name,
        profile=binding.runtime_profile,
        status="running",
        flow_name="probe",
        gateway_url=binding.gateway_url,
        runtime_instance_id="replacement-runtime"
        if boundary == "replacement"
        else binding.runtime_instance_id,
        gateway_identity="replacement-gateway"
        if boundary == "replacement"
        else binding.gateway_identity,
    )
    states.save(state)
    requests = []

    def gateway(request):
        requests.append((request.method, request.url.path))
        if request.url.path != "/tools" and boundary == "unreachable":
            raise httpx.ConnectError("offline")
        data = (
            {"gateway_identity": state.gateway_identity}
            if request.url.path == "/tools"
            else {"phase": boundary if boundary in {"running", "unknown"} else "succeeded"}
        )
        return httpx.Response(200, json={"ok": True, "data": data})

    async with ForgeToolClient(state.gateway_url, transport=httpx.MockTransport(gateway)) as client:
        registry = coordinator.binding_resolver.runtime_registry
        registry.replace(
            ActiveSkillRuntime(
                skill_name=state.skill_name,
                skill_version="1.2.3",
                profile=state.profile,
                runtime_instance_id=state.runtime_instance_id,
                gateway_url=state.gateway_url,
                gateway_identity=state.gateway_identity,
                client=client,
                invocation_ids=PersistentInvocationSet(state.skill_name, states),
                session_ids=PersistentSessionSet(state.skill_name, states),
                task_binding_ids=PersistentTaskBindingSet(state.skill_name, states),
            )
        )
        recovered = AgentTaskCoordinator(
            workspace=coordinator.workspace,
            config=coordinator.config,
            client=DynamicForgeToolClient(registry),
            binding_resolver=coordinator.binding_resolver,
            activation_manager=coordinator.activation_manager,
            store=coordinator.store,
            runtime_invocation_ids=DynamicRuntimeSet(registry, "invocation_ids"),
            runtime_session_ids=DynamicRuntimeSet(registry, "session_ids"),
            runtime_task_binding_ids=DynamicRuntimeSet(registry, "task_binding_ids"),
        )
        if boundary == "journal_error":
            monkeypatch.setattr(
                coordinator.store,
                "active",
                lambda: (_ for _ in ()).throw(OSError("journal unreadable")),
            )
        if boundary == "busy":
            with SkillOperationLock(states.root, state.skill_name):
                await recovered.reconcile_nonterminal()
        else:
            await recovered.reconcile_nonterminal()
    updated = states.load(state.skill_name)
    if boundary in {"running", "unknown", "unreachable"}:
        assert updated.active_invocations == (invocation,)
        assert updated.active_task_bindings == (binding.binding_id,)
        phase = "unknown" if boundary == "unreachable" else boundary
        assert recovered.get_task(task.task_id).execution_records[0].status == phase
    elif boundary == "valid":
        assert not updated.active_invocations
        assert updated.active_task_bindings == (binding.binding_id,)
        assert recovered.get_task(task.task_id).execution_records[0].status == "succeeded"
        assert requests == [("GET", "/tools"), ("GET", f"/invocations/{invocation}")]
    else:
        assert updated == state
        assert all(path != f"/invocations/{invocation}" for _, path in requests)
        assert recovered.get_task(task.task_id).execution_records[0].status == "unknown"


async def _unknown_task(tmp_path, monkeypatch, semantics="action"):
    coordinator, task, _, invocations, sessions, bindings = await _bound_system(
        tmp_path, monkeypatch
    )
    accepted = (
        await coordinator.start_session(task.task_id, "demo.session", {})
        if semantics == "session"
        else await coordinator.start_action(task.task_id, "demo.action", {})
    )
    invocation = accepted["data"]["invocation_id"]
    observe = coordinator.observe_session if semantics == "session" else coordinator.observe_action
    observe(task.task_id, invocation, {"ok": True, "data": {"phase": "unknown"}})
    await coordinator.finalize_task(task.task_id)
    binding = task.primary_skill_binding
    states = RuntimeStateStore(tmp_path / "states")
    state = RuntimeState(
        skill_name=binding.skill_name,
        profile=binding.runtime_profile,
        status="running",
        flow_name="paos-example-forge-local",
        gateway_url=binding.gateway_url,
        runtime_instance_id=binding.runtime_instance_id,
        gateway_identity=binding.gateway_identity,
        active_invocations=tuple(invocations),
        active_sessions=tuple(sessions),
        active_task_bindings=tuple(bindings),
    )
    states.save(state)
    return coordinator, task, invocation, state, states


@pytest.mark.parametrize("phase", ["succeeded", "failed", "cancelled", "stopped"])
@pytest.mark.parametrize("semantics", ["action", "session"])
async def test_terminal_task_unknown_execution_can_be_reconciled(
    tmp_path, monkeypatch, phase, semantics
):
    coordinator, task, invocation, state, states = await _unknown_task(
        tmp_path, monkeypatch, semantics
    )
    requests = []

    def gateway(request):
        requests.append((request.method, request.url.path))
        data = (
            {"gateway_identity": state.gateway_identity}
            if request.url.path == "/tools"
            else {"phase": phase}
        )
        return httpx.Response(200, json={"ok": True, "data": data})

    async with ForgeToolClient(state.gateway_url, transport=httpx.MockTransport(gateway)) as client:
        report = await reconcile_runtime_references(
            state.skill_name, states=states, tasks=coordinator.store, client=client
        )
    updated = coordinator.get_task(task.task_id)
    assert updated.status.value == "failed"  # preserve the original verdict
    assert updated.execution_records[0].status == phase
    released = report.released_sessions if semantics == "session" else report.released_invocations
    assert released == [invocation]
    assert not report.unresolved
    assert not states.load(state.skill_name).active_task_bindings
    assert not states.load(state.skill_name).active_invocations
    assert not states.load(state.skill_name).active_sessions
    assert requests == [("GET", "/tools"), ("GET", f"/invocations/{invocation}")]
    assert coordinator.store.has_event(task.task_id, "execution_reconciled")


@pytest.mark.parametrize(
    "failure",
    [
        "unknown",
        "running",
        "unreachable",
        "wrong_gateway",
        "wrong_invocation",
        "new_runtime",
        "missing_task",
    ],
)
async def test_uncertain_or_mismatched_facts_never_release_references(
    tmp_path, monkeypatch, failure
):
    coordinator, _, invocation, state, states = await _unknown_task(tmp_path, monkeypatch)
    if failure == "new_runtime":
        states.save(replace(state, runtime_instance_id="replacement-runtime"))
    if failure == "missing_task":
        from PhyAgentOS.forge.task import AgentTaskStore

        tasks = AgentTaskStore(tmp_path / "other-workspace")
    else:
        tasks = coordinator.store

    def gateway(request):
        if failure == "unreachable":
            raise httpx.ConnectError("offline")
        if request.url.path == "/tools":
            data = {
                "gateway_identity": "replacement"
                if failure == "wrong_gateway"
                else state.gateway_identity
            }
        else:
            data = {"phase": failure if failure in {"unknown", "running"} else "succeeded"}
            if failure == "wrong_invocation":
                data["invocation_id"] = "another-invocation"
        return httpx.Response(200, json={"ok": True, "data": data})

    async with ForgeToolClient(state.gateway_url, transport=httpx.MockTransport(gateway)) as client:
        report = await reconcile_runtime_references(
            state.skill_name, states=states, tasks=tasks, client=client
        )
    assert report.unresolved
    assert states.load(state.skill_name).active_invocations == (invocation,)
    assert states.load(state.skill_name).active_task_bindings == state.active_task_bindings


async def test_active_unknown_action_is_rechecked_on_agent_recovery(tmp_path, monkeypatch):
    coordinator, task, client, invocations, _, bindings = await _bound_system(tmp_path, monkeypatch)
    accepted = await coordinator.start_action(task.task_id, "demo.action", {})
    invocation = accepted["data"]["invocation_id"]
    coordinator.observe_action(task.task_id, invocation, {"ok": True, "data": {"phase": "unknown"}})
    updated = await coordinator.reconcile_nonterminal()
    assert client.status_calls == [invocation]
    assert updated.execution_records[0].status == "succeeded"
    assert not invocations
    assert bindings  # the task still needs finalization


async def test_runtime_changed_during_get_preserves_new_state_and_reports_no_release(
    tmp_path, monkeypatch
):
    coordinator, _, _, state, states = await _unknown_task(tmp_path, monkeypatch)

    def gateway(request):
        if request.url.path == "/tools":
            data = {"gateway_identity": state.gateway_identity}
        else:
            states.save(replace(state, runtime_instance_id="replacement-runtime"))
            data = {"phase": "succeeded"}
        return httpx.Response(200, json={"ok": True, "data": data})

    async with ForgeToolClient(state.gateway_url, transport=httpx.MockTransport(gateway)) as client:
        report = await reconcile_runtime_references(
            state.skill_name, states=states, tasks=coordinator.store, client=client
        )
    assert not report.released_invocations and not report.released_bindings
    assert report.unresolved
    assert states.load(state.skill_name).active_invocations == state.active_invocations
    assert states.load(state.skill_name).active_task_bindings == state.active_task_bindings


async def test_missing_runtime_state_is_reported_not_raised(tmp_path):
    from PhyAgentOS.forge.task import AgentTaskStore

    states = RuntimeStateStore(tmp_path / "states")
    report = await reconcile_runtime_references(
        "never-started", states=states, tasks=AgentTaskStore(tmp_path / "workspace")
    )
    assert not report.released_invocations and not report.released_bindings
    assert any("no runtime state" in reason for reason in report.unresolved)


async def test_busy_skill_lock_is_reported_not_raised(tmp_path):
    from PhyAgentOS.forge.task import AgentTaskStore
    from PhyAgentOS.skill_runtime.locking import SkillOperationLock

    states = RuntimeStateStore(tmp_path / "states")
    with SkillOperationLock(states.root, "busy"):
        report = await reconcile_runtime_references(
            "busy", states=states, tasks=AgentTaskStore(tmp_path / "workspace")
        )
    assert any("lifecycle operation" in reason for reason in report.unresolved)

import asyncio
import json
from dataclasses import replace

import pytest
from interaction_support import source_bundle, system

from PhyAgentOS.agent.tools.base import Tool
from PhyAgentOS.agent.tools.forge_tool_api import ForgeToolQueryTool
from PhyAgentOS.agent.tools.registry import ToolRegistry
from PhyAgentOS.forge.interaction.binding import RECEIPT
from PhyAgentOS.skill_runtime.installer import InstallerError, SkillInstaller
from PhyAgentOS.skill_runtime.state import RuntimeStateStore
from scripts.package_skill import package


async def finish(supervisor):
    await asyncio.wait_for(asyncio.gather(*supervisor.jobs.values()), 5)


async def test_real_task_closed_loop_and_evidence(tmp_path, monkeypatch):
    c, t, grid, sup, registry = await system(tmp_path, monkeypatch)
    result = await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await finish(sup)
    task = c.get_task(t.task_id)
    assert task.status.value == "succeeded", c.interaction.summary(t.task_id)
    assert c.verifier.calls == 1
    assert len([r for r in task.execution_records if r.semantics == "action"]) == 2
    assert len(task.revisions) == 1
    assert c.interaction.store.runs()[0]["settled"]
    assert len(c.interaction.store.steps(result["data"]["interaction_run_id"])) == 3
    assert not registry.current().task_binding_ids
    assert grid.stop_calls == 1
    assert task.evidence_bundle_ref
    await sup.shutdown()


@pytest.mark.parametrize(
    "fault", ["start_timeout", "submit_timeout", "unknown", "stop_timeout", "missing"]
)
async def test_uncertainty_never_redispatches_or_finalizes(tmp_path, monkeypatch, fault):
    c, t, grid, sup, registry = await system(tmp_path, monkeypatch)
    setattr(grid, fault, True)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await finish(sup)
    assert c.get_task(t.task_id).status.value != "succeeded"
    with pytest.raises(RuntimeError):
        await c.finalize_task(t.task_id)
    with pytest.raises(RuntimeError):
        c.begin_revision(t.task_id, reason="bypass")
    with pytest.raises(RuntimeError):
        await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    assert len([r for r in grid.calls if r[1] == "/tools/grid.run:invoke"]) == 1
    assert t.primary_skill_binding.binding_id in registry.current().task_binding_ids
    if fault == "unknown":
        assert grid.stop_calls == 1
    await sup.shutdown()


async def test_cancel_during_inference_drops_late_proposal(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def decide(context):
        entered.set()
        await release.wait()
        return {
            "kind": "proposal",
            "proposal": {"operation": "advance", "arguments": {"distance": 1}},
        }

    c, t, grid, sup, _ = await system(tmp_path, monkeypatch, decide=decide)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await entered.wait()
    await c.cancel_task(t.task_id, reason="user")
    release.set()
    await finish(sup)
    assert not any(call[1] == "/tools/grid.submit:invoke" for call in grid.calls)
    assert c.get_task(t.task_id).status.value == "cancelled"
    await sup.shutdown()


async def test_direct_tools_and_one_shot_are_rejected(tmp_path, monkeypatch):
    c, t, grid, sup, _ = await system(tmp_path, monkeypatch, long_lived=False)
    with pytest.raises(RuntimeError, match="one-shot"):
        await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    with pytest.raises(RuntimeError, match="trusted Runner"):
        await c.start_action(t.task_id, "grid.submit", {"role": "runner"})
    response = json.loads(await ForgeToolQueryTool(c.client, c).execute("grid.snapshot", {}))
    assert response["ok"] is False
    assert not any(method == "POST" for method, _, _ in grid.calls)


async def test_default_disabled_and_changed_bundle_fail_closed(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="enabled"):
        await system(tmp_path, monkeypatch, enabled=False)


class UnsafeTool(Tool):
    name = "exec"
    description = "unsafe"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        raise AssertionError("must not execute")


@pytest.mark.parametrize(
    "invalid", ["unverified", "supplied_receipt", "schema_escape", "unsupported"]
)
def test_installer_rejects_invalid_extensions(tmp_path, invalid):
    source = source_bundle(tmp_path / "src")
    if invalid == "supplied_receipt":
        (source / RECEIPT).write_text("{}")
    elif invalid in {"schema_escape", "unsupported"}:
        import yaml

        value = yaml.safe_load((source / "interaction.yaml").read_text())
        value["proposal_schema_file" if invalid == "schema_escape" else "protocol"] = (
            "../outside" if invalid == "schema_escape" else "v999"
        )
        (source / "interaction.yaml").write_text(yaml.safe_dump(value))
    archive = package(source, tmp_path / "archive")
    with pytest.raises(InstallerError):
        SkillInstaller(
            tmp_path / "installed", state_store=RuntimeStateStore(tmp_path / "state")
        ).install(archive, verify_archive_manifest=invalid != "unverified")
    assert not (tmp_path / "installed/grid").exists()


async def test_recovery_reuses_original_session_without_start_post(tmp_path, monkeypatch):
    from PhyAgentOS.forge.task import AgentTaskCoordinator

    c, t, grid, sup, registry = await system(tmp_path, monkeypatch)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    for job in sup.jobs.values():
        job.cancel()
    await asyncio.gather(*sup.jobs.values(), return_exceptions=True)
    for runner in sup.runners.values():
        runner.close()
    c.interaction.store.close()
    restored = AgentTaskCoordinator(
        workspace=c.workspace,
        config=c.config,
        client=c.client,
        binding_resolver=c.binding_resolver,
        verifier=c.verifier,
        runtime_invocation_ids=registry.current().invocation_ids,
        runtime_session_ids=registry.current().session_ids,
        runtime_task_binding_ids=registry.current().task_binding_ids,
    )
    resumed = restored.interaction.enable_in_process(c.interaction.deployment)
    await restored.reconcile_nonterminal()
    await finish(resumed)
    assert restored.get_task(t.task_id).status.value == "succeeded"
    assert len([x for x in grid.calls if x[1] == "/tools/grid.run:invoke"]) == 1
    await resumed.shutdown()


async def test_unresolved_journal_cannot_be_removed_or_disabled(tmp_path, monkeypatch):
    c, t, grid, sup, _ = await system(tmp_path, monkeypatch)
    grid.unknown = True
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await finish(sup)
    c.config.interaction.enabled = False
    with pytest.raises(RuntimeError):
        await c.finalize_task(t.task_id)
    await sup.shutdown()
    c.interaction.path.unlink()
    with pytest.raises(RuntimeError, match="journal missing"):
        await c.finalize_task(t.task_id)
    assert c.interaction.restricted()


async def test_root_goal_is_not_implied_by_finish_candidate(tmp_path, monkeypatch):
    async def decide(context):
        return {"kind": "finish_candidate", "reason": "model claims done"}

    c, t, grid, sup, _ = await system(tmp_path, monkeypatch, decide=decide)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await finish(sup)
    assert c.get_task(t.task_id).status.value == "failed"
    assert c.verifier.calls == 1
    await sup.shutdown()


async def test_one_root_episode_contains_trace_and_is_deduplicated(tmp_path, monkeypatch):
    from PhyAgentOS.agent.experience.coordinator import ExperienceCoordinator

    c, t, grid, sup, _ = await system(tmp_path, monkeypatch)
    experience = ExperienceCoordinator(
        workspace=c.workspace, analyzer=None, task_coordinator=c, max_calls=0
    )
    monkeypatch.setattr(experience, "_schedule_job", lambda root: None)
    c.set_experience(experience)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await finish(sup)
    first = experience.store.get_episode_by_root(t.task_id)
    experience.schedule_forge_completion(t.task_id)
    second = experience.store.get_episode_by_root(t.task_id)
    assert first.episode_id == second.episode_id
    assert len(first.outcome.record_refs) > len(first.outcome.lineage)
    assert first.root_task_id == t.task_id
    await sup.shutdown()


async def test_schema_change_between_validation_and_post_never_uses_new_runtime(
    tmp_path, monkeypatch
):
    c, t, grid, sup, registry = await system(tmp_path, monkeypatch)
    runtime = registry.current()
    original = runtime.client.start_session

    async def switch(*args, **kwargs):
        registry.replace(replace(runtime, runtime_instance_id="replacement"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.client, "start_session", switch)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await finish(sup)
    assert not any(x[1] == "/tools/grid.run:invoke" for x in grid.calls)
    assert c.get_task(t.task_id).status.value != "succeeded"
    await sup.shutdown()


async def test_agent_reply_returns_while_supervisor_runs_and_stop_ignores_chat_lock(
    tmp_path, monkeypatch
):
    from PhyAgentOS.agent.loop import AgentLoop
    from PhyAgentOS.bus.events import InboundMessage
    from PhyAgentOS.bus.queue import MessageBus
    from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest

    entered = asyncio.Event()
    release = asyncio.Event()

    async def decide(context):
        entered.set()
        await release.wait()
        return {
            "kind": "proposal",
            "proposal": {"operation": "advance", "arguments": {"distance": 1}},
        }

    c, t, grid, sup, _ = await system(tmp_path, monkeypatch, decide=decide)

    class Provider:
        count = 0

        def get_default_model(self):
            return "test"

        async def chat_with_retry(self, **kwargs):
            self.count += 1
            if self.count == 1:
                assert "exec" not in [tool["function"]["name"] for tool in kwargs["tools"]]
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="start",
                            name="forge_tool_start_session",
                            arguments={
                                "task_id": t.task_id,
                                "tool_id": "grid.run",
                                "ownership": "task",
                                "arguments": {"world_id": "world"},
                            },
                        ),
                        ToolCallRequest(id="bad", name="exec", arguments={"command": "false"}),
                    ],
                )
            return LLMResponse(content="Monitoring started.")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(),
        workspace=c.workspace,
        forge_task_coordinator=c,
        forge_tool_client=c.client,
    )
    async with loop._processing_lock:
        answer, _, messages = await loop._run_agent_loop([])
        assert answer == "Monitoring started."
        await asyncio.wait_for(entered.wait(), 1)
        assert any("unavailable" in str(m.get("content")) for m in messages)
        await asyncio.wait_for(
            loop._handle_stop(
                InboundMessage(channel="cli", chat_id="test", sender_id="user", content="/stop")
            ),
            1,
        )
        assert c.get_task(t.task_id).cancellation_requested
    release.set()
    await finish(sup)
    assert c.get_task(t.task_id).status.value == "cancelled"
    await loop.close_mcp()


async def test_active_unsafe_tool_and_existing_subagent_share_dynamic_gate(tmp_path, monkeypatch):
    from PhyAgentOS.agent.subagent import SubagentManager
    from PhyAgentOS.bus.queue import MessageBus

    c, t, grid, sup, _ = await system(tmp_path, monkeypatch)
    c.interaction.unsafe_calls = 1
    with pytest.raises(RuntimeError, match="unsafe tools"):
        await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    c.interaction.unsafe_calls = 0
    manager = SubagentManager(
        provider=None,
        model="test",
        workspace=c.workspace,
        bus=MessageBus(),
        tool_policy=c.interaction,
    )
    with pytest.raises(RuntimeError, match="delegation"):
        await manager.spawn("attempt access")
    tools = ToolRegistry(policy=manager.tool_policy)
    tools.register(UnsafeTool())
    assert "unavailable" in await tools.execute("exec", {})


async def test_cancel_during_root_verification_cannot_commit_success(tmp_path, monkeypatch):
    c, t, grid, sup, _ = await system(tmp_path, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = c.verifier.verify_agent_task

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(c.verifier, "verify_agent_task", delayed)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await entered.wait()
    await asyncio.wait_for(c.cancel_task(t.task_id, reason="cancel while verifying"), 1)
    release.set()
    await finish(sup)
    assert c.get_task(t.task_id).status.value == "cancelled"
    await sup.shutdown()


async def test_missing_step_or_tampered_artifact_cannot_be_used_as_evidence(tmp_path, monkeypatch):
    from PhyAgentOS.forge.interaction.evidence import checked_trace
    from PhyAgentOS.verification.request_builder import (
        VerificationEvidenceError,
        VerificationRequestBuilder,
    )

    c, t, grid, sup, _ = await system(tmp_path, monkeypatch)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await finish(sup)
    task = c.get_task(t.task_id)
    bundle = json.loads((c.workspace / task.evidence_bundle_ref).read_text())
    artifact = c.workspace / bundle["artifacts"][1]["uri"]
    artifact.write_text("{}")
    with pytest.raises(VerificationEvidenceError):
        VerificationRequestBuilder(c.workspace).build_agent_task(task, events=[], lessons="[]")
    run = c.interaction.store.runs()[0]
    step = c.interaction.store.steps(run["interaction_run_id"])[0]
    # Simulate storage corruption after a clean run; retain the immutable submission intent.
    c.interaction.store.db.execute("PRAGMA foreign_keys=OFF")
    c.interaction.store.db.execute("DELETE FROM decision_steps WHERE step_id=?", (step["step_id"],))
    with pytest.raises(RuntimeError, match="step evidence missing"):
        checked_trace(c.interaction, run)
    await sup.shutdown()


async def test_competing_recovery_does_not_mutate_live_run(tmp_path, monkeypatch):
    from PhyAgentOS.forge.interaction.runner_store import RunnerAlreadyOwnedError
    from PhyAgentOS.forge.task import AgentTaskCoordinator

    entered = asyncio.Event()
    release = asyncio.Event()

    async def decide(context):
        entered.set()
        await release.wait()
        return {"kind": "wait", "reason": "wait"}

    c, t, grid, sup, _ = await system(tmp_path, monkeypatch, decide=decide)
    await c.start_session(t.task_id, "grid.run", {"world_id": "world"})
    await entered.wait()
    other = AgentTaskCoordinator(
        workspace=c.workspace,
        config=c.config,
        client=c.client,
        binding_resolver=c.binding_resolver,
        verifier=c.verifier,
    )
    contender = other.interaction.enable_in_process(c.interaction.deployment)
    before = c.interaction.store.runs()[0]
    with pytest.raises(RunnerAlreadyOwnedError):
        await contender.recover()
    assert c.interaction.store.runs()[0] == before
    assert not contender.runners
    await c.cancel_task(t.task_id, reason="cleanup")
    release.set()
    await finish(sup)
    await sup.shutdown()

import asyncio
import copy
import json

import pytest
from benchmark_support import system

from PhyAgentOS.forge.benchmark.evidence import checked_report
from PhyAgentOS.forge.interaction.runner_store import RunnerAlreadyOwnedError


async def finish(coordinator, task, supervisor):
    await coordinator.start_session(task.task_id, "grid.run", {})
    await asyncio.wait_for(supervisor.monitors[task.task_id], 5)
    return coordinator.get_task(task.task_id)


@pytest.mark.asyncio
async def test_two_rounds_parallel_lessons_and_root_completeness(tmp_path, monkeypatch):
    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch)
    result = await finish(c, task, supervisor)
    assert result.status.value == "succeeded", c.benchmark.summary(task.task_id)
    report = checked_report(c.benchmark, task.task_id)
    assert report["rounds"][0]["counts"]["failure"] == 2
    assert report["rounds"][1]["counts"]["success"] == 2
    assert report["rounds"][0]["lessons_digest"] != report["rounds"][1]["lessons_digest"]
    assert fake.max_active == 2
    assert len([r for r in result.execution_records if r.semantics == "session"]) == 1
    assert len(report["episodes"]) == 4
    assert c.store.active() is None
    from PhyAgentOS.agent.experience.source import AgentTaskOutcomeSource

    outcome = AgentTaskOutcomeSource(c).build(task.task_id)
    assert outcome.learnable and not outcome.successful
    assert outcome.benchmark_evaluation is not None
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_environment_failure_is_valid_evaluation(tmp_path, monkeypatch):
    c, task, _, supervisor, _ = await system(tmp_path, monkeypatch, rounds=1, reflect=False)
    result = await finish(c, task, supervisor)
    assert result.status.value == "succeeded", c.benchmark.summary(task.task_id)
    assert checked_report(c.benchmark, task.task_id)["rounds"][0]["success_rate"] == 0
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_cancel_during_inference_discards_late_result(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def decide(context):
        entered.set()
        await asyncio.sleep(100)

    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch, decide=decide)
    await c.start_session(task.task_id, "grid.run", {})
    await entered.wait()
    result = await c.cancel_task(task.task_id, reason="test")
    assert result.cancellation_requested
    assert not [args for _, _, args in fake.calls if args.get("operation") == "decision.submit"]
    assert result.status.value == "cancelled", c.benchmark.summary(task.task_id)
    await supervisor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stop_timeout", "missing"])
async def test_unknown_retains_resources_without_resend(tmp_path, monkeypatch, failure):
    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch, rounds=1)
    setattr(fake, failure, True)
    result = await finish(c, task, supervisor)
    assert result.status.value != "succeeded"
    assert c.benchmark.store.get("run", task.task_id)["state"] == "blocked"
    calls = [
        args["request_id"]
        for _, _, args in fake.calls
        if "request_id" in args and "operation" in args
    ]
    assert len(calls) == len(set(calls))
    with pytest.raises(RuntimeError):
        c.benchmark.assert_settled(task.task_id)
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_owner_recovery_raises_without_mutating_run(tmp_path, monkeypatch):
    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch)
    supervisor.store.acquire_owner(supervisor)
    from PhyAgentOS.agent.benchmark.supervisor import BenchmarkSupervisor

    other = BenchmarkSupervisor(c.benchmark)
    before = copy.deepcopy(c.benchmark.store.list("run"))
    with pytest.raises(RunnerAlreadyOwnedError):
        c.benchmark.supervisor = other
        await c.reconcile_nonterminal()
    assert c.benchmark.store.list("run") == before
    assert not [v for _, p, v in fake.calls if p.endswith(":invoke")]
    c.benchmark.supervisor = supervisor
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_direct_submit_and_disabled_fallback_rejected(tmp_path, monkeypatch):
    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="capability|authority"):
        await c.start_action(task.task_id, "grid.submit", {"role": "runner"})
    assert not c.interaction.allows("exec")
    c.config.benchmark.enabled = False
    with pytest.raises(RuntimeError):
        await c.start_session(task.task_id, "grid.run", {})
    assert not task.execution_records
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_tampered_evidence_rejects_success(tmp_path, monkeypatch):
    c, task, _, supervisor, _ = await system(tmp_path, monkeypatch, rounds=1)
    await finish(c, task, supervisor)
    child = c.benchmark.store.list("subtask", task.task_id)[0]
    c.benchmark.store.update("subtask", child["subtask_id"], history=[])
    with pytest.raises(RuntimeError, match="trajectory"):
        checked_report(c.benchmark, task.task_id)
    await supervisor.shutdown()


def test_media_is_scoped_to_observation():
    from PhyAgentOS.agent.benchmark.media import ObservationArtifacts

    store = ObservationArtifacts()
    ref = store.register(
        {"subtask_id": "a"},
        {"version": 1},
        "frame",
        __import__("base64").b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jvXcAAAAASUVORK5CYII="
        ),
    )
    assert (
        store.resolve({"subtask_id": "a"}, {"observation": {"version": 1}}, ref)["type"]
        == "image_url"
    )
    with pytest.raises(ValueError):
        store.resolve({"subtask_id": "b"}, {"observation": {"version": 1}}, ref)
    with pytest.raises(ValueError):
        store.resolve({"subtask_id": "a"}, {"observation": {"version": 2}}, ref)
    with pytest.raises(ValueError):
        store.resolve({}, {"observation": {}}, {"url": "file:///etc/passwd"})


@pytest.mark.asyncio
async def test_one_global_episode_without_success_promotion(tmp_path, monkeypatch):
    from PhyAgentOS.agent.experience.coordinator import ExperienceCoordinator

    c, task, _, supervisor, _ = await system(tmp_path, monkeypatch)
    experience = ExperienceCoordinator(
        workspace=c.workspace, analyzer=None, task_coordinator=c, max_calls=0
    )
    monkeypatch.setattr(experience, "_schedule_job", lambda root: None)
    c.set_experience(experience)
    await finish(c, task, supervisor)
    first = experience.store.get_episode_by_root(task.task_id)
    experience.schedule_forge_completion(task.task_id)
    second = experience.store.get_episode_by_root(task.task_id)
    assert first.episode_id == second.episode_id
    assert not first.outcome.successful
    await experience._process_job(task.task_id)
    assert not experience.store.list_candidates(active_only=True)
    assert not experience.store.list_lessons(status="active")
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_binding_switch_after_validation_prevents_post(tmp_path, monkeypatch):
    from dataclasses import replace

    c, task, fake, supervisor, registry = await system(tmp_path, monkeypatch)
    runtime = registry.current()
    original = runtime.client.start_session

    async def switch(*args, **kwargs):
        registry.replace(replace(runtime, runtime_instance_id="other"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.client, "start_session", switch)
    with pytest.raises(RuntimeError):
        await c.start_session(task.task_id, "grid.run", {})
    await supervisor.monitors[task.task_id]
    assert not any(path == "/tools/grid.run:invoke" for _, path, _ in fake.calls)
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_lost_session_reply_is_not_redispatched(tmp_path, monkeypatch):
    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch)
    fake.lose_start = True
    with pytest.raises(Exception):
        await c.start_session(task.task_id, "grid.run", {})
    await supervisor.monitors[task.task_id]
    assert len([p for _, p, _ in fake.calls if p == "/tools/grid.run:invoke"]) == 1
    assert c.get_task(task.task_id).execution_records[0].status == "unknown"
    assert fake.root["stopped"]
    assert not c.benchmark.store.get("run", task.task_id)["settled"]
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_terminal_root_recovery_keeps_unknown_and_never_reissues(tmp_path, monkeypatch):
    from PhyAgentOS.agent.benchmark.supervisor import BenchmarkSupervisor
    from PhyAgentOS.forge.benchmark.store import BenchmarkStore
    from PhyAgentOS.forge.task import AgentTaskStatus

    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch, rounds=1)
    fake.missing = True
    await finish(c, task, supervisor)
    await supervisor.shutdown()
    c.store.update(
        task.task_id,
        lambda t: setattr(t, "status", AgentTaskStatus.FAILED),
        event_type="test_terminal",
    )
    calls = len([p for _, p, _ in fake.calls if p.endswith(":invoke") and "snapshot" not in p])
    c.benchmark._store.close()
    c.benchmark._store = BenchmarkStore(c.benchmark.path)
    c.benchmark.closing = False
    recovered = BenchmarkSupervisor(c.benchmark)
    c.benchmark.supervisor = recovered
    await c.reconcile_nonterminal()
    assert (
        len([p for _, p, _ in fake.calls if p.endswith(":invoke") and "snapshot" not in p]) == calls
    )
    assert c.benchmark.store.get("run", task.task_id)["state"] == "blocked"
    await recovered.shutdown()


@pytest.mark.asyncio
async def test_reflection_failure_preserves_snapshot_and_budget(tmp_path, monkeypatch):
    c, task, _, supervisor, _ = await system(tmp_path, monkeypatch)

    async def fail(context):
        raise ValueError("invalid reflection")

    c.benchmark.deployment.reflect = fail
    result = await finish(c, task, supervisor)
    assert result.status.value == "succeeded"
    rounds = c.benchmark.store.list("round", task.task_id)
    assert all(r["reflection_status"] == "no_update" and r["lessons"] == [] for r in rounds)
    assert len(c.benchmark.store.list("reflection", task.task_id)) == 4
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_duplicate_case_support_is_not_two_independent_cases(tmp_path, monkeypatch):
    c, task, _, supervisor, _ = await system(tmp_path, monkeypatch)

    async def duplicate(context):
        child = context["episodes"][0]
        return [
            {
                "workflow_key": "advance",
                "principle": "verify progress",
                "applies_when": ["ready"],
                "does_not_apply_when": ["stopped"],
                "supporting_subtasks": [child["subtask_id"]] * 2,
            }
        ]

    c.benchmark.deployment.reflect = duplicate
    await finish(c, task, supervisor)
    assert all(not r["next_lessons"] for r in c.benchmark.store.list("round", task.task_id))
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_cross_child_capability_and_diagnostic_query_rejected(tmp_path, monkeypatch):
    from PhyAgentOS.agent.tools.forge_tool_api import ForgeToolQueryTool
    from PhyAgentOS.forge.benchmark.client import SCOPE_KEYS

    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch, rounds=1)
    await finish(c, task, supervisor)
    client = supervisor.clients[task.task_id]
    a, b = c.benchmark.store.list("subtask", task.task_id)
    cap = client.child_capability(a)
    before = len(fake.calls)
    with pytest.raises(RuntimeError, match="cross-episode"):
        await client.query(cap, {**{k: b[k] for k in SCOPE_KEYS}, "view": "episode"})
    assert len(fake.calls) == before
    tool = ForgeToolQueryTool(c.client, c)
    reply = json.loads(await tool.execute("grid.snapshot", {"view": "root"}))
    assert not reply["ok"]
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_missing_journal_and_sidecar_fail_closed(tmp_path, monkeypatch):
    c, task, _, supervisor, _ = await system(tmp_path, monkeypatch)
    manifest = c.binding_resolver.catalog.get("grid")
    (manifest.bundle_root / "proposal.json").write_text("{}")
    with pytest.raises(ValueError):
        await c.start_session(task.task_id, "grid.run", {})
    assert not c.get_task(task.task_id).execution_records
    c.benchmark.path.unlink()
    with pytest.raises(RuntimeError, match="journal"):
        c.benchmark.managed(task.task_id)
    assert not c.interaction.allows("exec")
    supervisor.store.release_owner(supervisor)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["one_shot", "unsafe_inflight"])
async def test_start_lifecycle_gates(tmp_path, monkeypatch, reason):
    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch)
    if reason == "one_shot":
        c.benchmark.long_lived = False
    else:
        c.interaction.unsafe_calls = 1
    with pytest.raises(RuntimeError):
        await c.start_session(task.task_id, "grid.run", {})
    assert not c.get_task(task.task_id).execution_records
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_lost_action_reply_uses_independent_get_evidence(tmp_path, monkeypatch):
    c, task, fake, supervisor, registry = await system(tmp_path, monkeypatch, rounds=1)
    fake.lose_submit = True
    result = await finish(c, task, supervisor)
    assert result.status.value == "succeeded", c.benchmark.summary(task.task_id)
    assert any(r.status == "unknown" for r in result.execution_records)
    assert not registry.current().task_binding_ids
    intents = c.benchmark.store.list("intent", task.task_id)
    assert len([i for i in intents if i.get("reconciliation")]) == 2
    calls = [a["request_id"] for _, _, a in fake.calls if a.get("operation") == "decision.submit"]
    assert len(calls) == len(set(calls)) == 2
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_changed_observation_discards_proposal(tmp_path, monkeypatch):
    changed = set()
    fake = None

    async def decide(context):
        key = context["goal"]["case_key"]
        if key not in changed:
            changed.add(key)
            child = next(
                s for k, s in fake.episodes.items() if (":episode:0000" in k) == (key == "a")
            )
            child = copy.deepcopy(child)
            child.update(observation_version=1, observation={"position": 1})
            fake.save(child)
        return {"kind": "proposal", "proposal": {"distance": 1}}

    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch, rounds=1, decide=decide)
    result = await finish(c, task, supervisor)
    assert result.status.value == "succeeded", c.benchmark.summary(task.task_id)
    decisions = c.benchmark.store.list("decision", task.task_id)
    assert len([d for d in decisions if d["status"] == "discarded"]) == 2
    assert all(
        a["observation_version"] == 1
        for _, _, a in fake.calls
        if a.get("operation") == "decision.submit"
    )
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_cancel_immediately_before_transport_prevents_submission(tmp_path, monkeypatch):
    c, task, fake, supervisor, registry = await system(tmp_path, monkeypatch, rounds=1)
    original = registry.current().client.invoke_action

    async def cancel_first(tool, arguments, **kwargs):
        if arguments.get("operation") == "decision.submit":
            c.store.update(
                task.task_id,
                lambda t: setattr(t, "cancellation_requested", True),
                event_type="test_cancel",
            )
        return await original(tool, arguments, **kwargs)

    monkeypatch.setattr(registry.current().client, "invoke_action", cancel_first)
    await finish(c, task, supervisor)
    assert not [a for _, _, a in fake.calls if a.get("operation") == "decision.submit"]
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_persist_intent_failure_prevents_dispatch(tmp_path, monkeypatch):
    c, task, fake, supervisor, _ = await system(tmp_path, monkeypatch, rounds=1)
    original = supervisor.store.put

    def fail(kind, key, root, value, **kwargs):
        if kind == "intent" and value["arguments"]["operation"] == "decision.submit":
            raise OSError("disk full")
        return original(kind, key, root, value, **kwargs)

    monkeypatch.setattr(supervisor.store, "put", fail)
    await finish(c, task, supervisor)
    assert not [a for _, _, a in fake.calls if a.get("operation") == "decision.submit"]
    await supervisor.shutdown()


def test_owner_lock_is_exclusive_across_processes(tmp_path):
    import subprocess
    import sys

    from PhyAgentOS.forge.benchmark.store import BenchmarkStore

    store = BenchmarkStore(tmp_path / "benchmark.sqlite3")
    owner = object()
    store.acquire_owner(owner)
    script = """
import sys
from PhyAgentOS.forge.benchmark.store import BenchmarkStore
from PhyAgentOS.forge.interaction.runner_store import RunnerAlreadyOwnedError
store = BenchmarkStore(sys.argv[1])
try:
    store.acquire_owner(object())
except RunnerAlreadyOwnedError:
    sys.exit(0)
sys.exit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.path)], capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    store.close()


@pytest.mark.asyncio
async def test_profile_scoped_required_tools_are_supported(tmp_path, monkeypatch):
    c, task, _, supervisor, _ = await system(tmp_path, monkeypatch, rounds=1, profile_tools=True)
    result = await finish(c, task, supervisor)
    assert result.status.value == "succeeded"
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_provider_receives_scoped_image_and_no_tools():
    from types import SimpleNamespace

    from PhyAgentOS.agent.interaction.model import ProviderDecider

    class Provider:
        async def chat(self, **kwargs):
            assert kwargs["tools"] is None
            assert len(kwargs["messages"]) == 2
            image = kwargs["messages"][1]["content"][1]
            assert image["image_url"]["url"].startswith("data:image/png;base64,")
            return SimpleNamespace(
                content='{"kind":"finish_candidate","reason":"verify"}',
                usage={"total_tokens": 3},
                tool_calls=[],
            )

    decision = await ProviderDecider(Provider(), "fake")(
        {
            "goal": "evaluate",
            "observation": {},
            "proposal_schema": {},
            "decision_schema": {},
            "lessons": [],
            "lessons_digest": "empty",
            "media": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
        }
    )
    assert decision["usage"]["total_tokens"] == 3

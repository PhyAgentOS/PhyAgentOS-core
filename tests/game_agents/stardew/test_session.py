"""Use the actual OS lifecycle and a deterministic target/provider at its edges."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from importlib.resources import files

import pytest
import yaml

from PhyAgentOS.game_agents.stardew import (
    GeneralGameSkillRuntime,
    register_general_game,
)
from PhyAgentOS.game_agents.stardew.memory import GameMemory
from PhyAgentOS.providers.base import LLMProvider, LLMResponse
from PhyAgentOS.runtime.adapters.base import BaseTargetAdapter
from PhyAgentOS.runtime.adapters.factory import register_target_adapter
from PhyAgentOS.runtime.preflight.runtime_compatibility_preflight import (
    RuntimeCompatibilityPreflight,
)
from PhyAgentOS.runtime.schemas import SessionSpec, SkillRuntimeSpec, TargetSpec
from PhyAgentOS.runtime.sessions.session_runner import SessionRunner
from PhyAgentOS.runtime.targets.game.base import BaseGameTarget
from PhyAgentOS.runtime.watchdog.runtime_registry import SkillRuntimeRegistry
from PhyAgentOS.runtime.watchdog.scheduler import ScheduledSession


def plan(decision="new_phase", **kwargs):
    return {"decision": decision, "goal": "reach the goal", "reason": "observed state", **kwargs}


def act(action_type="move", **kwargs):
    return {"decision": "execute", "intent": "advance", "action": {"type": action_type}, **kwargs}


class ScriptedProvider(LLMProvider):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.contexts = []
        self.on_call = None
        self.request_cancelled = False

    def get_default_model(self):
        return "test-model"

    async def chat(self, messages, **kwargs):
        context = json.loads(messages[-1]["content"])["context"]
        self.contexts.append(context)
        if self.on_call:
            self.on_call(context)
        response = next(self.responses)
        if response == "wait":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.request_cancelled = True
                raise
        if isinstance(response, Exception):
            raise response
        return LLMResponse(
            content=response if isinstance(response, str) else json.dumps(response),
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


class GridTarget(BaseGameTarget):
    def __init__(self, goal=4, blocked=False):
        self.goal = goal
        self.blocked = blocked
        self.position = 0
        self._step_idx = 0
        self.resets = self.closes = self.builds = 0
        self.chunks = []

    def build(self):
        self.builds += 1

    def reset_step_counter(self):
        self._step_idx = 0

    def reset(self, session_ctx):
        self.resets += 1
        self.position = 0
        self.reset_step_counter()
        self._last_status = {"done": False, "success": False}
        return self.observe()

    def observe(self):
        return {"position": self.position, "observation_id": str(self._step_idx)}

    def step(self, action):
        self._step_idx += 1
        if not self.blocked:
            self.position += 1
        return {
            "done": self.position >= self.goal,
            "info": {
                "success": self.position >= self.goal,
            },
        }

    def action_chunk(self, chunk):
        assert chunk["source_observation_id"] == str(self._step_idx)
        assert len(chunk["actions"]) == 1
        self.chunks.append(chunk)
        status = super().action_chunk(chunk)
        status["ok"] = not self.blocked
        return status

    def cancel(self, reason):
        pass

    def close(self):
        self.closes += 1


class GridAdapter(BaseTargetAdapter):
    def to_runtime_observation(self, raw_obs, target_info):
        return {**raw_obs, "target_info": {"api_key": "do-not-expose"}}

    def to_executable_action_chunk(self, action_chunk, target_info):
        assert action_chunk["actions"][0]["type"] == "move"
        return action_chunk


@pytest.fixture
def make_runner(tmp_path):
    runners = []

    def make(responses, *, goal=4, blocked=False, max_steps=20, timeout=30, **options):
        provider = ScriptedProvider(responses)
        register_target_adapter("target_adapter://test_grid", GridAdapter)
        register_general_game(
            lambda: provider, model="test-model", action_catalog={"move": "one tile"}
        )
        target = GridTarget(goal, blocked)
        contract_path = tmp_path / "grid.runtime.yaml"
        contract_path.write_text(
            yaml.safe_dump(
                {
                    "version": "runtime_target_contract_v1",
                    "target_id": "grid",
                    "target_adapter": "target_adapter://test_grid",
                    "safety": {"require_target_side_validation": True},
                    "action_contract": {
                        "id": "grid_actions",
                        "accepted_representations": ["target_tool_call"],
                        "shape": [1, 1],
                        "dtype": "object",
                        "normalized": False,
                        "frame": "world",
                        "control_mode": "target_tool_call",
                        "control_hz": 1,
                        "components": [{"name": "command", "unit": "tool_call"}],
                    },
                }
            ),
            encoding="utf-8",
        )
        spec = TargetSpec(
            id="grid",
            target_class="local",
            target_kind="game",
            workspace=str(tmp_path),
            supported_skillruntimes=["general_game"],
            runtime={
                "target_runtime": "GridTarget",
                "target_adapter": "target_adapter://test_grid",
                "runtime_contract_ref": contract_path,
            },
            observation={"observation_type": "structured"},
        )
        skill = SkillRuntimeSpec.model_validate(
            yaml.safe_load(
                files("PhyAgentOS")
                .joinpath("templates/configs/skillruntimes/general_game.yaml")
                .read_text(encoding="utf-8"),
            )
        )
        session = SessionSpec(
            session_id="game-session",
            target_ref=spec.id,
            skillruntime_ref=skill.id,
            task_description="Reach the goal",
            execution={"max_steps": max_steps},
            timeouts={"execute_timeout_s": timeout},
        )
        preflight = RuntimeCompatibilityPreflight(tmp_path).check(
            ScheduledSession(session, spec, skill, spec.id, skill.id),
        )
        assert preflight.verdict == "accepted", preflight.missing_items
        runtime = GeneralGameSkillRuntime(
            lambda: provider,
            model="test-model",
            action_catalog={"move": "one tile"},
            **options,
        )
        runner = SessionRunner(
            session=session,
            target_spec=spec,
            skillruntime_spec=skill,
            adapter_plan=preflight.adapter_plan,
            target=target,
            skill_runtime=runtime,
            policy_client=None,
            perception_runtime=None,
            perception_plan=None,
        )
        runners.append(runner)
        return runner, target, provider

    yield make
    for runner in runners:
        runner.close()
        assert runner.target.closes == 1


def test_phase_round_feedback_chain_uses_native_session(make_runner):
    runner, target, provider = make_runner(
        [
            plan(),
            act(),
            act(),
            act(),
            plan("continue_phase"),
            act(),
        ]
    )
    result = runner.start()
    assert result.success and result.num_steps == 4
    assert target.resets == target.builds == 1
    assert runner.state.step_index == 4
    receipts = result.metadata["artifacts"]["loop_receipts"]
    assert [len(item["rounds"]) for item in receipts] == [3, 1]
    assert receipts[0]["phase"]["id"] == receipts[1]["phase"]["id"]
    assert provider.contexts[2]["previous_round"]["after"]["position"] == 1
    planner_receipt = provider.contexts[4]["last_receipt"]
    assert planner_receipt["rounds"][-1]["changes"]["position"] == {"before": 2, "after": 3}
    assert "action" not in json.dumps(planner_receipt)
    assert "do-not-expose" not in json.dumps(provider.contexts)
    assert result.metadata["model_calls"] == 6
    assert result.metadata["model_usage"]["prompt_tokens"] == 60


def test_failed_action_returns_receipt_to_planner(make_runner):
    runner, target, provider = make_runner(
        [
            plan(),
            act(),
            plan("replan", goal="try again"),
            act(),
        ],
        goal=1,
        blocked=True,
    )

    def unblock(context):
        if context.get("last_receipt", {}).get("end_reason") == "action_failed":
            target.blocked = False

    # Initial planner context has a null receipt.
    provider.on_call = lambda c: unblock(c) if c.get("last_receipt") else None
    result = runner.start()
    assert result.success and result.num_steps == 2
    assert provider.contexts[2]["last_receipt"]["rounds"][0]["feedback"]["ok"] is False
    receipts = result.metadata["artifacts"]["loop_receipts"]
    assert receipts[0]["phase"]["id"] != receipts[1]["phase"]["id"]


@pytest.mark.parametrize(
    "responses",
    [
        [plan("finish")],
        [plan(), {"decision": "yield", "intent": "done"}, plan("finish")],
    ],
)
def test_model_finish_cannot_assert_success(make_runner, responses):
    runner, target, _ = make_runner(responses)
    result = runner.start()
    assert not result.success
    assert result.error_code == "unverified_finish"
    assert not target.chunks


def test_explicit_task_verifier_uses_post_action_observation(make_runner):
    runner, _, _ = make_runner([plan(), act()], verify=lambda obs, status: obs["position"] == 1)
    assert runner.start().success


@pytest.mark.parametrize(
    "responses",
    [
        [plan("continue_phase")],
        [plan(max_rounds=4)],
        [plan(), act("reset")],
        [plan(), act(action={"type": "move", "actions": [{"type": "reset"}]})],
        [plan(), {"decision": "yield", "intent": "stop", "action": {"type": "move"}}],
        ["not json"],
    ],
)
def test_invalid_decisions_never_reach_target(make_runner, responses):
    runner, target, _ = make_runner(responses)
    result = runner.start()
    assert result.error_code == "invalid_decision"
    assert not target.chunks


def test_step_budget_caps_primitive_dispatch(make_runner):
    runner, target, _ = make_runner([plan(), act(), act()], max_steps=2)
    result = runner.start()
    assert result.error_code == "step_limit"
    assert len(target.chunks) == result.num_steps == 2


def test_success_on_last_allowed_step_is_preserved(make_runner):
    runner, _, _ = make_runner([plan(), act()], max_steps=1, goal=1)
    assert runner.start().success


def test_repeated_yield_cannot_spin_without_executing(make_runner):
    runner, target, _ = make_runner(
        [
            plan(),
            {"decision": "yield", "intent": "blocked"},
            plan("continue_phase"),
            {"decision": "yield", "intent": "blocked"},
        ],
        max_no_progress=2,
    )
    assert runner.start().error_code == "no_progress"
    assert not target.chunks


def test_transport_versions_do_not_count_as_progress(make_runner):
    runner, target, _ = make_runner(
        [
            plan(),
            act(),
            plan("replan"),
            act(),
        ],
        blocked=True,
        max_no_progress=2,
    )
    assert runner.start().error_code == "no_progress"
    assert len(target.chunks) == 2


def test_cancel_after_model_reply_prevents_action_and_counts_usage(make_runner):
    runner, target, provider = make_runner([plan(), act()])
    provider.on_call = lambda c: runner.cancel("stop") if "round" in c else None
    result = runner.start()
    assert result.status == "cancelled" and not target.chunks
    assert result.metadata["model_usage"]["prompt_tokens"] == 20


def test_cancel_interrupts_pending_model_call(make_runner):
    runner, target, provider = make_runner(["wait"])
    timer = threading.Timer(0.1, runner.cancel, args=("stop",))
    timer.start()
    try:
        assert runner.start().status == "cancelled"
    finally:
        timer.join()
    assert provider.request_cancelled and not target.chunks


def test_model_timeout_cancels_request_without_dispatch(make_runner):
    runner, target, provider = make_runner(["wait"], call_timeout_s=0.05)
    assert runner.start().status == "timed_out"
    assert provider.request_cancelled and not target.chunks


def test_provider_errors_are_not_exposed_as_credentials(make_runner):
    runner, target, _ = make_runner([ConnectionError("api-key=secret")])
    result = runner.start()
    assert result.error_code == "ConnectionError"
    assert "secret" not in result.model_dump_json() and not target.chunks


def candidate(role, evidence="loop-1/round-1"):
    return {
        "candidates": [
            {"role": role, "lesson": "One move advanced one tile.", "evidence": [evidence]}
        ]
    }


def test_frozen_memory_is_scoped_and_never_consolidated(make_runner, tmp_path):
    memory = GameMemory(tmp_path)
    memory.stores["planner"].write_long_term("planner-only")
    memory.stores["actor"].write_long_term("actor-only")
    runner, _, provider = make_runner([plan(), act()], goal=1, memory=memory)
    assert runner.start().success
    assert provider.contexts[0]["memory"] == "planner-only"
    assert provider.contexts[1]["memory"] == "actor-only"
    assert all(not store.history_file.exists() for store in memory.stores.values())


def test_evolution_records_candidates_after_episode_without_promoting_them(make_runner, tmp_path):
    memory = GameMemory(tmp_path)
    memory.stores["planner"].write_long_term("approved")
    runner, _, provider = make_runner(
        [
            plan(),
            act(),
            candidate("planner"),
            candidate("actor"),
        ],
        goal=1,
        memory=memory,
        evolve=True,
    )
    result = runner.start()
    assert result.success
    assert len(result.metadata["artifacts"]["memory_candidates"]) == 2
    assert provider.contexts[2]["outcome"]["status"] == "succeeded"
    assert "action" not in json.dumps(provider.contexts[2]["evidence"])
    assert provider.contexts[3]["evidence"][0]["rounds"][0]["action"]["type"] == "move"
    assert memory.stores["planner"].read_long_term() == "approved"
    assert "unverified" in memory.stores["actor"].history_file.read_text()


@pytest.mark.parametrize("invalid", [candidate("actor"), candidate("planner", "invented")])
def test_invalid_memory_evidence_does_not_change_result_or_memory(make_runner, tmp_path, invalid):
    memory = GameMemory(tmp_path)
    runner, _, _ = make_runner([plan(), act(), invalid], goal=1, memory=memory, evolve=True)
    result = runner.start()
    assert result.success and result.metadata["warnings"]
    assert all(not store.history_file.exists() for store in memory.stores.values())


def test_memory_snapshot_does_not_change_mid_session(make_runner, tmp_path):
    memory = GameMemory(tmp_path)
    memory.stores["actor"].write_long_term("original")
    runner, _, provider = make_runner([plan(), act()], goal=1, memory=memory)
    provider.on_call = lambda c: memory.stores["actor"].write_long_term("changed externally")
    assert runner.start().success
    assert provider.contexts[1]["memory"] == "original"


def test_registry_builds_distinct_runtime_instances():
    register_general_game(
        lambda: ScriptedProvider([]), model="test", action_catalog={"move": "one tile"}
    )
    registry = SkillRuntimeRegistry()
    first = registry.build("GeneralGameSkillRuntime")
    second = registry.build("GeneralGameSkillRuntime")
    first.cancel()
    assert first.snapshot()["status"] == "cancelled"
    assert second.snapshot()["status"] == "idle"


def test_control_reason_reaches_planner(make_runner):
    runner, _, provider = make_runner(
        [
            plan(),
            {"decision": "replan", "intent": "path is blocked"},
            plan("replan", goal="take another path"),
            act(),
        ],
        goal=1,
    )
    assert runner.start().success
    assert provider.contexts[2]["last_receipt"]["end_detail"] == "path is blocked"


def test_session_deadline_bounds_model_request(make_runner):
    runner, target, provider = make_runner(["wait"], timeout=1, call_timeout_s=5)
    assert runner.start().status == "timed_out"
    assert provider.request_cancelled and not target.chunks


def test_target_failure_is_not_model_success(make_runner):
    runner, target, _ = make_runner([plan(), act()], goal=1)
    original = target.action_chunk

    def failed_goal(chunk):
        status = original(chunk)
        status["success"] = False
        return status

    target.action_chunk = failed_goal
    result = runner.start()
    assert not result.success and result.error_code == "target_done"


def test_adapter_rejection_is_not_counted_as_an_executed_step(make_runner, monkeypatch):
    runner, target, _ = make_runner([plan(), act()])

    def reject(self, chunk, info):
        raise ValueError("invalid parameters")

    monkeypatch.setattr(GridAdapter, "to_executable_action_chunk", reject)
    result = runner.start()
    assert result.num_steps == 0 and not target.chunks
    assert result.metadata["action_attempts"] == 1


def test_loop_budget_bounds_successive_replans(make_runner):
    runner, target, _ = make_runner(
        [
            plan(),
            {"decision": "replan", "intent": "try a new goal"},
        ],
        max_loops=1,
    )
    assert runner.start().error_code == "loop_limit"
    assert not target.chunks


def test_startup_time_consumes_the_os_session_budget(make_runner):
    runner, target, provider = make_runner([plan(), act()])
    reset = target.reset

    def slow_reset(context):
        runner.state.started_at_ns -= 60_000_000_000
        return reset(context)

    target.reset = slow_reset
    assert runner.start().status == "timed_out"
    assert not provider.contexts and not target.chunks


def test_allowed_type_cannot_smuggle_an_alternate_action_payload(make_runner):
    runner, target, _ = make_runner(
        [
            plan(),
            act(action={"type": "move", "action": "another_skill()"}),
        ]
    )
    assert runner.start().error_code == "invalid_decision"
    assert not target.chunks


def test_provider_scope_closes_on_cancellation(make_runner):
    runner, target, provider = make_runner(["wait"])
    closed = []

    @asynccontextmanager
    async def factory():
        try:
            yield provider
        finally:
            closed.append(True)

    runner.skill_runtime.provider_factory = factory
    provider.on_call = lambda context: runner.cancel("stop")
    assert runner.start().status == "cancelled"
    assert closed == [True] and not target.chunks

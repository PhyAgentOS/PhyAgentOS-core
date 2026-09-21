"""Real Core targets, adapters and provider; mock only the external HTTP boundary."""

import asyncio
import json
from contextlib import asynccontextmanager
from importlib.resources import files

import httpx
import pytest
import yaml

from PhyAgentOS.cli.general_game_commands import success_check
from PhyAgentOS.game_agents.stardew import register_general_game
from PhyAgentOS.providers.custom_provider import CustomProvider
from PhyAgentOS.runtime.preflight.runtime_compatibility_preflight import (
    RuntimeCompatibilityPreflight,
)
from PhyAgentOS.runtime.schemas import SessionSpec, SkillRuntimeSpec, TargetSpec
from PhyAgentOS.runtime.sessions.session_runner import SessionRunner
from PhyAgentOS.runtime.watchdog.result_writer import ResultWriter
from PhyAgentOS.runtime.watchdog.runtime_registry import SkillRuntimeRegistry, TargetRuntimeRegistry
from PhyAgentOS.runtime.watchdog.scheduler import ScheduledSession

from .test_session import ScriptedProvider, act, plan


@pytest.fixture
def core_runner(tmp_path):
    runners = []

    def make(game, provider_factory, verify, bridge, *, action_catalog=None):
        contract = files("PhyAgentOS").joinpath(
            f"templates/configs/runtime/contracts/{game}.runtime.yaml",
        )
        data = yaml.safe_load(contract.read_text(encoding="utf-8"))
        runtime_name = "StardewValley" if game == "stardewvalley" else "Minecraft"
        target = TargetSpec(
            id=data["target_id"],
            target_class="local",
            target_kind="game",
            workspace=str(tmp_path),
            supported_skillruntimes=["general_game"],
            runtime={
                "target_runtime": f"{runtime_name}TargetRuntime",
                "target_adapter": data["target_adapter"],
                "runtime_contract_ref": str(contract),
            },
            config={"bridge_url": "http://game.test", "step_delay": 0},
        )
        skill = SkillRuntimeSpec(
            id="general_game",
            runtime="GeneralGameSkillRuntime",
            runtime_kind="builtin",
            loop_mode="builtin_loop",
            supported_target_kinds=["game"],
            observation_contract={"observation_type": "structured"},
        )
        session = SessionSpec(
            session_id=f"{game}-session",
            target_ref=target.id,
            skillruntime_ref=skill.id,
            task_description="Move to the next tile",
            execution={"max_steps": 5},
        )
        register_general_game(
            provider_factory,
            model="test-model",
            verify=verify,
            action_catalog=action_catalog or {"move": {"params": {"dx": 1, "dy": 0}}},
        )
        preflight = RuntimeCompatibilityPreflight(tmp_path).check(
            ScheduledSession(session, target, skill, target.id, skill.id),
        )
        assert preflight.verdict == "accepted", preflight.missing_items
        native_target = TargetRuntimeRegistry().build(target)
        native_target._http = httpx.Client(transport=httpx.MockTransport(bridge))
        runner = SessionRunner(
            session=session,
            target_spec=target,
            skillruntime_spec=skill,
            adapter_plan=preflight.adapter_plan,
            target=native_target,
            skill_runtime=SkillRuntimeRegistry().build(skill.runtime),
            policy_client=None,
            perception_runtime=None,
            perception_plan=None,
        )
        runners.append(runner)
        return runner

    yield make
    for runner in runners:
        runner.close()


def test_fatal_bridge_error_stops_core_without_another_model_or_action_call(core_runner):
    provider = ScriptedProvider(
        [
            plan(),
            act(action={"type": "move", "params": {"dx": 1, "dy": 0}}),
        ]
    )
    dispatched = []

    def bridge(request):
        if request.url.path == "/execute":
            dispatched.append(request)
            return httpx.Response(
                200,
                json={"ok": False, "fatal": True, "error": "action interrupted"},
            )
        return httpx.Response(200, json={"ok": True, "obs": {"position": [0, 0]}})

    runner = core_runner("stardewvalley", lambda: provider, lambda obs, feedback: False, bridge)
    result = runner.start()
    assert result.status == "failed"
    assert len(provider.contexts) == 2 and len(dispatched) == 1


@pytest.mark.parametrize("game", ["stardewvalley", "minecraft"])
def test_native_target_reset_clears_previous_session_feedback(core_runner, game):
    def bridge(request):
        return httpx.Response(200, json={"ok": True, "bot_spawned": True, "obs": {}})

    runner = core_runner(game, lambda: ScriptedProvider([]), None, bridge)
    target = runner.target
    target.build()
    target._last_status = {"done": True, "success": True, "reward": 10}
    target._step_idx = 7
    target.reset({})
    assert target.execution_status() == {}
    assert target._step_idx == 0


def test_stardew_native_adapter_observation_action_and_failure(core_runner, tmp_path):
    provider = ScriptedProvider(
        [
            plan(),
            act(action={"type": "move", "params": {"dx": 1, "dy": 0}}),
            plan("replan"),
            act(action={"type": "move", "params": {"dx": 1, "dy": 0}}),
        ]
    )
    state = {"position": [0, 0], "money": 500, "health": 100, "energy": 270}
    calls = []

    def bridge(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/execute":
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                return httpx.Response(200, json={"ok": False, "error": "path blocked"})
            state["position"] = [1, 0]
        return httpx.Response(200, json={"ok": True, "obs": state})

    runner = core_runner(
        "stardewvalley",
        lambda: provider,
        lambda obs, feedback: obs["stardew"]["position"] == [1, 0],
        bridge,
    )
    result = runner.start()
    assert result.success and result.num_steps == 2
    assert calls == [{"action": "move(1, 0)"}] * 2
    assert provider.contexts[0]["observation"]["stardew"]["money"] == 500
    receipt = provider.contexts[2]["last_receipt"]
    assert receipt["end_reason"] == "action_failed"
    assert receipt["rounds"][0]["feedback"]["error_message"] == "path blocked"
    writer = ResultWriter(tmp_path)
    writer.write_episode(runner.session, runner.target_spec, runner.skillruntime_spec.id, result)
    assert (tmp_path / result.artifact_dir).is_dir()
    # Reusing a native target must not expose its previous success/error flags.
    runner.target.reset({})
    assert runner.target.execution_status() == {}


def test_minecraft_native_numpy_observation_is_json_safe(core_runner):
    provider = ScriptedProvider([plan(), act(action={"type": "move", "params": {"forward": 1}})])
    state = {"position": {"x": 0, "y": 64, "z": 0}}
    calls = []

    def bridge(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"bot_spawned": True})
        if request.url.path == "/action":
            calls.append(json.loads(request.content))
            state["position"]["x"] = 1
            return httpx.Response(200, json={"ok": True, "result": "moved"})
        return httpx.Response(200, json={"bot": state, "health": 20, "hunger": 20})

    runner = core_runner(
        "minecraft",
        lambda: provider,
        lambda obs, feedback: obs["info"]["position"]["x"] == 1,
        bridge,
    )
    result = runner.start()
    assert result.success and result.num_steps == 1
    assert calls == [{"type": "move", "params": {"forward": 1}}]
    sensors = provider.contexts[0]["observation"]["sensors"]
    assert "front_rgb" not in sensors
    assert isinstance(sensors["proprio"]["data"], list)
    assert len(result.model_dump_json()) < 15000


def test_real_custom_provider_is_created_in_each_session_loop(core_runner, monkeypatch):
    providers = []
    contexts = []
    original_client = httpx.AsyncClient

    def model_api(request):
        payload = json.loads(request.content)
        context = json.loads(payload["messages"][-1]["content"])["context"]
        contexts.append(context)
        decision = (
            plan()
            if "task" in context
            else act(
                action={"type": "move", "params": {"dx": 1, "dy": 0}},
            )
        )
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "created": 1,
                "model": "test-model",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(decision),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    class ScopedClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(model_api), **kwargs)
            self.loop = asyncio.get_running_loop()

        async def send(self, *args, **kwargs):
            assert asyncio.get_running_loop() is self.loop
            return await super().send(*args, **kwargs)

        async def aclose(self):
            assert asyncio.get_running_loop() is self.loop
            await super().aclose()

    monkeypatch.setattr(httpx, "AsyncClient", ScopedClient)

    @asynccontextmanager
    async def factory():
        provider = CustomProvider(api_base="http://model.test/v1")
        providers.append(provider)
        async with provider._client:
            yield provider

    position = [0, 0]

    def bridge(request):
        if request.url.path == "/execute":
            position[0] += 1
        return httpx.Response(200, json={"ok": True, "obs": {"position": position}})

    for _ in range(2):
        position[0] = 0
        runner = core_runner(
            "stardewvalley",
            factory,
            lambda obs, feedback: obs["stardew"]["position"] == [1, 0],
            bridge,
        )
        assert len(providers) == _  # preflight and registry construction do not open clients
        result = runner.start()
        assert result.success and result.metadata["model_usage"]["total_tokens"] == 30
    assert len(providers) == 2 and len(contexts) == 4
    assert all(provider._client.is_closed() for provider in providers)


def test_example_completion_conditions_require_every_observed_value():
    verify = success_check({"stardew.position": [1, 0], "stardew.current_menu": None})
    assert verify({"stardew": {"position": [1, 0], "current_menu": None}}, {})
    assert not verify({"stardew": {"position": [1, 0]}}, {})
    assert not verify({"stardew": {"position": [0, 0], "current_menu": None}}, {})
    with pytest.raises(ValueError, match="nonempty"):
        success_check({})


def test_example_cli_runs_native_stardew_to_verified_completion(
    core_runner,
    tmp_path,
    monkeypatch,
):
    position = [0, 0]
    replies = iter([plan(), act(action={"type": "move", "params": {"dx": 1, "dy": 0}})])

    def http_api(request):
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "created": 1,
                    "model": "test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(next(replies)),
                            },
                        }
                    ],
                },
            )
        if request.url.path == "/execute":
            assert json.loads(request.content) == {"action": "move(1, 0)"}
            position[0] = 1
        return httpx.Response(200, json={"ok": True, "obs": {"position": position}})

    runner = core_runner("stardewvalley", lambda: ScriptedProvider([]), None, http_api)
    target_data = runner.target_spec.model_dump(mode="json")
    target_data["runtime"]["target_runtime"] = "StardewValleyTargetRuntime"
    arguments = {
        "target": target_data,
        "session": runner.session.model_dump(mode="json"),
        "actions": {"move": {"params": {"dx": 1, "dy": 0}}},
        "success-checks": {"stardew.position": [1, 0]},
    }
    argv = [
        "run_session.py",
        "--workspace",
        str(tmp_path),
        "--model",
        "test",
        "--api-base",
        "http://model.test/v1",
    ]
    for name, value in arguments.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        argv.extend([f"--{name}", str(path)])
    sync_client, async_client = httpx.Client, httpx.AsyncClient
    clients = []

    class AsyncClient(async_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(http_api), **kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: sync_client(
            transport=httpx.MockTransport(http_api),
            **kw,
        ),
    )
    from typer.testing import CliRunner

    from PhyAgentOS.cli.commands import app

    response = CliRunner().invoke(app, ["general-game", *argv[1:]])
    assert response.exit_code == 0, response.output
    result = json.loads(response.stdout)
    assert result["success"] and result["num_steps"] == 1
    assert all(client.is_closed for client in clients)

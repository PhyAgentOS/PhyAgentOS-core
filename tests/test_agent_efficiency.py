"""Agent-loop guards for real Forge arm skills: retries and live context."""

from __future__ import annotations

import copy
import json

from PhyAgentOS.agent_legacy.loop import AgentLoop
from PhyAgentOS.agent_legacy.tools.forge_tool_api import ForgeToolContextTool
from PhyAgentOS.providers.base import LLMProvider, StreamEvent, ToolCallRequest


class _ScriptedProvider(LLMProvider):
    def __init__(self, scripts: list[list[StreamEvent]]):
        super().__init__()
        self._scripts = [list(script) for script in scripts]
        self.requests: list[dict] = []

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat(self, *_args, **_kwargs):
        raise AssertionError("chat() should not be used when chat_stream exists")

    async def chat_stream(self, **kwargs):
        self.requests.append({
            **kwargs,
            "messages": copy.deepcopy(kwargs.get("messages", [])),
        })
        assert self._scripts, "unexpected extra chat_stream call"
        for event in self._scripts.pop(0):
            yield event


class _RecordingBus:
    def __init__(self) -> None:
        self.outbound: list = []

    async def publish_outbound(self, message) -> None:
        self.outbound.append(message)


def _loop(provider: _ScriptedProvider, tmp_path) -> AgentLoop:
    return AgentLoop(
        bus=_RecordingBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=10**9,
    )


def _call(call_id: str, name: str, arguments: dict) -> StreamEvent:
    return StreamEvent(
        type="tool_call",
        tool_call=ToolCallRequest(id=call_id, name=name, arguments=arguments),
    )


def _done() -> StreamEvent:
    return StreamEvent(type="done", finish_reason="tool_calls")


def _answer(text: str) -> list[StreamEvent]:
    return [
        StreamEvent(type="delta", content=text),
        StreamEvent(type="done", finish_reason="stop"),
    ]


def _tool_results(messages: list[dict]) -> list[dict]:
    values = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            parsed = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            values.append(parsed)
    return values


def _action(arguments: dict | None = None) -> dict:
    return {
        "task_id": "task_1",
        "tool_id": "motion.move_pose",
        "arguments": arguments or {"target_pose": {"x": 0.1}},
    }


async def test_repeated_identical_actions_are_not_blocked_by_the_loop(
    tmp_path, monkeypatch
) -> None:
    action = _action()
    provider = _ScriptedProvider([
        [_call("c1", "forge_tool_start_action", action), _done()],
        [_call("c2", "forge_tool_start_action", action), _done()],
        [_call("c3", "forge_tool_start_action", action), _done()],
        [_call("c4", "forge_tool_start_action", action), _done()],
        _answer("done"),
    ])
    loop = _loop(provider, tmp_path)
    executed: list[dict] = []

    async def fake_execute(_name, params):
        executed.append(params)
        return json.dumps({"ok": True, "data": {"accepted": True}})

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    reply = await loop.process_direct("move", "cli:test")

    assert reply == "done"
    assert executed == [action, action, action, action]
    assert not [
        item for item in _tool_results(provider.requests[-1]["messages"])
        if item.get("blocked")
    ]


async def test_changed_arguments_are_executed_in_order(tmp_path, monkeypatch) -> None:
    first = _action({"target_pose": {"x": 0.1}})
    second = _action({"target_pose": {"x": 0.2}})
    provider = _ScriptedProvider([
        [_call("c1", "forge_tool_start_action", first), _done()],
        [_call("c2", "forge_tool_start_action", second), _done()],
        _answer("done"),
    ])
    loop = _loop(provider, tmp_path)
    executed: list[dict] = []

    async def fake_execute(_name, params):
        executed.append(params)
        return json.dumps({"ok": True})

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    await loop.process_direct("move", "cli:test")

    assert executed == [first, second]


async def test_finalize_evidence_errors_are_left_in_the_tool_result(
    tmp_path, monkeypatch
) -> None:
    finalize_result = json.dumps({
        "ok": True,
        "data": {
            "task_id": "task_1",
            "status": "failed",
            "evidence_errors": [
                "verification failed: Evidence Bundle is incomplete: "
                "before:snapshot, after:rgb_image:front"
            ],
        },
    })
    provider = _ScriptedProvider([
        [_call("c1", "forge_task_finalize", {"task_id": "task_1"}), _done()],
        _answer("evidence inspected"),
    ])
    loop = _loop(provider, tmp_path)

    async def fake_execute(_name, _params):
        return finalize_result

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    reply = await loop.process_direct("finish", "cli:test")

    assert reply == "evidence inspected"
    messages = provider.requests[1]["messages"]
    results = _tool_results(messages)
    assert results == [json.loads(finalize_result)]
    assert not [
        message for message in messages
        if message.get("role") == "user"
        and "Evidence Bundle is incomplete" in message.get("content", "")
    ]


class _FakeContextClient:
    def __init__(self) -> None:
        self.tool_calls = 0
        self.context_calls = 0
        self.ready = True
        self.pose = 0.0

    async def get_tool(self, tool_id: str) -> dict:
        self.tool_calls += 1
        return {"ok": True, "data": {"tool_id": tool_id}}

    async def get_tool_context(self, tool_id: str) -> dict:
        self.context_calls += 1
        return {
            "ok": True,
            "data": {"tool_id": tool_id, "ready": self.ready, "pose": self.pose},
        }


async def test_forge_tool_context_always_reads_live_context() -> None:
    client = _FakeContextClient()
    tool = ForgeToolContextTool(client)

    first = json.loads(await tool.execute("motion.move_pose"))
    client.ready = False
    client.pose = 0.25
    second = json.loads(await tool.execute("motion.move_pose"))

    assert first["data"]["context"] == {
        "tool_id": "motion.move_pose", "ready": True, "pose": 0.0,
    }
    assert second["data"]["context"] == {
        "tool_id": "motion.move_pose", "ready": False, "pose": 0.25,
    }
    assert (client.tool_calls, client.context_calls) == (2, 2)

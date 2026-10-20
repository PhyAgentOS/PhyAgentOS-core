"""Efficiency guards for real Forge arm skills: duplicate actions, evidence hints, context cache."""

from __future__ import annotations

import copy
import json

from PhyAgentOS.agent.loop import AgentLoop
from PhyAgentOS.agent.tools import forge_tool_api
from PhyAgentOS.agent.tools.forge_tool_api import ForgeToolContextTool
from PhyAgentOS.providers.base import LLMProvider, StreamEvent, ToolCallRequest


class _ScriptedProvider(LLMProvider):
    def __init__(self, scripts: list[list[StreamEvent]]):
        super().__init__()
        self._scripts = [list(script) for script in scripts]
        self.requests: list[dict] = []

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat(self, *args, **kwargs):
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


async def test_third_identical_action_is_blocked_and_execution_stops(tmp_path, monkeypatch) -> None:
    provider = _ScriptedProvider([
        [_call("c1", "forge_tool_start_action", _action()), _done()],
        [_call("c2", "forge_tool_start_action", _action()), _done()],
        [_call("c3", "forge_tool_start_action", _action()), _done()],
        [_call("c4", "forge_tool_start_action", _action()), _done()],
        _answer("done"),
    ])
    loop = _loop(provider, tmp_path)
    executed: list[str] = []

    async def fake_execute(name, params):
        executed.append(name)
        return json.dumps({"ok": True, "data": {"accepted": True}})

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    reply = await loop.process_direct("move", "cli:test")

    assert reply == "done"
    assert executed == ["forge_tool_start_action", "forge_tool_start_action"]
    blocked = [item for item in _tool_results(provider.requests[3]["messages"]) if item.get("blocked")]
    assert len(blocked) == 1
    assert "identical action already submitted 2 times" in blocked[0]["reason"]
    fourth = [item for item in _tool_results(provider.requests[-1]["messages"]) if item.get("blocked")]
    assert len(fourth) == 2
    assert "already submitted 3 times" in fourth[-1]["reason"]


async def test_changed_arguments_restart_the_duplicate_count(tmp_path, monkeypatch) -> None:
    first = _action({"target_pose": {"x": 0.1}})
    second = _action({"target_pose": {"x": 0.2}})
    provider = _ScriptedProvider([
        [_call("c1", "forge_tool_start_action", first), _done()],
        [_call("c2", "forge_tool_start_action", first), _done()],
        [_call("c3", "forge_tool_start_action", second), _done()],
        [_call("c4", "forge_tool_start_action", second), _done()],
        _answer("done"),
    ])
    loop = _loop(provider, tmp_path)
    executed: list[tuple[str, dict]] = []

    async def fake_execute(name, params):
        executed.append((name, params))
        return json.dumps({"ok": True})

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    await loop.process_direct("move", "cli:test")

    assert [params for _, params in executed] == [first, first, second, second]
    assert not [
        item for item in _tool_results(provider.requests[-1]["messages"]) if item.get("blocked")
    ]


async def test_query_and_status_tools_are_never_suppressed(tmp_path, monkeypatch) -> None:
    query = {"task_id": "task_1", "tool_id": "motion.resolve_relative_pose", "arguments": {}}
    status = {"task_id": "task_1", "invocation_id": "inv_1"}
    provider = _ScriptedProvider([
        [
            _call("c1", "forge_tool_query", query),
            _call("c2", "forge_tool_action_status", status),
            _call("c3", "forge_tool_action_result", status),
            _call("c4", "forge_tool_context", {"tool_id": "motion.move_pose"}),
            _done(),
        ],
        [
            _call("c5", "forge_tool_query", query),
            _call("c6", "forge_tool_action_status", status),
            _call("c7", "forge_tool_action_result", status),
            _call("c8", "forge_tool_context", {"tool_id": "motion.move_pose"}),
            _done(),
        ],
        _answer("done"),
    ])
    loop = _loop(provider, tmp_path)
    executed: list[str] = []

    async def fake_execute(name, params):
        executed.append(name)
        return json.dumps({"ok": True})

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    await loop.process_direct("inspect", "cli:test")

    assert executed == [
        "forge_tool_query",
        "forge_tool_action_status",
        "forge_tool_action_result",
        "forge_tool_context",
        "forge_tool_query",
        "forge_tool_action_status",
        "forge_tool_action_result",
        "forge_tool_context",
    ]
    assert not [
        item for item in _tool_results(provider.requests[-1]["messages"]) if item.get("blocked")
    ]


async def test_finalize_evidence_gap_adds_missing_items_without_repeating_action(
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
    executed: list[str] = []

    async def fake_execute(name, params):
        executed.append(name)
        return finalize_result

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    reply = await loop.process_direct("finish", "cli:test")

    assert reply == "evidence inspected"
    assert executed == ["forge_task_finalize"]
    messages = provider.requests[1]["messages"]
    hints = [
        message["content"] for message in messages
        if message.get("role") == "user" and "Evidence Bundle is incomplete" in message.get("content", "")
    ]
    assert len(hints) == 1
    assert "before:snapshot" in hints[0]
    assert "after:rgb_image:front" in hints[0]
    assert "forge_task_get(task_id=task_1)" in hints[0]
    assert "Do not resubmit the same Action" in hints[0]
    assert not [
        item for item in _tool_results(messages)
        if item.get("blocked")
    ]


class _FakeContextClient:
    def __init__(self) -> None:
        self.tool_calls = 0
        self.context_calls = 0
        self.runtime_revision: str | None = "rev_1"

    async def get_tool(self, tool_id: str) -> dict:
        self.tool_calls += 1
        return {"ok": True, "data": {"tool_id": tool_id}}

    async def get_tool_context(self, tool_id: str) -> dict:
        self.context_calls += 1
        return {"ok": True, "data": {"tool_id": tool_id, "ready": True}}


async def test_context_cache_serves_repeated_reads_without_client_requests() -> None:
    client = _FakeContextClient()
    tool = ForgeToolContextTool(client)

    first = json.loads(await tool.execute("motion.move_pose"))
    second = json.loads(await tool.execute("motion.move_pose"))

    assert first == second
    assert first["data"]["context"]["ready"] is True
    assert (client.tool_calls, client.context_calls) == (1, 1)


async def test_context_cache_invalidates_on_runtime_revision_change() -> None:
    client = _FakeContextClient()
    tool = ForgeToolContextTool(client)

    await tool.execute("motion.move_pose")
    client.runtime_revision = "rev_2"
    await tool.execute("motion.move_pose")

    assert (client.tool_calls, client.context_calls) == (2, 2)


async def test_context_cache_expires_after_ttl(monkeypatch) -> None:
    client = _FakeContextClient()
    tool = ForgeToolContextTool(client)
    now = [100.0]
    monkeypatch.setattr(forge_tool_api, "monotonic", lambda: now[0])

    await tool.execute("motion.move_pose")
    now[0] += 29.0
    await tool.execute("motion.move_pose")
    now[0] += 2.0
    await tool.execute("motion.move_pose")

    assert (client.tool_calls, client.context_calls) == (2, 2)

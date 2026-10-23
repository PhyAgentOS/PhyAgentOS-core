"""B6: controlled parallelism for tools that declare themselves side-effect free."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from PhyAgentOS.agent.loop import AgentLoop
from PhyAgentOS.agent.tools.base import Tool
from PhyAgentOS.agent.tools.cron import CronTool
from PhyAgentOS.agent.tools.filesystem import (
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from PhyAgentOS.agent.tools.forge_task import ForgeTaskCreateTool
from PhyAgentOS.agent.tools.forge_tool_api import ForgeToolStartActionTool
from PhyAgentOS.agent.tools.image import ImageTool
from PhyAgentOS.agent.tools.message import MessageTool
from PhyAgentOS.agent.tools.scene_graph import SceneGraphQueryTool
from PhyAgentOS.agent.tools.shell import ExecTool
from PhyAgentOS.agent.tools.spawn import SpawnTool
from PhyAgentOS.agent.tools.web import WebFetchTool, WebSearchTool
from PhyAgentOS.bus.agent_events import AgentEvent, AgentEventType
from PhyAgentOS.providers.base import LLMProvider, StreamEvent, ToolCallRequest


class _ScriptedProvider(LLMProvider):
    def __init__(self, scripts: list[list[StreamEvent]]):
        super().__init__()
        self._scripts = [list(script) for script in scripts]

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat(self, *args, **kwargs):
        raise AssertionError("chat() should not be used when chat_stream exists")

    async def chat_stream(self, **kwargs):
        for event in self._scripts.pop(0):
            yield event


class _RecordingBus:
    def __init__(self) -> None:
        self.outbound: list = []

    async def publish_outbound(self, message) -> None:
        self.outbound.append(message)


def _loop(provider: LLMProvider, tmp_path) -> AgentLoop:
    return AgentLoop(
        bus=_RecordingBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=10**9,
    )


def _provider_for(*calls: tuple[str, str]) -> _ScriptedProvider:
    return _ScriptedProvider([
        [
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(id=call_id, name=name, arguments={}),
            )
            for call_id, name in calls
        ] + [StreamEvent(type="done", finish_reason="tool_calls")],
        [
            StreamEvent(type="delta", content="finished"),
            StreamEvent(type="done"),
        ],
    ])


def _run(loop: AgentLoop, on_event=None):
    return loop._run_agent_loop(
        [{"role": "user", "content": "go"}],
        on_event=on_event,
    )


def _tool_ids(messages: list[dict[str, Any]]) -> list[str]:
    return [
        message["tool_call_id"]
        for message in messages
        if message.get("role") == "tool"
    ]


class _ProbeTool(Tool):
    """Counts overlapping executions and optionally blocks on a gate."""

    def __init__(
        self,
        tool_name: str,
        *,
        parallel_safe: bool | None = None,
        gate: asyncio.Event | None = None,
        result: str | None = None,
    ) -> None:
        self._name = tool_name
        if parallel_safe is not None:
            self.parallel_safe = parallel_safe
        self.gate = gate
        self.result = result or f"{tool_name} result"
        self.started = asyncio.Event()
        self.started_count = 0
        self.active = 0
        self.max_active = 0
        self.finished = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.started_count += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                await asyncio.sleep(0)
        finally:
            self.active -= 1
        self.finished += 1
        return self.result


class _UndeclaredTool(_ProbeTool):
    """Tool that never overrides the base capability declaration."""


def test_tool_capability_declarations() -> None:
    for tool_cls in (
        ReadFileTool,
        ListDirTool,
        WebSearchTool,
        WebFetchTool,
        SceneGraphQueryTool,
    ):
        assert tool_cls.parallel_safe is True

    for tool_cls in (
        WriteFileTool,
        ExecTool,
        MessageTool,
        SpawnTool,
        CronTool,
        ImageTool,
        ForgeTaskCreateTool,
        ForgeToolStartActionTool,
    ):
        assert tool_cls.parallel_safe is False

    assert Tool.parallel_safe is False


async def test_parallel_safe_tools_overlap(tmp_path) -> None:
    gate = asyncio.Event()
    first = _ProbeTool("read_a", parallel_safe=True, gate=gate)
    second = _ProbeTool("read_b", parallel_safe=True, gate=gate)
    loop = _loop(_provider_for(("c1", "read_a"), ("c2", "read_b")), tmp_path)
    loop.tools.register(first)
    loop.tools.register(second)

    turn = asyncio.create_task(_run(loop))
    await asyncio.wait_for(
        asyncio.gather(first.started.wait(), second.started.wait()), timeout=1
    )

    # Both tools reached their await point before either was released.
    assert first.finished == 0
    assert second.finished == 0
    gate.set()
    await asyncio.wait_for(turn, timeout=1)

    assert first.started_count == 1
    assert second.started_count == 1


async def test_parallel_batches_are_capped_at_four(tmp_path) -> None:
    tool = _ProbeTool("read_burst", parallel_safe=True)
    calls = [(f"c{i}", "read_burst") for i in range(20)]
    loop = _loop(_provider_for(*calls), tmp_path)
    loop.tools.register(tool)

    await asyncio.wait_for(_run(loop), timeout=2)

    assert tool.started_count == 20
    assert 1 < tool.max_active <= 4


async def test_read_only_tool_does_not_overlap_with_writer(tmp_path) -> None:
    gate = asyncio.Event()
    reader = _ProbeTool("read_file", parallel_safe=True, gate=gate)
    writer = _ProbeTool("write_file")
    loop = _loop(_provider_for(("c1", "read_file"), ("c2", "write_file")), tmp_path)
    loop.tools.register(reader)
    loop.tools.register(writer)

    turn = asyncio.create_task(_run(loop))
    await asyncio.wait_for(reader.started.wait(), timeout=1)
    for _ in range(3):
        await asyncio.sleep(0)

    assert writer.started_count == 0
    gate.set()
    await asyncio.wait_for(turn, timeout=1)

    assert writer.started_count == 1
    assert writer.max_active == 1


async def test_completion_events_follow_finish_order_and_messages_keep_call_order(
    tmp_path,
) -> None:
    slow_gate = asyncio.Event()
    slow = _ProbeTool("read_slow", parallel_safe=True, gate=slow_gate)
    fast = _ProbeTool("read_fast", parallel_safe=True)
    loop = _loop(_provider_for(("c1", "read_slow"), ("c2", "read_fast")), tmp_path)
    loop.tools.register(slow)
    loop.tools.register(fast)

    events: list[AgentEvent] = []
    fast_done = asyncio.Event()

    async def on_event(event: AgentEvent) -> None:
        events.append(event)
        if (
            event.type == AgentEventType.TOOL_PROGRESS
            and event.metadata.get("phase") == "done"
            and event.metadata.get("tool_call_id") == "c2"
        ):
            fast_done.set()

    turn = asyncio.create_task(_run(loop, on_event))
    await asyncio.wait_for(fast_done.wait(), timeout=1)
    slow_gate.set()
    _, messages = await asyncio.wait_for(turn, timeout=1)

    done_ids = [
        event.metadata.get("tool_call_id")
        for event in events
        if event.type == AgentEventType.TOOL_PROGRESS
        and event.metadata.get("phase") == "done"
    ]
    assert done_ids == ["c2", "c1"]
    assert _tool_ids(messages) == ["c1", "c2"]


async def test_cancelled_batch_fills_unfinished_results(tmp_path, monkeypatch) -> None:
    never = asyncio.Event()
    fast = _ProbeTool("read_fast", parallel_safe=True)
    blocked = _ProbeTool("read_blocked", parallel_safe=True, gate=never)
    later = _ProbeTool("write_later")
    loop = _loop(
        _provider_for(
            ("c1", "read_fast"),
            ("c2", "read_blocked"),
            ("c3", "write_later"),
        ),
        tmp_path,
    )
    loop.tools.register(fast)
    loop.tools.register(blocked)
    loop.tools.register(later)

    appended: list[tuple[str, str]] = []
    original_add = loop.context.add_tool_result

    def record_result(messages, tool_call_id, tool_name, result):
        appended.append((tool_call_id, result))
        return original_add(messages, tool_call_id, tool_name, result)

    monkeypatch.setattr(loop.context, "add_tool_result", record_result)

    events: list[AgentEvent] = []

    async def on_event(event: AgentEvent) -> None:
        events.append(event)

    turn = asyncio.create_task(_run(loop, on_event))
    await asyncio.wait_for(blocked.started.wait(), timeout=1)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert later.started_count == 0
    assert [tool_id for tool_id, _ in appended] == ["c1", "c2", "c3"]
    assert appended[0][1] == "read_fast result"
    assert appended[1][1] == "Tool execution cancelled"
    assert appended[2][1] == "Tool execution cancelled"
    assert any(event.type == AgentEventType.CANCELLED for event in events)


async def test_cancelled_turn_is_persisted_with_cancelled_results(tmp_path) -> None:
    fast = _ProbeTool("read_fast", parallel_safe=True)
    blocked = _ProbeTool("read_blocked", parallel_safe=True, gate=asyncio.Event())
    later = _ProbeTool("write_later")
    provider = _ScriptedProvider([
        [
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c0", name="read_fast", arguments={},
                ),
            ),
            StreamEvent(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(type="delta", content="first finished"),
            StreamEvent(type="done"),
        ],
        [
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c1", name="read_fast", arguments={},
                ),
            ),
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c2", name="read_blocked", arguments={},
                ),
            ),
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c3", name="write_later", arguments={},
                ),
            ),
            StreamEvent(type="done", finish_reason="tool_calls"),
        ],
    ])
    loop = _loop(provider, tmp_path)
    loop.tools.register(fast)
    loop.tools.register(blocked)
    loop.tools.register(later)

    assert await loop.process_direct("first", "cli:test") == "first finished"
    session = loop.sessions.get_or_create("cli:test")
    assert _tool_ids(session.messages) == ["c0"]

    fast_done = asyncio.Event()

    async def on_event(event: AgentEvent) -> None:
        if (
            event.type == AgentEventType.TOOL_PROGRESS
            and event.metadata.get("phase") == "done"
            and event.metadata.get("tool_call_id") == "c1"
        ):
            fast_done.set()

    turn = asyncio.create_task(
        loop.process_direct("second", "cli:test", on_event=on_event)
    )
    await asyncio.wait_for(
        asyncio.gather(blocked.started.wait(), fast_done.wait()), timeout=2
    )
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=2)

    assert later.started_count == 0
    tool_messages = [
        message for message in session.messages
        if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "c0", "c1", "c2", "c3",
    ]
    assert tool_messages[1]["content"] == "read_fast result"
    assert tool_messages[2]["content"] == "Tool execution cancelled"
    assert tool_messages[3]["content"] == "Tool execution cancelled"


async def test_undeclared_tools_stay_serial(tmp_path) -> None:
    gate = asyncio.Event()
    tool = _UndeclaredTool("custom_tool", gate=gate)
    assert tool.parallel_safe is False
    loop = _loop(
        _provider_for(("c1", "custom_tool"), ("c2", "custom_tool")), tmp_path
    )
    loop.tools.register(tool)

    turn = asyncio.create_task(_run(loop))
    await asyncio.wait_for(tool.started.wait(), timeout=1)
    for _ in range(3):
        await asyncio.sleep(0)

    assert tool.started_count == 1
    gate.set()
    await asyncio.wait_for(turn, timeout=1)

    assert tool.started_count == 2
    assert tool.max_active == 1

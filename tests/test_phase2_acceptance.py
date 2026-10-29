"""Phase-2 acceptance: B1-B6 composed through the real runtime paths.

Each test joins components that already have focused unit tests:

* concurrent chat turns, cron ``process_direct`` and ``/stop`` share one loop;
* the real ChannelManager isolates a slow channel from a fast one;
* ``/correct`` arrives through the bus while a tool is running;
* one turn produces an oversized Forge result, then a later session is
  compressed, without either step writing long-term memory;
* cancelling a parallel read batch still persists a complete tool history.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from PhyAgentOS.agent_legacy.loop import AgentLoop
from PhyAgentOS.agent_legacy.tool_results import DEFAULT_MAX_CHARS
from PhyAgentOS.agent_legacy.tools.base import Tool
from PhyAgentOS.bus.agent_events import AgentEvent, AgentEventType
from PhyAgentOS.bus.events import InboundMessage, OutboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.channels.base import BaseChannel
from PhyAgentOS.channels.manager import ChannelManager
from PhyAgentOS.cli.command_groups import gateway_commands
from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest
from PhyAgentOS.session.manager import SessionManager

_SUMMARY_MARKER = "compress the older messages"


def _loop(tmp_path, provider, **kwargs) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=10**9,
        **kwargs,
    )


async def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def _collect_outbound(bus: MessageBus, sink: list[OutboundMessage]) -> None:
    while True:
        sink.append(await bus.consume_outbound())


# ---------------------------------------------------------------------------
# 1. Two chat sessions, one cron session, and /stop on a control session.
# ---------------------------------------------------------------------------


class _SessionGatedProvider:
    """Answers each session only after the test releases that session."""

    def __init__(self) -> None:
        self.entered: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.calls: dict[str, int] = {}
        self.active = 0
        self.max_active = 0

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        text = str(kwargs["messages"][-1].get("content"))
        marker = next(
            name
            for name in ("session-A", "session-B", "cron-job", "session-D")
            if name in text
        )
        self.calls[marker] = self.calls.get(marker, 0) + 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.setdefault(marker, asyncio.Event()).set()
        try:
            await self.release.setdefault(marker, asyncio.Event()).wait()
        finally:
            self.active -= 1
        return LLMResponse(content=f"reply-{marker}")


async def test_two_chat_sessions_and_cron_stay_isolated(tmp_path):
    provider = _SessionGatedProvider()
    loop = _loop(tmp_path, provider, max_concurrent_turns=4)
    runner = asyncio.create_task(loop.run())
    outbound: list[OutboundMessage] = []
    collector = asyncio.create_task(_collect_outbound(loop.bus, outbound))

    async def _send(chat_id: str, content: str) -> None:
        await loop.bus.publish_inbound(InboundMessage(
            channel="cli", chat_id=chat_id, sender_id="user", content=content,
        ))

    try:
        await _send("A", "session-A")
        await _send("B", "session-B")
        cron_task = asyncio.create_task(gateway_commands._process_direct_for_scheduler(
            loop, source="cron:job-1", content="cron-job", session_key="cron:job-1",
        ))
        running = {"session-A", "session-B", "cron-job"}
        await _wait_until(lambda: running <= set(provider.entered))
        assert provider.max_active == 3

        # A fourth session is the /stop target; the first three must survive.
        await _send("D", "session-D")
        await _wait_until(lambda: "session-D" in provider.entered)
        await asyncio.wait_for(provider.entered["session-D"].wait(), 2)
        await _send("D", "/stop")
        await _wait_until(lambda: any(
            message.chat_id == "D"
            and message.event is not None
            and message.event.type == AgentEventType.CANCELLED
            for message in outbound
        ))

        assert loop._has_active_turn("cli:A")
        assert loop._has_active_turn("cli:B")
        assert loop._has_active_turn("cron:job-1")
        assert not loop._has_active_turn("cli:D")

        for marker in running:
            provider.release[marker].set()

        cron_reply = await asyncio.wait_for(cron_task, 2)
        assert cron_reply == "reply-cron-job"
        job = SimpleNamespace(payload=SimpleNamespace(
            deliver=True, to="cron-target", channel="cli", message="cron-job",
        ))
        await gateway_commands._deliver_cron_response(job, cron_reply, loop.bus)
        await _wait_until(lambda: {
            message.chat_id
            for message in outbound
            if message.metadata.get("_turn_final")
        } >= {"A", "B"})
        await _wait_until(lambda: any(
            message.chat_id == "cron-target"
            and message.content == "reply-cron-job"
            for message in outbound
        ))

        replies = {
            message.chat_id: message.content
            for message in outbound
            if message.metadata.get("_turn_final")
        }
        assert replies == {"A": "reply-session-A", "B": "reply-session-B"}
        for message in outbound:
            if message.content.startswith("reply-session-"):
                assert message.chat_id == message.content.removeprefix("reply-session-")
        assert [
            message.chat_id for message in outbound
            if message.content == "reply-cron-job"
        ] == ["cron-target"]
        assert provider.calls == {
            "session-A": 1, "session-B": 1, "cron-job": 1, "session-D": 1,
        }
        assert sum(
            message.content == "reply-cron-job" for message in outbound
        ) == 1
        assert not [message for message in outbound if message.channel == "cron"]

        cancelled = [
            message for message in outbound
            if message.event is not None
            and message.event.type == AgentEventType.CANCELLED
        ]
        assert [message.chat_id for message in cancelled] == ["D"]
        assert "Stopped 1 task(s)." in [
            message.content for message in outbound if message.chat_id == "D"
        ]
    finally:
        loop.stop()
        collector.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner, 2)
        with contextlib.suppress(asyncio.CancelledError):
            await collector


# ---------------------------------------------------------------------------
# 2. Slow channel isolation through ChannelManager.
# ---------------------------------------------------------------------------


class _StreamingChannel(BaseChannel):
    supports_streaming = True

    def __init__(self, name: str, *, gate: asyncio.Event | None = None) -> None:
        super().__init__(SimpleNamespace(allow_from=["*"]), bus=None)
        self.name = name
        self.display_name = name
        self.gate = gate
        self.events: list[OutboundMessage] = []
        self.messages: list[OutboundMessage] = []
        self.terminal = asyncio.Event()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.messages.append(msg)

    async def send_event(self, msg: OutboundMessage) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.events.append(msg)
        if msg.event is not None and msg.event.type == AgentEventType.TURN_DONE:
            self.terminal.set()


def _manager(*channels: BaseChannel) -> ChannelManager:
    manager = ChannelManager.__new__(ChannelManager)
    manager.config = SimpleNamespace(
        channels=SimpleNamespace(send_progress=True, send_tool_hints=False)
    )
    manager.channels = {channel.name: channel for channel in channels}
    manager._dispatch_task = None
    manager._init_dispatch_state()
    manager.STREAM_FLUSH_CHARS = 1
    manager.STREAM_COALESCE_INTERVAL = 1000
    return manager


def _event(channel: str, event: AgentEvent) -> OutboundMessage:
    return OutboundMessage(
        channel=channel, chat_id="chat", content=event.content or "", event=event,
    )


def _turn_events(channel: str) -> list[OutboundMessage]:
    return [
        _event(channel, AgentEvent(type=AgentEventType.TURN_STARTED, turn_id="t1")),
        _event(channel, AgentEvent(
            type=AgentEventType.TEXT_DELTA, content="hello", turn_id="t1",
        )),
        _event(channel, AgentEvent(
            type=AgentEventType.TURN_DONE, content="hello", turn_id="t1",
        )),
    ]


async def test_slow_channel_does_not_block_fast_channel_and_gets_terminal():
    slow_gate = asyncio.Event()
    slow = _StreamingChannel("slow", gate=slow_gate)
    fast = _StreamingChannel("fast")
    manager = _manager(slow, fast)

    try:
        for channel in ("slow", "fast"):
            for message in _turn_events(channel):
                manager._route(message)

        await asyncio.wait_for(fast.terminal.wait(), 2)

        assert slow.events == []
        assert [message.event.type for message in fast.events] == [
            AgentEventType.TURN_STARTED,
            AgentEventType.TEXT_DELTA,
            AgentEventType.TURN_DONE,
        ]
        assert fast.events[-1].content == "hello"

        slow_gate.set()
        await asyncio.wait_for(slow.terminal.wait(), 2)

        assert [message.event.type for message in slow.events] == [
            AgentEventType.TURN_STARTED,
            AgentEventType.TEXT_DELTA,
            AgentEventType.TURN_DONE,
        ]
        assert slow.events[-1].content == "hello"
    finally:
        slow_gate.set()
        await manager.stop_all()


# ---------------------------------------------------------------------------
# 3. /correct through the bus while a tool runs.
# ---------------------------------------------------------------------------


class _GatedTool(Tool):
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def name(self) -> str:
        return "gated_tool"

    @property
    def description(self) -> str:
        return "Test tool that blocks until released."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return "tool result"


class _SnapshotProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.snapshots: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        self.snapshots.append(copy.deepcopy(kwargs["messages"]))
        return self._responses.pop(0)


def _has_correction(messages: list[dict], text: str) -> bool:
    return any(
        message.get("role") == "user"
        and message.get("content") == f"User correction: {text}"
        for message in messages
    )


async def test_correction_during_tool_runs_once_and_keeps_turn(tmp_path):
    tool = _GatedTool()
    provider = _SnapshotProvider([
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="gated_tool", arguments={})],
        ),
        LLMResponse(content="final answer"),
    ])
    loop = _loop(tmp_path, provider)
    loop.tools.register(tool)
    runner = asyncio.create_task(loop.run())
    outbound: list[OutboundMessage] = []
    collector = asyncio.create_task(_collect_outbound(loop.bus, outbound))

    try:
        await loop.bus.publish_inbound(InboundMessage(
            channel="cli", chat_id="test", sender_id="user", content="start",
        ))
        await asyncio.wait_for(tool.entered.wait(), 2)

        await loop.bus.publish_inbound(InboundMessage(
            channel="cli", chat_id="test", sender_id="user",
            content="/correct use cups",
        ))
        await _wait_until(lambda: any(
            message.metadata.get("correction_ack") for message in outbound
        ))
        assert provider.calls == 1
        assert tool.calls == 1
        assert tool.release.is_set() is False

        tool.release.set()
        await _wait_until(lambda: any(
            message.content == "final answer" for message in outbound
        ))
        await _wait_until(lambda: not loop._has_active_turn("cli:test"))

        assert tool.calls == 1
        assert provider.calls == 2
        assert not _has_correction(provider.snapshots[0], "use cups")
        assert _has_correction(provider.snapshots[1], "use cups")
        assert "cli:test" not in loop._corrections
        assert sum(
            bool(message.metadata.get("_turn_final"))
            and message.content == "final answer"
            for message in outbound
        ) == 1
    finally:
        loop.stop()
        collector.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner, 2)
        with contextlib.suppress(asyncio.CancelledError):
            await collector


# ---------------------------------------------------------------------------
# 4. Oversized Forge result, then session compression without MEMORY.md writes.
# ---------------------------------------------------------------------------


class _ForgePayloadTool(Tool):
    name = "forge_task_get"

    @property
    def description(self) -> str:
        return "Return an oversized Forge status payload."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return json.dumps({
            "status": "unknown",
            "error": "gateway heartbeat lost",
            "invocation_id": "inv-acceptance",
            "attempt_id": "att-acceptance",
            "task_id": "task-acceptance",
            "session_id": "session-acceptance",
            "remote_state": {"state": "unknown"},
            "evidence_refs": ["invocation:inv-acceptance"],
            "result": "R" * 20_000,
            "logs": "L" * 20_000,
        })


class _ForgeThenSummaryProvider:
    def __init__(self) -> None:
        self.normal_calls = 0
        self.summary_calls = 0
        self.requests: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub-model"

    def estimate_prompt_tokens(self, messages, tools, model):
        chars = sum(len(str(message.get("content") or "")) for message in messages)
        return chars, "chars"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        system = str(kwargs["messages"][0].get("content") or "")
        if _SUMMARY_MARKER in system.lower():
            self.summary_calls += 1
            return LLMResponse(content="## Current goal\ncontinue seeded task")
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        self.normal_calls += 1
        if self.normal_calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="call-forge", name="forge_task_get", arguments={},
                )],
            )
        if self.normal_calls == 2:
            return LLMResponse(content="answer-after-tool")
        return LLMResponse(content="answer-after-compression")


async def test_long_tool_result_then_compression_keeps_task_running(tmp_path):
    provider = _ForgeThenSummaryProvider()
    loop = _loop(tmp_path, provider)
    loop.tools.register(_ForgePayloadTool())

    assert await loop.process_direct("check forge", "cli:forge") == "answer-after-tool"

    tool_messages = [
        message for message in provider.requests[1] if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert len(tool_messages[0]["content"]) <= DEFAULT_MAX_CHARS
    limited = json.loads(tool_messages[0]["content"])
    for key in (
        "status", "error", "invocation_id", "attempt_id", "task_id",
        "session_id", "remote_state", "evidence_refs",
    ):
        assert key in limited
    assert limited["status"] == "unknown"
    assert limited["invocation_id"] == "inv-acceptance"
    assert limited["attempt_id"] == "att-acceptance"
    assert limited["task_id"] == "task-acceptance"
    assert limited["session_id"] == "session-acceptance"
    assert limited["remote_state"] == {"state": "unknown"}
    saved = tmp_path / limited["_truncated"]["original_path"]
    assert saved.exists()

    # B5: the same loop now compresses one old session. This is session-local
    # and must not write the long-term memory file.
    loop.memory_consolidator.context_window_tokens = 14_000
    session = loop.sessions.get_or_create("cli:compress")
    for index in range(3):
        session.add_message("user", f"seed-user-{index} " + "x" * 2_000)
        session.add_message("assistant", f"seed-assistant-{index} " + "y" * 2_000)
    loop.sessions.save(session)

    assert await loop.process_direct("next step", "cli:compress") == (
        "answer-after-compression"
    )
    assert provider.summary_calls >= 1

    session = loop.sessions.get_or_create("cli:compress")
    assert "continue seeded task" in session.summary
    history = session.get_history(max_messages=0)
    assert history[-2]["content"] == "next step"
    assert history[-1]["content"] == "answer-after-compression"
    assert not (tmp_path / "memory" / "MEMORY.md").exists()


# ---------------------------------------------------------------------------
# 5. Cancel a parallel read batch and keep the saved history complete.
# ---------------------------------------------------------------------------


class _ReadTool(Tool):
    def __init__(self, name: str, *, gate: asyncio.Event | None = None) -> None:
        self._name = name
        self.gate = gate
        self.finished = 0
        self.active = 0
        self.started = asyncio.Event()
        self.completed = asyncio.Event()

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    parallel_safe = True

    async def execute(self, **kwargs: Any) -> str:
        self.active += 1
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                await asyncio.sleep(0)
        finally:
            self.active -= 1
        self.finished += 1
        self.completed.set()
        return f"{self._name} result"


class _ParallelProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("the cancelled turn must not call the model again")
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id="c1", name="read_fast", arguments={}),
                ToolCallRequest(id="c2", name="read_blocked", arguments={}),
            ],
        )


async def test_cancel_parallel_read_batch_saves_complete_history(tmp_path):
    provider = _ParallelProvider()
    fast = _ReadTool("read_fast")
    blocked = _ReadTool("read_blocked", gate=asyncio.Event())
    loop = _loop(tmp_path, provider)
    loop.tools.register(fast)
    loop.tools.register(blocked)

    turn = asyncio.create_task(loop.process_direct("go", "cli:parallel"))
    await asyncio.wait_for(
        asyncio.gather(fast.started.wait(), blocked.started.wait()), 2
    )
    await asyncio.wait_for(fast.completed.wait(), 2)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert fast.finished == 1
    assert blocked.finished == 0
    assert blocked.active == 0
    assert provider.calls == 1
    assert "cli:parallel" not in loop._active_tasks
    assert "cli:parallel" not in loop._session_slots

    session = SessionManager(tmp_path).get_or_create("cli:parallel")
    tool_messages = [
        message for message in session.messages if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == ["c1", "c2"]
    assert tool_messages[0]["content"] == "read_fast result"
    assert tool_messages[1]["content"] == "Tool execution cancelled"

    assistant = [
        message for message in session.messages if message.get("tool_calls")
    ]
    assert [call["id"] for call in assistant[0]["tool_calls"]] == ["c1", "c2"]

    history = session.get_history(max_messages=0)
    assert [
        message["tool_call_id"] for message in history
        if message.get("role") == "tool"
    ] == ["c1", "c2"]

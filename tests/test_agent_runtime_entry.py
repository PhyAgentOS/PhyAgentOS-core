"""Entry-point identity and per-turn tool state in the agent runtime.

Covers the first Agent-layer phase: every turn (chat, process_direct, cron,
heartbeat, CLI single) is tracked under _active_tasks, turns for one session
stay serial, and message/spawn/cron tool state is scoped to the running turn
instead of shared across sessions.
"""

from __future__ import annotations

import asyncio

from PhyAgentOS.agent_legacy.loop import AgentLoop
from PhyAgentOS.agent_legacy.tools.message import MessageTool
from PhyAgentOS.bus.events import InboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest


class _StaticProvider:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        return self._responses.pop(0)


class _SequenceProvider:
    """Blocks the first turn so a second one can queue behind the lock."""

    def __init__(self) -> None:
        self.calls = 0
        self.order: list[int] = []
        self.first_entered = asyncio.Event()
        self.second_entered = asyncio.Event()
        self.release_first = asyncio.Event()

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        number = self.calls
        self.order.append(number)
        if number == 1:
            self.first_entered.set()
            await self.release_first.wait()
        else:
            self.second_entered.set()
        return LLMResponse(content=f"reply-{number}")


def _drain(queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def _loop(tmp_path, provider) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=10**9,
    )


async def test_process_direct_is_registered_and_stoppable(tmp_path):
    entered = asyncio.Event()

    class _HangingProvider:
        def get_default_model(self) -> str:
            return "stub-model"

        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            entered.set()
            await asyncio.Event().wait()

    loop = _loop(tmp_path, _HangingProvider())
    turn = asyncio.create_task(loop.process_direct("hello", "cli:test"))
    await asyncio.wait_for(entered.wait(), 2)

    assert [t for t in loop._active_tasks.get("cli:test", []) if not t.done()]

    await asyncio.wait_for(
        loop._handle_stop(InboundMessage(
            channel="cli", chat_id="test", sender_id="user", content="/stop",
        )),
        2,
    )

    assert await asyncio.wait_for(turn, 2) == ""
    assert "cli:test" not in loop._active_tasks


async def test_two_process_direct_turns_for_one_session_serialize(tmp_path):
    provider = _SequenceProvider()
    loop = _loop(tmp_path, provider)

    first = asyncio.create_task(loop.process_direct("first", "cli:test"))
    await asyncio.wait_for(provider.first_entered.wait(), 2)
    second = asyncio.create_task(loop.process_direct("second", "cli:test"))
    await asyncio.sleep(0.05)

    assert provider.calls == 1
    assert not provider.second_entered.is_set()
    assert len(loop._active_tasks.get("cli:test", [])) == 2

    provider.release_first.set()
    assert await asyncio.wait_for(first, 2) == "reply-1"
    assert await asyncio.wait_for(second, 2) == "reply-2"
    assert provider.order == [1, 2]
    assert "cli:test" not in loop._active_tasks


async def test_message_tool_state_does_not_leak_between_sessions(tmp_path):
    provider = _StaticProvider([
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="message", arguments={"content": "A reply"})],
        ),
        LLMResponse(content="A done"),
        LLMResponse(content="B reply"),
    ])
    loop = _loop(tmp_path, provider)

    reply_a = await loop._process_message(InboundMessage(
        channel="telegram", chat_id="chat-a", sender_id="a", content="hi A",
    ))
    reply_b = await loop._process_message(InboundMessage(
        channel="telegram", chat_id="chat-b", sender_id="b", content="hi B",
    ))

    assert reply_a is None
    assert reply_b is not None
    assert (reply_b.channel, reply_b.chat_id, reply_b.content) == (
        "telegram", "chat-b", "B reply",
    )
    sent = [
        msg for msg in _drain(loop.bus.outbound) if not msg.metadata.get("_progress")
    ]
    assert [(m.channel, m.chat_id, m.content) for m in sent] == [
        ("telegram", "chat-a", "A reply"),
    ]


async def test_concurrent_message_tool_turns_keep_their_own_targets():
    delivered = []

    async def send(msg):
        delivered.append(msg)

    tool = MessageTool(send_callback=send)
    start = asyncio.Event()

    async def turn(channel, chat, key):
        tool.set_context(channel, chat, session_key=key, turn_id=f"{key}-turn")
        tool.start_turn()
        await start.wait()
        await tool.execute("hello")
        return tool._sent_in_turn

    tasks = [
        asyncio.create_task(turn("telegram", "a", "A")),
        asyncio.create_task(turn("telegram", "b", "B")),
    ]
    start.set()
    assert await asyncio.gather(*tasks) == [True, True]
    assert {(msg.channel, msg.chat_id) for msg in delivered} == {
        ("telegram", "a"), ("telegram", "b"),
    }


async def test_spawn_announce_writes_to_parent_session_key(tmp_path):
    loop = _loop(tmp_path, _StaticProvider([LLMResponse(content="subagent done")]))
    spawn = loop.tools.get("spawn")
    assert spawn is not None

    spawn.set_context("telegram", "chat", session_key="telegram:thread-42", turn_id="turn-1")
    await spawn.execute("do the thing")

    msg = await asyncio.wait_for(loop.bus.consume_inbound(), 2)
    assert msg.channel == "system"
    assert msg.session_key == "telegram:thread-42"
    assert msg.session_key_override == "telegram:thread-42"
    assert msg.metadata.get("origin_turn_id") == "turn-1"


async def test_stop_cancels_only_its_own_sessions_subagent(tmp_path):
    class _GatedProvider:
        def __init__(self) -> None:
            self.entered = {"A": asyncio.Event(), "B": asyncio.Event()}
            self.release = {"A": asyncio.Event(), "B": asyncio.Event()}

        def get_default_model(self) -> str:
            return "stub-model"

        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            task = str(kwargs["messages"][-1]["content"])
            key = "A" if "task-A" in task else "B"
            self.entered[key].set()
            await self.release[key].wait()
            return LLMResponse(content=f"done {key}")

    provider = _GatedProvider()
    loop = _loop(tmp_path, provider)
    spawn = loop.tools.get("spawn")
    assert spawn is not None

    spawn.set_context("telegram", "chat", session_key="sess-a", turn_id="ta")
    await spawn.execute("task-A")
    spawn.set_context("telegram", "chat", session_key="sess-b", turn_id="tb")
    await spawn.execute("task-B")
    await asyncio.wait_for(provider.entered["A"].wait(), 2)
    await asyncio.wait_for(provider.entered["B"].wait(), 2)
    assert loop.subagents.get_running_count() == 2

    await asyncio.wait_for(
        loop._handle_stop(InboundMessage(
            channel="telegram", chat_id="chat", sender_id="u", content="/stop",
            session_key_override="sess-a",
        )),
        2,
    )

    assert loop.subagents.get_running_count() == 1
    provider.release["B"].set()
    msg = await asyncio.wait_for(loop.bus.consume_inbound(), 2)
    assert msg.session_key == "sess-b"
    await asyncio.sleep(0.05)
    assert loop.subagents.get_running_count() == 0


async def test_system_message_with_tool_send_replies_once(tmp_path):
    provider = _StaticProvider([
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="message", arguments={"content": "only once"})],
        ),
        LLMResponse(content="final text"),
    ])
    loop = _loop(tmp_path, provider)

    reply = await loop._process_message(InboundMessage(
        channel="system", sender_id="subagent", chat_id="telegram:chat",
        content="subagent report",
    ))

    assert reply is None
    sent = [
        msg for msg in _drain(loop.bus.outbound) if not msg.metadata.get("_progress")
    ]
    assert [(m.channel, m.chat_id, m.content) for m in sent] == [
        ("telegram", "chat", "only once"),
    ]

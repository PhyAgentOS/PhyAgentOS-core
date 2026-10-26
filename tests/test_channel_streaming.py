"""B2: per-channel streaming, coalescing, and rate-limit behaviour."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from PhyAgentOS.bus.agent_events import AgentEvent, AgentEventType
from PhyAgentOS.bus.events import OutboundMessage
from PhyAgentOS.channels import telegram as telegram_module
from PhyAgentOS.channels import wecom as wecom_module
from PhyAgentOS.channels.base import BaseChannel
from PhyAgentOS.channels.manager import ChannelManager
from PhyAgentOS.channels.telegram import TelegramChannel
from PhyAgentOS.channels.wecom import WecomChannel
from PhyAgentOS.config.schema import TelegramConfig, WecomConfig


class _FakeChannel(BaseChannel):
    def __init__(self, name: str, *, delay: float = 0.0, streaming: bool = True):
        super().__init__(SimpleNamespace(allow_from=["*"]), bus=None)
        self.name = name
        self.display_name = name
        self.supports_streaming = streaming
        self.delay = delay
        self.messages: list[OutboundMessage] = []
        self.events: list[OutboundMessage] = []
        self.finished: list[float] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.messages.append(msg)
        self.finished.append(time.monotonic())

    async def send_event(self, msg: OutboundMessage) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.events.append(msg)
        self.finished.append(time.monotonic())


def _manager(*channels: BaseChannel, **options) -> ChannelManager:
    manager = ChannelManager.__new__(ChannelManager)
    manager.config = SimpleNamespace(
        channels=SimpleNamespace(
            send_progress=options.get("send_progress", True),
            send_tool_hints=options.get("send_tool_hints", False),
        )
    )
    manager.channels = {channel.name: channel for channel in channels}
    manager._dispatch_task = None
    manager._init_dispatch_state()
    manager.WORKER_IDLE_TIMEOUT = 0.05
    return manager


def _message(
    channel: str,
    content: str,
    *,
    event: AgentEvent | None = None,
    chat_id: str = "chat",
    media: list[str] | None = None,
    thread_id: int | None = None,
    turn_id: str | None = None,
    final: bool = False,
    **metadata,
) -> OutboundMessage:
    meta = dict(metadata)
    if thread_id is not None:
        meta["message_thread_id"] = thread_id
    if turn_id is not None:
        meta["turn_id"] = turn_id
    if final:
        meta["_turn_final"] = True
    return OutboundMessage(
        channel=channel, chat_id=chat_id, content=content,
        media=media or [], metadata=meta, event=event,
    )


def _started(channel: str, *, turn_id: str | None = None,
             thread_id: int | None = None) -> OutboundMessage:
    return _message(
        channel, "", thread_id=thread_id,
        event=AgentEvent(type=AgentEventType.TURN_STARTED, turn_id=turn_id),
    )


def _delta(channel: str, content: str, *, turn_id: str | None = None,
           thread_id: int | None = None) -> OutboundMessage:
    return _message(
        channel, content, thread_id=thread_id,
        event=AgentEvent(type=AgentEventType.TEXT_DELTA, content=content,
                         turn_id=turn_id),
    )


def _terminal(channel: str, event_type: AgentEventType, *,
              content: str | None = None, turn_id: str | None = None,
              thread_id: int | None = None) -> OutboundMessage:
    return _message(
        channel, content or "", thread_id=thread_id,
        event=AgentEvent(type=event_type, content=content, turn_id=turn_id),
    )


async def _wait_idle(manager: ChannelManager, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not manager._workers and not manager._queues and not manager._streams:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("manager did not become idle")


async def test_slow_channel_does_not_block_fast_channel() -> None:
    slow = _FakeChannel("slow", delay=0.25)
    fast = _FakeChannel("fast")
    manager = _manager(slow, fast)

    manager._route(_message("slow", "slow reply"))
    manager._route(_message("fast", "fast reply"))

    await asyncio.sleep(0.08)
    assert [m.content for m in fast.messages] == ["fast reply"]
    assert slow.messages == []

    await asyncio.sleep(0.3)
    assert [m.content for m in slow.messages] == ["slow reply"]


async def test_text_deltas_are_coalesced() -> None:
    channel = _FakeChannel("fake")
    manager = _manager(channel)
    manager.STREAM_COALESCE_INTERVAL = 0.05
    manager.STREAM_FLUSH_CHARS = 1000

    manager._route(_started("fake", turn_id="t1"))
    for piece in ("he", "llo", " world"):
        manager._route(_delta("fake", piece, turn_id="t1"))

    await asyncio.sleep(0.12)

    updates = [m for m in channel.events if m.event.type == AgentEventType.TEXT_DELTA]
    assert len(updates) == 1
    assert updates[0].content == "hello world"


async def test_media_message_does_not_close_the_stream() -> None:
    channel = _FakeChannel("fake")
    manager = _manager(channel)
    manager.STREAM_COALESCE_INTERVAL = 1000

    manager._route(_started("fake", turn_id="t1", thread_id=7))
    manager._route(_delta("fake", "partial", turn_id="t1", thread_id=7))
    manager._route(_message(
        "fake", "photo caption", media=["/tmp/a.png"],
        thread_id=7,
    ))

    await asyncio.sleep(0.02)
    assert ("fake", "chat", "7") in manager._streams
    media_msg = channel.messages[-1]
    assert media_msg.media == ["/tmp/a.png"]
    assert media_msg.content == "photo caption"

    manager._route(_message(
        "fake", "final answer", final=True, turn_id="t1", thread_id=7,
    ))
    await _wait_idle(manager)

    assert manager._streams == {}
    terminal = [
        m for m in channel.events
        if m.event.type == AgentEventType.TURN_DONE
    ]
    assert [m.content for m in terminal] == ["final answer"]


async def test_full_queue_rejects_new_delta_and_keeps_terminal() -> None:
    channel = _FakeChannel("fake", delay=0.03)
    manager = _manager(channel)
    manager.QUEUE_LIMIT = 2
    manager.STREAM_FLUSH_CHARS = 1
    manager.STREAM_COALESCE_INTERVAL = 0.0

    for piece in "abcdefghij":
        manager._route(_delta("fake", piece))

    queue = manager._queues[("fake", "chat")]
    assert [msg.content for msg in queue.items] == ["a", "b"]

    manager._route(_message("fake", "abcdefghij", final=True))
    assert len(queue.items) == 3
    terminal = queue.items[-1]
    assert terminal.event.type == AgentEventType.TURN_DONE
    assert terminal.content == "abcdefghij"

    await _wait_idle(manager)
    deltas = [
        m.content for m in channel.events
        if m.event.type == AgentEventType.TEXT_DELTA
    ]
    assert "".join(deltas) == "ab"
    assert channel.events[-1].event.type == AgentEventType.TURN_DONE


async def test_turn_done_finishes_stream_and_suppresses_duplicate_reply() -> None:
    channel = _FakeChannel("fake")
    manager = _manager(channel)
    manager.STREAM_COALESCE_INTERVAL = 0.01

    manager._route(_started("fake", turn_id="t1"))
    manager._route(_delta("fake", "answer", turn_id="t1"))
    manager._route(_terminal("fake", AgentEventType.TURN_DONE, turn_id="t1"))
    await _wait_idle(manager)

    assert manager._streams == {}
    terminal = [
        m for m in channel.events
        if m.event.type == AgentEventType.TURN_DONE
    ]
    assert [m.content for m in terminal] == ["answer"]

    manager._route(_message("fake", "answer", final=True, turn_id="t1"))
    await _wait_idle(manager)
    assert channel.messages == []

    # An ordinary message with the same text is not the turn's final reply.
    manager._route(_message("fake", "answer"))
    await _wait_idle(manager)
    assert [m.content for m in channel.messages] == ["answer"]


async def test_non_streaming_channel_only_receives_final_reply() -> None:
    channel = _FakeChannel("plain", streaming=False)
    manager = _manager(channel, send_progress=True, send_tool_hints=False)

    manager._route(_started("plain", turn_id="t1"))
    manager._route(_delta("plain", "partial", turn_id="t1"))
    manager._route(_message(
        "plain", "read_file()", event=AgentEvent(
            type=AgentEventType.TOOL_PROGRESS,
            content="read_file()",
        ),
        _progress=True, _tool_hint=True,
    ))
    manager._route(_terminal("plain", AgentEventType.TURN_DONE, turn_id="t1"))
    manager._route(_message("plain", "final reply", final=True, turn_id="t1"))
    await _wait_idle(manager)

    assert channel.events == []
    assert [m.content for m in channel.messages] == ["final reply"]


async def test_terminal_event_with_other_turn_id_does_not_close_stream() -> None:
    channel = _FakeChannel("fake")
    manager = _manager(channel)
    manager.STREAM_COALESCE_INTERVAL = 1000

    manager._route(_started("fake", turn_id="t1"))
    manager._route(_delta("fake", "partial", turn_id="t1"))
    manager._route(_terminal("fake", AgentEventType.TURN_DONE, turn_id="t2"))

    assert ("fake", "chat", "") in manager._streams
    assert channel.events == []

    manager._route(_message("fake", "final", final=True, turn_id="t1"))
    await _wait_idle(manager)
    assert manager._streams == {}


async def test_cancel_without_stream_still_delivers_status() -> None:
    channel = _FakeChannel("fake")
    manager = _manager(channel)

    manager._route(_terminal("fake", AgentEventType.CANCELLED, turn_id="t9"))
    await _wait_idle(manager)

    assert [m.event.type for m in channel.events] == [AgentEventType.CANCELLED]
    assert channel.events[0].content == "Cancelled."


def _telegram_channel() -> tuple[TelegramChannel, "_FakeBot"]:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, allow_from=["*"]), bus=None,
    )
    bot = _FakeBot()
    channel._app = SimpleNamespace(bot=bot)
    return channel, bot


class _FakeBot:
    def __init__(self) -> None:
        self.drafts: list[dict] = []
        self.messages: list[dict] = []

    async def send_message_draft(self, **kwargs) -> None:
        self.drafts.append(kwargs)

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.messages))


def _telegram_event(
    channel: TelegramChannel,
    event: AgentEvent,
    content: str = "",
    *,
    thread_id: int | None = None,
) -> OutboundMessage:
    metadata = {} if thread_id is None else {"message_thread_id": thread_id}
    return OutboundMessage(
        channel="telegram", chat_id="42", content=content,
        metadata=metadata, event=event,
    )


async def test_telegram_updates_one_draft_then_persists_once(monkeypatch) -> None:
    monkeypatch.setattr(telegram_module, "TELEGRAM_STREAM_INTERVAL", 0.0)
    monkeypatch.setattr(telegram_module, "TELEGRAM_STREAM_MIN_CHARS", 0)
    channel, bot = _telegram_channel()

    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TURN_STARTED),
    ))
    for text in ("Hel", "lo", " world"):
        await channel.send_event(_telegram_event(
            channel,
            AgentEvent(type=AgentEventType.TEXT_DELTA, content=text),
            text,
        ))

    assert len(bot.drafts) == 3
    assert len({draft["draft_id"] for draft in bot.drafts}) == 1
    assert bot.drafts[-1]["text"] == "Hello world"

    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TURN_DONE), "Hello world",
    ))

    assert channel._streams == {}
    assert len(bot.messages) == 1
    assert bot.messages[0]["text"] == "Hello world"


async def test_telegram_skips_small_updates_and_cancel_leaves_no_state(monkeypatch) -> None:
    monkeypatch.setattr(telegram_module, "TELEGRAM_STREAM_INTERVAL", 0.0)
    monkeypatch.setattr(telegram_module, "TELEGRAM_STREAM_MIN_CHARS", 10)
    channel, bot = _telegram_channel()

    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TURN_STARTED),
    ))
    for text in ("a", "b", "c"):
        await channel.send_event(_telegram_event(
            channel,
            AgentEvent(type=AgentEventType.TEXT_DELTA, content=text),
            text,
        ))
    assert len(bot.drafts) == 1
    assert bot.drafts[0]["text"] == "a"

    await channel.send_event(_telegram_event(
        channel,
        AgentEvent(type=AgentEventType.TEXT_DELTA, content="defghijklm"),
        "defghijklm",
    ))
    assert len(bot.drafts) == 2
    assert bot.drafts[-1]["text"] == "abcdefghijklm"

    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.CANCELLED),
    ))
    assert channel._streams == {}
    assert bot.messages == []


async def test_telegram_topics_keep_independent_drafts_and_threads(monkeypatch) -> None:
    monkeypatch.setattr(telegram_module, "TELEGRAM_STREAM_INTERVAL", 0.0)
    monkeypatch.setattr(telegram_module, "TELEGRAM_STREAM_MIN_CHARS", 0)
    channel, bot = _telegram_channel()

    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TURN_STARTED),
        thread_id=1,
    ))
    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TURN_STARTED),
        thread_id=2,
    ))
    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TEXT_DELTA, content="Hello "),
        "Hello ", thread_id=1,
    ))
    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TEXT_DELTA, content="World"),
        "World", thread_id=2,
    ))
    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TEXT_DELTA, content="topic"),
        "topic", thread_id=1,
    ))

    assert set(channel._streams) == {("42", "1"), ("42", "2")}
    drafts_by_thread: dict[int, list[str]] = {}
    for draft in bot.drafts:
        drafts_by_thread.setdefault(draft["message_thread_id"], []).append(
            draft["text"]
        )
    assert drafts_by_thread[1][-1] == "Hello topic"
    assert drafts_by_thread[2][-1] == "World"

    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TURN_DONE), "Hello topic",
        thread_id=1,
    ))
    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.CANCELLED), "Cancelled.",
        thread_id=2,
    ))

    assert channel._streams == {}
    assert [message["message_thread_id"] for message in bot.messages] == [1, 2]
    assert bot.messages[0]["text"] == "Hello topic"
    assert bot.messages[1]["text"] == "Cancelled."


async def test_telegram_final_message_is_split(monkeypatch) -> None:
    monkeypatch.setattr(telegram_module, "TELEGRAM_STREAM_INTERVAL", 0.0)
    channel, bot = _telegram_channel()

    text = "a" * 9001
    await channel.send_event(_telegram_event(
        channel, AgentEvent(type=AgentEventType.TURN_DONE), text,
    ))

    assert len(bot.messages) == 3
    assert all(len(message["text"]) <= telegram_module.TELEGRAM_MAX_MESSAGE_LEN
               for message in bot.messages)
    assert "".join(message["text"] for message in bot.messages) == text


class _FakeWecomClient:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[dict] = []
        self.failures = failures

    async def reply_stream(self, frame, stream_id, content, *, finish) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("stream failed")
        self.calls.append({
            "frame": frame, "stream_id": stream_id,
            "content": content, "finish": finish,
        })


def _wecom_channel(failures: int = 0) -> tuple[WecomChannel, _FakeWecomClient]:
    channel = WecomChannel(
        WecomConfig(enabled=True, allow_from=["*"], bot_id="bot", secret="secret"),
        bus=None,
    )
    client = _FakeWecomClient(failures=failures)
    channel._client = client
    channel._chat_frames["chat"] = object()
    channel._generate_req_id = lambda prefix: "stream-1"
    return channel, client


def _wecom_event(event: AgentEvent, content: str = "",
                 metadata: dict | None = None) -> OutboundMessage:
    return OutboundMessage(
        channel="wecom", chat_id="chat", content=content,
        metadata=dict(metadata or {}), event=event,
    )


async def test_wecom_reuses_stream_id_and_finishes_once(monkeypatch) -> None:
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_INTERVAL", 0.0)
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_MIN_CHARS", 0)
    channel, client = _wecom_channel()

    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.TURN_STARTED),
    ))
    for text in ("Hello", " world"):
        await channel.send_event(_wecom_event(
            AgentEvent(type=AgentEventType.TEXT_DELTA, content=text), text,
        ))
    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.TURN_DONE, content="Hello world"),
        "Hello world", {"_stream_text": "Hello world"},
    ))

    assert {call["stream_id"] for call in client.calls} == {"stream-1"}
    assert [call["finish"] for call in client.calls] == [False, False, True]
    assert "".join(call["content"] for call in client.calls) == "Hello world"
    assert channel._streams == {}


async def test_wecom_cancel_finishes_the_same_stream(monkeypatch) -> None:
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_INTERVAL", 0.0)
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_MIN_CHARS", 0)
    channel, client = _wecom_channel()

    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.TURN_STARTED),
    ))
    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.TEXT_DELTA, content="part"), "part",
    ))
    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.CANCELLED), "Cancelled.",
    ))

    assert [call["finish"] for call in client.calls] == [False, True]
    assert client.calls[-1]["content"] == "Cancelled."
    assert channel._streams == {}


async def test_wecom_terminal_without_stream_sends_status(monkeypatch) -> None:
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_INTERVAL", 0.0)
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_MIN_CHARS", 0)
    channel, client = _wecom_channel()

    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.CANCELLED), "Cancelled.",
    ))
    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.ERROR), "provider exploded",
    ))

    assert [call["finish"] for call in client.calls] == [True, True]
    assert [call["content"] for call in client.calls] == [
        "Cancelled.", "provider exploded",
    ]


async def test_wecom_retries_pending_text_after_failure(monkeypatch) -> None:
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_INTERVAL", 0.0)
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_MIN_CHARS", 0)
    channel, client = _wecom_channel(failures=1)

    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.TURN_STARTED),
    ))
    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.TEXT_DELTA, content="hello"), "hello",
    ))
    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.TEXT_DELTA, content=" world"), " world",
    ))
    await channel.send_event(_wecom_event(
        AgentEvent(type=AgentEventType.TURN_DONE, content="hello world"),
        "hello world", {"_stream_text": "hello world"},
    ))

    assert [call["finish"] for call in client.calls] == [False, True]
    assert client.calls[0]["content"] == "hello world"
    assert "".join(call["content"] for call in client.calls) == "hello world"


async def test_failed_reply_is_not_sent_twice_when_error_follows() -> None:
    channel = _FakeChannel("telegram")
    manager = _manager(channel)
    manager.STREAM_COALESCE_INTERVAL = 0.0

    manager._route(_started("telegram", turn_id="t1"))
    manager._route(_message(
        "telegram", "LLM call failed: boom", final=True, turn_id="t1",
    ))
    manager._route(_terminal(
        "telegram", AgentEventType.ERROR, content="boom", turn_id="t1",
    ))
    await _wait_idle(manager)

    delivered = [m.content for m in channel.messages] + [
        m.content for m in channel.events
    ]
    assert delivered.count("LLM call failed: boom") == 1
    assert "boom" not in delivered


async def test_failed_reply_without_a_stream_is_not_sent_twice() -> None:
    channel = _FakeChannel("telegram")
    manager = _manager(channel)

    manager._route(_message(
        "telegram", "Sorry, I encountered an error.", final=True, turn_id="t2",
    ))
    manager._route(_terminal(
        "telegram", AgentEventType.ERROR,
        content="Sorry, I encountered an error.", turn_id="t2",
    ))
    await _wait_idle(manager)

    delivered = [m.content for m in channel.messages] + [
        m.content for m in channel.events
    ]
    assert delivered.count("Sorry, I encountered an error.") == 1


async def test_manager_to_wecom_has_no_gap_or_duplicate(monkeypatch) -> None:
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_INTERVAL", 0.0)
    monkeypatch.setattr(wecom_module, "WECOM_STREAM_MIN_CHARS", 0)
    channel, client = _wecom_channel()
    manager = _manager(channel)
    manager.QUEUE_LIMIT = 2
    manager.STREAM_FLUSH_CHARS = 1
    manager.STREAM_COALESCE_INTERVAL = 0.0

    manager._route(_started("wecom", turn_id="t1"))
    for piece in "abcdefghij":
        manager._route(_delta("wecom", piece, turn_id="t1"))
    manager._route(_message(
        "wecom", "abcdefghij", final=True, turn_id="t1",
    ))
    manager._route(_terminal("wecom", AgentEventType.TURN_DONE, turn_id="t1"))
    await _wait_idle(manager)

    assert {call["stream_id"] for call in client.calls} == {"stream-1"}
    assert client.calls[-1]["finish"] is True
    assert "".join(call["content"] for call in client.calls) == "abcdefghij"

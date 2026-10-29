"""A7/A8: streaming events, CLI rendering, and pre-request budget checks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from PhyAgentOS.agent_legacy import loop as loop_module
from PhyAgentOS.agent_legacy.loop import AgentLoop
from PhyAgentOS.bus.agent_events import AgentEvent, AgentEventType
from PhyAgentOS.bus.events import InboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.providers.base import (
    GenerationSettings,
    LLMProvider,
    StreamEvent,
    ToolCallRequest,
)


class _StreamingProvider(LLMProvider):
    """Replays scripted stream events, one script per completion."""

    def __init__(self, scripts: list[list[StreamEvent]]):
        super().__init__()
        self._scripts = [list(script) for script in scripts]
        self.calls = 0
        self.max_tokens: list[int | None] = []

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat(self, *args, **kwargs):
        raise AssertionError("chat() should not be used when chat_stream exists")

    async def chat_stream(self, **kwargs):
        self.calls += 1
        self.max_tokens.append(kwargs.get("max_tokens"))
        assert self._scripts, "unexpected extra chat_stream call"
        for event in self._scripts.pop(0):
            yield event


class _RecordingBus:
    def __init__(self) -> None:
        self.outbound: list = []

    async def publish_outbound(self, message) -> None:
        self.outbound.append(message)


def _loop(provider, tmp_path, *, context_window_tokens: int = 10**9) -> AgentLoop:
    return AgentLoop(
        bus=_RecordingBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=context_window_tokens,
    )


def _text(events: list[AgentEvent]) -> str:
    return "".join(
        event.content or ""
        for event in events
        if event.type == AgentEventType.TEXT_DELTA
    )


class _StubAgentBase:
    def __init__(self, **kwargs):
        # CLI progress must not be gated by channel switches.
        self.channels_config = SimpleNamespace(
            send_tool_hints=False, send_progress=False,
        )

    def stop(self) -> None:
        pass

    async def close_mcp(self) -> None:
        pass


def _invoke_cli_agent(tmp_path, monkeypatch, agent_cls):
    from PhyAgentOS.cli import commands
    from PhyAgentOS.cli.legacy import legacy_agent
    from PhyAgentOS.config.schema import Config

    config = Config()
    config.agents.defaults.workspace = str(tmp_path)

    monkeypatch.setattr(legacy_agent, "load_command_config", lambda *a, **k: config)
    monkeypatch.setattr(legacy_agent, "apply_startup_overrides", lambda cfg, *a, **k: cfg)
    monkeypatch.setattr(legacy_agent, "print_deprecated_memory_window_notice", lambda c: None)
    monkeypatch.setattr(legacy_agent, "make_provider", lambda c: object())
    monkeypatch.setattr(legacy_agent, "make_evolution_provider", lambda c, p: (None, None))
    monkeypatch.setattr(
        legacy_agent, "build_forge_components", lambda c, p: (None, None, None, None)
    )
    monkeypatch.setattr("PhyAgentOS.cron.service.CronService", lambda *a, **k: object())
    monkeypatch.setattr("PhyAgentOS.agent_legacy.loop.AgentLoop", agent_cls)

    return CliRunner().invoke(
        commands.app,
        ["agent", "--message", "hi", "--session", "cli:test"],
    )


async def test_stream_events_order_and_tool_progress(tmp_path, monkeypatch) -> None:
    provider = _StreamingProvider([
        [
            StreamEvent(type="delta", content="Checking "),
            StreamEvent(type="delta", content="the file."),
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c1", name="read_file", arguments={"path": "a.txt"},
                ),
            ),
            StreamEvent(
                type="done",
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            ),
        ],
        [
            StreamEvent(type="delta", content="All done"),
            StreamEvent(
                type="done",
                usage={"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
            ),
        ],
    ])
    loop = _loop(provider, tmp_path)
    executed: list[tuple[str, dict]] = []

    async def fake_execute(name, params):
        executed.append((name, params))
        return "file body"

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    events: list[AgentEvent] = []

    async def on_event(event: AgentEvent) -> None:
        events.append(event)

    reply = await loop.process_direct("read a.txt", "cli:test", on_event=on_event)

    assert reply == "All done"
    assert [event.type for event in events] == [
        AgentEventType.TURN_STARTED,
        AgentEventType.TEXT_DELTA,
        AgentEventType.TEXT_DELTA,
        AgentEventType.TOOL_PROGRESS,
        AgentEventType.TOOL_PROGRESS,
        AgentEventType.TEXT_DELTA,
        AgentEventType.TURN_DONE,
    ]
    assert _text(events) == "Checking the file.All done"
    assert [event.metadata.get("phase") for event in events
            if event.type == AgentEventType.TOOL_PROGRESS] == ["start", "done"]
    assert all("read_file" in (event.content or "") for event in events
               if event.type == AgentEventType.TOOL_PROGRESS)
    assert all("a.txt" in (event.content or "") for event in events
               if event.type == AgentEventType.TOOL_PROGRESS)
    assert executed == [("read_file", {"path": "a.txt"})]
    done_events = [event for event in events if event.type == AgentEventType.TURN_DONE]
    assert len(done_events) == 1


async def test_turn_done_records_duration_and_model_call_count(
    tmp_path, monkeypatch
) -> None:
    provider = _StreamingProvider([
        [
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c1", name="read_file", arguments={"path": "a.txt"},
                ),
            ),
            StreamEvent(
                type="done",
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            ),
        ],
        [
            StreamEvent(type="delta", content="All done"),
            StreamEvent(
                type="done",
                usage={"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
            ),
        ],
    ])
    loop = _loop(provider, tmp_path)

    async def fake_execute(name, params):
        return "file body"

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    records: list[tuple[str, tuple]] = []

    class _CaptureLogger:
        def info(self, message, *args, **kwargs):
            records.append((message, args))

        def error(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

    monkeypatch.setattr(loop_module, "logger", _CaptureLogger())

    events: list[AgentEvent] = []

    async def on_event(event: AgentEvent) -> None:
        events.append(event)

    message = InboundMessage(
        channel="cli", chat_id="test", sender_id="user", content="read a.txt",
        turn_id="turn-abc",
    )
    await loop._dispatch(message, publish=False, on_event=on_event)

    done_events = [event for event in events if event.type == AgentEventType.TURN_DONE]
    assert len(done_events) == 1
    done = done_events[0]
    assert done.turn_id == "turn-abc"
    assert done.metadata["duration_s"] >= 0
    assert done.metadata["model_call_count"] == 2
    assert provider.calls == 2

    usage_records = [
        args for message_, args in records if message_.startswith("LLM usage")
    ]
    assert len(usage_records) == 2
    assert usage_records[0][1] == "turn-abc:1"
    assert usage_records[1][1] == "turn-abc:2"
    assert all(args[-1] >= 0 for args in usage_records)  # elapsed seconds


async def test_incomplete_tool_arguments_do_not_execute(tmp_path, monkeypatch) -> None:
    provider = _StreamingProvider([
        [
            StreamEvent(type="delta", content='{"path": "a.tx'),
            StreamEvent(type="done"),
        ],
        [
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c1", name="read_file", arguments={"path": "a.txt"},
                ),
            ),
            StreamEvent(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(type="delta", content="complete"),
            StreamEvent(type="done"),
        ],
    ])
    loop = _loop(provider, tmp_path)
    executed: list[str] = []

    async def fake_execute(name, params):
        executed.append(name)
        return "ok"

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    await loop.process_direct("partial", "cli:test")
    assert executed == []

    reply = await loop.process_direct("complete", "cli:test")
    assert reply == "complete"
    assert executed == ["read_file"]


def test_cli_single_mode_streams_once(tmp_path, monkeypatch) -> None:
    class _StubAgent(_StubAgentBase):
        async def process_direct(self, message, session_id, on_event=None):
            assert on_event is not None
            await on_event(AgentEvent(
                type=AgentEventType.TOOL_PROGRESS,
                content='read_file("a.txt")',
            ))
            await on_event(AgentEvent(
                type=AgentEventType.TEXT_DELTA, content="Hel",
            ))
            await on_event(AgentEvent(
                type=AgentEventType.TEXT_DELTA, content="lo",
            ))
            await on_event(AgentEvent(type=AgentEventType.TURN_DONE))
            return "Hello"

    result = _invoke_cli_agent(tmp_path, monkeypatch, _StubAgent)

    assert result.exit_code == 0, result.output
    output = result.stdout
    assert output.count("Hello") == 1
    assert "read_file" in output


async def test_input_budget_rejects_before_request(tmp_path) -> None:
    provider = _StreamingProvider([
        [StreamEvent(type="delta", content="never"), StreamEvent(type="done")],
    ])
    loop = _loop(provider, tmp_path, context_window_tokens=10)
    events: list[AgentEvent] = []

    async def on_event(event: AgentEvent) -> None:
        events.append(event)

    reply = await loop.process_direct("hello", "cli:test", on_event=on_event)

    assert "上下文超出预算" in reply
    assert provider.calls == 0
    assert any(event.type == AgentEventType.ERROR for event in events)
    assert not any(event.type == AgentEventType.TURN_DONE for event in events)


async def test_budget_shrinks_output_tokens_instead_of_failing(
    tmp_path, monkeypatch
) -> None:
    provider = _StreamingProvider([
        [StreamEvent(type="delta", content="ok"), StreamEvent(type="done")],
    ])
    provider.generation = GenerationSettings(max_tokens=32768)
    loop = _loop(provider, tmp_path, context_window_tokens=65536)
    monkeypatch.setattr(
        loop_module, "estimate_prompt_tokens_chain",
        lambda *args, **kwargs: (32953, "tiktoken"),
    )

    reply = await loop.process_direct("move the arm", "cli:test")

    assert reply == "ok"
    assert provider.calls == 1
    assert provider.max_tokens == [65536 - 32953 - loop._OUTPUT_BUDGET_SAFETY_MARGIN]


async def test_unexpected_exception_emits_error_and_final_reply(
    tmp_path, monkeypatch
) -> None:
    loop = _loop(_StreamingProvider([]), tmp_path)

    async def fail(_message, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(loop, "_process_message", fail)
    events: list[AgentEvent] = []

    async def on_event(event: AgentEvent) -> None:
        events.append(event)

    reply = await loop._dispatch(
        InboundMessage(
            channel="telegram", sender_id="test", chat_id="chat", content="hi",
        ),
        on_event=on_event,
    )

    assert reply is not None
    assert reply.content == "Sorry, I encountered an error."
    assert reply.metadata["_turn_final"] is True
    assert [event.type for event in events] == [AgentEventType.ERROR]


async def test_incomplete_tool_arguments_are_rejected_before_execution(
    tmp_path, monkeypatch
) -> None:
    provider = _StreamingProvider([
        [
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c1",
                    name="write_file",
                    arguments={"path": "a.txt"},
                    raw_arguments='{"path": "a.txt"',
                ),
            ),
            StreamEvent(type="done", finish_reason="tool_calls"),
        ],
        [
            StreamEvent(type="delta", content="not executed"),
            StreamEvent(type="done"),
        ],
    ])
    loop = _loop(provider, tmp_path)
    executed: list[str] = []

    async def fake_execute(name, params):
        executed.append(name)
        return "should not run"

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    reply = await loop.process_direct("write it", "cli:test")

    assert reply == "not executed"
    assert executed == []
    session = loop.sessions.get_or_create("cli:test")
    tool_messages = [m for m in session.messages if m.get("role") == "tool"]
    assert tool_messages
    assert "incomplete arguments" in tool_messages[-1]["content"]


async def test_truncated_answer_is_marked_not_silently_complete(tmp_path) -> None:
    provider = _StreamingProvider([
        [
            StreamEvent(type="delta", content="half an answer"),
            StreamEvent(type="done", finish_reason="length"),
        ],
    ])
    loop = _loop(provider, tmp_path)

    reply = await loop.process_direct("answer", "cli:test")

    assert reply.startswith("half an answer")
    assert "Output truncated" in reply


async def test_cancel_emits_cancelled_without_turn_done(tmp_path) -> None:
    class _BlockingProvider(LLMProvider):
        def __init__(self) -> None:
            super().__init__()

        def get_default_model(self) -> str:
            return "stub-model"

        async def chat(self, *args, **kwargs):
            raise AssertionError("chat() should not be used")

        async def chat_stream(self, **kwargs):
            yield StreamEvent(type="delta", content="partial")
            await asyncio.Event().wait()
            yield StreamEvent(type="done")

    loop = _loop(_BlockingProvider(), tmp_path)
    events: list[AgentEvent] = []
    delta_seen = asyncio.Event()

    async def on_event(event: AgentEvent) -> None:
        events.append(event)
        if event.type == AgentEventType.TEXT_DELTA:
            delta_seen.set()

    turn = asyncio.create_task(
        loop.process_direct("hello", "cli:test", on_event=on_event)
    )
    await asyncio.wait_for(delta_seen.wait(), 2)

    await asyncio.wait_for(loop._handle_stop(InboundMessage(
        channel="cli", chat_id="test", sender_id="user", content="/stop",
    )), 2)

    assert await asyncio.wait_for(turn, 2) == ""
    assert events[-1].type == AgentEventType.CANCELLED
    assert not any(event.type == AgentEventType.TURN_DONE for event in events)


async def test_stream_done_reasoning_reaches_both_assistant_messages(
    tmp_path, monkeypatch
) -> None:
    provider = _StreamingProvider([
        [
            StreamEvent(type="delta", content="checking"),
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c1", name="read_file", arguments={"path": "a.txt"},
                ),
            ),
            StreamEvent(
                type="done",
                finish_reason="tool_calls",
                reasoning_content="first reasoning",
                thinking_blocks=[{"type": "thinking", "thinking": "first"}],
            ),
        ],
        [
            StreamEvent(type="delta", content="answer"),
            StreamEvent(
                type="done",
                reasoning_content="second reasoning",
                thinking_blocks=[{"type": "thinking", "thinking": "second"}],
            ),
        ],
    ])
    loop = _loop(provider, tmp_path)

    async def fake_execute(name, params):
        return "body"

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    assert await loop.process_direct("go", "cli:test") == "answer"

    session = loop.sessions.get_or_create("cli:test")
    assistants = [m for m in session.messages if m.get("role") == "assistant"]
    tool_turn = next(m for m in assistants if m.get("tool_calls"))
    final_turn = next(m for m in assistants if not m.get("tool_calls"))
    assert tool_turn["reasoning_content"] == "first reasoning"
    assert tool_turn["thinking_blocks"] == [{"type": "thinking", "thinking": "first"}]
    assert final_turn["reasoning_content"] == "second reasoning"
    assert final_turn["thinking_blocks"] == [
        {"type": "thinking", "thinking": "second"}
    ]


def test_think_filter_hides_closed_and_unclosed_blocks() -> None:
    from PhyAgentOS.cli.legacy.legacy_agent import _ThinkFilter

    split = _ThinkFilter()
    assert split.feed("<thi") == ""
    assert split.feed("nk>secret</thi") == ""
    assert split.feed("nk>Hello") == "Hello"
    assert split.flush() == ""

    unclosed = _ThinkFilter()
    assert unclosed.feed("A<think>hidden") == "A"
    assert unclosed.flush() == ""

    mixed = _ThinkFilter()
    assert mixed.feed("x<think>y</think>z") == "xz"


def test_cli_single_mode_does_not_leak_think(tmp_path, monkeypatch) -> None:
    class _StubAgent(_StubAgentBase):
        async def process_direct(self, message, session_id, on_event=None):
            await on_event(AgentEvent(
                type=AgentEventType.TEXT_DELTA, content="<think>sec",
            ))
            await on_event(AgentEvent(
                type=AgentEventType.TEXT_DELTA, content="ret</think>Hel",
            ))
            await on_event(AgentEvent(
                type=AgentEventType.TEXT_DELTA, content="lo",
            ))
            return "Hello"

    result = _invoke_cli_agent(tmp_path, monkeypatch, _StubAgent)

    assert result.exit_code == 0, result.output
    assert "secret" not in result.stdout
    assert result.stdout.count("Hello") == 1


def test_cli_single_mode_prints_max_iterations_fallback(
    tmp_path, monkeypatch
) -> None:
    fallback = "I reached the maximum number of tool call iterations (1)."

    class _StubAgent(_StubAgentBase):
        async def process_direct(self, message, session_id, on_event=None):
            await on_event(AgentEvent(
                type=AgentEventType.TEXT_DELTA, content="Working",
            ))
            return fallback

    result = _invoke_cli_agent(tmp_path, monkeypatch, _StubAgent)

    assert result.exit_code == 0, result.output
    assert fallback in result.stdout
    assert result.stdout.count("Working") == 1


def test_cli_single_mode_keyboard_interrupt_exits_130(
    tmp_path, monkeypatch
) -> None:
    class _StubAgent(_StubAgentBase):
        async def process_direct(self, message, session_id, on_event=None):
            raise KeyboardInterrupt

    result = _invoke_cli_agent(tmp_path, monkeypatch, _StubAgent)

    assert result.exit_code == 130


async def test_dispatch_publishes_reply_before_turn_done(tmp_path) -> None:
    provider = _StreamingProvider([
        [
            StreamEvent(type="delta", content="One"),
            StreamEvent(type="done"),
        ],
        [
            StreamEvent(type="delta", content="Two"),
            StreamEvent(type="done"),
        ],
    ])
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus, provider=provider, workspace=tmp_path,
        context_window_tokens=10**9,
    )

    def message() -> InboundMessage:
        return InboundMessage(
            channel="cli", chat_id="test", sender_id="user", content="hi",
        )

    # The second input arrives while the first turn is still finishing.
    first = asyncio.create_task(loop._dispatch(message()))
    second = asyncio.create_task(loop._dispatch(message()))
    await asyncio.gather(first, second)

    sequence = []
    while not bus.outbound.empty():
        item = bus.outbound.get_nowait()
        sequence.append(
            ("event", item.event.type) if item.event else ("reply", item.content)
        )
    assert sequence == [
        ("event", AgentEventType.TURN_STARTED),
        ("event", AgentEventType.TEXT_DELTA),
        ("reply", "One"),
        ("event", AgentEventType.TURN_DONE),
        ("event", AgentEventType.TURN_STARTED),
        ("event", AgentEventType.TEXT_DELTA),
        ("reply", "Two"),
        ("event", AgentEventType.TURN_DONE),
    ]


async def test_cancel_chain_cancels_subagents_and_forge_task(tmp_path) -> None:
    class _FakeSubagents:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def cancel_by_session(self, session_key: str) -> int:
            self.calls.append(session_key)
            return 1

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.record = SimpleNamespace(
                task_id="task-1", origin_session_key="cli:test",
            )
            self.store = SimpleNamespace(active=lambda: self.record)
            self.interaction = SimpleNamespace(long_lived=False, supervisor=None)
            self.cancelled: list[tuple[str, str]] = []

        async def cancel_task(self, task_id: str, *, reason: str):
            self.cancelled.append((task_id, reason))
            return self.record

    class _BlockingProvider:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        def get_default_model(self) -> str:
            return "stub-model"

        async def chat_with_retry(self, **kwargs):
            self.entered.set()
            await asyncio.Event().wait()

    provider = _BlockingProvider()
    loop = _loop(provider, tmp_path)
    subagents = _FakeSubagents()
    coordinator = _FakeCoordinator()
    loop.subagents = subagents
    loop.forge_task_coordinator = coordinator
    loop._forge_reconciled = True

    turn = asyncio.create_task(loop.process_direct("hello", "cli:test"))
    await asyncio.wait_for(provider.entered.wait(), 2)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, 2)

    assert subagents.calls == ["cli:test"]
    assert coordinator.cancelled == [("task-1", "user_stop")]


async def test_cron_delivery_skips_empty_and_message_tool_output(
    tmp_path,
) -> None:
    from PhyAgentOS.cli.command_groups.gateway_commands import _deliver_cron_response

    job = SimpleNamespace(payload=SimpleNamespace(
        deliver=True, to="chat", channel="telegram", message="run",
    ))
    bus = _RecordingBus()

    await _deliver_cron_response(job, "reply", bus)
    assert [m.content for m in bus.outbound] == ["reply"]

    await _deliver_cron_response(job, "", bus)
    assert [m.content for m in bus.outbound] == ["reply"]

    # A turn whose message tool already delivered returns "" and must not be
    # delivered a second time by the cron path.
    provider = _StreamingProvider([
        [
            StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="c1", name="message", arguments={"content": "only once"},
                ),
            ),
            StreamEvent(type="done", finish_reason="tool_calls"),
        ],
        [StreamEvent(type="delta", content="ignored"), StreamEvent(type="done")],
    ])
    loop = _loop(provider, tmp_path)
    response = await loop.process_direct("cron work", "cron:job-1")
    await _deliver_cron_response(job, response, loop.bus)

    assert response == ""
    assert [
        m.content for m in loop.bus.outbound if not m.metadata.get("_progress")
    ] == ["only once"]

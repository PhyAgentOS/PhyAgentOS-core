"""B3: /correct is a correction, ordinary text is an append, /stop still cancels.

A correction queues per session and enters the model context only between
model requests, so a running tool is never interrupted and the correction is
never deferred into the next turn. Ordinary text keeps the B1 semantics: it
waits for the current turn and runs as a new turn afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import io
from typing import Any, Awaitable, Callable

from rich.console import Console

from PhyAgentOS.agent.loop import AgentLoop
from PhyAgentOS.agent.tools.base import Tool
from PhyAgentOS.bus.agent_events import AgentEvent, AgentEventType
from PhyAgentOS.bus.events import InboundMessage, OutboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.cli import commands
from PhyAgentOS.cli.commands import _InteractiveRuntime
from PhyAgentOS.providers.base import LLMResponse, StreamEvent, ToolCallRequest


def _loop(tmp_path, provider) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=10**9,
    )


def _correct(text: str, session_key: str) -> InboundMessage:
    return InboundMessage(
        channel="cli",
        chat_id=session_key.split(":", 1)[-1],
        sender_id="user",
        content=f"/correct {text}",
        session_key_override=session_key,
    )


def _stop(session_key: str) -> InboundMessage:
    return InboundMessage(
        channel="cli",
        chat_id=session_key.split(":", 1)[-1],
        sender_id="user",
        content="/stop",
        session_key_override=session_key,
    )


def _has_correction(messages: list[dict], text: str) -> bool:
    return any(
        message.get("role") == "user" and f"User correction: {text}" == message.get("content")
        for message in messages
    )


async def _next_reply(bus, predicate):
    """Consume outbound messages until a non-progress reply matches."""
    while True:
        message = await asyncio.wait_for(bus.consume_outbound(), 3)
        if message.metadata.get("_progress"):
            continue
        if predicate(message.content):
            return message


class _Provider:
    """Replays scripted responses and keeps a snapshot of every request."""

    def __init__(
        self,
        responses: list[LLMResponse],
        on_call: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._on_call = on_call
        self.calls = 0
        self.snapshots: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        self.snapshots.append(copy.deepcopy(kwargs["messages"]))
        if self._on_call is not None:
            await self._on_call(self.calls)
        return self._responses.pop(0)


class _GatedProvider:
    """Blocks the first call until released, then answers immediately."""

    def __init__(self) -> None:
        self.calls = 0
        self.snapshots: list[list[dict]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        self.snapshots.append(copy.deepcopy(kwargs["messages"]))
        number = self.calls
        if number == 1:
            self.entered.set()
            await self.release.wait()
        return LLMResponse(content=f"reply-{number}")


class _GatedTool(Tool):
    """Counts executions and holds the turn open until the test releases it."""

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


async def test_correction_during_tool_execution_reaches_next_request(tmp_path):
    tool = _GatedTool()
    provider = _Provider([
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="gated_tool", arguments={})],
        ),
        LLMResponse(content="done"),
    ])
    loop = _loop(tmp_path, provider)
    loop.tools.register(tool)

    turn = asyncio.create_task(loop.process_direct("start", "sess-a"))
    await asyncio.wait_for(tool.entered.wait(), 2)
    assert loop._correction_ready["sess-a"] is True

    ack = await loop._handle_correct(_correct("use less salt", "sess-a"))
    assert "next safe point" in ack
    assert loop._corrections["sess-a"] == ["use less salt"]
    assert provider.calls == 1

    tool.release.set()
    assert await asyncio.wait_for(turn, 2) == "done"

    assert tool.calls == 1
    assert provider.calls == 2
    assert _has_correction(provider.snapshots[1], "use less salt")
    assert "sess-a" not in loop._corrections


async def test_correction_before_final_reply_keeps_the_same_turn_open(tmp_path):
    loop: AgentLoop | None = None

    async def _inject(call: int) -> None:
        if call == 1:
            await loop._handle_correct(
                _correct("actually use cups", "sess-a"), notify=False,
            )

    provider = _Provider(
        [LLMResponse(content="first answer"), LLMResponse(content="second answer")],
        on_call=_inject,
    )
    loop = _loop(tmp_path, provider)

    reply = await asyncio.wait_for(loop.process_direct("start", "sess-a"), 2)

    assert reply == "second answer"
    assert provider.calls == 2
    assert _has_correction(provider.snapshots[1], "actually use cups")
    assert "sess-a" not in loop._corrections
    assert "sess-a" not in loop._active_tasks


async def test_ordinary_message_waits_and_is_not_inserted_into_running_turn(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider)

    first = asyncio.create_task(loop.process_direct("first", "sess-a"))
    await asyncio.wait_for(provider.entered.wait(), 2)

    second = asyncio.create_task(loop.process_direct("second", "sess-a"))
    await asyncio.sleep(0.05)

    assert provider.calls == 1
    assert not any(
        message.get("content") == "second" for message in provider.snapshots[0]
    )
    assert len(loop._active_tasks.get("sess-a", [])) == 2

    provider.release.set()
    assert await asyncio.wait_for(first, 2) == "reply-1"
    assert await asyncio.wait_for(second, 2) == "reply-2"

    last_user = [
        message for message in provider.snapshots[1] if message.get("role") == "user"
    ][-1]
    assert str(last_user["content"]).endswith("second")
    assert "User correction" not in str(last_user["content"])


async def test_stop_cancels_the_turn_and_drops_corrections(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider)

    turn = asyncio.create_task(loop.process_direct("first", "sess-a"))
    await asyncio.wait_for(provider.entered.wait(), 2)

    await loop._handle_correct(_correct("change plan", "sess-a"))
    assert loop._corrections["sess-a"] == ["change plan"]

    await asyncio.wait_for(loop._handle_stop(_stop("sess-a")), 2)

    assert await asyncio.wait_for(turn, 2) == ""
    assert "sess-a" not in loop._corrections
    assert "sess-a" not in loop._active_tasks

    provider.release.set()
    assert await asyncio.wait_for(loop.process_direct("again", "sess-a"), 2) == "reply-2"
    assert not _has_correction(provider.snapshots[1], "change plan")


async def test_correct_without_active_turn_does_not_start_a_turn(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider)

    ack = await loop._handle_correct(_correct("too late", "sess-a"))

    assert "No active task to correct" in ack
    assert "sess-a" not in loop._corrections
    assert provider.calls == 0

    usage = await loop._handle_correct(InboundMessage(
        channel="cli", chat_id="a", sender_id="user", content="/correct",
        session_key_override="sess-a",
    ))
    assert usage == "Usage: /correct <text>"


async def test_corrections_do_not_cross_sessions(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider)

    first = asyncio.create_task(loop.process_direct("first", "sess-a"))
    await asyncio.wait_for(provider.entered.wait(), 2)

    await loop._handle_correct(_correct("only for A", "sess-a"))

    second = asyncio.create_task(loop.process_direct("other", "sess-b"))
    assert await asyncio.wait_for(second, 2) == "reply-2"
    assert not any(
        _has_correction(snapshot, "only for A") for snapshot in provider.snapshots[1:]
    )
    assert "sess-b" not in loop._corrections

    provider.release.set()
    assert await asyncio.wait_for(first, 2) == "reply-3"
    assert _has_correction(provider.snapshots[-1], "only for A")


async def test_correct_command_enters_run_through_the_bus(tmp_path):
    tool = _GatedTool()
    provider = _Provider([
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="gated_tool", arguments={})],
        ),
        LLMResponse(content="done"),
    ])
    loop = _loop(tmp_path, provider)
    loop.tools.register(tool)

    runner = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(InboundMessage(
            channel="cli", chat_id="test", sender_id="user", content="start",
        ))
        await asyncio.wait_for(tool.entered.wait(), 2)

        await loop.bus.publish_inbound(InboundMessage(
            channel="cli", chat_id="test", sender_id="user",
            content="/correct use less salt",
        ))
        ack = await _next_reply(loop.bus, lambda content: "next safe point" in content)
        assert "next safe point" in ack.content

        tool.release.set()
        reply = await _next_reply(loop.bus, lambda content: content == "done")
        assert reply.content == "done"
    finally:
        loop.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner, 3)

    assert tool.calls == 1
    assert _has_correction(provider.snapshots[1], "use less salt")


class _ScriptedInput:
    """Stands in for prompt_toolkit: reads whatever the test sends."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def __call__(self) -> str:
        return await self._queue.get()

    def send(self, text: str) -> None:
        self._queue.put_nowait(text)


class _ChunkProvider:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_stream(self, **kwargs):
        self.calls += 1
        for chunk in self.chunks:
            yield StreamEvent(type="delta", content=chunk)
        yield StreamEvent(type="done", finish_reason="stop")


def _user_texts(snapshot: list[dict]) -> list[str]:
    return [str(m.get("content")) for m in snapshot if m.get("role") == "user"]


@contextlib.contextmanager
def _capture_console(monkeypatch):
    buffer = io.StringIO()
    monkeypatch.setattr(
        commands, "console",
        Console(file=buffer, force_terminal=False, color_system=None, width=200),
    )
    yield buffer


def _make_runtime(loop: AgentLoop, script: _ScriptedInput) -> _InteractiveRuntime:
    return _InteractiveRuntime(
        loop,
        loop.bus,
        channel="cli",
        chat_id="test",
        markdown=False,
        thinking_ctx=lambda: contextlib.nullcontext(),
        turn_control={"active": False, "session_key": "cli:test"},
        read_input=script,
    )


def _start_interactive(loop: AgentLoop, script: _ScriptedInput):
    runtime = _make_runtime(loop, script)
    return runtime, asyncio.create_task(runtime.run())


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


TOOL_CALL = LLMResponse(
    content="",
    tool_calls=[ToolCallRequest(id="c1", name="gated_tool", arguments={})],
)


async def test_interactive_correction_during_tool_keeps_turn_running(tmp_path, monkeypatch):
    tool = _GatedTool()
    provider = _Provider([TOOL_CALL, LLMResponse(content="done")])
    loop = _loop(tmp_path, provider)
    loop.tools.register(tool)
    script = _ScriptedInput()

    with _capture_console(monkeypatch) as output:
        _, runtime_task = _start_interactive(loop, script)
        script.send("start")
        await asyncio.wait_for(tool.entered.wait(), 2)

        script.send("/correct use less salt")
        await _wait_until(lambda: bool(loop._corrections.get("cli:test")))
        assert not runtime_task.done()
        assert provider.calls == 1

        tool.release.set()
        await _wait_until(lambda: provider.calls == 2)
        script.send("exit")
        await asyncio.wait_for(runtime_task, 5)

    assert "Correction received" in output.getvalue()
    assert tool.calls == 1
    assert _has_correction(provider.snapshots[1], "use less salt")


async def test_interactive_messages_wait_for_turn_end_in_order(tmp_path, monkeypatch):
    tool = _GatedTool()
    provider = _Provider([
        TOOL_CALL,
        LLMResponse(content="first reply"),
        LLMResponse(content="second reply"),
        LLMResponse(content="third reply"),
        LLMResponse(content="fourth reply"),
    ])
    loop = _loop(tmp_path, provider)
    loop.tools.register(tool)
    script = _ScriptedInput()

    with _capture_console(monkeypatch) as output:
        _, runtime_task = _start_interactive(loop, script)
        script.send("start")
        await asyncio.wait_for(tool.entered.wait(), 2)

        script.send("second")
        script.send("third")
        script.send("fourth")
        await _wait_until(lambda: output.getvalue().count("Queued for after this turn") == 3)
        assert provider.calls == 1

        tool.release.set()
        await _wait_until(lambda: provider.calls == 5)
        script.send("exit")
        await asyncio.wait_for(runtime_task, 5)

    # The first turn takes two model calls (tool call, then its answer); the
    # three queued messages follow it as separate turns, in typed order.
    users = [
        _user_texts(snapshot)[-1].rsplit("\n", 1)[-1]
        for snapshot in provider.snapshots[2:]
    ]
    assert users == ["second", "third", "fourth"]


async def test_interactive_stop_cancels_the_running_turn(tmp_path, monkeypatch):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider)
    script = _ScriptedInput()

    with _capture_console(monkeypatch) as output:
        _, runtime_task = _start_interactive(loop, script)
        script.send("start")
        await asyncio.wait_for(provider.entered.wait(), 2)

        script.send("/stop")
        await _wait_until(lambda: "Cancelled." in output.getvalue())
        assert not runtime_task.done()
        assert "cli:test" not in loop._active_tasks

        script.send("exit")
        await asyncio.wait_for(runtime_task, 5)

    assert provider.calls == 1


async def test_interactive_final_reply_is_printed_once(tmp_path, monkeypatch):
    provider = _ChunkProvider(["final ", "answer"])
    loop = _loop(tmp_path, provider)
    script = _ScriptedInput()

    with _capture_console(monkeypatch) as output:
        _, runtime_task = _start_interactive(loop, script)
        script.send("hi")
        await _wait_until(
            lambda: "final answer" in output.getvalue()
            and "cli:test" not in loop._active_tasks
        )
        script.send("exit")
        await asyncio.wait_for(runtime_task, 5)

    assert output.getvalue().count("final answer") == 1


async def test_interactive_ignores_other_sessions_outbound(tmp_path, monkeypatch):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider)
    script = _ScriptedInput()

    with _capture_console(monkeypatch) as output:
        runtime, runtime_task = _start_interactive(loop, script)
        script.send("start")
        await asyncio.wait_for(provider.entered.wait(), 2)

        await loop.bus.publish_outbound(OutboundMessage(
            channel="system",
            chat_id="other:chat",
            content="subagent answer",
            event=AgentEvent(type=AgentEventType.TURN_DONE),
        ))
        await loop.bus.publish_outbound(OutboundMessage(
            channel="system",
            chat_id="other:chat",
            content="subagent answer",
        ))
        await asyncio.sleep(0.1)

        assert not runtime._turn_done.is_set()
        assert runtime.turn_control["active"] is True
        assert "subagent answer" not in output.getvalue()

        provider.release.set()
        await _wait_until(lambda: "reply-1" in output.getvalue())
        script.send("exit")
        await asyncio.wait_for(runtime_task, 5)


async def test_completed_input_line_is_kept(tmp_path):
    loop = _loop(tmp_path, _ChunkProvider(["ok"]))
    script = _ScriptedInput()
    runtime = _make_runtime(loop, script)

    script.send("kept line")
    runtime._input_task = asyncio.create_task(script())
    await asyncio.sleep(0.05)
    assert runtime._input_task.done()

    assert await runtime._take_input() == "kept line"
    assert runtime._input_task is None
    assert script._queue.empty()


async def test_correction_after_final_check_is_refused(tmp_path):
    provider = _Provider([LLMResponse(content="done")])
    loop = _loop(tmp_path, provider)
    seen: dict[str, str] = {}

    async def _on_event(event) -> None:
        if event.type == AgentEventType.TURN_DONE:
            seen["ack"] = await loop._handle_correct(
                _correct("too late", "sess-a"), notify=False,
            )

    await loop.process_direct("start", "sess-a", on_event=_on_event)

    assert "finishing" in seen["ack"]
    assert "safe point" not in seen["ack"]
    assert "sess-a" not in loop._corrections
    assert "sess-a" not in loop._correction_ready

"""CLI scheduling and presentation tests using only local fake sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from io import StringIO

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.types import TurnRequest, TurnResult, TurnStatus
from PhyAgentOS.cli.interaction.agent_output import (
    AgentOutput,
    ThinkFilter,
    display_text,
    visible_text,
)
from PhyAgentOS.cli.interaction.interactive_session import InteractiveAgent, _Queued
from PhyAgentOS.cli.interaction.terminal_input import TerminalInput


class FakeInput:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.reads = 0
        self.active_reads = 0
        self.cancelled_reads = 0

    def send(self, *lines: str | BaseException) -> None:
        for line in lines:
            self.lines.put_nowait(line)

    async def __call__(self) -> str:
        self.reads += 1
        self.active_reads += 1
        try:
            line = await self.lines.get()
            if isinstance(line, BaseException):
                raise line
            return line
        except asyncio.CancelledError:
            self.cancelled_reads += 1
            raise
        finally:
            self.active_reads -= 1


class FakeSession:
    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []
        self.releases: list[asyncio.Event] = []
        self.cancelled: list[str] = []
        self.interrupted: set[int] = set()
        self.active: int | None = None
        self.active_submits = 0
        self.max_active_submits = 0
        self.closed = False
        self.fail = False

    async def submit(self, request: TurnRequest) -> TurnResult:
        index = len(self.requests)
        self.requests.append(request)
        released = asyncio.Event()
        self.releases.append(released)
        self.active = index
        self.active_submits += 1
        self.max_active_submits = max(self.max_active_submits, self.active_submits)
        try:
            await released.wait()
            if self.fail:
                raise RuntimeError("fake storage unavailable")
            status = (
                TurnStatus.INTERRUPTED if index in self.interrupted else TurnStatus.COMPLETED
            )
            return TurnResult(
                session_id=request.session_id,
                turn_id=f"turn-{index}",
                status=status,
                reply=f"reply:{request.text}",
            )
        finally:
            self.active_submits -= 1
            self.active = None

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)
        if self.active is not None:
            self.interrupted.add(self.active)
            self.releases[self.active].set()

    async def aclose(self) -> None:
        self.closed = True


class FakeOutput:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.results: list[TurnResult] = []
        self.turn_starts = 0
        self.turn_finishes = 0
        self.cancel_requests = 0
        self.tool_views: list[str] = []

    def info(self, text: str) -> None:
        self.infos.append(text)

    def error(self, text: str) -> None:
        self.errors.append(text)

    def result(self, result: TurnResult) -> None:
        self.results.append(result)

    def turn_started(self, session_id=None) -> None:
        self.turn_starts += 1

    def turn_finished(self) -> None:
        self.turn_finishes += 1

    def cancel_requested(self) -> None:
        self.cancel_requests += 1

    def tool_view(self, argument: str) -> str:
        self.tool_views.append(argument)
        return f"tool view:{argument}"


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.fixture
async def running_agent():
    session = FakeSession()
    reader = FakeInput()
    output = FakeOutput()
    agent = InteractiveAgent(session, session_id="cli:test", output=output, read_input=reader)
    output.agent = agent
    task = asyncio.create_task(agent.run())
    try:
        yield session, reader, output, task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert reader.active_reads == 0
        assert session.active_submits == 0
        assert not session.closed


async def test_accepts_fifo_input_while_tool_blocked_and_queues_exit(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: len(session.requests) == 1)
    reader.send("second", "third", "exit")
    await until(lambda: reader.reads >= 4)
    assert [request.text for request in session.requests] == ["first"]
    assert not task.done()
    assert not session.cancelled
    session.releases[0].set()
    await until(lambda: len(session.requests) == 2)
    session.releases[1].set()
    await until(lambda: len(session.requests) == 3)
    session.releases[2].set()
    await asyncio.wait_for(task, 2)
    assert [request.text for request in session.requests] == ["first", "second", "third"]
    assert {request.session_id for request in session.requests} == {"cli:test"}
    assert session.max_active_submits == 1
    assert [result.turn_id for result in output.results] == ["turn-0", "turn-1", "turn-2"]


@pytest.mark.parametrize("exit_command", ["exit", "quit", "/exit", "/quit", ":q", " EXIT "])
async def test_exit_waits_for_active_turn(running_agent, exit_command):
    session, reader, output, task = running_agent
    reader.send("move")
    await until(lambda: session.active is not None)
    reader.send(exit_command)
    await until(lambda: reader.reads == 3)
    assert not task.done()
    assert not session.cancelled
    session.releases[0].set()
    await asyncio.wait_for(task, 2)
    assert len(output.results) == 1


async def test_pending_read_survives_end_of_turn_without_losing_input(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None and reader.reads == 2)
    session.releases[0].set()
    await until(lambda: len(output.results) == 1)
    assert reader.reads == 2
    assert reader.cancelled_reads == 0
    reader.send("second")
    await until(lambda: len(session.requests) == 2)
    reader.send("exit")
    session.releases[1].set()
    await asyncio.wait_for(task, 2)
    assert [request.text for request in session.requests] == ["first", "second"]
    assert len(output.results) == 2


async def test_programmatic_stop_targets_current_turn_and_keeps_reading(running_agent):
    session, reader, output, task = running_agent
    reader.send("blocked action")
    await until(lambda: session.active is not None)
    output.agent.request_stop()
    await until(lambda: len(session.cancelled) == 1)
    reader.send("next")
    await until(lambda: len(session.requests) == 2)
    await until(lambda: len(session.requests) == 2)
    assert output.results[0].status is TurnStatus.INTERRUPTED
    assert session.requests[1].text == "next"
    assert not task.done()
    session.releases[1].set()
    await until(lambda: len(output.results) == 2)
    reader.send("last", "exit")
    await until(lambda: len(session.requests) == 3)
    session.releases[2].set()
    await asyncio.wait_for(task, 2)
    assert session.cancelled == ["cli:test"]
    assert len(output.results) == 3


async def test_stop_does_not_block_reading_while_cancel_is_settling(running_agent):
    session, reader, output, task = running_agent
    cancelled = asyncio.Event()
    finish_cancel = asyncio.Event()

    async def cancel(session_id):
        session.cancelled.append(session_id)
        cancelled.set()
        await finish_cancel.wait()
        session.interrupted.add(0)
        session.releases[0].set()

    session.cancel = cancel
    reader.send("first")
    await until(lambda: session.active is not None)
    output.agent.request_stop()
    await asyncio.wait_for(cancelled.wait(), 2)
    reader.send("second", "exit")
    await until(lambda: reader.reads >= 3)
    assert len(session.requests) == 1
    finish_cancel.set()
    await until(lambda: len(session.requests) == 2)
    session.releases[1].set()
    await asyncio.wait_for(task, 2)
    assert len(output.results) == 2


@pytest.mark.parametrize("signal", [EOFError, KeyboardInterrupt])
async def test_terminal_interrupt_cancels_active_and_drops_queued_input(running_agent, signal):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    reader.send("queued", signal())
    await asyncio.wait_for(task, 2)
    assert [request.text for request in session.requests] == ["first"]
    assert session.cancelled == ["cli:test"]
    assert len(output.results) == 1
    assert output.results[0].status is TurnStatus.INTERRUPTED


async def test_idle_eof_exits_without_submitting_or_closing(running_agent):
    session, reader, output, task = running_agent
    reader.send(EOFError())
    await asyncio.wait_for(task, 2)
    assert not session.requests
    assert not session.cancelled
    assert not output.results


async def test_task_cancellation_cleans_pending_reader_and_active_turn(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None and reader.active_reads == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.cancelled == ["cli:test"]
    assert reader.cancelled_reads == 1
    assert len(output.results) == 1


async def test_correct_is_rejected_without_a_model_turn(running_agent):
    session, reader, output, task = running_agent
    reader.send("/correct", "/correct previous move", "exit")
    await asyncio.wait_for(task, 2)
    assert not session.requests
    assert len(output.errors) == 2
    assert all("not supported" in error for error in output.errors)


async def test_correct_is_rejected_promptly_during_a_blocked_tool(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    reader.send("/correct replacement", "exit")
    await until(lambda: len(output.errors) == 1)
    assert not task.done()
    session.releases[0].set()
    await asyncio.wait_for(task, 2)
    assert len(session.requests) == 1


async def test_model_commands_run_between_turns_in_fifo_order():
    session, reader, output = FakeSession(), FakeInput(), FakeOutput()
    commands = []

    async def model_command(text):
        assert session.active is None
        commands.append(text)
        return f"accepted {text}"

    agent = InteractiveAgent(
        session,
        session_id="cli:test",
        output=output,
        read_input=reader,
        model_command=model_command,
    )
    task = asyncio.create_task(agent.run())
    try:
        reader.send("first")
        await until(lambda: session.active is not None)
        reader.send("/provider fake", "/model fake-model", "second", "/effort high", "/status", "exit")
        await until(lambda: reader.reads == 8)
        assert not commands
        session.releases[0].set()
        await until(lambda: len(session.requests) == 2)
        assert commands == ["/provider fake", "/model fake-model"]
        session.releases[1].set()
        await asyncio.wait_for(task, 2)
        assert commands == ["/provider fake", "/model fake-model", "/effort high", "/status"]
        assert [
            info for info in output.infos if info.startswith("accepted ")
        ] == [f"accepted {command}" for command in commands]
        assert len(output.results) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_model_commands_without_callback_do_not_reach_session(running_agent):
    session, reader, output, task = running_agent
    reader.send("/model fake", "/provider fake", "/effort high", "/status", "exit")
    await asyncio.wait_for(task, 2)
    assert not session.requests
    assert len(output.errors) == 4


async def test_eof_cancels_and_awaits_active_model_command():
    session, reader, output = FakeSession(), FakeInput(), FakeOutput()
    started, finished = asyncio.Event(), asyncio.Event()

    async def command(text):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    agent = InteractiveAgent(
        session, session_id="cli:test", output=output, read_input=reader, model_command=command
    )
    task = asyncio.create_task(agent.run())
    try:
        reader.send("/status")
        await asyncio.wait_for(started.wait(), 2)
        reader.send(EOFError())
        await asyncio.wait_for(task, 2)
        assert finished.is_set()
        assert not session.requests
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_submit_failure_is_reported_once_without_retry(running_agent):
    session, reader, output, task = running_agent
    session.fail = True
    reader.send("first", "exit")
    await until(lambda: session.active is not None)
    session.releases[0].set()
    await asyncio.wait_for(task, 2)
    assert len(session.requests) == 1
    assert not output.results
    assert len(output.errors) == 1
    assert "fake storage unavailable" in output.errors[0]
    assert "does not mean a remote action failed" in output.errors[0]


async def test_cancel_failure_is_reported_without_retry_and_eof_settles_tasks(running_agent):
    session, reader, output, task = running_agent

    async def cancel(session_id):
        session.cancelled.append(session_id)
        raise RuntimeError("fake cancel unavailable")

    session.cancel = cancel
    reader.send("first")
    await until(lambda: session.active is not None)
    reader.send(EOFError())
    await asyncio.wait_for(task, 2)
    assert session.cancelled == ["cli:test"]
    assert len(output.errors) == 1
    assert "fake cancel unavailable" in output.errors[0]
    assert not output.results


async def test_parent_cancel_racing_with_completed_turn_still_prints_final_result(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    session.releases[0].set()
    # The submit task completes on this scheduler turn; its shield's wake-up
    # reaches run() after the explicit parent cancellation below.
    await asyncio.sleep(0)
    assert session.active_submits == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(output.results) == 1
    assert output.results[0].status is TurnStatus.COMPLETED


async def test_blank_input_never_reaches_session(running_agent):
    session, reader, output, task = running_agent
    reader.send("", "  ", "\n", "exit")
    await asyncio.wait_for(task, 2)
    assert not session.requests
    assert not output.errors


async def test_request_stop_is_idempotent_while_cancellation_is_pending(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    output.agent.request_stop()
    output.agent.request_stop()
    output.agent.request_stop()
    await until(lambda: session.cancelled == ["cli:test"])
    assert output.cancel_requests == 1
    assert not task.done()
    session.releases[0].set()
    await until(lambda: len(output.results) == 1)
    reader.send("exit")
    await asyncio.wait_for(task, 2)
    assert [request.text for request in session.requests] == ["first"]


async def test_stop_command_is_a_local_hint_and_never_cancels(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    reader.send("/stop")
    await until(lambda: any("Esc" in info for info in output.infos))
    assert not session.cancelled
    assert session.active is not None
    assert not task.done()
    assert not any("cancellation requested" in info.lower() for info in output.infos)
    session.releases[0].set()
    await until(lambda: len(output.results) == 1)
    reader.send("exit")
    await asyncio.wait_for(task, 2)
    assert [request.text for request in session.requests] == ["first"]


async def test_queue_receipt_is_immediate_and_never_echoes_message_content(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    reader.send("second with sk-secret-value")
    await until(lambda: any("Queued message 2" in info for info in output.infos))
    assert not any("sk-secret-value" in info for info in output.infos)
    session.releases[0].set()
    await until(lambda: len(session.requests) == 2)
    assert any("Processing message 2" in info for info in output.infos)
    session.releases[1].set()
    await until(lambda: len(output.results) == 2)
    reader.send("exit")
    await asyncio.wait_for(task, 2)
    assert [request.text for request in session.requests] == [
        "first",
        "second with sk-secret-value",
    ]


async def test_help_is_answered_locally_without_a_model_turn(running_agent):
    session, reader, output, task = running_agent
    reader.send("/help", "exit")
    await asyncio.wait_for(task, 2)
    assert not session.requests
    assert any("Local commands" in info and "/tools" in info for info in output.infos)


async def test_unknown_slash_command_is_rejected_locally(running_agent):
    session, reader, output, task = running_agent
    reader.send("/frobnicate secret-argument", "exit")
    await asyncio.wait_for(task, 2)
    assert not session.requests
    assert len(output.errors) == 1
    assert "Unknown command /frobnicate" in output.errors[0]
    assert "not sent to the model" in output.errors[0]
    assert "secret-argument" not in output.errors[0]


async def test_tools_command_toggles_locally_even_while_a_turn_is_running(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    reader.send("/tools on")
    await until(lambda: output.tool_views == ["on"])
    assert any("tool view:on" in info for info in output.infos)
    assert not session.cancelled
    session.releases[0].set()
    await until(lambda: len(output.results) == 1)
    reader.send("/tools", "exit")
    await asyncio.wait_for(task, 2)
    assert output.tool_views == ["on", ""]
    assert [request.text for request in session.requests] == ["first"]


async def test_turn_lifecycle_marks_start_and_cleanup_on_success_and_failure(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    assert output.turn_starts == 1
    session.releases[0].set()
    await until(lambda: len(output.results) == 1)
    assert output.turn_finishes >= 1
    reader.send("failed", "exit")
    await until(lambda: session.active is not None)
    session.fail = True
    session.releases[1].set()
    await asyncio.wait_for(task, 2)
    assert output.turn_finishes >= 2
    assert output.turn_starts == 2


async def test_stop_is_still_read_after_exit_was_queued(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    reader.send("exit")
    await until(lambda: reader.reads == 3)
    assert not task.done()
    output.agent.request_stop()
    await asyncio.wait_for(task, 2)
    assert session.cancelled == ["cli:test"]
    assert len(output.results) == 1
    assert output.results[0].status is TurnStatus.INTERRUPTED


async def test_eof_after_queued_exit_cancels_active_turn(running_agent):
    session, reader, output, task = running_agent
    reader.send("first")
    await until(lambda: session.active is not None)
    reader.send("exit", EOFError())
    await asyncio.wait_for(task, 2)
    assert session.cancelled == ["cli:test"]
    assert output.results[0].status is TurnStatus.INTERRUPTED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, ""),
        ("plain", "plain"),
        ("before<think>secret</think>after", "beforeafter"),
        ("visible<think>unfinished secret", "visible"),
        ("<think>first</think>ok<think>second</think>end", "okend"),
        ("keep <thi", "keep <thi"),
    ],
)
def test_visible_text_preserves_legacy_filter(text, expected):
    assert visible_text(text) == expected


def test_think_filter_hides_tags_split_at_every_position():
    text = "before<think>private</think>after"
    for split in range(len(text) + 1):
        think = ThinkFilter()
        assert think.feed(text[:split]) + think.feed(text[split:]) + think.flush() == "beforeafter"


@pytest.mark.parametrize("markdown", [True, False])
def test_output_prints_final_reply_only_once_and_hides_reasoning(markdown):
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=160, color_system=None), markdown=markdown)
    result = TurnResult("session", "turn", TurnStatus.COMPLETED, "<think>secret</think>final reply")
    output.result(result)
    output.result(result)
    assert buffer.getvalue().count("final reply") == 1
    assert "secret" not in buffer.getvalue()
    assert "🍞 PhyAgentOS:" in buffer.getvalue()


def test_output_failed_turn_has_error_and_does_not_claim_remote_failure():
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=160, color_system=None))
    result = TurnResult("session", "turn", TurnStatus.FAILED, error="provider timed out")
    output.result(result)
    output.result(result)
    text = buffer.getvalue()
    assert text.count("provider timed out") == 1
    assert "Turn failed" in text
    assert "does not mean a remote action failed" in text


def test_output_interrupted_means_only_local_cancellation():
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=160, color_system=None))
    output.result(TurnResult("session", "turn", TurnStatus.INTERRUPTED))
    assert "interrupted locally" in buffer.getvalue()
    assert "Remote actions may still be running" in buffer.getvalue()


def test_output_tool_events_show_only_name_and_status():
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=160, color_system=None))
    common = dict(session_id="session", turn_id="turn", step_id="step")
    payload = {"tool_call_id": "call", "tool_name": "fake_tool", "arguments": {"secret": "token"}}
    output.event(
        RuntimeEvent(type=EventType.TURN_STARTED, session_id="session", turn_id="turn")
    )
    output.event(RuntimeEvent(type=EventType.TOOL_INTENT, payload=payload, **common))
    output.event(
        RuntimeEvent(
            type=EventType.TOOL_SETTLED,
            payload={**payload, "status": "unknown", "error": "secret failure details"},
            **common,
        )
    )
    output.event(RuntimeEvent(type=EventType.TURN_COMPLETED, session_id="session", turn_id="turn"))
    lines = buffer.getvalue().splitlines()
    assert lines == ["Thinking", "Tool fake_tool: requested", "! Tool fake_tool: unknown"]
    assert "secret" not in buffer.getvalue() and "token" not in buffer.getvalue()


def _vt100_output() -> tuple[StringIO, Vt100_Output]:
    stream = StringIO()
    return stream, Vt100_Output(stream, lambda: Size(rows=24, columns=80))


async def test_terminal_input_preserves_rich_ansi_through_patch_stdout():
    """The prompt proxy must not turn Rich styling into visible "?[36m" text."""
    stream, vt100 = _vt100_output()
    terminal = TerminalInput()

    class FakePrompt:
        async def prompt_async(self, _message, default=""):
            console = Console(
                force_terminal=True, color_system="standard", width=80
            )
            console.print(Text("plain", style="red"))
            console.print(Markdown("**bold** and `code`"))
            return "hello"

    terminal.prompt = FakePrompt()
    with create_app_session(output=vt100):
        assert await terminal.read() == "hello"

    rendered = stream.getvalue()
    assert "plain" in rendered and "bold" in rendered and "code" in rendered
    assert "\x1b[31m" in rendered
    assert "\x1b[1m" in rendered
    assert "?[31m" not in rendered
    assert "?[1m" not in rendered


def test_agent_output_strips_external_control_sequences_before_raw_passthrough():
    stream, vt100 = _vt100_output()
    with create_app_session(output=vt100):
        with patch_stdout(raw=True):
            output = AgentOutput(
                Console(force_terminal=True, color_system="standard", width=80)
            )
            output.info("hello \x1b[2Jworld")
            output.result(
                TurnResult(
                    "session",
                    "turn",
                    TurnStatus.COMPLETED,
                    reply="**ok** \x1b]0;pwn\x07done",
                )
            )

    rendered = stream.getvalue()
    assert "\x1b[2J" not in rendered
    assert "\x1b]0;pwn" not in rendered
    assert "hello [2Jworld" in rendered
    assert "ok" in rendered and "done" in rendered
    assert "\x1b[1m" in rendered


def test_display_text_keeps_layout_while_removing_control_characters():
    assert display_text("a\nb\tc\x1b[31m") == "a\nb\tc[31m"
    assert display_text("bell\x07") == "bell"


async def test_undo_is_local_and_reports_the_callback_message():
    session, reader, output = FakeSession(), FakeInput(), FakeOutput()
    calls: list[str | None] = []

    async def undo_command(binding):
        assert session.active is None
        calls.append(binding.session_id)
        return "reverted turn-1"

    agent = InteractiveAgent(
        session,
        session_id="cli:test",
        output=output,
        read_input=reader,
        undo_command=undo_command,
    )
    run = asyncio.create_task(agent.run())
    try:
        reader.send("/undo", "exit")
        await asyncio.wait_for(run, 2)
        assert calls == ["cli:test"]
        assert not session.requests
        assert "reverted turn-1" in output.infos
        assert not output.errors
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)


async def test_undo_is_refused_while_a_turn_runs_without_calling_the_callback():
    session, reader, output = FakeSession(), FakeInput(), FakeOutput()
    calls: list[str | None] = []

    async def undo_command(binding):
        calls.append(binding.session_id)
        return "reverted"

    agent = InteractiveAgent(
        session,
        session_id="cli:test",
        output=output,
        read_input=reader,
        undo_command=undo_command,
    )
    run = asyncio.create_task(agent.run())
    try:
        reader.send("first")
        await until(lambda: session.active is not None)
        reader.send("/undo")
        await until(lambda: len(output.errors) == 1)
        assert "while a turn is running" in output.errors[0]
        assert calls == []
        session.releases[0].set()
        await until(lambda: len(output.results) == 1)
        reader.send("exit")
        await asyncio.wait_for(run, 2)
        assert calls == []
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)


async def test_undo_is_refused_while_input_is_queued():
    session, reader, output = FakeSession(), FakeInput(), FakeOutput()
    calls: list[str | None] = []

    async def undo_command(binding):
        calls.append(binding.session_id)
        return "reverted"

    agent = InteractiveAgent(
        session,
        session_id="cli:test",
        output=output,
        read_input=reader,
        undo_command=undo_command,
    )
    agent._pending.put_nowait(_Queued("queued", agent._binding))
    await agent._handle_line("/undo")
    assert calls == []
    assert len(output.errors) == 1
    assert "queued input is waiting" in output.errors[0]


async def test_undo_without_a_callback_reports_it_is_unavailable(running_agent):
    session, reader, output, task = running_agent
    reader.send("/undo", "exit")
    await asyncio.wait_for(task, 2)
    assert not session.requests
    assert len(output.errors) == 1
    assert "/undo is not available" in output.errors[0]


async def test_undo_with_an_argument_is_rejected_without_calling_the_callback(
    running_agent,
):
    session, reader, output, task = running_agent
    reader.send("/undo now", "exit")
    await asyncio.wait_for(task, 2)
    assert not session.requests
    assert len(output.errors) == 1
    assert "takes no arguments" in output.errors[0]


async def test_undo_callback_failure_is_reported_and_the_reader_keeps_running():
    session, reader, output = FakeSession(), FakeInput(), FakeOutput()

    async def undo_command(binding):
        raise RuntimeError("store offline")

    agent = InteractiveAgent(
        session,
        session_id="cli:test",
        output=output,
        read_input=reader,
        undo_command=undo_command,
    )
    run = asyncio.create_task(agent.run())
    try:
        reader.send("/undo", "exit")
        await asyncio.wait_for(run, 2)
        assert not session.requests
        assert len(output.errors) == 1
        assert "Undo failed" in output.errors[0]
        assert "store offline" in output.errors[0]
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)


async def test_help_lists_undo_with_its_side_effect_warning(running_agent):
    session, reader, output, task = running_agent
    reader.send("/help", "exit")
    await asyncio.wait_for(task, 2)
    help_text = next(info for info in output.infos if "Local commands" in info)
    assert "/undo" in help_text
    assert "not undone" in help_text

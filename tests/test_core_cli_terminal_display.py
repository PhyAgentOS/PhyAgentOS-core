"""Behaviour of the live terminal region and its plain fallback.

These tests use a deterministic clock for the spinner and the real
prompt_toolkit pipe input plus Vt100 output for the toolbar. No provider,
Gateway or robot is contacted.
"""

from __future__ import annotations

import asyncio
import re
import sys
from io import StringIO

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.types import TurnResult, TurnStatus
from PhyAgentOS.cli.interaction import terminal_input as terminal_input_module
from PhyAgentOS.cli.interaction.interactive_session import InteractiveAgent
from PhyAgentOS.cli.interaction.terminal_display import TerminalDisplay, safe_detail
from PhyAgentOS.cli.interaction.terminal_input import TerminalInput

_TURN_SCOPE = {
    EventType.TURN_STARTED,
    EventType.TURN_COMPLETED,
    EventType.TURN_FAILED,
    EventType.TURN_INTERRUPTED,
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _FakeApp:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1


class _FakePrompt:
    def __init__(self) -> None:
        self.app = _FakeApp()


def _display(*, live: bool, clock: _Clock | None = None, size=None,
             session="session s · openai/gpt-5"):
    buffer = StringIO()
    console = Console(file=buffer, width=120, color_system=None)
    display = TerminalDisplay(
        console,
        session_label=session,
        clock=clock or _Clock(),
        size_provider=size,
    )
    if live:
        display.attach_prompt(_FakePrompt())
    return display, buffer


def _prompt_text(display) -> str:
    return "".join(text for _, text in display.prompt_message())


def _event(kind, turn="turn", step="step", **payload):
    return RuntimeEvent(
        type=kind,
        session_id="session",
        turn_id=turn,
        step_id=None if kind in _TURN_SCOPE else step,
        payload=payload,
    )


def _tool(display, turn, call_id, name, status=None, error=None):
    display.note_event(
        _event(EventType.TOOL_INTENT, turn=turn, tool_call_id=call_id, tool_name=name)
    )
    display.note_event(
        _event(EventType.TOOL_DISPATCHED, turn=turn, tool_call_id=call_id, tool_name=name)
    )
    if status is not None:
        payload = {"tool_call_id": call_id, "tool_name": name, "status": status}
        if error is not None:
            payload["error"] = error
        display.note_event(_event(EventType.TOOL_SETTLED, turn=turn, **payload))


def test_spinner_appears_switches_and_clears():
    clock = _Clock()
    display, _ = _display(live=True, clock=clock)
    assert display.phase == "idle"
    assert display.render_lines(width=80, rows=24) == []
    assert "session s · openai/gpt-5" in _prompt_text(display)
    assert "You:" in _prompt_text(display)

    display.begin_turn()
    first = display.render_lines(width=80, rows=24)[0]
    assert first.startswith(".  ") and "Preparing 0.0s" in first

    clock.now += 0.4
    second = display.render_lines(width=80, rows=24)[0]
    assert second.startswith("..") and "Preparing 0.4s" in second
    assert second != first

    display.note_event(_event(EventType.TURN_STARTED))
    assert "Thinking" in display.render_lines(width=80, rows=24)[0]

    display.note_event(
        _event(EventType.TOOL_INTENT, tool_call_id="call", tool_name="fake_tool")
    )
    assert "Calling fake_tool" in display.render_lines(width=80, rows=24)[0]
    display.note_event(
        _event(
            EventType.TOOL_SETTLED, tool_call_id="call", tool_name="fake_tool",
            status="succeeded",
        )
    )
    assert "Thinking" in display.render_lines(width=80, rows=24)[0]

    display.end_turn()
    assert display.phase == "idle"
    assert display.render_lines(width=80, rows=24) == []
    assert "session s · openai/gpt-5" in _prompt_text(display)
    assert "You:" in _prompt_text(display)
    display.end_turn()  # idempotent cleanup must not print another summary


def test_tool_area_shows_last_four_updates_one_call_and_counts_hidden():
    display, buffer = _display(live=True)
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    for index in range(6):
        _tool(display, "turn", f"c{index}", f"tool{index}", "succeeded")
    lines = display.render_lines(width=80, rows=24)
    header = lines[1]
    assert "6 calls" in header and "2 hidden" in header
    rows = [line for line in lines if line.startswith("| ")]
    assert len(rows) == 1 + 4  # header plus the four most recent rows
    assert "tool5" in rows[-1] and "succeeded" in rows[-1]

    # The same call reported again updates its row instead of adding one.
    _tool(display, "turn", "c5", "tool5", "succeeded")
    display.note_event(
        _event(
            EventType.TOOL_SETTLED, tool_call_id="c5", tool_name="tool5", status="failed"
        )
    )
    assert len(display.records) == 6
    header = display.render_lines(width=80, rows=24)[1]
    assert "6 calls" in header and "1 failed/unknown" in header
    latest = [line for line in display.render_lines(width=80, rows=24)
              if line.startswith("| ")][-1]
    assert "tool5 failed" in latest and "tool5 succeeded" not in latest

    # A hidden failure is never silently dropped.
    _tool(display, "turn", "c0", "tool0", "unknown")
    header = display.render_lines(width=80, rows=24)[1]
    assert "hidden include failures" in header

    display.end_turn()
    summary = buffer.getvalue().splitlines()[-1]
    assert "failed/unknown" in summary and "\n" not in summary


def test_expanded_record_shows_redacted_error_without_arguments_or_outcomes():
    display, buffer = _display(live=True)
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    long_message = "boom https://secret.example/path?key=abc sk-abcdef123456 " + "x" * 900
    display.note_event(
        _event(
            EventType.TOOL_INTENT,
            tool_call_id="call",
            tool_name="fake_tool",
            arguments={"TOP_SECRET_ARG": "TOP_SECRET_VALUE"},
        )
    )
    display.note_event(
        _event(
            EventType.TOOL_SETTLED,
            tool_call_id="call",
            tool_name="fake_tool",
            status="failed",
            error={"code": "bad_request", "message": long_message},
            outcome={"TOP_SECRET_OUTCOME": "TOP_SECRET_RESULT"},
        )
    )
    message = display.tool_view("on")
    text = buffer.getvalue()
    assert "Tool record" in text and "bad_request" in text
    assert "TOP_SECRET_ARG" not in text and "TOP_SECRET_OUTCOME" not in text
    assert "TOP_SECRET_VALUE" not in text and "TOP_SECRET_RESULT" not in text
    assert "secret.example" not in text and "sk-abcdef123456" not in text
    assert "<url>" in text and "<redacted>" in text
    assert "x" * 500 not in text
    assert len(safe_detail(long_message)) <= 400
    assert "expanded" in message.lower()

    display.note_event(
        _event(
            EventType.TOOL_SETTLED,
            tool_call_id="call",
            tool_name="fake_tool",
            status="unknown",
        )
    )
    # Later settlements while expanded print one redacted line each.
    assert buffer.getvalue().count("1. fake_tool") == 2


def test_new_turn_does_not_inherit_old_tool_state_or_duplicate_events():
    display, _ = _display(live=True)
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED, turn="old"))
    _tool(display, "old", "c1", "old_tool", "failed", {"code": "e", "message": "old"})
    assert len(display.records) == 1
    display.note_event(_event(EventType.TURN_COMPLETED, turn="old"))

    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED, turn="new"))
    assert display.records == ()
    # A late event from the completed turn cannot repopulate the new one.
    display.note_event(
        _event(
            EventType.TOOL_SETTLED, turn="old", tool_call_id="c1",
            tool_name="old_tool", status="failed",
        )
    )
    assert display.records == ()
    _tool(display, "new", "c2", "new_tool", "succeeded")
    assert len(display.records) == 1
    # Replaying the whole event set does not double the total.
    _tool(display, "new", "c2", "new_tool", "succeeded")
    assert len(display.records) == 1


@pytest.mark.parametrize(("columns", "rows"), [(28, 9), (40, 24), (12, 6)])
def test_toolbar_fits_narrow_and_short_terminals(columns, rows):
    display, _ = _display(live=True, size=lambda: (columns, rows))
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    for index in range(5):
        _tool(display, "turn", f"c{index}", f"very_long_tool_name_{index}", "succeeded")
    lines = display.render_lines(width=columns, rows=rows)
    assert lines
    assert max(len(line) for line in lines) <= columns
    limit = 1 if rows <= 8 else 2 if rows <= 12 else 4
    hidden = max(0, 5 - limit)
    assert "5 calls" in lines[1]
    if columns >= 24:
        assert f"{hidden} hidden" in lines[1]
    tool_rows = [line for line in lines if line.startswith("| ")]
    assert len(tool_rows) <= limit + 1
    fragments = display.toolbar()
    assert max(len(text.rstrip("\n")) for _, text in fragments) <= columns


def test_plain_fallback_uses_lines_without_cursor_or_animation_codes():
    buffer = StringIO()
    display = TerminalDisplay(Console(file=buffer, width=120, color_system=None))
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    _tool(display, "turn", "c1", "demo", "failed", {"code": "e", "message": "boom"})
    display.end_turn()
    text = buffer.getvalue()
    assert "\x1b" not in text and "\r" not in text
    lines = text.splitlines()
    assert lines[0] == "Preparing"
    assert lines[1] == "Thinking"
    assert "Tool demo: requested" in lines
    assert "! Tool demo: failed" in lines
    # No spinner frames and no repeated Thinking after each tool call.
    assert sum(1 for line in lines if line == "Thinking") == 1


class _TerminalOutput(Vt100_Output):
    """Vt100 output that claims a known height, like a real terminal CPR."""

    def get_rows_below_cursor_position(self) -> int:
        return self.get_size().rows


def _prompt_session(pipe, output, display):
    return PromptSession(
        input=pipe,
        output=output,
        bottom_toolbar=display.toolbar,
        refresh_interval=0.1,
    )


async def _wait(seconds: float = 0.25) -> None:
    await asyncio.sleep(seconds)


async def test_real_pipe_input_and_vt100_render_spinner_toolarea_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    size = [Size(rows=24, columns=80)]
    output = _TerminalOutput(stream, lambda: size[0])
    display, _ = _display(live=True)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=output):
            terminal = TerminalInput(display=display, input=pipe, output=output)
            terminal.initialize()
            reading = asyncio.create_task(terminal.read())
            await _wait(0.2)

            display.begin_turn()
            display.note_event(_event(EventType.TURN_STARTED))
            display.note_event(
                _event(EventType.TOOL_INTENT, tool_call_id="c1", tool_name="fake_tool")
            )
            await _wait(0.45)

            rendered = _ANSI.sub("", stream.getvalue())
            assert re.search(r"\.\.? +Calling fake_tool 0\.\ds", rendered)
            assert "| 1 calls" in rendered
            assert "| . fake_tool requested" in rendered

            # A resize must not raise or corrupt the prompt.
            size[0] = Size(rows=10, columns=40)
            output._min_available_height = 0
            await _wait(0.3)
            # Busy input is blocked, so end the turn first; the cleared live
            # region is then redrawn while the prompt is still reading.
            boundary = len(stream.getvalue())
            display.end_turn()
            await _wait(0.3)
            pipe.send_text("hello\r")
            assert await asyncio.wait_for(reading, 2) == "hello"
            terminal.restore()

    after_cleanup = _ANSI.sub("", stream.getvalue()[boundary:])
    assert "Calling fake_tool" not in after_cleanup
    assert "session s · openai/gpt-5" in after_cleanup


async def test_pause_ends_prompt_menu_cancel_is_contained_and_resume_reads_again(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    output = _TerminalOutput(stream, lambda: Size(rows=24, columns=80))
    display, _ = _display(live=True)
    displayed: list[str] = []

    class Session:
        def __init__(self) -> None:
            self.requests = []
            self.release = asyncio.Event()

        async def submit(self, request):
            self.requests.append(request)
            await self.release.wait()
            return TurnResult(
                request.session_id, f"turn-{len(self.requests)}", TurnStatus.COMPLETED
            )

        async def cancel(self, session_id):
            self.release.set()

    class Output:
        def info(self, text):
            displayed.append(text)

        def error(self, text):
            displayed.append(text)

        def result(self, result):
            displayed.append(result.turn_id)

        def turn_started(self):
            displayed.append("started")

        def turn_finished(self):
            displayed.append("finished")

        def cancel_requested(self):
            displayed.append("cancel")

        def tool_view(self, argument):
            return "tool view"

    session = Session()
    menu_started = asyncio.Event()
    menu_done = asyncio.Event()
    paused_prompt_active: list[bool] = []

    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=output):
            terminal = TerminalInput(display=display, input=pipe, output=output)
            terminal.initialize()
            stop_calls: list[int] = []
            terminal.bind_stop(lambda: stop_calls.append(1))

            async def menu_command(text):
                await terminal.pause()
                paused_prompt_active.append(terminal.prompt_active)
                menu_started.set()
                try:
                    bindings = KeyBindings()

                    @bindings.add("escape")
                    def escape(event):
                        event.app.exit(exception=KeyboardInterrupt())

                    app = Application(
                        layout=Layout(Window(FormattedTextControl(text="menu"))),
                        key_bindings=bindings,
                        full_screen=False,
                        input=pipe,
                        output=output,
                    )
                    pipe.send_text("\x1b")
                    try:
                        await app.run_async()
                    except KeyboardInterrupt:
                        return "Model selection cancelled; current model unchanged."
                finally:
                    terminal.resume()
                    menu_done.set()
                return None

            agent = InteractiveAgent(
                session,
                session_id="cli:test",
                output=Output(),
                read_input=terminal.read,
                model_command=menu_command,
            )
            run = asyncio.create_task(agent.run())
            try:
                pipe.send_text("/model\r")
                await asyncio.wait_for(menu_started.wait(), 3)
                assert paused_prompt_active == [False]
                await asyncio.wait_for(menu_done.wait(), 3)
                pipe.send_text("hello\r")
                await asyncio.wait_for(_until(lambda: session.requests), 3)
                session.release.set()
                await asyncio.wait_for(
                    _until(lambda: displayed.count("finished") >= 1), 3
                )
                pipe.send_text("exit\r")
                await asyncio.wait_for(run, 3)
            finally:
                session.release.set()
                if not run.done():
                    run.cancel()
                await asyncio.gather(run, return_exceptions=True)

    assert len(session.requests) == 1
    assert any("Model selection cancelled" in item for item in displayed)
    assert session.requests[0].text == "hello"
    assert stop_calls == []


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0)


def test_non_tty_input_reads_plain_lines_without_escape_codes(monkeypatch):
    stdin = StringIO("hello\r\n")
    stdout = StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    terminal = TerminalInput()
    assert terminal.interactive is False
    assert terminal.menu_capable is False

    async def read_once():
        return await terminal.read()

    assert asyncio.run(read_once()) == "hello\r\n"
    assert "You: " in stdout.getvalue()
    assert "\x1b" not in stdout.getvalue()
    assert "\r" not in stdout.getvalue().replace("You: ", "")


def test_markup_like_tool_text_is_data_not_rich_markup():
    display, buffer = _display(live=True)
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    display.note_event(
        _event(
            EventType.TOOL_INTENT,
            tool_call_id="call",
            tool_name="evil[red]tool",
            arguments={"hidden": "[bold]TOP_SECRET_ARG[/bold]"},
        )
    )
    display.note_event(
        _event(
            EventType.TOOL_SETTLED,
            tool_call_id="call",
            tool_name="evil[red]tool",
            status="failed",
            error={"code": "bad", "message": "[bold]boom[/] [not-a-tag]"},
        )
    )
    # None of these may raise a Rich markup error or apply formatting.
    lines = display.render_lines(width=100, rows=24)
    display.tool_view("on")
    text = buffer.getvalue()
    assert "evil[red]tool" in text
    assert "[bold]boom[/]" in text
    assert "TOP_SECRET_ARG" not in text
    assert any("evil[red]tool failed" in line for line in lines)


def test_stop_without_a_running_turn_leaves_no_status_behind():
    display, buffer = _display(live=True)
    display.request_stop()
    assert display.phase == "idle"
    assert display.render_lines(width=80, rows=24) == []
    assert "session s · openai/gpt-5" in _prompt_text(display)
    assert buffer.getvalue() == ""

    plain, plain_buffer = _display(live=False)
    plain.request_stop()
    assert plain.phase == "idle"
    assert plain_buffer.getvalue() == ""


def test_stop_while_running_says_local_and_clears_after_the_turn():
    display, _ = _display(live=True)
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    display.request_stop()
    line = display.render_lines(width=80, rows=24)[0]
    assert "Stopping locally (remote unconfirmed)" in line
    assert display.phase == "stopping"
    display.end_turn()
    assert display.phase == "idle"
    assert "Stopping" not in "".join(display.render_lines(width=80, rows=24))


def test_stopping_status_is_not_overwritten_by_later_events():
    display, _ = _display(live=True)
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    display.request_stop()
    assert "Stopping locally (remote unconfirmed)" in display.render_lines(width=90, rows=24)[0]

    for kind in (
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
    ):
        payload = {}
        if kind in (EventType.REQUEST_PREPARED, EventType.MODEL_ATTEMPT_STARTED):
            payload = {"model_call_id": "model-call"}
        else:
            payload = {"tool_call_id": "call", "tool_name": "late_tool"}
        display.note_event(_event(kind, **payload))
        assert display.phase == "stopping"
        assert "Stopping locally (remote unconfirmed)" in display.render_lines(
            width=90, rows=24
        )[0]

    display.note_event(
        _event(
            EventType.TOOL_SETTLED,
            tool_call_id="call",
            tool_name="late_tool",
            status="unknown",
        )
    )
    # The settlement still updates the tool row...
    lines = display.render_lines(width=90, rows=24)
    assert display.phase == "stopping"
    assert "Stopping locally (remote unconfirmed)" in lines[0]
    assert any("late_tool unknown" in line for line in lines)
    # ...but only a terminal turn event clears the stop state.
    display.note_event(_event(EventType.TURN_INTERRUPTED))
    assert display.phase == "idle"
    assert "Stopping" not in "".join(display.render_lines(width=90, rows=24))


@pytest.mark.parametrize("columns", [1, 2, 12, 30])
@pytest.mark.parametrize("name", ["中文工具", "工具🤖", "🙂emoji工具"])
def test_toolbar_width_counts_cjk_and_emoji_as_display_columns(columns, name):
    from prompt_toolkit.utils import get_cwidth

    display, _ = _display(live=True, size=lambda: (columns, 24))
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED))
    _tool(display, "turn", "c1", name, "succeeded")
    lines = display.render_lines(width=columns, rows=24)
    assert lines
    for line in lines:
        assert get_cwidth(line) <= columns
    assert max(get_cwidth(line) for line in lines) <= columns
    if columns >= 30:
        assert any(name in line for line in lines)
    fragments = display.toolbar()
    assert max(get_cwidth(text.rstrip("\n")) for _, text in fragments) <= columns


@pytest.mark.parametrize(
    ("raw", "secrets"),
    [
        ('{"api_key": "short-secret"}', ["short-secret"]),
        ('{"Authorization": "Bearer short"}', ["Bearer short", "short"]),
        (
            "Authorization: Basic dXNlcjpwYXNz; Authorization: Bearer abc.def",
            ["dXNlcjpwYXNz", "abc.def", "Basic dXNlcjpwYXNz"],
        ),
        (
            '{"access_token":"tok-123","client_secret":"cs-9"}',
            ["tok-123", "cs-9"],
        ),
        (
            "failed at https://api.example.test/v1?key=abc while reading",
            ["api.example.test", "key=abc"],
        ),
    ],
)
def test_safe_detail_redacts_json_auth_schemes_and_urls(raw, secrets):
    text = safe_detail(raw)
    for secret in secrets:
        assert secret not in text
    assert len(text) <= 400
    if "://" in raw:
        assert "<url>" in text
    if "Bearer" in raw or "Basic" in raw or "api_key" in raw or "access_token" in raw:
        assert "<redacted>" in text


async def test_pause_preserves_a_partially_typed_line(tmp_path, monkeypatch):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    output = _TerminalOutput(stream, lambda: Size(rows=24, columns=80))
    display, _ = _display(live=True)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=output):
            terminal = TerminalInput(display=display, input=pipe, output=output)
            terminal.initialize()
            first = asyncio.create_task(terminal.read())
            await _wait(0.2)
            pipe.send_text("half a message")
            await _wait(0.3)
            await terminal.pause()
            assert await asyncio.wait_for(first, 2) == ""
            assert "half a message" in _ANSI.sub("", stream.getvalue())
            terminal.resume()
            second = asyncio.create_task(terminal.read())
            await _wait(0.2)
            pipe.send_text("\r")
            assert await asyncio.wait_for(second, 2) == "half a message"
            terminal.restore()


async def test_bottom_toolbar_uses_default_background_and_keeps_tool_colours(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    output = _TerminalOutput(stream, lambda: Size(rows=24, columns=80))
    display, _ = _display(live=True)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=output):
            terminal = TerminalInput(display=display, input=pipe, output=output)
            terminal.initialize()
            style = terminal.prompt.app.style
            base = style.get_attrs_for_style_str("class:bottom-toolbar")
            text = style.get_attrs_for_style_str("class:bottom-toolbar.text")
            green = style.get_attrs_for_style_str(
                "class:bottom-toolbar class:bottom-toolbar.text fg:ansigreen"
            )
            for attrs in (base, text, green):
                assert attrs.reverse is False
                assert attrs.bgcolor == "default"
            assert green.color == "ansigreen"
            terminal.restore()


async def test_busy_hides_marker_and_cursor_blocks_keys_and_restores_draft(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    output = _TerminalOutput(stream, lambda: Size(rows=24, columns=80))
    display, _ = _display(live=True)
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=output):
            terminal = TerminalInput(display=display, input=pipe, output=output)
            terminal.initialize()
            terminal.prompt.app.timeoutlen = 0.05
            reading = asyncio.create_task(terminal.read())
            await _wait(0.3)

            idle = _ANSI.sub("", stream.getvalue())
            # The session line is above the You marker, not in the toolbar.
            assert idle.index("session s · openai/gpt-5") < idle.index("You:")
            assert display.render_lines(width=80, rows=24) == []
            assert "\x1b[?25h" in stream.getvalue()

            # A draft typed while idle must survive the busy phase.
            pipe.send_text("kept text")
            await _wait(0.2)
            assert terminal.prompt.default_buffer.text == "kept text"

            marker = len(stream.getvalue())
            display.begin_turn()
            await _wait(0.3)
            busy = stream.getvalue()[marker:]
            assert "You:" not in _ANSI.sub("", busy)
            assert "session s · openai/gpt-5" in _prompt_text(display)
            assert "\x1b[?25l" in busy and "\x1b[?25h" not in busy
            # Ordinary keys do not edit the buffer, echo text, or ring a bell.
            pipe.send_text("must not stick")
            await _wait(0.2)
            assert terminal.prompt.default_buffer.text == "kept text"
            assert "must not stick" not in _ANSI.sub("", stream.getvalue()[marker:])
            assert "\a" not in stream.getvalue()[marker:]
            # Enter while busy is not submitted either.
            pipe.send_text("\r")
            await _wait(0.2)
            assert not reading.done()

            restored = len(stream.getvalue())
            display.end_turn()
            await _wait(0.3)
            assert "You:" in _ANSI.sub("", stream.getvalue()[restored:])
            assert "\x1b[?25h" in stream.getvalue()[restored:]
            pipe.send_text("\r")
            assert await asyncio.wait_for(reading, 2) == "kept text"
            terminal.restore()


async def test_escape_hint_then_second_escape_stops_once_and_queues(
    tmp_path, monkeypatch
):
    from PhyAgentOS.cli.interaction.agent_output import AgentOutput

    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    output = _TerminalOutput(stream, lambda: Size(rows=24, columns=80))
    log = StringIO()
    agent_output = AgentOutput(
        Console(file=log, width=120, color_system=None), markdown=False
    )

    class Session:
        def __init__(self):
            self.requests = []
            self.gates = []
            self.interrupted = set()
            self.active = None
            self.cancels = 0

        async def submit(self, request):
            index = len(self.requests)
            self.requests.append(request)
            gate = asyncio.Event()
            self.gates.append(gate)
            self.active = index
            await gate.wait()
            status = (
                TurnStatus.INTERRUPTED
                if index in self.interrupted
                else TurnStatus.COMPLETED
            )
            self.active = None
            return TurnResult(request.session_id, f"turn-{index}", status)

        async def cancel(self, session_id):
            self.cancels += 1
            if self.active is not None:
                self.interrupted.add(self.active)
                self.gates[self.active].set()

    session = Session()
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=output):
            terminal = TerminalInput(
                display=agent_output.display, input=pipe, output=output
            )
            terminal.initialize()
            terminal.prompt.app.timeoutlen = 0.05
            agent = InteractiveAgent(
                session,
                session_id="cli:test",
                output=agent_output,
                read_input=terminal.read,
            )
            terminal.bind_stop(agent.request_stop)
            run = asyncio.create_task(agent.run())
            try:
                await _until(lambda: "You:" in _ANSI.sub("", stream.getvalue()))
                pipe.send_text("first\r")
                await asyncio.wait_for(_until(lambda: session.active == 0), 2)
                await _until(lambda: agent_output.display.busy)
                busy_marker = len(stream.getvalue())
                await _wait(0.2)
                assert "You:" not in _ANSI.sub("", stream.getvalue()[busy_marker:])

                # First Esc: yellow hint, no cancellation.
                pipe.send_text("\x1b")
                await asyncio.wait_for(
                    _until(lambda: agent_output.display.stop_hint), 2
                )
                assert session.cancels == 0 and session.active == 0
                await _wait(0.25)
                hint_lines = [
                    line for line in agent_output.display.render_lines(
                        width=120, rows=24
                    )
                    if line.startswith("Press Esc again")
                ]
                assert hint_lines
                assert "Press Esc again" in _ANSI.sub("", stream.getvalue())
                hint_style = [
                    style for style, text in agent_output.display.toolbar()
                    if text.startswith("Press Esc again")
                ]
                assert hint_style and "ansiyellow" in hint_style[0]

                # Second Esc within the window: exactly one cancel.
                pipe.send_text("\x1b")
                await asyncio.wait_for(
                    _until(lambda: not agent_output.display.busy), 3
                )
                assert session.cancels == 1
                assert session.interrupted == {0}
                await _wait(0.3)
                assert "You:" in _ANSI.sub("", stream.getvalue()[busy_marker:])
                assert not agent_output.display.stop_hint

                # The next turn starts with a clean, unarmed hint.
                pipe.send_text("second\r")
                await asyncio.wait_for(_until(lambda: session.active == 1), 2)
                await _until(lambda: agent_output.display.busy)
                assert not agent_output.display.stop_hint
                # While busy, ordinary text (including Enter) is blocked at
                # the terminal layer; it is not queued behind the turn.
                before_third = log.getvalue()
                pipe.send_text("third\r")
                await _wait(0.3)
                assert log.getvalue() == before_third
                assert terminal.prompt.default_buffer.text == ""
                session.gates[1].set()
                await asyncio.wait_for(
                    _until(lambda: not agent_output.display.busy), 3
                )
                pipe.send_text("exit\r")
                await asyncio.wait_for(run, 3)
            finally:
                for gate in session.gates:
                    gate.set()
                if not run.done():
                    run.cancel()
                await asyncio.gather(run, return_exceptions=True)
            terminal.restore()

    assert [request.text for request in session.requests] == ["first", "second"]
    assert session.interrupted == {0}
    assert session.cancels == 1


async def test_idle_escape_does_not_arm_or_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    output = _TerminalOutput(stream, lambda: Size(rows=24, columns=80))
    display, _ = _display(live=True)
    stops: list[int] = []
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=output):
            terminal = TerminalInput(display=display, input=pipe, output=output)
            terminal.initialize()
            terminal.prompt.app.timeoutlen = 0.05
            terminal.bind_stop(lambda: stops.append(1))
            reading = asyncio.create_task(terminal.read())
            await _wait(0.3)
            pipe.send_text("\x1b")
            await _wait(0.5)
            assert not display.stop_hint and stops == [] and not reading.done()
            pipe.send_text("idle ok\r")
            assert await asyncio.wait_for(reading, 2) == "idle ok"
            terminal.restore()


async def test_escape_window_expiry_other_keys_and_arrows_do_not_stop(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    output = _TerminalOutput(stream, lambda: Size(rows=24, columns=80))
    clock = _Clock()
    display, _ = _display(live=True, clock=clock)
    stops: list[int] = []
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=output):
            terminal = TerminalInput(display=display, input=pipe, output=output)
            terminal.initialize()
            terminal.prompt.app.timeoutlen = 0.05
            terminal.bind_stop(lambda: stops.append(1))
            reading = asyncio.create_task(terminal.read())
            await _wait(0.3)
            display.begin_turn()
            pipe.send_text("\x1b")
            await asyncio.wait_for(_until(lambda: display.stop_hint), 2)
            clock.now += 2.5
            await _wait(0.3)
            assert not display.stop_hint

            # A second Esc after the window is a new first press.
            pipe.send_text("\x1b")
            await asyncio.wait_for(_until(lambda: display.stop_hint), 2)
            await _wait(0.15)
            pipe.send_text("a")
            await _wait(0.4)
            assert not display.stop_hint and stops == []

            pipe.send_text("\x1b")
            await asyncio.wait_for(_until(lambda: display.stop_hint), 2)
            await _wait(0.15)
            pipe.send_text("\x1b[B")  # down: a real key sequence, not Escape
            await _wait(0.4)
            assert not display.stop_hint and stops == []

            # Two Alt-like escape+letter sequences are not two lone Esc keys.
            pipe.send_text("\x1ba\x1ba")
            await _wait(0.4)
            assert not display.stop_hint and stops == []

            # Still can stop with two Esc presses after the disarms.
            pipe.send_text("\x1b")
            await asyncio.wait_for(_until(lambda: display.stop_hint), 2)
            pipe.send_text("\x1b")
            await asyncio.wait_for(_until(lambda: len(stops) == 1), 2)
            assert stops == [1]
            assert terminal.prompt.default_buffer.text == ""
            display.end_turn()
            pipe.send_text("exit\r")
            assert await asyncio.wait_for(reading, 2) == "exit"
            terminal.restore()

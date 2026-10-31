"""Independent acceptance of the confirmed CLI display through InteractiveAgent."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from rich.console import Console
from test_core_cli_interactive import FakeInput, FakeSession, until
from test_core_cli_runtime import _config
from test_core_cli_terminal_display import _ANSI, _display, _event, _TerminalOutput, _tool

from PhyAgentOS.agent.core.events import EventType
from PhyAgentOS.cli.command_groups import agent_commands
from PhyAgentOS.cli.command_groups.agent_commands import run_core_agent
from PhyAgentOS.cli.interaction import agent_output as agent_output_module
from PhyAgentOS.cli.interaction import interactive_session as interactive_session_module
from PhyAgentOS.cli.interaction import terminal_input as terminal_input_module
from PhyAgentOS.cli.interaction.agent_output import AgentOutput
from PhyAgentOS.cli.interaction.interactive_session import InteractiveAgent
from PhyAgentOS.cli.interaction.terminal_input import TerminalInput


async def test_two_real_turns_keep_requests_in_order_without_queue_notices():
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=120, color_system=None), markdown=False)
    session = FakeSession()
    reader = FakeInput()
    agent = InteractiveAgent(session, session_id="cli:test", output=output, read_input=reader)
    task = asyncio.create_task(agent.run())
    try:
        reader.send("first request")
        await until(lambda: session.active is not None)
        reader.send("second request")
        session.releases[0].set()
        await until(lambda: len(session.requests) == 2)
        session.releases[1].set()
        await until(lambda: "End Turn 2" in buffer.getvalue())
        reader.send("exit")
        await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    text = buffer.getvalue()
    assert [request.text for request in session.requests] == ["first request", "second request"]
    assert text.count("│ first request") == 1
    assert text.count("│ second request") == 1
    assert "You:" not in text
    assert text.count("PhyAgentOS · Turn 1") == 1
    assert text.count("PhyAgentOS · Turn 2") == 1
    assert text.count("End Turn 1") == 1
    assert text.count("End Turn 2") == 1
    assert "Queued message" not in text
    assert "Processing message" not in text
    assert text.index("│ first request") < text.index("PhyAgentOS · Turn 1")
    assert text.index("reply:first request") < text.index("End Turn 1")
    assert text.index("End Turn 1") < text.index("│ second request")
    assert text.index("│ second request") < text.index("PhyAgentOS · Turn 2")
    assert text.index("reply:second request") < text.index("End Turn 2")


async def test_display_all_step_is_the_only_local_step_display_command():
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=120, color_system=None))
    session = FakeSession()
    reader = FakeInput()
    calls: list[str] = []
    original = output.tool_view

    def record(argument: str = "") -> str:
        calls.append(argument)
        return original(argument)

    output.tool_view = record
    agent = InteractiveAgent(session, session_id="cli:test", output=output, read_input=reader)
    reader.send("/display_all_step on", "/display_all_step off", "/display_all_step", "/tools on", "exit")
    await asyncio.wait_for(agent.run(), 2)

    assert calls == ["on", "off", ""]
    assert session.requests == []
    text = buffer.getvalue()
    assert "Unknown command /tools" in text
    assert "not sent to the model" in text


async def test_real_tty_keeps_two_turn_transcripts_above_the_live_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    stream = StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    terminal_output = _TerminalOutput(stream, lambda: Size(rows=30, columns=100))
    output = AgentOutput(
        Console(width=100, force_terminal=True, color_system=None), markdown=False
    )
    output.set_session_label("session cli:test · fake/fake-model")
    session = FakeSession()

    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=terminal_output):
            terminal = TerminalInput(display=output.display, input=pipe, output=terminal_output)
            terminal.initialize()
            agent = InteractiveAgent(
                session, session_id="cli:test", output=output, read_input=terminal.read
            )
            terminal.bind_stop(agent.request_stop)
            task = asyncio.create_task(agent.run())

            def plain() -> str:
                return _ANSI.sub("", stream.getvalue()).replace("\r\n", "\n").replace("\r", "\n")

            try:
                await until(lambda: "│" in plain())
                pipe.send_text("first tty request\r")
                await until(lambda: session.active is not None)
                session.releases[0].set()
                await until(lambda: "End Turn 1" in plain())
                pipe.send_text("second tty request\r")
                await until(lambda: len(session.requests) == 2)
                session.releases[1].set()
                await until(lambda: "End Turn 2" in plain())
                pipe.send_text("exit\r")
                await asyncio.wait_for(task, 3)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                terminal.restore()

    text = plain()
    assert text.count("│ first tty request") == 1
    assert text.count("│ second tty request") == 1
    assert "You:" not in text
    assert text.count("PhyAgentOS · Turn 1") == 1
    assert text.count("PhyAgentOS · Turn 2") == 1
    assert text.count("End Turn 1") == 1
    assert text.count("End Turn 2") == 1
    assert "Queued message" not in text and "Processing message" not in text
    assert text.index("│ first tty request") < text.index("PhyAgentOS · Turn 1")
    assert text.index("End Turn 1") < text.index("│ second tty request")
    assert text.index("│ second tty request") < text.index("PhyAgentOS · Turn 2")


def test_display_all_step_preference_survives_turn_boundaries_until_disabled():
    display, buffer = _display(live=True)
    display.tool_view("on")  # Selected while idle, before the first request.
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED, turn="turn-one"))
    for index in range(6):
        _tool(display, "turn-one", f"one-{index}", f"first_{index}", "succeeded")

    text = buffer.getvalue()
    for index in range(6):
        assert f"{index + 1}. first_{index}" in text

    display.end_turn()
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED, turn="turn-two"))
    _tool(display, "turn-two", "two-0", "second_still_full", "succeeded")
    assert "1. second_still_full" in buffer.getvalue()

    display.tool_view("off")
    display.end_turn()
    display.begin_turn()
    display.note_event(_event(EventType.TURN_STARTED, turn="turn-three"))
    marker = len(buffer.getvalue())
    _tool(display, "turn-three", "three-0", "third_limited", "succeeded")
    assert "1. third_limited" not in buffer.getvalue()[marker:]
    assert len([line for line in display.render_lines(width=100, rows=24) if "third_limited" in line]) == 1


@pytest.mark.parametrize("fail", [False, True])
async def test_real_compact_command_always_releases_the_input_lock(tmp_path, monkeypatch, fail):
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=120, color_system=None))
    monkeypatch.setattr(agent_output_module, "AgentOutput", lambda *args, **kwargs: output)
    monkeypatch.setattr(agent_commands, "load_agent_config", lambda path, workspace: _config(tmp_path))
    monkeypatch.setattr(agent_commands, "get_cron_dir", lambda: tmp_path / "cron")
    busy_during_compact = []

    class Sessions:
        def get(self, session_id):
            return SimpleNamespace(name="fake", model="fake-model", effort=None)

    class Session:
        sessions = Sessions()
        catalog = None
        raw_store = None
        instructions = ""

        async def compact(self, session_id):
            busy_during_compact.append(output.display.busy)
            if fail:
                raise RuntimeError("controlled compaction failure")
            return SimpleNamespace(
                before_tokens=100,
                after_tokens=40,
                warnings=(),
                checkpoint=SimpleNamespace(
                    summary="summary", retained_messages=(), covers_sequence=0
                ),
            )

    @asynccontextmanager
    async def fake_open(config, **kwargs):
        yield Session()

    class Terminal:
        def __init__(self, display):
            self.display = display

        def initialize(self):
            pass

        def bind_stop(self, handler):
            pass

        async def read(self):
            return "exit"

        def restore(self):
            pass

    class Interactive:
        def __init__(self, session, **kwargs):
            self.kwargs = kwargs

        def request_stop(self):
            pass

        async def run(self):
            binding = SimpleNamespace(session_id="cli:test", fallback="cli:test", error=False)
            message = await self.kwargs["compact_command"]("/compact", binding)
            if message:
                self.kwargs["output"].info(message)

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(terminal_input_module, "TerminalInput", Terminal)
    monkeypatch.setattr(interactive_session_module, "InteractiveAgent", Interactive)

    code = await run_core_agent(
        message=None,
        session_id="cli:test",
        workspace=None,
        config_path=None,
        markdown=False,
        logs=False,
        provider_name=None,
        model=None,
        effort=None,
        console=Console(file=buffer, width=120, color_system=None),
    )

    assert code == 0
    assert busy_during_compact == [True]
    assert output.display.busy is False
    text = buffer.getvalue()
    assert "PhyAgentOS · Turn" not in text and "End Turn" not in text
    if fail:
        assert "controlled compaction failure" in text
    else:
        assert "Context compacted: 100 -> 40" in text

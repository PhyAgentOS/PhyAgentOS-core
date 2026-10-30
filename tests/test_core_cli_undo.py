"""End-to-end wiring of the local /undo command.

A fake store implements the undo contract and a fake host records turns; the
CLI runs the real InteractiveAgent and TerminalInput, so the message, the
input refill and the guards are exercised without a provider, Gateway or
robot.
"""

from __future__ import annotations

import asyncio
import re
import sys
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.session import SQLiteSessionStore, UndoOutcome
from PhyAgentOS.agent.core.types import TurnRequest, TurnResult, TurnStatus
from PhyAgentOS.agent.services.usage import UsageLedger
from PhyAgentOS.cli.command_groups import agent_commands
from PhyAgentOS.cli.command_groups.agent_commands import CoreAgentSession, run_core_agent
from PhyAgentOS.cli.interaction import terminal_input as terminal_input_module
from PhyAgentOS.cli.interaction.terminal_input import TerminalInput
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.service import RuntimeSelection

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_SELECTION = RuntimeSelection("fake", "fake-model", None, None, object(), 128000)
_REQUEST_TEXT = "move the arm to 2"


def _outcome(**overrides) -> UndoOutcome:
    fields = {
        "session_id": "cli:test",
        "turn_id": "turn-1",
        "status": "turn.completed",
        "request_text": _REQUEST_TEXT,
        "context_reverted": True,
        "block_reason": None,
    }
    fields.update(overrides)
    return UndoOutcome(**fields)


class FakeUndoStore:
    def __init__(self, outcome: UndoOutcome | None) -> None:
        self.outcome = outcome
        self.calls: list[str] = []
        self.closed = False

    async def undo_last_turn(self, session_id: str) -> UndoOutcome | None:
        self.calls.append(session_id)
        return self.outcome

    async def aclose(self) -> None:
        self.closed = True


class _FakeRuntime:
    async def stop(self) -> None:
        pass


class _RecordingHost:
    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []
        self.cancel_calls = 0

    async def submit(self, request: TurnRequest) -> TurnResult:
        self.requests.append(request)
        return TurnResult(
            request.session_id,
            f"turn-{len(self.requests)}",
            TurnStatus.COMPLETED,
            reply="ok",
        )

    async def cancel(self, session_id: str) -> None:
        self.cancel_calls += 1


class _GateHost:
    def __init__(self) -> None:
        self.submitted = asyncio.Event()
        self.released = asyncio.Event()

    async def submit(self, request: TurnRequest) -> TurnResult:
        self.submitted.set()
        await self.released.wait()
        return TurnResult(request.session_id, "turn-gated", TurnStatus.INTERRUPTED)

    async def cancel(self, session_id: str) -> None:
        self.released.set()


class _Sessions:
    def get(self, session_id: str) -> RuntimeSelection:
        return _SELECTION


def _session(store, host=None, **kwargs) -> CoreAgentSession:
    host = host or _RecordingHost()
    assembly = SimpleNamespace(host=host, runtime=_FakeRuntime(), capabilities=())
    return CoreAgentSession(
        assembly,
        _SELECTION,
        build=lambda selection: assembly,
        store=store,
        sessions=_Sessions(),
        output=SimpleNamespace(),
        **kwargs,
    )


class _TerminalOutput(Vt100_Output):
    def get_rows_below_cursor_position(self) -> int:
        return self.get_size().rows


async def _until(predicate) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def _config(tmp_path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    return config


def _patch_cli(monkeypatch, session, tmp_path) -> None:
    @asynccontextmanager
    async def fake_open(config, *, output, **_kwargs):
        try:
            yield session
        finally:
            await session.aclose()

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )


async def _run_redirected(session, tmp_path, monkeypatch, *, lines) -> tuple[int, str]:
    _patch_cli(monkeypatch, session, tmp_path)
    monkeypatch.setattr(sys, "stdin", StringIO("".join(f"{line}\n" for line in lines)))
    monkeypatch.setattr(sys, "stdout", StringIO())
    log = StringIO()
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
        console=Console(file=log, width=160, color_system=None),
    )
    return code, log.getvalue()


async def test_undo_reports_a_removed_request_and_prints_it_without_a_prompt(
    tmp_path, monkeypatch
):
    store = FakeUndoStore(_outcome())
    host = _RecordingHost()
    code, text = await _run_redirected(
        _session(store, host), tmp_path, monkeypatch, lines=["/undo", "exit"]
    )
    assert code == 0
    assert store.calls == ["cli:test"]
    assert not host.requests
    assert "/undo" in text  # the startup banner names the command
    assert "Request turn-1 was removed from the context used by later turns." in text
    assert "No input line is available to refill" in text
    assert _REQUEST_TEXT in text
    assert "Tool side effects (robot actions, file changes) are not undone." in text


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            _outcome(status="turn.failed", context_reverted=False),
            [
                "never entered the context",
                "turn.failed",
                "only the input was refilled",
                "are not undone",
            ],
            id="failed-turn",
        ),
        pytest.param(
            _outcome(status="turn.interrupted", context_reverted=False),
            ["never entered the context", "turn.interrupted", "are not undone"],
            id="interrupted-turn",
        ),
        pytest.param(
            _outcome(block_reason="checkpoint", context_reverted=False),
            [
                "covered by a compaction summary",
                "cannot be removed from the context used by later turns",
                _REQUEST_TEXT,
                "are not undone",
            ],
            id="checkpoint-blocked",
        ),
        pytest.param(
            _outcome(request_text=None),
            [
                "was removed from the context used by later turns",
                "original request text is not available",
                "are not undone",
            ],
            id="no-request-text",
        ),
    ],
)
async def test_undo_message_variants_on_redirected_input(
    tmp_path, monkeypatch, outcome, expected
):
    store = FakeUndoStore(outcome)
    host = _RecordingHost()
    code, text = await _run_redirected(
        _session(store, host), tmp_path, monkeypatch, lines=["/undo", "exit"]
    )
    assert code == 0
    assert store.calls == ["cli:test"]
    assert not host.requests
    for fragment in expected:
        assert fragment in text


async def test_undo_with_no_earlier_turn_says_there_is_nothing_to_revert(
    tmp_path, monkeypatch
):
    store = FakeUndoStore(None)
    host = _RecordingHost()
    code, text = await _run_redirected(
        _session(store, host), tmp_path, monkeypatch, lines=["/undo", "exit"]
    )
    assert code == 0
    assert store.calls == ["cli:test"]
    assert not host.requests
    assert "No earlier request to undo in this session." in text


async def test_interactive_undo_refills_the_prompt_and_the_next_read_uses_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        terminal_input_module, "get_cli_history_path", lambda: tmp_path / "history"
    )
    store = FakeUndoStore(_outcome())
    host = _RecordingHost()
    session = _session(store, host)
    _patch_cli(monkeypatch, session, tmp_path)
    stream = StringIO()
    vt100 = _TerminalOutput(stream, lambda: Size(rows=24, columns=80))
    log = StringIO()

    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=vt100):
            created: list[TerminalInput] = []

            class RecordingTerminal(TerminalInput):
                def __init__(self, display=None):
                    super().__init__(display=display, input=pipe, output=vt100)
                    created.append(self)

            monkeypatch.setattr(terminal_input_module, "TerminalInput", RecordingTerminal)
            run = asyncio.create_task(
                run_core_agent(
                    message=None,
                    session_id="cli:test",
                    workspace=None,
                    config_path=None,
                    markdown=False,
                    logs=False,
                    provider_name=None,
                    model=None,
                    effort=None,
                    console=Console(file=log, width=120, color_system=None),
                )
            )
            try:
                await _until(lambda: "You:" in _ANSI.sub("", stream.getvalue()))
                pipe.send_text("/undo\r")
                await _until(lambda: store.calls == ["cli:test"])
                await _until(lambda: "Tool side effects" in log.getvalue())
                terminal = created[0]
                await _until(
                    lambda: terminal.prompt is not None
                    and terminal.prompt.default_buffer.text == _REQUEST_TEXT
                )
                pipe.send_text("\r")
                await _until(lambda: host.requests)
                assert host.requests[0].text == _REQUEST_TEXT
                pipe.send_text("exit\r")
                await asyncio.wait_for(run, 3)
            finally:
                if not run.done():
                    run.cancel()
                await asyncio.gather(run, return_exceptions=True)

    assert "Its text is back in the input line" in log.getvalue()
    assert "Tool side effects (robot actions, file changes) are not undone." in log.getvalue()


async def test_core_session_undo_returns_the_store_outcome():
    store = FakeUndoStore(_outcome())
    session = _session(store)
    try:
        outcome = await session.undo("cli:test")
        assert outcome is store.outcome
        assert store.calls == ["cli:test"]
    finally:
        await session.aclose()
    assert store.closed


async def test_core_session_undo_refuses_a_running_turn():
    store = FakeUndoStore(_outcome())
    host = _GateHost()
    session = _session(store, host)
    task = asyncio.create_task(session.submit(TurnRequest("cli:test", "first")))
    try:
        await asyncio.wait_for(host.submitted.wait(), 2)
        with pytest.raises(RuntimeError, match="running or queued"):
            await session.undo("cli:test")
        assert store.calls == []
    finally:
        host.released.set()
        await asyncio.gather(task, return_exceptions=True)
        await session.aclose()


async def test_core_session_undo_refuses_a_running_compaction():
    store = FakeUndoStore(_outcome())
    started, released = asyncio.Event(), asyncio.Event()

    async def compact(session_id, selection):
        started.set()
        await released.wait()
        return "compacted"

    session = _session(store, raw_store=store, compact=compact)
    task = asyncio.create_task(session.compact("cli:test"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(RuntimeError, match="compaction"):
            await session.undo("cli:test")
        assert store.calls == []
    finally:
        released.set()
        assert await asyncio.wait_for(task, 2) == "compacted"
        await session.aclose()


async def test_core_session_undo_reports_a_store_without_undo_support():
    class PlainStore:
        async def aclose(self) -> None:
            pass

    session = _session(PlainStore())
    try:
        with pytest.raises(RuntimeError, match="does not support undo"):
            await session.undo("cli:test")
    finally:
        await session.aclose()


async def _append_completed_turn(store, session_id: str, turn_id: str, text: str) -> None:
    """Write one turn the way the runtime and loop driver persist it."""
    history = list(await store.read_conversation(session_id))
    for event in (
        RuntimeEvent(
            type=EventType.INPUT_ACCEPTED,
            session_id=session_id,
            payload={"characters": len(text)},
        ),
        RuntimeEvent(type=EventType.TURN_STARTED, session_id=session_id, turn_id=turn_id),
        RuntimeEvent(
            type=EventType.REQUEST_PREPARED,
            session_id=session_id,
            turn_id=turn_id,
            step_id=f"{turn_id}-step",
            payload={
                "model_call_id": f"{turn_id}-model",
                "model": "fake-model",
                "messages": [*history, {"role": "user", "content": text}],
                "tools": [{"name": turn_id}],
            },
        ),
        RuntimeEvent(
            type=EventType.TURN_COMPLETED,
            session_id=session_id,
            turn_id=turn_id,
            payload={"reply": f"reply {turn_id}"},
        ),
    ):
        await store.append(event)


async def test_status_estimate_and_tools_drop_after_undo(tmp_path) -> None:
    """The status line must follow the reverted projection, not the raw log.

    A revert marker is written by the store itself, so the cached event list
    only sees it after a refresh. The estimate and the offered-tool snapshot
    both have to skip the reverted turn.
    """
    store = SQLiteSessionStore(tmp_path / "events.sqlite")
    session = SimpleNamespace(
        raw_store=store,
        sessions=_Sessions(),
        instructions="system",
        config=None,
    )
    context = agent_commands._ContextStatus(session, SimpleNamespace(ledger=UsageLedger()))
    try:
        await _append_completed_turn(store, "cli:test", "turn-1", "first " * 200)
        await context.load("cli:test")
        before = context.snapshot("cli:test").tokens
        assert context._tools["cli:test"] == [{"name": "turn-1"}]

        await _append_completed_turn(store, "cli:test", "turn-2", "second " * 200)
        await context.refresh("cli:test")
        grown = context.snapshot("cli:test").tokens
        assert grown is not None and before is not None and grown > before
        assert context._tools["cli:test"] == [{"name": "turn-2"}]

        await store.undo_last_turn("cli:test")
        await context.refresh("cli:test")
        assert context.snapshot("cli:test").tokens == before
        assert context._tools["cli:test"] == [{"name": "turn-1"}]
    finally:
        await store.aclose()

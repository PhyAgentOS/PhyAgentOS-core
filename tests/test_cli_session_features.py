"""CLI session lifecycle, switching, streaming and compaction wiring."""

from __future__ import annotations

import asyncio
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
from PhyAgentOS.agent.core.session import SQLiteSessionStore
from PhyAgentOS.agent.core.types import TurnResult, TurnStatus
from PhyAgentOS.agent.services.session_catalog import SessionCatalog
from PhyAgentOS.agent.services.usage import UsageLedger
from PhyAgentOS.cli.command_groups import agent_commands
from PhyAgentOS.cli.command_groups.agent_commands import (
    CoreAgentSession,
    _SelectedProvider,
    run_core_agent,
)
from PhyAgentOS.cli.interaction.agent_output import AgentOutput
from PhyAgentOS.cli.interaction.interactive_session import InteractiveAgent
from PhyAgentOS.cli.interaction.model_picker import _session_picker
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.base import StreamEvent


class _Selections:
    def __init__(self, selection) -> None:
        self.selection = selection

    def get(self, session_id: str):
        return self.selection

    def select(self, session_id, name, model, effort=None):
        self.selection = SimpleNamespace(
            name=name,
            model=model,
            effort=None if effort in (None, "none") else effort,
            provider=None,
            context_window_tokens=128000,
        )
        return "updated"


class _FakeSession:
    def __init__(self, path, config, output, *, provider="custom", model="deepseek-flash"):
        from pathlib import Path as _Path

        _Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.catalog = SessionCatalog(path)
        self.raw_store = SQLiteSessionStore(path)
        self.instructions = "system"
        self.config = config
        self.sessions = _Selections(
            SimpleNamespace(
                name=provider,
                model=model,
                effort=None,
                provider=None,
                context_window_tokens=128000,
            )
        )
        self.output = output
        self.requests: list[tuple[str, str]] = []
        self.commits: list[str] = []
        self.compact_error: Exception | None = None
        self.stream: tuple[str, str] | None = None
        self.stream_gate: asyncio.Event | None = None

    async def submit(self, request):
        self.requests.append((request.session_id, request.text))
        if self.stream is not None:
            call_id, text = self.stream
            self.output.event(
                RuntimeEvent(
                    type=EventType.MODEL_RESPONSE_DELTA,
                    session_id=request.session_id,
                    turn_id=f"turn-{len(self.requests)}",
                    step_id="step-1",
                    payload={"model_call_id": call_id, "content": text},
                )
            )
            if self.stream_gate is not None:
                await self.stream_gate.wait()
            self.output.event(
                RuntimeEvent(
                    type=EventType.MODEL_ATTEMPT_COMPLETED,
                    session_id=request.session_id,
                    turn_id=f"turn-{len(self.requests)}",
                    step_id="step-1",
                    payload={
                        "model_call_id": call_id,
                        "content": text,
                        "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                    },
                )
            )
        return TurnResult(
            request.session_id,
            f"turn-{len(self.requests)}",
            TurnStatus.COMPLETED,
            reply=request.text if self.stream is None else self.stream[1],
        )

    async def cancel(self, session_id: str) -> None:
        self.commits.append(f"cancel:{session_id}")

    async def compact(self, session_id: str):
        if self.compact_error is not None:
            raise self.compact_error
        return SimpleNamespace(
            before_tokens=1000,
            after_tokens=400,
            warnings=("summary input was shortened",),
            checkpoint=SimpleNamespace(
                summary="older history", retained_messages=(), usage={}
            ),
            usage={"prompt_tokens": 40, "completion_tokens": 8},
        )


def _config(tmp_path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    return config


def _install_fake_open(monkeypatch, tmp_path, holder):
    monkeypatch.setattr(agent_commands, "get_cron_dir", lambda: tmp_path / "cron")

    @asynccontextmanager
    async def fake_open(config, *, provider_name=None, model=None, effort=None, output, additional_capabilities=(), **_kwargs):
        monkeypatch.setattr(agent_commands, "get_cron_dir", lambda: tmp_path / "cron")
        path = config.workspace_path / "sessions" / "core.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        session = _FakeSession(
            path,
            config,
            output,
            provider=provider_name or "custom",
            model=model or "deepseek-flash",
        )
        holder.append(session)
        try:
            yield session
        finally:
            await session.raw_store.aclose()

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )


async def _run(tmp_path, *, message=None, session_id=None, model=None, console=None):
    buffer = StringIO()
    if console is None:
        console = Console(file=buffer, width=120, color_system=None)
    code = await run_core_agent(
        message=message,
        session_id=session_id,
        workspace=None,
        config_path=None,
        markdown=True,
        logs=False,
        provider_name=None,
        model=model,
        effort=None,
        console=console,
    )
    return code


async def test_default_start_creates_a_new_session_and_explicit_session_reuses_it(
    tmp_path, monkeypatch
):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)

    assert await _run(tmp_path, message="hello") == 0
    assert await _run(tmp_path, message="again") == 0
    first = holder[0].requests[0][0]
    ids = {
        info.session_id
        for session in holder
        for info in session.catalog.list_sessions()
    }
    assert len(ids) == 2
    assert all(session_id.startswith("cli:") for session_id in ids)

    assert await _run(tmp_path, message="reuse", session_id=first) == 0
    assert holder[-1].requests == [(first, "reuse")]
    assert holder[-1].catalog.get_session(first) is not None


async def test_first_input_sets_the_session_title_without_a_model_call(tmp_path, monkeypatch):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)
    assert await _run(tmp_path, message="检查机械臂状态。然后继续") == 0
    session = holder[-1]
    info = session.catalog.list_sessions()[0]
    assert info.title == "检查机械臂状态"
    assert info.session_id.startswith("cli:")
    # The title is local metadata; the submitted text is unchanged.
    assert session.requests[0][1] == "检查机械臂状态。然后继续"


async def test_explicit_session_keeps_its_id_when_metadata_cannot_be_read(
    tmp_path, monkeypatch
):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)

    def failing_ensure(self, session_id):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(SessionCatalog, "ensure_session", failing_ensure)
    assert await _run(tmp_path, message="hello", session_id="cli:explicit") == 0
    assert holder[-1].requests == [("cli:explicit", "hello")]


async def test_saved_selection_is_loaded_for_the_next_start(tmp_path, monkeypatch):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)
    assert await _run(tmp_path, message="hello", model="model-one") == 0
    sid = holder[-1].requests[0][0]
    saved = holder[-1].catalog.load_selection(sid)
    assert (saved.provider, saved.model) == ("custom", "model-one")

    assert await _run(tmp_path, message="again", session_id=sid) == 0
    assert holder[-1].sessions.get(sid).model == "model-one"


async def test_commands_do_not_reach_the_model_and_a_queued_switch_is_isolated(
    tmp_path, monkeypatch
):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)
    # Create a destination session before the interactive run.
    setup = _FakeSession(tmp_path / "workspace/sessions/core.db", _config(tmp_path), None)
    target = setup.catalog.create_session("cli").session_id
    await setup.raw_store.aclose()
    setup.catalog.close()

    lines = iter(["/new", "first", f"/session {target}", "second", "/compact", "exit"])

    class Terminal:
        menu_capable = False

        def __init__(self, display=None):
            self._stop = None

        def initialize(self):
            pass

        async def read(self):
            try:
                return next(lines)
            except StopIteration:
                await asyncio.Event().wait()

        def bind_stop(self, handler):
            self._stop = handler

        def restore(self):
            pass

        async def pause(self):
            pass

        def resume(self):
            pass

    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.terminal_input.TerminalInput", Terminal
    )
    console = Console(file=StringIO(), width=120, color_system=None)
    assert await _run(tmp_path, message=None, console=console) == 0
    session = holder[-1]
    texts = [text for _sid, text in session.requests]
    assert texts == ["first", "second"]
    assert all(text not in session.requests[0] for text in ("/new", "/session"))
    assert session.requests[0][0] != target
    assert session.requests[1][0] == target
    # /compact is queued as a command, not sent as a user message.
    assert "/compact" not in texts


async def test_compact_success_shows_before_after_and_warnings(tmp_path, monkeypatch):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)
    lines = iter(["/compact", "exit"])

    class Terminal:
        menu_capable = False

        def __init__(self, display=None):
            self._stop = None

        def initialize(self):
            pass

        async def read(self):
            try:
                return next(lines)
            except StopIteration:
                await asyncio.Event().wait()

        def bind_stop(self, handler):
            self._stop = handler

        def restore(self):
            pass

        async def pause(self):
            pass

        def resume(self):
            pass

    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.terminal_input.TerminalInput", Terminal
    )
    buffer = StringIO()
    console = Console(file=buffer, width=120, color_system=None)
    assert await _run(tmp_path, message=None, console=console) == 0
    assert holder[-1].requests == []
    assert "1000 -> 400" in buffer.getvalue()
    assert "Warnings" in buffer.getvalue()
    assert "summary input was shortened" in " ".join(buffer.getvalue().split())


async def test_failed_compact_reports_attempt_accounting_without_claiming_no_write(
    tmp_path, monkeypatch
):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)
    lines = iter(["/compact", "exit"])

    class Terminal:
        menu_capable = False

        def __init__(self, display=None):
            self._stop = None

        def initialize(self):
            pass

        async def read(self):
            try:
                return next(lines)
            except StopIteration:
                await asyncio.Event().wait()

        def bind_stop(self, handler):
            self._stop = handler

        def restore(self):
            pass

        async def pause(self):
            pass

        def resume(self):
            pass

    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.terminal_input.TerminalInput", Terminal
    )
    async def failing_compact(self, session_id):
        raise RuntimeError("summary provider failed")

    monkeypatch.setattr(_FakeSession, "compact", failing_compact)
    buffer = StringIO()
    console = Console(file=buffer, width=120, color_system=None)
    assert await _run(tmp_path, message=None, console=console) == 0
    assert holder[-1].requests == []
    assert "attempt is recorded for accounting" in " ".join(buffer.getvalue().split())
    assert "no checkpoint was written" not in buffer.getvalue()


async def test_streaming_text_is_visible_before_the_terminal_result(tmp_path, monkeypatch):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)
    buffer = StringIO()
    console = Console(file=buffer, width=120, color_system=None)

    gate = asyncio.Event()

    @asynccontextmanager
    async def fake_open(config, *, provider_name=None, model=None, effort=None, output, additional_capabilities=(), **_kwargs):
        monkeypatch.setattr(agent_commands, "get_cron_dir", lambda: tmp_path / "cron")
        path = config.workspace_path / "sessions" / "core.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        session = _FakeSession(path, config, output)
        session.stream = ("call-1", "Hello streamed")
        session.stream_gate = gate
        holder.append(session)
        try:
            yield session
        finally:
            await session.raw_store.aclose()

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )
    task = asyncio.create_task(
        run_core_agent(
            message="hello",
            session_id=None,
            workspace=None,
            config_path=None,
            markdown=True,
            logs=False,
            provider_name=None,
            model=None,
            effort=None,
            console=console,
        )
    )
    for _ in range(100):
        if "Hello streamed" in buffer.getvalue():
            break
        await asyncio.sleep(0.01)
    assert "Hello streamed" in buffer.getvalue()
    gate.set()
    assert await asyncio.wait_for(task, 2) == 0
    # The final reply is not printed a second time in raw (non-TTY) mode.
    assert buffer.getvalue().count("Hello streamed") == 1


async def test_queued_new_switch_binds_later_input_to_the_new_session(tmp_path, monkeypatch):
    holder: list[_FakeSession] = []
    _install_fake_open(monkeypatch, tmp_path, holder)
    session = _FakeSession(
        tmp_path / "workspace/sessions/core.db", _config(tmp_path), None
    )
    first_gate = asyncio.Event()
    original_submit = session.submit

    async def gated_submit(request):
        if request.text == "first":
            await first_gate.wait()
        return await original_submit(request)

    session.submit = gated_submit
    lines = iter(["first", "/new", "second", "exit"])

    class Terminal:
        menu_capable = False

        def __init__(self, display=None):
            self._stop = None

        def initialize(self):
            pass

        async def read(self):
            try:
                return next(lines)
            except StopIteration:
                await asyncio.Event().wait()

        def bind_stop(self, handler):
            self._stop = handler

        def restore(self):
            pass

        async def pause(self):
            pass

        def resume(self):
            pass

    # This test uses InteractiveAgent directly to control the first-turn gate.
    async def reader():
        try:
            return next(lines)
        except StopIteration:
            await asyncio.Event().wait()

    output = AgentOutput(Console(file=StringIO(), width=120, color_system=None))
    async def session_command(text, binding):
        if text == "/new":
            info = session.catalog.create_session("cli")
            binding.session_id = info.session_id
            return f"new {info.session_id}"
        return None

    agent = InteractiveAgent(
        session,
        session_id="cli:old",
        output=output,
        read_input=reader,
        session_command=session_command,
    )
    task = asyncio.create_task(agent.run())
    for _ in range(100):
        if session.requests:
            break
        await asyncio.sleep(0.01)
    first_gate.set()
    await asyncio.wait_for(task, 2)
    assert [text for _sid, text in session.requests] == ["first", "second"]
    assert session.requests[0][0] == "cli:old"
    assert session.requests[1][0].startswith("cli:")
    assert session.requests[1][0] != "cli:old"


async def test_session_picker_enter_selects_and_escape_cancels():
    sessions = [
        SimpleNamespace(session_id="cli:a", label="A - cli:a"),
        SimpleNamespace(session_id="cli:b", label="B - cli:b"),
    ]
    with create_pipe_input() as pipe:
        output = Vt100_Output(StringIO(), lambda: Size(columns=80, rows=24))
        with create_app_session(input=pipe, output=output):
            task = asyncio.create_task(_session_picker(sessions, current="cli:a"))
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b[B\r")
            assert await asyncio.wait_for(task, 2) == "cli:b"

    with create_pipe_input() as pipe:
        output = Vt100_Output(StringIO(), lambda: Size(columns=80, rows=24))
        with create_app_session(input=pipe, output=output):
            task = asyncio.create_task(_session_picker(sessions, current="cli:a"))
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b")
            assert await asyncio.wait_for(task, 2) is None


async def test_selected_provider_exposes_native_stream_and_closes_it():
    closed = asyncio.Event()

    class Native:
        supports_native_stream = True

        async def chat_stream(self, *, messages, tools=None, **kwargs):
            try:
                yield StreamEvent(type="delta", content="one")
                yield StreamEvent(type="done", finish_reason="stop")
            finally:
                closed.set()

    selection = SimpleNamespace(
        name="custom",
        model="m",
        effort=None,
        provider=Native(),
        context_window_tokens=128000,
    )
    wrapper = _SelectedProvider(selection, 128000, 4096)
    assert wrapper.supports_native_stream is True
    stream = wrapper.chat_stream(messages=[{"role": "user", "content": "hi"}])
    assert (await stream.__anext__()).content == "one"
    await stream.aclose()
    assert closed.is_set()


async def test_selected_provider_does_not_relabel_a_non_native_stream():
    class Wrapped:
        supports_native_stream = False

        async def chat_stream(self, **kwargs):
            yield StreamEvent(type="done", finish_reason="stop")

        async def chat_with_retry(self, **kwargs):
            raise AssertionError("fallback path must be chosen by the adapter")

    selection = SimpleNamespace(
        name="custom",
        model="m",
        effort=None,
        provider=Wrapped(),
        context_window_tokens=128000,
    )
    wrapper = _SelectedProvider(selection, 128000, 4096)
    assert wrapper.supports_native_stream is False


class _BlockingSummaryProvider:
    def __init__(self):
        self.started = asyncio.Event()

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


class _FakeRuntime:
    async def stop(self):
        return None


class _FakeHost:
    async def cancel(self, session_id):
        return None

    async def aclose(self):
        return None


def _seed_session(store):
    async def append(event_type, turn, step, payload):
        await store.append(
            RuntimeEvent(
                type=event_type,
                session_id="s",
                turn_id=turn,
                step_id=step,
                payload=payload,
            )
        )

    return append


async def _conversation_events(store):
    append = _seed_session(store)
    messages_1 = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first task"},
    ]
    await append(EventType.TURN_STARTED, "t1", None, {})
    await append(
        EventType.REQUEST_PREPARED,
        "t1",
        "s1",
        {"model_call_id": "m1", "messages": messages_1, "tools": []},
    )
    await append(EventType.MODEL_ATTEMPT_STARTED, "t1", "s1", {"model_call_id": "m1"})
    await append(
        EventType.MODEL_ATTEMPT_COMPLETED,
        "t1",
        "s1",
        {"model_call_id": "m1", "content": "ok1", "usage": {}},
    )
    await append(EventType.TURN_COMPLETED, "t1", None, {"reply": "ok1"})
    messages_2 = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first task"},
        {"role": "assistant", "content": "ok1"},
        {"role": "user", "content": "second task"},
    ]
    await append(EventType.TURN_STARTED, "t2", None, {})
    await append(
        EventType.REQUEST_PREPARED,
        "t2",
        "s2",
        {"model_call_id": "m2", "messages": messages_2, "tools": []},
    )
    await append(EventType.MODEL_ATTEMPT_STARTED, "t2", "s2", {"model_call_id": "m2"})
    await append(
        EventType.MODEL_ATTEMPT_COMPLETED,
        "t2",
        "s2",
        {"model_call_id": "m2", "content": "ok2", "usage": {}},
    )
    await append(EventType.TURN_COMPLETED, "t2", None, {"reply": "ok2"})


class _FakePromptCounter:
    """Deterministic provider counter: content length, no tokenizer needed."""

    def estimate_prompt_tokens(self, messages, tools, model):
        total = sum(len(str(message.get("content") or "")) for message in messages)
        total += sum(len(str(tool)) for tool in tools or ())
        return total, "fake"


async def test_status_keeps_compacted_projection_until_a_request_is_prepared(tmp_path):
    store = SQLiteSessionStore(tmp_path / "status-core.db")
    try:
        await _conversation_events(store)
        append = _seed_session(store)
        await append(EventType.TURN_STARTED, "t3", None, {})
        await append(
            EventType.REQUEST_PREPARED,
            "t3",
            "s3",
            {
                "model_call_id": "m3",
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "z" * 400},
                ],
                "tools": [],
            },
        )
        await append(EventType.MODEL_ATTEMPT_STARTED, "t3", "s3", {"model_call_id": "m3"})
        await append(
            EventType.MODEL_ATTEMPT_COMPLETED,
            "t3",
            "s3",
            {"model_call_id": "m3", "content": "done", "usage": {}},
        )
        await append(EventType.TURN_COMPLETED, "t3", None, {"reply": "done"})

        selection = SimpleNamespace(
            name="custom",
            model="m",
            effort=None,
            provider=_FakePromptCounter(),
            context_window_tokens=100000,
        )
        session = SimpleNamespace(
            sessions=_Selections(selection),
            config=_config(tmp_path),
            instructions="system",
            raw_store=store,
        )
        context = agent_commands._ContextStatus(
            session, SimpleNamespace(ledger=UsageLedger())
        )
        await context.load("s")
        original = context.snapshot("s")

        context.apply_compaction(
            "s",
            SimpleNamespace(
                checkpoint=SimpleNamespace(summary="short", retained_messages=())
            ),
        )
        compacted = context.snapshot("s")
        assert compacted.tokens < original.tokens

        # turn.started must not fall back to the old pre-checkpoint events.
        context.ingest(
            RuntimeEvent(
                type=EventType.TURN_STARTED,
                session_id="s",
                turn_id="t3",
                payload={},
            )
        )
        assert context.snapshot("s").tokens == compacted.tokens

        # The new request snapshot replaces the checkpoint projection.
        context.ingest(
            RuntimeEvent(
                type=EventType.REQUEST_PREPARED,
                session_id="s",
                turn_id="t3",
                step_id="s3",
                payload={
                    "model_call_id": "m3",
                    "messages": [
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "x" * 50},
                    ],
                    "tools": [],
                },
            )
        )
        assert context.snapshot("s").tokens != compacted.tokens
    finally:
        await store.aclose()


def _compact_session(tmp_path, provider):
    config = _config(tmp_path)
    store = SQLiteSessionStore(tmp_path / "compact-core.db")
    selection = SimpleNamespace(
        name="custom",
        model="m",
        effort=None,
        provider=provider,
        context_window_tokens=100000,
    )
    session = CoreAgentSession(
        SimpleNamespace(runtime=_FakeRuntime(), host=_FakeHost(), capabilities=()),
        selection,
        build=lambda current: None,
        store=store,
        sessions=_Selections(selection),
        output=SimpleNamespace(),
        raw_store=store,
    )
    session.config = config
    return session, store


async def test_cancel_stops_a_blocked_compaction_and_records_an_interrupted_attempt(
    tmp_path,
):
    provider = _BlockingSummaryProvider()
    session, store = _compact_session(tmp_path, provider)
    await _conversation_events(store)
    before = await store.read_conversation("s")

    task = asyncio.create_task(session.compact("s"))
    await asyncio.wait_for(provider.started.wait(), 2)
    await asyncio.wait_for(session.cancel("s"), 2)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)

    attempts = await store.list_compaction_attempts("s")
    assert [attempt.status for attempt in attempts] == ["interrupted"]
    assert await store.read_conversation("s") == before
    assert session._pending == 0
    assert session._slots["s"].compact is None
    await store.aclose()


async def test_aclose_stops_a_blocked_compaction_without_waiting_for_the_provider(
    tmp_path,
):
    provider = _BlockingSummaryProvider()
    session, store = _compact_session(tmp_path, provider)
    await _conversation_events(store)
    before = await store.read_conversation("s")

    task = asyncio.create_task(session.compact("s"))
    await asyncio.wait_for(provider.started.wait(), 2)
    await asyncio.wait_for(session.aclose(), 2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session._pending == 0

    replay = SQLiteSessionStore(tmp_path / "compact-core.db")
    try:
        attempts = await replay.list_compaction_attempts("s")
        assert [attempt.status for attempt in attempts] == ["interrupted"]
        assert await replay.read_conversation("s") == before
    finally:
        await replay.aclose()


async def test_request_stop_cancels_a_compacting_command():
    class Session:
        def __init__(self):
            self.cancelled = []

        async def submit(self, request):
            raise AssertionError("compact must not submit a turn")

        async def cancel(self, session_id):
            self.cancelled.append(session_id)

    class Output:
        def __init__(self):
            self.infos = []
            self.errors = []
            self.cancels = 0

        def info(self, text):
            self.infos.append(text)

        def error(self, text):
            self.errors.append(text)

        def result(self, result):
            pass

        def turn_started(self, session_id=None):
            pass

        def turn_finished(self):
            pass

        def cancel_requested(self):
            self.cancels += 1

        def tool_view(self, argument):
            return ""

    session, output = Session(), Output()
    started = asyncio.Event()

    async def compact_command(text, binding):
        started.set()
        await asyncio.Event().wait()

    lines = iter(["/compact", "exit"])

    async def reader():
        try:
            return next(lines)
        except StopIteration:
            await asyncio.Event().wait()

    agent = InteractiveAgent(
        session,
        session_id="cli:test",
        output=output,
        read_input=reader,
        compact_command=compact_command,
    )
    task = asyncio.create_task(agent.run())
    await asyncio.wait_for(started.wait(), 2)
    agent.request_stop()
    await asyncio.wait_for(task, 2)
    assert output.cancels >= 1
    assert session.cancelled == ["cli:test"]
    assert any("cancelled locally" in text for text in output.infos)


async def test_prompt_preview_shows_deltas_and_final_markdown_once():
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=80, color_system=None))

    class Prompt:
        bottom_toolbar = None
        app = SimpleNamespace(invalidate=lambda: None)

    output.attach_prompt(Prompt())
    output.turn_started("s")
    output.event(
        RuntimeEvent(
            type=EventType.MODEL_RESPONSE_DELTA,
            session_id="s",
            turn_id="t",
            step_id="step",
            payload={"model_call_id": "m", "content": "Hello preview"},
        )
    )
    assert "Hello preview" in "\n".join(output.display.render_lines(width=80, rows=24))
    output.event(
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_COMPLETED,
            session_id="s",
            turn_id="t",
            step_id="step",
            payload={"model_call_id": "m", "content": "Hello preview", "usage": {}},
        )
    )
    output.event(
        RuntimeEvent(
            type=EventType.TURN_COMPLETED,
            session_id="s",
            turn_id="t",
            payload={"reply": "Hello preview"},
        )
    )
    output.result(
        TurnResult("s", "t", TurnStatus.COMPLETED, reply="Hello preview")
    )
    # The preview lived in the toolbar; the transcript has the reply once.
    assert buffer.getvalue().count("Hello preview") == 1


async def test_long_unwrapped_preview_is_not_duplicated_in_the_final_transcript():
    buffer = StringIO()
    output = AgentOutput(Console(file=buffer, width=40, color_system=None))

    class Prompt:
        bottom_toolbar = None
        app = SimpleNamespace(invalidate=lambda: None)

    output.attach_prompt(Prompt())
    output.turn_started("s")
    text = "机械臂状态检查" * 20
    output.event(
        RuntimeEvent(
            type=EventType.MODEL_RESPONSE_DELTA,
            session_id="s",
            turn_id="t",
            step_id="step",
            payload={"model_call_id": "m", "content": text},
        )
    )
    output.event(
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_COMPLETED,
            session_id="s",
            turn_id="t",
            step_id="step",
            payload={"model_call_id": "m", "content": text, "usage": {}},
        )
    )
    output.event(
        RuntimeEvent(
            type=EventType.TURN_COMPLETED,
            session_id="s",
            turn_id="t",
            payload={"reply": text},
        )
    )
    output.result(TurnResult("s", "t", TurnStatus.COMPLETED, reply=text))
    rendered = "".join(buffer.getvalue().split())
    assert rendered.count(text) == 1

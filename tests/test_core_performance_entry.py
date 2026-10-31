"""Independent checks of performance controls through the real Core entry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core_background_support import (
    FakeCoordinator,
    ScriptedProvider,
    fake_forge_builder,
    make_config,
    make_selection,
    response,
    tool_call,
)

from PhyAgentOS.agent.capabilities.spawn_capability import SpawnCapability
from PhyAgentOS.agent.core import TurnRequest, TurnStatus
from PhyAgentOS.cli.command_groups import agent_commands


class Output:
    def __init__(self):
        self.events = []
        self.infos = []
        self.errors = []

    def event(self, event):
        self.events.append(event)

    def info(self, text):
        self.infos.append(text)

    def error(self, text):
        self.errors.append(text)


def opened(monkeypatch, tmp_path, responder, *, additional_capabilities=()):
    monkeypatch.setattr(
        agent_commands, "build_forge_components", fake_forge_builder(FakeCoordinator())
    )
    config = make_config(tmp_path)
    config.tools.restrict_to_workspace = True
    provider = ScriptedProvider(responder)
    return config, provider, agent_commands.open_core_session(
        config, service=SimpleNamespace(), selected=make_selection(provider), output=Output(),
        additional_capabilities=additional_capabilities,
    )


async def test_entry_exposes_new_capabilities_and_stable_static_system(monkeypatch, tmp_path):
    async def answer(index, messages, tools):
        return response("done")

    config, provider, session_cm = opened(monkeypatch, tmp_path, answer)
    configured_model = config.agents.defaults.model
    async with session_cm as session:
        first = await session.submit(TurnRequest("performance:one", "first"))
        second = await session.submit(TurnRequest("performance:one", "second"))
        assert first.status is second.status is TurnStatus.COMPLETED
        names = {item["function"]["name"] for item in provider.calls[0]["tools"]}
        assert {"execute_program", "job_status", "job_wait", "job_cancel"} <= names
        assert "tool_search" not in names  # No catalogue overhead without configured MCP tools.
        assert provider.calls[0]["messages"][0] == provider.calls[1]["messages"][0]
        assert config.agents.defaults.model == configured_model
        assert all(call["kwargs"]["model"] == make_selection(provider).model for call in provider.calls)


async def test_program_executes_real_read_and_records_each_internal_call(monkeypatch, tmp_path):
    async def answer(index, messages, tools):
        if index == 1:
            return tool_call("program-call", "execute_program", {
                "code": 'result = await tools.call("read_file", {"path": "fact.txt"})\nreturn result',
            })
        assert any("verified-fact" in str(message.get("content")) for message in messages)
        return response("checked")

    config, provider, session_cm = opened(monkeypatch, tmp_path, answer)
    config.workspace_path.mkdir(parents=True, exist_ok=True)
    (config.workspace_path / "fact.txt").write_text("verified-fact")
    async with session_cm as session:
        result = await session.submit(TurnRequest("performance:program", "read the fact"))
        assert result.status is TurnStatus.COMPLETED, result.error
        events = await session.raw_store.read_session("performance:program")
        settled = [event for event in events if event.type.value == "tool.settled"]
        assert any(event.payload.get("tool_name") == "read_file" for event in settled)
        assert len(provider.calls) == 2


async def test_real_entry_correct_is_consumed_in_same_turn(monkeypatch, tmp_path):
    tool_started = asyncio.Event()
    allow_reply = asyncio.Event()

    async def answer(index, messages, tools):
        if index == 1:
            tool_started.set()
            await allow_reply.wait()
            return tool_call("read-before-correction", "list_dir", {"path": "."})
        assert any(message.get("content") == "Use only confirmed facts." for message in messages)
        return response("corrected")

    _config, _provider, session_cm = opened(monkeypatch, tmp_path, answer)
    async with session_cm as session:
        task = asyncio.create_task(session.submit(TurnRequest("performance:correct", "inspect")))
        try:
            await asyncio.wait_for(tool_started.wait(), 3)
            await session.correct("performance:correct", "Use only confirmed facts.")
            allow_reply.set()
            result = await asyncio.wait_for(task, 3)
            assert result.status is TurnStatus.COMPLETED, result.error
            events = await session.raw_store.read_session("performance:correct")
            assert sum(event.type.value == "turn.started" for event in events) == 1
            assert sum(event.type.value == "input.steered" for event in events) == 1
            undone = await session.undo("performance:correct")
            assert undone.request_text == "inspect"
        finally:
            allow_reply.set()
            if not task.done():
                await session.cancel("performance:correct")
            await task


async def test_entry_resume_retains_successful_tool_progress(monkeypatch, tmp_path):
    async def answer(index, messages, tools):
        if index == 1:
            return tool_call("confirmed-read", "read_file", {"path": "fact.txt"})
        if index == 2:
            raise RuntimeError("provider unavailable after read")
        assert any("confirmed-progress" in str(message.get("content")) for message in messages)
        return response("continued without rereading")

    config, provider, session_cm = opened(monkeypatch, tmp_path, answer)
    config.workspace_path.mkdir(parents=True, exist_ok=True)
    (config.workspace_path / "fact.txt").write_text("confirmed-progress")
    async with session_cm as session:
        failed = await session.submit(TurnRequest("performance:resume", "read and conclude"))
        assert failed.status is TurnStatus.FAILED
        continued = await session.resume(TurnRequest("performance:resume", "continue"))
        assert continued.status is TurnStatus.COMPLETED, continued.error
        assert len(provider.calls) == 3
        events = await session.raw_store.read_session("performance:resume")
        starts = [event for event in events if event.type.value == "turn.started"]
        assert starts[-1].payload["resumed_from_turn_id"] == failed.turn_id
        reads = [
            event for event in events
            if event.type.value == "tool.dispatched" and event.payload.get("tool_name") == "read_file"
        ]
        assert len(reads) == 1


async def test_final_reply_reports_unapplied_correction(monkeypatch, tmp_path):
    request_started, finish = asyncio.Event(), asyncio.Event()

    async def answer(index, messages, tools):
        request_started.set()
        await finish.wait()
        return response("already final")

    _config, _provider, session_cm = opened(monkeypatch, tmp_path, answer)
    async with session_cm as session:
        task = asyncio.create_task(session.submit(TurnRequest("late:correction", "answer")))
        await asyncio.wait_for(request_started.wait(), 3)
        await session.correct("late:correction", "A late correction")
        finish.set()
        result = await asyncio.wait_for(task, 3)
        assert result.status is TurnStatus.COMPLETED
        assert any("Correction was not applied" in text for text in session.output.errors)
        events = await session.raw_store.read_session("late:correction")
        terminal = [event for event in events if event.type.value == "turn.completed"][-1]
        assert terminal.payload["unapplied_corrections"] == ["A late correction"]


async def test_child_requests_share_the_originating_parent_budget(monkeypatch, tmp_path):
    child_called = asyncio.Event()
    child_calls = []

    async def answer(index, messages, tools):
        user_texts = [message.get("content") for message in messages if message.get("role") == "user"]
        if "bounded-child" in user_texts:
            child_calls.append(index)
            result = tool_call("child-list", "list_dir", {"path": "."})
            result.usage = {"total_tokens": 20}
            child_called.set()
            return result
        if index == 1:
            result = tool_call("delegate-bounded", "spawn", {"task": "bounded-child"})
            result.usage = {"total_tokens": 1}
            return result
        await asyncio.wait_for(child_called.wait(), 3)
        return response("parent done")

    config, _provider, session_cm = opened(monkeypatch, tmp_path, answer)
    config.agents.defaults.performance.max_turn_tokens = 20
    async with session_cm as session:
        await session.submit(TurnRequest("budget:parent", "delegate with a cap"))
        spawn = next(item for item in session._canonical_capabilities if isinstance(item, SpawnCapability))
        async with asyncio.timeout(3):
            while not spawn.results("budget:parent"):
                await asyncio.sleep(0.01)
        result = spawn.results("budget:parent")[-1]
        assert result.status == "failed"
        assert len(child_calls) == 1
        assert "budget" in (result.error or "").lower()


async def test_real_entry_overflow_trims_once_without_replaying_tools(monkeypatch, tmp_path):
    from PhyAgentOS.providers.base import LLMResponse

    async def answer(index, messages, tools):
        if index == 1:
            return tool_call("large-read", "read_file", {"path": "large.txt"})
        if index == 2:
            return LLMResponse(
                content="structured overflow", finish_reason="error",
                error_code="context_overflow", usage={"prompt_tokens": 7},
            )
        assert index == 3
        assert any("truncated_tool_text" in str(message.get("content")) for message in messages)
        return response("recovered")

    config, provider, session_cm = opened(monkeypatch, tmp_path, answer)
    config.workspace_path.mkdir(parents=True, exist_ok=True)
    (config.workspace_path / "large.txt").write_text("abcdef0123456789" * 500)
    async with session_cm as session:
        result = await session.submit(TurnRequest("overflow:entry", "read"))
        assert result.status is TurnStatus.COMPLETED, result.error
        assert len(provider.calls) == 3
        events = await session.raw_store.read_session("overflow:entry")
        assert sum(event.type.value == "context.compacted" for event in events) == 1
        assert sum(event.type.value == "tool.dispatched" for event in events) == 1


async def test_failed_before_first_model_snapshot_still_refills_original_input(monkeypatch, tmp_path):
    async def answer(index, messages, tools):
        raise AssertionError("The impossible local budget must prevent a provider call")

    config, provider, session_cm = opened(monkeypatch, tmp_path, answer)
    config.agents.defaults.context_window_tokens = 1
    async with session_cm as session:
        failed = await session.submit(TurnRequest("before:snapshot", "original request"))
        assert failed.status is TurnStatus.FAILED
        outcome = await session.undo("before:snapshot")
        assert outcome.request_text == "original request"
        assert provider.calls == []


async def test_parallel_tools_overlap_with_real_sqlite_and_respect_config(monkeypatch, tmp_path):
    from PhyAgentOS.agent.core import ToolDefinition, ToolOutcome, ToolRegistration, ToolStatus
    from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest

    active = 0
    peak = 0
    first_pair = asyncio.Event()

    class ParallelReads:
        def tool_registrations(self):
            return (ToolRegistration(
                definition=ToolDefinition(
                    name="controlled_read", description="Controlled independent read",
                    parameters={"type": "object", "properties": {}},
                ), handler=self.handle, parallel_safe=True,
            ),)

        def prompt_context(self, context):
            return None

        async def handle(self, call, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                first_pair.set()
            try:
                await asyncio.wait_for(first_pair.wait(), 2)
                await asyncio.sleep(0)
                return ToolOutcome(
                    tool_call_id=call.tool_call_id, tool_name=call.name,
                    status=ToolStatus.SUCCEEDED, text=call.tool_call_id,
                )
            finally:
                active -= 1

        async def cancel(self, session_id, context):
            pass

        async def aclose(self):
            pass

    async def answer(index, messages, tools):
        if index == 1:
            return LLMResponse(
                content=None, finish_reason="tool_calls", tool_calls=[
                    ToolCallRequest(id=f"parallel-{i}", name="controlled_read", arguments={})
                    for i in range(4)
                ],
            )
        results = [message["content"] for message in messages if message.get("role") == "tool"]
        assert results == [f"parallel-{i}" for i in range(4)]
        return response("done")

    config, _provider, session_cm = opened(
        monkeypatch, tmp_path, answer, additional_capabilities=(ParallelReads(),),
    )
    config.agents.defaults.performance.max_parallel_tools = 2
    async with session_cm as session:
        result = await session.submit(TurnRequest("parallel:sqlite", "read independently"))
        assert result.status is TurnStatus.COMPLETED, result.error
        assert peak == 2
        events = await session.raw_store.read_session("parallel:sqlite")
        for i in range(4):
            trace = [event.type.value for event in events if event.payload.get("tool_call_id") == f"parallel-{i}"]
            assert trace == ["tool.intent", "tool.dispatched", "tool.settled"]


async def test_partial_native_stream_context_error_is_not_replayed(monkeypatch, tmp_path):
    from PhyAgentOS.providers.base import StreamEvent

    async def answer(index, messages, tools):
        raise AssertionError("Native stream must not fall back to a second completion")

    _config, provider, session_cm = opened(monkeypatch, tmp_path, answer)
    stream_calls = []

    async def stream(**kwargs):
        stream_calls.append(kwargs)
        yield StreamEvent(type="delta", content="partial body")
        yield StreamEvent(type="error", content="overflow after body", error_code="context_overflow")

    provider.chat_stream = stream
    provider.supports_native_stream = True
    async with session_cm as session:
        result = await session.submit(TurnRequest("stream:partial", "answer"))
        assert result.status is TurnStatus.FAILED
        assert len(stream_calls) == 1
        assert provider.calls == []
        events = await session.raw_store.read_session("stream:partial")
        assert sum(event.type.value == "model.attempt.started" for event in events) == 1
        assert not any(event.type.value == "context.compacted" for event in events)

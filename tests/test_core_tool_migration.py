"""Exercise migrated tools through the actual Core entry assembly."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from core_background_support import (
    FakeCoordinator,
    ScriptedProvider,
    fake_forge_builder,
    make_config,
    make_selection,
    response,
    tool_call,
)

from PhyAgentOS.agent.core import TurnRequest, TurnStatus
from PhyAgentOS.agent.core.events import EventType
from PhyAgentOS.agent.services.background_runtime import TurnBinding, TurnRoutes
from PhyAgentOS.cli.command_groups import agent_commands
from PhyAgentOS.config.schema import MCPServerConfig
from PhyAgentOS.tooling import mcp as mcp_module


class Output:
    def __init__(self):
        self.events = []
        self.infos = []
        self.errors = []
        self.notified = asyncio.Event()

    def event(self, event):
        self.events.append(event)

    def info(self, text):
        self.infos.append(text)
        self.notified.set()

    def error(self, text):
        self.errors.append(text)


def entry(monkeypatch, tmp_path, provider, *, output=None, **kwargs):
    monkeypatch.setattr(
        agent_commands, "build_forge_components", fake_forge_builder(FakeCoordinator())
    )
    config = make_config(tmp_path)
    config.tools.restrict_to_workspace = True
    selected = make_selection(provider)
    session = agent_commands.open_core_session(
        config, service=SimpleNamespace(), selected=selected,
        output=output or Output(), **kwargs,
    )
    return config, session


async def test_real_core_entry_offers_all_general_tools_without_skill_activation(
    tmp_path, monkeypatch,
):
    async def answer(count, messages, tools):
        return response("done")

    provider = ScriptedProvider(answer)
    _, opened = entry(monkeypatch, tmp_path, provider)
    async with opened as session:
        result = await session.submit(TurnRequest("cli:tools", "hello"))
        assert result.status is TurnStatus.COMPLETED
        names = {item["function"]["name"] for item in provider.calls[0]["tools"]}
        assert {
            "read_file", "write_file", "edit_file", "list_dir", "exec",
            "web_search", "web_fetch", "query_scene_graph", "image", "spawn",
            "activate_skill",
        } <= names
        assert "agent_mode" not in names  # A normal leaf has no legacy mode manager.
        assert "forge_task_create" not in names  # Forge's activation guard stays intact.


@pytest.mark.parametrize(
    ("name", "arguments", "prepare", "expected"),
    [
        ("read_file", {"path": "source.txt"}, "source.txt", "migration-content"),
        ("write_file", {"path": "written.txt", "content": "saved"}, None, "saved"),
        ("list_dir", {"path": "."}, "source.txt", "source.txt"),
        ("exec", {"command": "printf migration-output"}, None, "migration-output"),
        ("query_scene_graph", {"query_type": "list_zones"}, None, "zones"),
    ],
)
async def test_tool_executes_and_persists_via_real_core_entry(
    tmp_path, monkeypatch, name, arguments, prepare, expected,
):
    async def answer(count, messages, tools):
        if count == 1:
            return tool_call("migration-call", name, arguments)
        return response("done")

    provider = ScriptedProvider(answer)
    config, opened = entry(monkeypatch, tmp_path, provider)
    config.workspace_path.mkdir(parents=True)
    if prepare:
        (config.workspace_path / prepare).write_text("migration-content")
    async with opened as session:
        result = await session.submit(TurnRequest("cli:execute", "use tool"))
        assert result.status is TurnStatus.COMPLETED
        events = await session.raw_store.read_session("cli:execute")
        tool_events = [item for item in events if item.type.value.startswith("tool.")]
        assert [item.type for item in tool_events] == [
            EventType.TOOL_INTENT, EventType.TOOL_DISPATCHED, EventType.TOOL_SETTLED,
        ]
        assert tool_events[-1].payload["status"] == "succeeded"
        if name == "write_file":
            assert (config.workspace_path / "written.txt").read_text() == expected
        else:
            tool_messages = [
                item for item in provider.calls[-1]["messages"] if item["role"] == "tool"
            ]
            assert expected in tool_messages[-1]["content"]


async def test_core_entry_records_missing_file_as_failed_tool(tmp_path, monkeypatch):
    async def answer(count, messages, tools):
        if count == 1:
            return tool_call("missing", "read_file", {"path": "absent.txt"})
        return response("file missing")

    _, opened = entry(monkeypatch, tmp_path, ScriptedProvider(answer))
    async with opened as session:
        await session.submit(TurnRequest("cli:missing", "read absent"))
        events = await session.raw_store.read_session("cli:missing")
        outcome = next(item for item in events if item.type is EventType.TOOL_SETTLED)
        assert outcome.payload["status"] == "failed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group cancellation")
async def test_core_entry_cancel_reaps_running_shell_and_keeps_other_sessions_usable(
    tmp_path, monkeypatch,
):
    async def answer(count, messages, tools):
        if messages[-1]["role"] == "user" and messages[-1]["content"] == "long tool":
            return tool_call(
                "long-shell", "exec", {"command": "echo $$ > running.pid; exec sleep 60"},
            )
        return response("done")

    config, opened = entry(monkeypatch, tmp_path, ScriptedProvider(answer))
    async with opened as session:
        running = asyncio.create_task(session.submit(TurnRequest("cli:long", "long tool")))
        pid_file = config.workspace_path / "running.pid"
        try:
            async with asyncio.timeout(5):
                while not pid_file.exists() or not pid_file.read_text().strip():
                    await asyncio.sleep(0.01)
            pid = int(pid_file.read_text())
            os.kill(pid, 0)
            other = await session.submit(TurnRequest("cli:other", "hello"))
            assert other.status is TurnStatus.COMPLETED
            await asyncio.wait_for(session.cancel("cli:long"), timeout=15)
            interrupted = await asyncio.wait_for(running, timeout=5)
            assert interrupted.status is TurnStatus.INTERRUPTED
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            events = await session.raw_store.read_session("cli:long")
            settled = [event for event in events if event.type is EventType.TOOL_SETTLED]
            assert len(settled) == 1
            assert settled[0].payload["status"] == "unknown"
        finally:
            if not running.done():
                await session.cancel("cli:long")
            await asyncio.gather(running, return_exceptions=True)


async def test_mcp_discovery_precedes_real_entry_composition(tmp_path, monkeypatch):
    from test_core_mcp_capability import FakeSession, FakeTransport

    transport = FakeTransport()
    remote = FakeSession()
    monkeypatch.setattr(mcp_module, "stdio_client", transport.run)
    monkeypatch.setattr(mcp_module, "ClientSession", lambda *args: remote)

    async def answer(count, messages, tools):
        names = {item["function"]["name"] for item in tools}
        assert "mcp_local_echo" in names
        if count == 1:
            return tool_call("mcp-call", "mcp_local_echo", {})
        assert "echo:{}" in messages[-1]["content"]
        return response("done")

    config, opened = entry(monkeypatch, tmp_path, ScriptedProvider(answer))
    config.tools.mcp_servers["local"] = MCPServerConfig(command="configured-fake")
    async with opened as session:
        result = await session.submit(TurnRequest("cli:mcp", "echo"))
        assert result.status is TurnStatus.COMPLETED
        assert remote.call_records == [("echo", {})]
    assert remote.closed == 1
    assert transport.exited == 1


async def test_mcp_initialization_failure_is_visible_without_secrets(tmp_path, monkeypatch):
    from test_core_mcp_capability import FakeSession, FakeTransport

    remote = FakeSession(init_error=RuntimeError("private-token-do-not-print"))
    monkeypatch.setattr(mcp_module, "stdio_client", FakeTransport().run)
    monkeypatch.setattr(mcp_module, "ClientSession", lambda *args: remote)

    async def answer(count, messages, tools):
        assert not any(item["function"]["name"].startswith("mcp_") for item in tools)
        return response("done")

    output = Output()
    config, opened = entry(monkeypatch, tmp_path, ScriptedProvider(answer), output=output)
    config.tools.mcp_servers["unavailable"] = MCPServerConfig(command="configured-fake")
    async with opened as session:
        await session.submit(TurnRequest("cli:mcp-failed", "hello"))
    assert any("MCP server 'unavailable'" in item for item in output.errors)
    assert all("private-token" not in item for item in output.errors)


async def test_mode_tool_is_registered_for_compatible_entry_provider(tmp_path, monkeypatch):
    async def answer(count, messages, tools):
        assert "agent_mode" in {item["function"]["name"] for item in tools}
        if count == 1:
            return tool_call("mode-call", "agent_mode", {"method": "list"})
        assert "Available Models" in messages[-1]["content"]
        return response("done")

    provider = ScriptedProvider(answer)
    provider.list_models = lambda: [{"name": "main", "model": "fake-model", "describe": "test"}]
    provider.get_default_mode = lambda: "main"
    provider.set_default_mode = lambda *args: None
    provider.add_mode = lambda *args: None
    provider.remove_mode = lambda *args: None
    provider.update_mode = lambda *args: None
    _, opened = entry(monkeypatch, tmp_path, provider)
    async with opened as session:
        result = await session.submit(TurnRequest("cli:mode", "list modes"))
        assert result.status is TurnStatus.COMPLETED


async def test_tool_provider_resolver_tracks_each_running_turn_selection(tmp_path, monkeypatch):
    async def answer(count, messages, tools):
        return response("done")

    first, second = ScriptedProvider(answer), ScriptedProvider(answer)
    _, opened = entry(monkeypatch, tmp_path, first)
    async with opened as session:
        first_selection = session.tool_selection("cli:selection")
        await session.submit(TurnRequest("cli:selection", "first"))
        assert session.tool_selection("cli:selection").provider is first
        # Set the next turn's selection; the previous turn's auxiliary work
        # still resolves its own frozen provider until the next turn starts.
        replacement = make_selection(second)
        session.sessions.service.resolve = lambda *args: replacement
        session.sessions.select("cli:selection", "fake", "fake-model")
        assert session.tool_selection("cli:selection") is first_selection
        await session.submit(TurnRequest("cli:selection", "second"))
        assert session.tool_selection("cli:selection").provider is second


async def test_spawn_completion_reaches_cli_with_parent_identity(tmp_path, monkeypatch):
    async def answer(count, messages, tools):
        if messages[-1]["role"] == "user":
            if messages[-1]["content"] == "delegate":
                return tool_call("spawn-call", "spawn", {"task": "child-report"})
            return response("child completed")
        return response("delegated")

    output = Output()
    _, opened = entry(monkeypatch, tmp_path, ScriptedProvider(answer), output=output)
    async with opened as session:
        result = await session.submit(TurnRequest("cli:parent", "delegate"))
        await asyncio.wait_for(output.notified.wait(), timeout=5)
        assert result.status is TurnStatus.COMPLETED
        assert any(
            "cli:parent" in text and "child completed" in text for text in output.infos
        )


async def test_spawn_reply_uses_original_route_after_parent_unbinds(tmp_path, monkeypatch):
    child_started, release_child, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sent = []

    async def answer(count, messages, tools):
        if messages[-1]["role"] == "user":
            if messages[-1]["content"] == "delegate":
                return tool_call("spawn-call", "spawn", {"task": "child-report"})
            child_started.set()
            await release_child.wait()
            return response("child completed")
        return response("delegated")

    async def send(message):
        sent.append(message)
        delivered.set()

    routes = TurnRoutes()
    routes.bind(
        "channel:parent",
        TurnBinding("test", "first-chat", reply_to="message-one", metadata={"message_thread_id": 9}),
    )
    _, opened = entry(
        monkeypatch, tmp_path, ScriptedProvider(answer), routes=routes, send=send,
    )
    async with opened as session:
        await session.submit(TurnRequest("channel:parent", "delegate"))
        await asyncio.wait_for(child_started.wait(), timeout=5)
        routes.unbind("channel:parent")
        routes.bind("channel:parent", TurnBinding("test", "different-chat"))
        release_child.set()
        await asyncio.wait_for(delivered.wait(), timeout=5)
        assert len(sent) == 1
        assert sent[0].channel == "test"
        assert sent[0].chat_id == "first-chat"
        assert sent[0].reply_to == "message-one"
        assert sent[0].metadata["message_thread_id"] == 9
        assert "child completed" in sent[0].content

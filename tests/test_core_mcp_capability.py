"""Focused native MCP capability tests using a local fake SDK and transport."""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import pytest

from PhyAgentOS.agent.capabilities.mcp_capability import MCPCapability
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolStatus,
)
from PhyAgentOS.tooling import mcp as mcp_module
from PhyAgentOS.tooling.mcp import (
    MCPConnectionError,
    MCPConnectionManager,
    MCPServerConnection,
)
from PhyAgentOS.tooling.tool_results import DEFAULT_MAX_CHARS


class FakeText:
    def __init__(self, text: str) -> None:
        self.text = text

    def __str__(self) -> str:
        return self.text


class FakeToolDef:
    def __init__(
        self,
        name: str,
        description: str = "",
        input_schema: dict | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.inputSchema = input_schema or {"type": "object", "properties": {}}


class FakeListResult:
    def __init__(self, tools: list[FakeToolDef], next_cursor: str | None = None) -> None:
        self.tools = tools
        self.nextCursor = next_cursor


class FakeCallResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [FakeText(text)]
        self.structuredContent = None
        self.isError = is_error


def server_config(command: str, *, tool_timeout: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        type="stdio",
        command=command,
        args=[],
        env={},
        url="",
        headers={},
        tool_timeout=tool_timeout,
    )


class FakeSession:
    def __init__(
        self,
        pages: dict[str | None, FakeListResult] | None = None,
        *,
        init_error: Exception | None = None,
        init_gate: asyncio.Event | None = None,
        call_handler=None,
    ) -> None:
        self.pages = pages or {
            None: FakeListResult([FakeToolDef("echo", "Echo")]),
        }
        self.init_error = init_error
        self.init_gate = init_gate
        self.init_started = asyncio.Event()
        self.call_handler = call_handler
        self.list_calls: list[str | None] = []
        self.call_records: list[tuple[str, dict]] = []
        self.closed = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.closed += 1

    async def initialize(self) -> None:
        if self.init_error is not None:
            raise self.init_error
        self.init_started.set()
        if self.init_gate is not None:
            await self.init_gate.wait()

    async def list_tools(self, cursor: str | None = None) -> FakeListResult:
        self.list_calls.append(cursor)
        return self.pages[cursor]

    async def call_tool(self, name: str, arguments: dict):
        self.call_records.append((name, dict(arguments)))
        if self.call_handler is not None:
            return await self.call_handler(name, arguments)
        return FakeCallResult(f"{name}:{arguments}")


class FakeTransport:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def run(self, params):
        self.entered += 1
        try:
            yield (params.command, "write")
        finally:
            self.exited += 1


@pytest.fixture
def install_fake_sdk(monkeypatch):
    def install(sessions: dict[str, FakeSession], transport: FakeTransport):
        def client_factory(read, write):
            del write
            return sessions[read]

        monkeypatch.setattr(mcp_module, "stdio_client", transport.run)
        monkeypatch.setattr(mcp_module, "ClientSession", client_factory)
        return sessions

    return install


def execution_context(session_id: str = "s1") -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="t1",
        step_id="step1",
        cancellation=CancellationToken(),
        budget=Budget(3),
    )


def tool_call(
    name: str, arguments: dict, tool_call_id: str = "call-1"
) -> ToolCall:
    return ToolCall(
        tool_call_id=tool_call_id, name=name, arguments=arguments
    )


async def test_empty_configuration_opens_no_connections() -> None:
    capability = MCPCapability({})
    await capability.open()

    assert capability.tool_registrations() == ()
    await capability.aclose()
    await capability.aclose()


async def test_discovery_follows_pagination_and_uses_dynamic_names(
    install_fake_sdk,
) -> None:
    transport = FakeTransport()
    session = FakeSession(
        pages={
            None: FakeListResult(
                [
                    FakeToolDef(
                        "first",
                        "First tool",
                        {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    )
                ],
                "page-2",
            ),
            "page-2": FakeListResult([FakeToolDef("second", "Second tool")]),
        }
    )
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability({"srv": server_config("srv")})
    await capability.open()
    try:
        definitions = {
            registration.definition.name: registration.definition
            for registration in capability.tool_registrations()
        }
        assert set(definitions) == {"mcp_srv_first", "mcp_srv_second"}
        assert definitions["mcp_srv_first"].description == "First tool"
        assert definitions["mcp_srv_first"].parameters["required"] == ["value"]
        assert session.list_calls == [None, "page-2"]
    finally:
        await capability.aclose()

    assert session.closed == 1
    assert transport.entered == 1
    assert transport.exited == 1


async def test_dispatch_forwards_arguments_and_returns_text(install_fake_sdk) -> None:
    transport = FakeTransport()

    async def handler(name, arguments):
        del name
        return FakeCallResult("done: " + str(arguments))

    session = FakeSession(call_handler=handler)
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability({"srv": server_config("srv")})
    await capability.open()
    try:
        registration = capability.tool_registrations()[0]
        outcome = await registration.handler(
            tool_call("mcp_srv_echo", {"value": 7}),
            execution_context(),
        )
    finally:
        await capability.aclose()

    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.text == "done: {'value': 7}"
    assert session.call_records == [("echo", {"value": 7})]


async def test_timeout_is_unknown_and_is_not_replayed(install_fake_sdk) -> None:
    transport = FakeTransport()

    async def slow(name, arguments):
        del name, arguments
        await asyncio.sleep(5)
        return FakeCallResult("late")

    session = FakeSession(call_handler=slow)
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability({"srv": server_config("srv", tool_timeout=0.01)})
    await capability.open()
    try:
        outcome = await capability.tool_registrations()[0].handler(
            tool_call("mcp_srv_echo", {}),
            execution_context(),
        )
    finally:
        await capability.aclose()

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.error is not None and outcome.error.code == "mcp_timeout"
    assert "Do not retry automatically" in outcome.text
    assert len(session.call_records) == 1


async def test_is_error_is_failed_not_success(install_fake_sdk) -> None:
    transport = FakeTransport()

    async def handler(name, arguments):
        del name, arguments
        return FakeCallResult("bad input", is_error=True)

    session = FakeSession(call_handler=handler)
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability({"srv": server_config("srv")})
    await capability.open()
    try:
        outcome = await capability.tool_registrations()[0].handler(
            tool_call("mcp_srv_echo", {}),
            execution_context(),
        )
    finally:
        await capability.aclose()

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None and outcome.error.code == "mcp_tool_error"


async def test_large_success_is_bounded_and_saved(
    tmp_path, install_fake_sdk
) -> None:
    transport = FakeTransport()
    full = "mcp-success:" + "A" * 20_000

    async def handler(name, arguments):
        del name, arguments
        return FakeCallResult(full)

    session = FakeSession(call_handler=handler)
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability(
        {"srv": server_config("srv")}, workspace=tmp_path
    )
    await capability.open()
    try:
        outcome = await capability.tool_registrations()[0].handler(
            tool_call("mcp_srv_echo", {}, "call-mcp-big"),
            execution_context(),
        )
    finally:
        await capability.aclose()

    assert outcome.status is ToolStatus.SUCCEEDED
    assert len(outcome.text) <= DEFAULT_MAX_CHARS
    assert ".paos/tool_outputs/call-mcp-big-" in outcome.text
    assert outcome.content == {"server": "srv", "tool": "echo"}
    saved = list(
        (tmp_path / ".paos" / "tool_outputs").glob("call-mcp-big-*.txt")
    )
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == full


async def test_large_is_error_is_bounded_saved_and_stays_failed(
    tmp_path, install_fake_sdk
) -> None:
    transport = FakeTransport()
    full = "mcp-error:" + "B" * 20_000

    async def handler(name, arguments):
        del name, arguments
        return FakeCallResult(full, is_error=True)

    session = FakeSession(call_handler=handler)
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability(
        {"srv": server_config("srv")}, workspace=tmp_path
    )
    await capability.open()
    try:
        outcome = await capability.tool_registrations()[0].handler(
            tool_call("mcp_srv_echo", {}, "call-mcp-error"),
            execution_context(),
        )
    finally:
        await capability.aclose()

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "mcp_tool_error"
    assert outcome.error.message == outcome.text
    assert len(outcome.text) <= DEFAULT_MAX_CHARS
    assert ".paos/tool_outputs/call-mcp-error-" in outcome.text
    saved = list(
        (tmp_path / ".paos" / "tool_outputs").glob("call-mcp-error-*.txt")
    )
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == full


async def test_remote_cancellation_is_unknown(install_fake_sdk) -> None:
    transport = FakeTransport()

    async def remote_cancel(name, arguments):
        del name, arguments
        raise asyncio.CancelledError

    session = FakeSession(call_handler=remote_cancel)
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability({"srv": server_config("srv")})
    await capability.open()
    try:
        outcome = await capability.tool_registrations()[0].handler(
            tool_call("mcp_srv_echo", {}),
            execution_context(),
        )
    finally:
        await capability.aclose()

    assert outcome.status is ToolStatus.UNKNOWN
    assert outcome.error is not None and outcome.error.code == "mcp_remote_cancelled"


async def test_cancelling_one_session_leaves_other_calls_and_connection_alive(
    install_fake_sdk,
) -> None:
    transport = FakeTransport()
    started = asyncio.Event()

    async def routed_call(name, arguments):
        del name
        if arguments.get("who") == "a":
            started.set()
            await asyncio.Event().wait()
        return FakeCallResult("ok:" + str(arguments["who"]))

    session = FakeSession(call_handler=routed_call)
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability({"srv": server_config("srv", tool_timeout=5)})
    await capability.open()
    registration = capability.tool_registrations()[0]
    try:
        first = asyncio.create_task(
            registration.handler(
                tool_call("mcp_srv_echo", {"who": "a"}),
                execution_context("session-a"),
            )
        )
        await started.wait()
        second = asyncio.create_task(
            registration.handler(
                tool_call("mcp_srv_echo", {"who": "b"}),
                execution_context("session-b"),
            )
        )
        second_outcome = await second
        await capability.cancel("session-a", None)
        first_outcome = await first

        follow_up = await registration.handler(
            tool_call("mcp_srv_echo", {"who": "c"}),
            execution_context("session-b"),
        )
    finally:
        await capability.aclose()

    assert second_outcome.status is ToolStatus.SUCCEEDED
    assert first_outcome.status is ToolStatus.CANCELLED
    assert follow_up.status is ToolStatus.SUCCEEDED
    assert session.closed == 1


async def test_failed_initialization_closes_its_resources(install_fake_sdk) -> None:
    transport = FakeTransport()
    session = FakeSession(init_error=RuntimeError("boom"))
    install_fake_sdk({"bad": session}, transport)

    capability = MCPCapability({"bad": server_config("bad")})
    await capability.open()
    try:
        assert capability.tool_registrations() == ()
        assert set(capability.failures) == {"bad"}
    finally:
        await capability.aclose()

    assert session.closed == 1
    assert transport.entered == 1
    assert transport.exited == 1


async def test_repeat_open_returns_only_usable_servers(install_fake_sdk) -> None:
    transport = FakeTransport()
    good = FakeSession()
    bad = FakeSession(init_error=RuntimeError("boom"))
    install_fake_sdk({"good": good, "bad": bad}, transport)

    manager = MCPConnectionManager(
        {"good": server_config("good"), "bad": server_config("bad")}
    )
    first = await manager.open()
    second = await manager.open()
    try:
        assert [connection.name for connection in first] == ["good"]
        assert [connection.name for connection in second] == ["good"]
        assert set(manager.failures) == {"bad"}
    finally:
        await manager.aclose()

    assert bad.closed == 1


async def test_aclose_cancels_blocked_initialization(install_fake_sdk) -> None:
    transport = FakeTransport()
    init_gate = asyncio.Event()
    session = FakeSession(init_gate=init_gate)
    install_fake_sdk({"slow": session}, transport)

    connection = MCPServerConnection("slow", server_config("slow"))
    start_task = asyncio.create_task(connection.start())
    await asyncio.wait_for(session.init_started.wait(), timeout=2)

    await asyncio.wait_for(connection.aclose(), timeout=2)
    with pytest.raises(MCPConnectionError):
        await start_task

    assert session.closed == 1
    assert transport.exited == 1
    assert connection._task is not None and connection._task.done()


async def test_cancelling_open_during_initialization_cleans_up(
    install_fake_sdk,
) -> None:
    transport = FakeTransport()
    init_gate = asyncio.Event()
    session = FakeSession(init_gate=init_gate)
    install_fake_sdk({"slow": session}, transport)

    manager = MCPConnectionManager({"slow": server_config("slow")})
    open_task = asyncio.create_task(manager.open())
    await asyncio.wait_for(session.init_started.wait(), timeout=2)
    open_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await open_task

    await asyncio.wait_for(manager.aclose(), timeout=2)
    assert session.closed == 1
    assert transport.exited == 1


async def test_close_is_idempotent(install_fake_sdk) -> None:
    transport = FakeTransport()
    session = FakeSession()
    install_fake_sdk({"srv": session}, transport)

    capability = MCPCapability({"srv": server_config("srv")})
    await capability.open()
    await asyncio.gather(capability.aclose(), capability.aclose())
    await capability.aclose()

    assert session.closed == 1
    assert transport.exited == 1


async def test_duplicate_exposed_names_keep_only_the_first_tool(
    install_fake_sdk,
) -> None:
    transport = FakeTransport()
    first = FakeSession(pages={None: FakeListResult([FakeToolDef("b_c")])})
    second = FakeSession(pages={None: FakeListResult([FakeToolDef("c")])})
    install_fake_sdk({"a": first, "a_b": second}, transport)

    capability = MCPCapability(
        {"a": server_config("a"), "a_b": server_config("a_b")}
    )
    await capability.open()
    try:
        registrations = capability.tool_registrations()
        assert [item.definition.name for item in registrations] == ["mcp_a_b_c"]
        outcome = await registrations[0].handler(
            tool_call("mcp_a_b_c", {}),
            execution_context(),
        )
    finally:
        await capability.aclose()

    assert outcome.status is ToolStatus.SUCCEEDED
    assert first.call_records and not second.call_records


async def test_legacy_shim_keeps_old_import_path(install_fake_sdk, monkeypatch) -> None:
    from PhyAgentOS.agent_legacy.tools import mcp as legacy_mcp

    transport = FakeTransport()
    session = FakeSession(pages={None: FakeListResult([FakeToolDef("echo")])})
    install_fake_sdk({"srv": session}, transport)
    monkeypatch.setattr(
        legacy_mcp, "ClientSession", mcp_module.ClientSession
    )

    class Registry:
        def __init__(self) -> None:
            self.tools = {}

        def register(self, tool) -> None:
            self.tools[tool.name] = tool

    registry = Registry()
    stack = AsyncExitStack()
    await stack.__aenter__()
    try:
        await legacy_mcp.connect_mcp_servers(
            {"srv": server_config("srv")}, registry, stack
        )
    finally:
        await stack.aclose()

    assert set(registry.tools) == {"mcp_srv_echo"}
    assert legacy_mcp.MCPToolWrapper is mcp_module.MCPToolWrapper


async def test_real_stdio_fastmcp_round_trip_and_cross_task_close(tmp_path) -> None:
    """End-to-end stdio check against the installed MCP SDK, no network."""
    script = tmp_path / "local_mcp_server.py"
    script.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "\n"
        "server = FastMCP('local-test', log_level='ERROR')\n"
        "\n"
        "@server.tool()\n"
        "def echo(value: str) -> str:\n"
        "    \"\"\"Return the value prefixed with echo.\"\"\"\n"
        "    return f'echo:{value}'\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    server.run('stdio')\n",
        encoding="utf-8",
    )
    config = server_config(sys.executable)
    config.args = [str(script)]
    config.tool_timeout = 10

    capability = MCPCapability({"local": config})
    owner = None
    try:
        await asyncio.wait_for(capability.open(), timeout=30)
        assert capability.failures == {}
        registrations = {
            item.definition.name: item
            for item in capability.tool_registrations()
        }
        assert set(registrations) == {"mcp_local_echo"}
        assert (
            registrations["mcp_local_echo"]
            .definition.parameters["properties"]["value"]["type"]
            == "string"
        )

        outcome = await asyncio.wait_for(
            registrations["mcp_local_echo"].handler(
                tool_call("mcp_local_echo", {"value": "hello"}),
                execution_context(),
            ),
            timeout=10,
        )
        assert outcome.status is ToolStatus.SUCCEEDED
        assert "echo:hello" in outcome.text
    finally:
        owner = capability._manager._connections.get("local")
        owner_task = owner._task if owner is not None else None

        # Close from a task distinct from the task that awaited open().
        close_task = asyncio.create_task(
            capability.aclose(), name="mcp-test-close"
        )
        try:
            await asyncio.wait_for(close_task, timeout=20)
        finally:
            if owner_task is not None and not owner_task.done():
                owner_task.cancel()
                await asyncio.wait_for(
                    asyncio.gather(owner_task, return_exceptions=True),
                    timeout=10,
                )

    assert owner_task is not None and owner_task.done()
    leftovers = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("mcp-server:local") and not task.done()
    ]
    assert leftovers == []

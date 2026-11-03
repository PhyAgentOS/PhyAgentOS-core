"""Live Forge tool discovery: real GET /tools reads, runtime pinning, no bypass."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx

from PhyAgentOS.agent.capabilities.forge_capability import ForgeCapability
from PhyAgentOS.agent.capabilities.forge_tool_api import (
    TOOL_API_DEFINITIONS,
    execute_tool_api_tool,
)
from PhyAgentOS.agent.core.tools import ToolExecutorImpl
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolPolicy,
    ToolStatus,
)
from PhyAgentOS.agent_legacy.tools.forge_tool_api import (
    ForgeToolContextTool,
    ForgeToolListTool,
    ForgeToolQueryTool,
    build_forge_tool_api_tools,
)
from PhyAgentOS.agent_legacy.tools.registry import ToolRegistry
from PhyAgentOS.forge.binding import BoundToolSpec, ForgeSkillBinding
from PhyAgentOS.forge.interaction.service import SAFE_TOOLS, InteractionService
from PhyAgentOS.forge.tool_client import ForgeToolClient


def _ok(data: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"ok": True, "data": data})


def _context() -> ExecutionContext:
    return ExecutionContext(
        session_id="session-1",
        turn_id="turn-1",
        step_id="step-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _call(name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(tool_call_id="call-1", name=name, arguments=arguments)


class _PlainListClient:
    """Duck-typed client: no runtime registry, one list_tools method."""

    def __init__(self, catalog: dict[str, Any]) -> None:
        self.catalog = catalog
        self.base_url = "http://gateway.test"
        self.list_calls = 0

    async def list_tools(self) -> dict[str, Any]:
        self.list_calls += 1
        return {"ok": True, "data": self.catalog}

    async def get_tool(self, tool_id: str) -> dict[str, Any]:
        return {"ok": True, "data": {"tool_id": tool_id}}

    async def get_tool_context(self, tool_id: str) -> dict[str, Any]:
        return {"ok": True, "data": {"tool_id": tool_id, "ready": True}}


# ---------------------------------------------------------------------------
# live catalog reads
# ---------------------------------------------------------------------------


async def test_forge_tool_list_reads_live_catalog_and_keeps_unknown_shape() -> None:
    revisions = iter(
        [
            {
                "tools": [{"tool_id": "demo.old"}],
                "gateway_marker": {"revision": "v1"},
            },
            {
                "tools": [{"tool_id": "demo.old"}, {"tool_id": "demo.new"}],
                "gateway_marker": {"revision": "v2"},
            },
        ]
    )
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _ok(next(revisions))

    client = ForgeToolClient(
        "http://gateway.test", transport=httpx.MockTransport(handler)
    )
    try:
        first = await execute_tool_api_tool("forge_tool_list", client, None, {})
        second = await execute_tool_api_tool("forge_tool_list", client, None, {})
    finally:
        await client.close()

    # The Gateway data object is copied unchanged; no guessed subfields.
    assert first["data"]["catalog"] == {
        "tools": [{"tool_id": "demo.old"}],
        "gateway_marker": {"revision": "v1"},
    }
    assert second["data"]["catalog"] == {
        "tools": [{"tool_id": "demo.old"}, {"tool_id": "demo.new"}],
        "gateway_marker": {"revision": "v2"},
    }
    assert paths == ["/tools", "/tools"]
    source = second["data"]["source"]
    assert source["kind"] == "live_gateway"
    assert source["endpoint"] == "GET /tools"
    assert source["snapshot"] == "single_get_request"
    assert source["runtime"] is None
    assert isinstance(source["read_at"], str) and source["read_at"]
    assert source["gateway_url"] == "http://gateway.test"


async def test_forge_tool_context_reads_both_live_values_on_every_call() -> None:
    revision = {"value": "v1"}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/tools/motion.move_pose":
            return _ok(
                {"tool_id": "motion.move_pose", "fixture_revision": revision["value"]}
            )
        return _ok({"tool_id": "motion.move_pose", "fixture_revision": revision["value"]})

    client = ForgeToolClient(
        "http://gateway.test", transport=httpx.MockTransport(handler)
    )
    try:
        first = await execute_tool_api_tool(
            "forge_tool_context",
            client,
            None,  # type: ignore[arg-type]
            {"tool_id": "motion.move_pose"},
        )
        revision["value"] = "v2"
        second = await execute_tool_api_tool(
            "forge_tool_context",
            client,
            None,  # type: ignore[arg-type]
            {"tool_id": "motion.move_pose"},
        )
    finally:
        await client.close()

    assert first["data"]["tool"]["fixture_revision"] == "v1"
    assert first["data"]["context"]["fixture_revision"] == "v1"
    assert second["data"]["tool"]["fixture_revision"] == "v2"
    assert second["data"]["context"]["fixture_revision"] == "v2"
    assert paths == [
        "/tools/motion.move_pose",
        "/tools/motion.move_pose/context",
        "/tools/motion.move_pose",
        "/tools/motion.move_pose/context",
    ]
    source = second["data"]["source"]
    assert source["kind"] == "live_gateway"
    assert source["snapshot"] == "not_atomic_two_independent_get_requests"
    assert isinstance(source["read_at"], str) and source["read_at"]


# ---------------------------------------------------------------------------
# one call pins one runtime
# ---------------------------------------------------------------------------


class _Registry:
    def __init__(self, active: Any | None) -> None:
        self.active = active

    def current(self) -> Any | None:
        return self.active


class _DynamicClient:
    """Duck-typed stand-in for DynamicForgeToolClient."""

    def __init__(self, registry: _Registry) -> None:
        self.registry = registry

    def __getattr__(self, name: str) -> Any:
        active = self.registry.current()
        if active is None:
            raise RuntimeError("no ready Forge Skill runtime is active")
        return getattr(active.client, name)


def _active(client: Any, instance_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        client=client,
        skill_name="demo-skill",
        skill_version="1.0.0",
        profile="local",
        runtime_instance_id=instance_id,
        gateway_url="http://gateway.test",
        gateway_identity=f"identity-{instance_id}",
    )


class _SwitchClient:
    def __init__(
        self,
        name: str,
        registry: _Registry,
        replacement: SimpleNamespace | None,
    ) -> None:
        self.name = name
        self.registry = registry
        self.replacement = replacement
        self.base_url = "http://gateway.test"
        self.tool_calls = 0
        self.context_calls = 0

    def _switch_once(self) -> None:
        if self.replacement is not None:
            self.registry.active = self.replacement
            self.replacement = None

    async def get_tool(self, tool_id: str) -> dict[str, Any]:
        self.tool_calls += 1
        self._switch_once()
        return {"ok": True, "data": {"tool_id": tool_id, "client": self.name}}

    async def get_tool_context(self, tool_id: str) -> dict[str, Any]:
        self.context_calls += 1
        return {"ok": True, "data": {"tool_id": tool_id, "client": self.name}}


async def test_runtime_switch_during_a_call_keeps_one_client() -> None:
    registry = _Registry(None)
    first = _SwitchClient("runtime-a", registry, None)
    second = _SwitchClient("runtime-b", registry, None)
    registry.active = _active(first, "runtime-a")
    first.replacement = _active(second, "runtime-b")
    client = _DynamicClient(registry)

    payload = await execute_tool_api_tool(
        "forge_tool_context",
        client,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        {"tool_id": "motion.move_pose"},
    )

    assert payload["ok"] is True
    # The first GET switched the registry, but both GETs stayed on runtime-a.
    assert payload["data"]["tool"]["client"] == "runtime-a"
    assert payload["data"]["context"]["client"] == "runtime-a"
    assert (first.tool_calls, first.context_calls) == (1, 1)
    assert (second.tool_calls, second.context_calls) == (0, 0)
    source = payload["data"]["source"]
    assert source["runtime"]["runtime_instance_id"] == "runtime-a"
    assert source["runtime"]["gateway_identity"] == "identity-runtime-a"
    assert "gateway_url" not in source


async def test_discovery_without_active_runtime_is_a_clear_error_not_a_cache() -> None:
    client = _DynamicClient(_Registry(None))

    payload = await execute_tool_api_tool(
        "forge_tool_list",
        client,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        {},
    )

    assert payload["ok"] is False
    assert payload["error"]["type"] == "runtime"
    assert "no ready Forge Skill runtime is active" in payload["error"]["message"]
    assert "cached catalog is used" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# discovery does not expand the AgentTask allowlist
# ---------------------------------------------------------------------------


def _binding() -> ForgeSkillBinding:
    return ForgeSkillBinding(
        binding_id="binding_test",
        skill_name="demo-skill",
        skill_version="1.0.0",
        manifest_sha256="0" * 64,
        skill_document_sha256="0" * 64,
        runtime_profile="local",
        runtime_instance_id="runtime-a",
        gateway_url="http://gateway.test",
        gateway_identity="identity-runtime-a",
        required_tools=(
            BoundToolSpec(
                tool_id="demo.old",
                semantics="action",
                spec_sha256="1" * 64,
                ready_at_binding=True,
            ),
        ),
    )


async def test_list_reports_task_allowlist_without_changing_it() -> None:
    binding = _binding()
    task = SimpleNamespace(task_id="task_1", primary_skill_binding=binding)

    class Coordinator:
        def get_task(self, task_id: str) -> Any:
            assert task_id == "task_1"
            return task

    client = _PlainListClient(
        {
            "tools": [{"tool_id": "demo.old"}, {"tool_id": "demo.new"}],
        }
    )

    payload = await execute_tool_api_tool(
        "forge_tool_list",
        client,  # type: ignore[arg-type]
        Coordinator(),  # type: ignore[arg-type]
        {"task_id": "task_1"},
    )

    view = payload["data"]["binding"]
    assert view["available"] is True
    assert view["allowlist"] == ["demo.old"]
    assert view["binding_id"] == binding.binding_id
    assert view["runtime_profile"] == "local"
    assert "does not expand it" in view["note"]
    assert [item.tool_id for item in binding.required_tools] == ["demo.old"]
    assert payload["data"]["catalog"]["tools"] == [
        {"tool_id": "demo.old"},
        {"tool_id": "demo.new"},
    ]


# ---------------------------------------------------------------------------
# Core registration and legacy compatibility
# ---------------------------------------------------------------------------


def test_forge_tool_list_is_exposed_through_core_and_legacy() -> None:
    definitions = {item.name: item for item in TOOL_API_DEFINITIONS}
    definition = definitions["forge_tool_list"]
    assert definition.parameters["properties"].keys() == {"task_id"}
    assert definition.parameters.get("required", []) == []
    assert definition.parameters["additionalProperties"] is False

    client = _PlainListClient({"tools": []})
    capability = ForgeCapability(object(), client=client)  # type: ignore[arg-type]
    registrations = {
        item.definition.name: item for item in capability.tool_registrations()
    }
    assert "forge_tool_list" in registrations

    legacy_tools = {
        tool.name: tool
        for tool in build_forge_tool_api_tools(
            client,  # type: ignore[arg-type]
            coordinator=object(),  # type: ignore[arg-type]
        )
    }
    assert isinstance(legacy_tools["forge_tool_list"], ForgeToolListTool)

    no_coordinator = {
        tool.name: tool
        for tool in build_forge_tool_api_tools(client)  # type: ignore[arg-type]
    }
    assert set(no_coordinator) == {"forge_tool_context", "forge_tool_list"}


async def test_core_registration_runs_the_live_discovery_handler() -> None:
    client = _PlainListClient({"tools": [{"tool_id": "demo.old"}]})
    capability = ForgeCapability(object(), client=client)  # type: ignore[arg-type]
    registrations = {
        item.definition.name: item for item in capability.tool_registrations()
    }

    outcome = await registrations["forge_tool_list"].handler(
        _call("forge_tool_list", {}), _context()
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    payload = json.loads(outcome.text)
    assert payload["data"]["catalog"] == {"tools": [{"tool_id": "demo.old"}]}
    assert client.list_calls == 1


async def test_legacy_list_wrapper_returns_the_live_catalog() -> None:
    client = _PlainListClient({"tools": [{"tool_id": "demo.old"}]})
    tool = ForgeToolListTool(client)  # type: ignore[arg-type]

    payload = json.loads(await tool.execute())

    assert payload["ok"] is True
    assert payload["data"]["catalog"] == {"tools": [{"tool_id": "demo.old"}]}
    assert client.list_calls == 1


async def test_empty_gateway_catalog_is_preserved_as_empty_object() -> None:
    client = _PlainListClient({})

    payload = await execute_tool_api_tool("forge_tool_list", client, None, {})

    assert payload["ok"] is True
    assert payload["data"]["catalog"] == {}
    assert payload["data"]["source"]["endpoint"] == "GET /tools"


# ---------------------------------------------------------------------------
# managed interaction: read-only discovery stays available
# ---------------------------------------------------------------------------


class _AppendStore:
    async def append(self, _event: Any) -> None:
        return None


async def test_restricted_interaction_allows_list_and_context_handlers(
    tmp_path, monkeypatch,
) -> None:
    assert "forge_tool_list" in SAFE_TOOLS
    assert "forge_tool_context" in SAFE_TOOLS
    assert not {"forge_tool_query", "forge_tool_start_action"} & SAFE_TOOLS

    service = InteractionService(SimpleNamespace(workspace=tmp_path))
    monkeypatch.setattr(service, "restricted", lambda: True)
    assert service.allows("forge_tool_list")
    assert service.allows("forge_tool_context")

    client = _PlainListClient({"tools": [{"tool_id": "demo.old"}]})
    capability = ForgeCapability(object(), client=client)  # type: ignore[arg-type]
    executor = ToolExecutorImpl(capability.tool_registrations(), _AppendStore())
    restricted = ToolPolicy(allowed_tools=frozenset(SAFE_TOOLS))
    context = ExecutionContext(
        session_id="session-1",
        turn_id="turn-1",
        step_id="step-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
        policy=restricted,
    )

    listed = await executor.execute(_call("forge_tool_list", {}), context)
    described = await executor.execute(
        _call("forge_tool_context", {"tool_id": "demo.old"}), context
    )

    assert listed.status is ToolStatus.SUCCEEDED
    assert described.status is ToolStatus.SUCCEEDED
    assert json.loads(listed.text)["data"]["catalog"]["tools"][0]["tool_id"] == "demo.old"

    # The legacy registry uses the same restricted service as its policy.
    registry = ToolRegistry(policy=service)
    registry.register(ForgeToolListTool(client))  # type: ignore[arg-type]
    registry.register(ForgeToolContextTool(client))  # type: ignore[arg-type]
    registry.register(ForgeToolQueryTool(client, SimpleNamespace()))  # type: ignore[arg-type]
    legacy_list = json.loads(await registry.execute("forge_tool_list", {}))
    legacy_context = json.loads(
        await registry.execute("forge_tool_context", {"tool_id": "demo.old"})
    )
    denied = await registry.execute("forge_tool_query", {"tool_id": "demo.old", "arguments": {}})
    assert legacy_list["ok"] is True
    assert legacy_context["ok"] is True
    assert "tool is unavailable in governed interaction mode" in denied
    assert service.unsafe_calls == 0


# ---------------------------------------------------------------------------
# a new conversation does not read historical task state automatically
# ---------------------------------------------------------------------------


async def test_new_conversation_does_not_read_a_historical_task() -> None:
    class Coordinator:
        def get_task(self, task_id: str) -> Any:
            raise AssertionError(f"unexpected historical task read: {task_id}")

    client = _PlainListClient({"tools": []})
    payload = await execute_tool_api_tool(
        "forge_tool_list",
        client,  # type: ignore[arg-type]
        Coordinator(),  # type: ignore[arg-type]
        {},
    )
    assert payload["ok"] is True
    assert "binding" not in payload["data"]

    class Store:
        def latest_for_session(self, _session_id: str) -> Any:
            return SimpleNamespace(terminal=True)

    capability = ForgeCapability(SimpleNamespace(store=Store()))  # type: ignore[arg-type]
    assert capability.prompt_context(_context()) is None

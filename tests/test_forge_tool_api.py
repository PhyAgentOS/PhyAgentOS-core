from __future__ import annotations

import asyncio
import json
import time

import httpx

from PhyAgentOS.agent.capabilities.forge_tool_api import (
    TOOL_API_DEFINITIONS,
    execute_tool_api_tool,
)
from PhyAgentOS.agent_legacy.tools.forge_tool_api import build_forge_tool_api_tools
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.task import AgentTaskCoordinator
from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.verification.contracts import (
    TaskVerificationContract,
    VerificationEvidencePolicy,
)


def _ok(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"ok": True, "data": data})


def _coordinator(
    tmp_path,
    client: ForgeToolClient,
    *,
    tracked=None,
    config: ForgeConfig | None = None,
):
    # Keep evidence collection disabled in transport-only tests; AsyncClient retains its base URL.
    client.base_url = ""
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path,
        config=config or ForgeConfig(action_wait_ms=0),
        client=client,
        runtime_invocation_ids=tracked,
    )
    task = coordinator.create_task(
        task_description="transport contract",
        verification=TaskVerificationContract(
            evidence_policy=VerificationEvidencePolicy(required_kinds=[])
        ),
    )
    return coordinator, task


async def test_tool_client_uses_authoritative_query_and_action_routes() -> None:
    requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, payload))
        if request.method == "GET" and request.url.path == "/tools/motion.resolve_relative_pose":
            return _ok(
                {
                    "tool_id": "motion.resolve_relative_pose",
                    "endpoint_id": "motion.relative_pose",
                    "operation": "resolve",
                    "semantics": "query",
                }
            )
        if request.url.path == "/tools/motion.relative_pose/resolve:invoke":
            return _ok(
                {
                    "endpoint_id": "motion.relative_pose",
                    "operation": "resolve",
                    "response": {"target_pose": {"x": 0.05}},
                }
            )
        if request.url.path == "/tools/motion.move_pose:invoke":
            return _ok(
                {"invocation_id": "inv_1", "attempt_id": "attempt_1"},
                status=202,
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        query = await client.invoke_query_tool(
            "motion.resolve_relative_pose",
            {"translation_m": {"x": 0.05, "y": 0, "z": 0}},
            caller_id="paos:test",
            timeout_ms=500,
        )
        action = await client.invoke_action(
            "motion.move_pose",
            {"target_pose": {"x": 0.05}},
            caller_id="paos:test",
        )
    finally:
        await client.close()

    assert query["data"]["response"]["target_pose"]["x"] == 0.05
    assert action["data"] == {"invocation_id": "inv_1", "attempt_id": "attempt_1"}
    assert requests == [
        ("GET", "/tools/motion.resolve_relative_pose", None),
        (
            "POST",
            "/tools/motion.relative_pose/resolve:invoke",
            {
                "arguments": {"translation_m": {"x": 0.05, "y": 0, "z": 0}},
                "caller_id": "paos:test",
                "timeout_ms": 500,
            },
        ),
        (
            "POST",
            "/tools/motion.move_pose:invoke",
            {"arguments": {"target_pose": {"x": 0.05}}, "caller_id": "paos:test"},
        ),
    ]


async def test_core_start_action_passes_no_timeout_when_omitted() -> None:
    calls: list[int | None] = []

    class Coordinator:
        async def start_action(
            self, task_id, tool_id, arguments, *, timeout_ms=None, wait_ms=None
        ):
            del task_id, tool_id, arguments
            calls.append((timeout_ms, wait_ms))
            return {"ok": True, "data": {"status": "accepted"}}

    payload = await execute_tool_api_tool(
        "forge_tool_start_action",
        None,
        Coordinator(),  # type: ignore[arg-type]
        {"task_id": "task_1", "tool_id": "motion.move", "arguments": {}},
    )
    assert payload["data"]["status"] == "accepted"
    assert calls == [(None, 0)]

    await execute_tool_api_tool(
        "forge_tool_start_action",
        None,
        Coordinator(),  # type: ignore[arg-type]
        {
            "task_id": "task_1",
            "tool_id": "motion.move",
            "arguments": {},
            "timeout_ms": 500,
        },
    )
    assert calls == [(None, 0), (500, 0)]


def test_tool_schema_keeps_optional_timeout_with_omit_policy() -> None:
    definitions = {item.name: item for item in TOOL_API_DEFINITIONS}

    for name in ("forge_tool_query", "forge_tool_start_action"):
        parameters = definitions[name].parameters
        assert "timeout_ms" in parameters["properties"]
        assert "timeout_ms" not in parameters["required"]
        assert "Omit" in parameters["properties"]["timeout_ms"]["description"]
        assert "Omit timeout_ms" in definitions[name].description


async def test_action_pending_cancel_and_unknown_do_not_imply_stopped(tmp_path) -> None:
    phases = iter(
        [
            _ok({"status": "pending"}, status=202),
            _ok(
                {"invocation_id": "inv_unknown", "cancel_status": "requested"},
                status=202,
            ),
            _ok({"phase": "unknown"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tools/motion.move_pose:invoke":
            return _ok(
                {"invocation_id": "inv_unknown", "attempt_id": "attempt_unknown"},
                status=202,
            )
        return next(phases)

    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(handler),
    )
    tracked: set[str] = set()
    coordinator, task = _coordinator(tmp_path, client, tracked=tracked)
    tools = {
        tool.name: tool
        for tool in build_forge_tool_api_tools(client, coordinator=coordinator)
    }
    try:
        started = json.loads(
            await tools["forge_tool_start_action"].execute(
                task.task_id, "motion.move_pose", {}
            )
        )
        pending = json.loads(
            await tools["forge_tool_action_result"].execute(task.task_id, "inv_unknown")
        )
        cancelled = json.loads(
            await tools["forge_tool_cancel_action"].execute(task.task_id, "inv_unknown")
        )
        unknown = json.loads(
            await tools["forge_tool_action_status"].execute(task.task_id, "inv_unknown")
        )
    finally:
        await client.close()

    assert started["data"]["invocation_id"] == "inv_unknown"
    assert pending["data"]["status"] == "pending"
    assert cancelled["data"]["cancel_status"] == "requested"
    assert unknown["data"]["phase"] == "unknown"
    assert tracked == {"inv_unknown"}


async def test_timeout_is_reported_as_unknown_remote_state(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late", request=request)

    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(handler),
    )
    coordinator, task = _coordinator(tmp_path, client)
    tools = {
        tool.name: tool
        for tool in build_forge_tool_api_tools(client, coordinator=coordinator)
    }
    try:
        result = json.loads(
            await tools["forge_tool_start_action"].execute(
                task.task_id, "demo.action", {}
            )
        )
    finally:
        await client.close()

    assert result["ok"] is False
    assert result["error"]["type"] == "timeout"
    assert result["error"]["remote_state"] == "unknown"
    assert result["error"]["stopped"] is False


async def test_gateway_invocation_id_survives_local_tracking_failure(tmp_path) -> None:
    class BrokenTrackingSet(set[str]):
        def add(self, _value: str) -> None:
            raise OSError("state store unavailable")

    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(
            lambda _request: _ok(
                {"invocation_id": "inv_retain", "attempt_id": "attempt_retain"},
                status=202,
            )
        ),
    )
    coordinator, task = _coordinator(tmp_path, client, tracked=BrokenTrackingSet())
    tools = {
        tool.name: tool
        for tool in build_forge_tool_api_tools(
            client,
            coordinator=coordinator,
        )
    }
    try:
        result = json.loads(
            await tools["forge_tool_start_action"].execute(
                task.task_id, "motion.move_pose", {}
            )
        )
    finally:
        await client.close()

    assert result["ok"] is True
    assert result["data"]["invocation_id"] == "inv_retain"
    assert result["paos_warnings"][0]["type"] == "local_tracking"


async def test_invalid_admission_contract_still_exposes_and_tracks_invocation_id(tmp_path) -> None:
    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(
            lambda _request: _ok({"invocation_id": "inv_incomplete"}, status=202)
        ),
    )
    tracked: set[str] = set()
    coordinator, task = _coordinator(tmp_path, client, tracked=tracked)
    tools = {
        tool.name: tool
        for tool in build_forge_tool_api_tools(client, coordinator=coordinator)
    }
    try:
        result = json.loads(
            await tools["forge_tool_start_action"].execute(
                task.task_id, "motion.move_pose", {}
            )
        )
    finally:
        await client.close()

    assert result["ok"] is False
    assert result["error"]["invocation_id"] == "inv_incomplete"
    assert tracked == {"inv_incomplete"}


def test_mutating_tools_are_not_registered_without_a_coordinator() -> None:
    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(lambda _: _ok({})),
    )
    names = {tool.name for tool in build_forge_tool_api_tools(client)}

    assert names == {"forge_tool_context", "forge_tool_list"}
    assert not names & {
        "forge_execute_task",
        "forge_get_session",
        "forge_cancel_session",
        "forge_get_context",
        "forge_reset",
        "verify_forge_session",
        "create_replanned_forge_session",
    }


async def test_pre_dispatch_validation_code_is_passed_through_and_failed(tmp_path) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            400,
            json={
                "ok": False,
                "msg": "timeout_ms must not exceed configured maximum",
                "error": {
                    "code": "FORGE_TOOL_REQUEST_VALIDATION_REJECTED",
                    "message": "timeout_ms must not exceed configured maximum",
                    "retryable": False,
                },
            },
        )

    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(handler),
    )
    coordinator, task = _coordinator(tmp_path, client)
    try:
        payload = await execute_tool_api_tool(
            "forge_tool_start_action",
            client,
            coordinator,
            {
                "task_id": task.task_id,
                "tool_id": "motion.move_pose",
                "arguments": {"distance_m": 0.05},
                "timeout_ms": 60001,
            },
        )
    finally:
        await client.close()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "FORGE_TOOL_REQUEST_VALIDATION_REJECTED"
    assert payload["error"]["status_code"] == 400
    assert payload["error"]["retryable"] is False
    record = coordinator.get_task(task.task_id).execution_records[0]
    assert record.status == "failed"
    assert record.rejected_before_dispatch is True
    assert record.invocation_id is None
    assert requests == [("POST", "/tools/motion.move_pose:invoke")]


async def test_start_action_wait_returns_terminal_result(tmp_path) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith(":invoke"):
            return _ok(
                {"invocation_id": "inv_wait", "attempt_id": "attempt_wait"},
                status=202,
            )
        if request.url.path == "/invocations/inv_wait/result":
            return _ok(
                {
                    "status": "available",
                    "result": {"status": "succeeded", "outputs": {"moved": True}},
                }
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(handler),
    )
    coordinator, task = _coordinator(
        tmp_path,
        client,
        tracked=(tracked := set()),
        config=ForgeConfig(action_wait_ms=1_000, poll_interval_s=0.1),
    )
    try:
        payload = await execute_tool_api_tool(
            "forge_tool_start_action",
            client,
            coordinator,
            {"task_id": task.task_id, "tool_id": "motion.move_pose", "arguments": {}},
        )
    finally:
        await client.close()

    assert payload["ok"] is True
    assert payload["data"]["result"]["status"] == "succeeded"
    assert payload["data"]["invocation_id"] == "inv_wait"
    assert payload["data"]["attempt_id"] == "attempt_wait"
    record = coordinator.get_task(task.task_id).execution_records[0]
    assert record.status == "succeeded"
    assert record.invocation_id == "inv_wait"
    assert tracked == set()
    assert ("GET", "/invocations/inv_wait/result") in requests


async def test_start_action_wait_timeout_stays_pending(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith(":invoke"):
            return _ok(
                {"invocation_id": "inv_slow", "attempt_id": "attempt_slow"},
                status=202,
            )
        if request.url.path == "/invocations/inv_slow/result":
            return _ok({"status": "pending"}, status=202)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(handler),
    )
    coordinator, task = _coordinator(
        tmp_path,
        client,
        config=ForgeConfig(action_wait_ms=150, poll_interval_s=0.1),
    )
    try:
        payload = await execute_tool_api_tool(
            "forge_tool_start_action",
            client,
            coordinator,
            {"task_id": task.task_id, "tool_id": "motion.move_pose", "arguments": {}},
        )
    finally:
        await client.close()

    assert payload["ok"] is True
    assert payload["data"]["status"] == "pending"
    assert payload["data"]["invocation_id"] == "inv_slow"
    assert payload["data"]["attempt_id"] == "attempt_slow"
    record = coordinator.get_task(task.task_id).execution_records[0]
    assert record.status == "accepted"
    assert record.invocation_id == "inv_slow"


async def test_start_action_wait_bounds_a_slow_result_read(tmp_path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _ok(
                {"invocation_id": "inv_read", "attempt_id": "attempt_read"},
                status=202,
            )
        await asyncio.sleep(5)
        return _ok({"status": "pending"}, status=202)

    client = ForgeToolClient(
        "http://gateway.test", transport=httpx.MockTransport(handler)
    )
    coordinator, task = _coordinator(
        tmp_path, client, config=ForgeConfig(action_wait_ms=50)
    )
    try:
        started = time.monotonic()
        payload = await asyncio.wait_for(
            execute_tool_api_tool(
                "forge_tool_start_action",
                client,
                coordinator,
                {"task_id": task.task_id, "tool_id": "motion.move_pose", "arguments": {}},
            ),
            timeout=1,
        )
        elapsed = time.monotonic() - started
    finally:
        await client.close()

    assert elapsed < 1
    assert payload["data"]["invocation_id"] == "inv_read"
    assert coordinator.get_task(task.task_id).execution_records[0].status == "accepted"

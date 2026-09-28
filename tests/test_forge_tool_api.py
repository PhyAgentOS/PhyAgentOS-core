from __future__ import annotations

import json

import httpx

from PhyAgentOS.agent.tools.forge_tool_api import build_forge_tool_api_tools
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.task import AgentTaskCoordinator
from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.verification.contracts import (
    TaskVerificationContract,
    VerificationEvidencePolicy,
)


def _ok(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"ok": True, "data": data})


def _coordinator(tmp_path, client: ForgeToolClient, *, tracked=None):
    # Keep evidence collection disabled in transport-only tests; AsyncClient retains its base URL.
    client.base_url = ""
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(),
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

    assert names == {"forge_tool_context"}
    assert not names & {
        "forge_execute_task",
        "forge_get_session",
        "forge_cancel_session",
        "forge_get_context",
        "forge_reset",
        "verify_forge_session",
        "create_replanned_forge_session",
    }

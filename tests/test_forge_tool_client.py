from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from PhyAgentOS.forge.tool_client import (
    ForgeToolAPIError,
    ForgeToolAPITimeoutError,
    ForgeToolClient,
)


@pytest.mark.asyncio
async def test_tool_client_covers_discovery_query_and_action_lifecycle() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        responses = {
            ("GET", "/tools"): (200, {"ok": True, "data": {"tools": []}}),
            ("GET", "/tools/robot.pick"): (
                200,
                {"ok": True, "data": {"tool_id": "robot.pick"}},
            ),
            ("GET", "/tools/robot.pick/context"): (
                200,
                {"ok": True, "data": {"ready": True}},
            ),
            ("POST", "/tools/vision.pose/resolve:invoke"): (
                200,
                {"ok": True, "data": {"response": {"outcome": "completed"}}},
            ),
            ("POST", "/tools/robot.pick:invoke"): (
                202,
                {"ok": True, "data": {"invocation_id": "inv-1", "phase": "dispatching"}},
            ),
            ("GET", "/invocations/inv-1"): (
                200,
                {"ok": True, "data": {"invocation_id": "inv-1", "phase": "unknown"}},
            ),
            ("GET", "/invocations/inv-1/result"): (
                202,
                {"ok": True, "data": {"invocation_id": "inv-1", "status": "pending"}},
            ),
            ("POST", "/invocations/inv-1/cancel"): (
                202,
                {
                    "ok": True,
                    "data": {"invocation_id": "inv-1", "cancel_status": "requested"},
                },
            ),
        }
        status, payload = responses[(request.method, request.url.path)]
        return httpx.Response(status, json=payload)

    client = ForgeToolClient(
        "http://gateway.test/",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (await client.list_tools())["data"]["tools"] == []
        assert (await client.get_tool("robot.pick"))["data"]["tool_id"] == "robot.pick"
        assert (await client.get_tool_context("robot.pick"))["data"]["ready"] is True
        query = await client.invoke_query(
            "vision.pose",
            "resolve",
            {"offset_m": 0.05},
            caller_id="paos",
            timeout_ms=500,
        )
        assert query["data"]["response"]["outcome"] == "completed"
        action = await client.invoke_action("robot.pick", {"target": "cup"})
        assert action["data"]["phase"] == "dispatching"
        assert (await client.invocation_status("inv-1"))["data"]["phase"] == "unknown"
        assert (await client.invocation_result("inv-1"))["data"]["status"] == "pending"
        assert (await client.cancel_invocation("inv-1"))["data"]["cancel_status"] == "requested"
    finally:
        await client.close()

    query_request = requests[3]
    assert query_request.method == "POST"
    assert query_request.url.path == "/tools/vision.pose/resolve:invoke"
    assert query_request.read() == (
        b'{"arguments":{"offset_m":0.05},"caller_id":"paos","timeout_ms":500}'
    )


@pytest.mark.asyncio
async def test_query_tool_id_resolves_configured_binding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "tool_id": "motion.resolve_relative_pose",
                        "endpoint_id": "motion.relative_pose",
                        "operation": "resolve",
                        "semantics": "query",
                    },
                },
            )
        assert request.url.path == "/tools/motion.relative_pose/resolve:invoke"
        return httpx.Response(
            200,
            json={"ok": True, "data": {"response": {"outcome": "completed"}}},
        )

    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.invoke_query_tool(
            "motion.resolve_relative_pose",
            {"translation_m": {"x": 0.0, "y": 0.0, "z": 0.05}},
        )
    finally:
        await client.close()

    assert result["data"]["response"]["outcome"] == "completed"


@pytest.mark.asyncio
async def test_tool_client_preserves_structured_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "ok": False,
                "msg": "endpoint unavailable",
                "error": {
                    "code": "FORGE_TOOL_ENDPOINT_UNAVAILABLE",
                    "message": "endpoint unavailable",
                    "retryable": True,
                },
            },
        )

    client = ForgeToolClient("http://gateway.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ForgeToolAPIError) as caught:
            await client.invoke_query("vision.pose", "resolve")
    finally:
        await client.close()

    assert caught.value.status_code == 503
    assert caught.value.error_code == "FORGE_TOOL_ENDPOINT_UNAVAILABLE"
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_tool_client_rejects_wrong_success_status_and_non_json() -> None:
    responses = iter(
        (
            httpx.Response(200, json={"ok": True, "data": {"invocation_id": "inv"}}),
            httpx.Response(200, text="not json"),
        )
    )
    client = ForgeToolClient(
        "http://gateway.test",
        transport=httpx.MockTransport(lambda request: next(responses)),
    )
    try:
        with pytest.raises(ForgeToolAPIError, match="unexpected HTTP 200"):
            await client.invoke_action("robot.pick")
        with pytest.raises(ForgeToolAPIError, match="non-JSON"):
            await client.list_tools()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tool_client_timeout_does_not_issue_cancel() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        raise httpx.ReadTimeout("provider did not reply", request=request)

    client = ForgeToolClient("http://gateway.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ForgeToolAPITimeoutError, match="remote state is unknown"):
            await client.invoke_query("vision.pose", "resolve")
    finally:
        await client.close()

    assert paths == ["/tools/vision.pose/resolve:invoke"]

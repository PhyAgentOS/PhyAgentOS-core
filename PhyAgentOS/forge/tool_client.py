"""Asynchronous client for the Forge Gateway Tool API."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class ForgeToolAPIError(RuntimeError):
    """A Tool API transport, HTTP, or response-contract failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retryable = retryable
        self.payload = payload


class ForgeToolAPITimeoutError(ForgeToolAPIError):
    """The HTTP exchange timed out; remote execution state is not implied."""


class ForgeToolClient:
    """Strict async client for Gateway Tool discovery and invocation resources."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=max(0.1, float(timeout_s)),
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> ForgeToolClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def list_tools(self) -> dict[str, Any]:
        return await self._request("GET", "/tools", expected_statuses={200})

    async def get_tool(self, tool_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/tools/{_path_component(tool_id, 'tool_id')}", expected_statuses={200}
        )

    async def get_tool_context(self, tool_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/tools/{_path_component(tool_id, 'tool_id')}/context",
            expected_statuses={200},
        )

    async def invoke_query(
        self,
        endpoint_id: str,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        caller_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        payload = _invoke_payload(arguments, caller_id=caller_id, timeout_ms=timeout_ms)
        return await self._request(
            "POST",
            (
                f"/tools/{_path_component(endpoint_id, 'endpoint_id')}/"
                f"{_path_component(operation, 'operation')}:invoke"
            ),
            payload=payload,
            expected_statuses={200},
        )

    async def invoke_query_tool(
        self,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        caller_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Resolve a configured Query ToolSpec, then invoke its bound operation."""
        tool = await self.get_tool(tool_id)
        spec = tool["data"]
        endpoint_id = spec.get("endpoint_id")
        operation = spec.get("operation")
        semantics = spec.get("semantics")
        if semantics != "query":
            raise ForgeToolAPIError(
                f"Forge Tool {tool_id!r} is not a Query",
                payload=tool,
            )
        if not isinstance(endpoint_id, str) or not isinstance(operation, str):
            raise ForgeToolAPIError(
                f"Forge Tool {tool_id!r} has an invalid Query binding",
                payload=tool,
            )
        return await self.invoke_query(
            endpoint_id,
            operation,
            arguments,
            caller_id=caller_id,
            timeout_ms=timeout_ms,
        )

    async def invoke_action(
        self,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        caller_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        payload = _invoke_payload(arguments, caller_id=caller_id, timeout_ms=timeout_ms)
        return await self._request(
            "POST",
            f"/tools/{_path_component(tool_id, 'tool_id')}:invoke",
            payload=payload,
            expected_statuses={202},
        )

    async def start_session(
        self,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        caller_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a Gateway Session; Sessions deliberately have no deadline."""
        tool = await self.get_tool(tool_id)
        spec = tool.get("data")
        if not isinstance(spec, dict) or spec.get("semantics") != "session":
            raise ForgeToolAPIError(f"Forge Tool {tool_id!r} is not a Session", payload=tool)
        return await self._request(
            "POST",
            f"/tools/{_path_component(tool_id, 'tool_id')}:invoke",
            payload=_invoke_payload(arguments, caller_id=caller_id, timeout_ms=None),
            expected_statuses={202},
        )

    async def invocation_status(self, invocation_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/invocations/{_path_component(invocation_id, 'invocation_id')}",
            expected_statuses={200},
        )

    async def invocation_result(self, invocation_id: str) -> dict[str, Any]:
        # 202 is a successful pending snapshot, not an HTTP error.
        return await self._request(
            "GET",
            f"/invocations/{_path_component(invocation_id, 'invocation_id')}/result",
            expected_statuses={200, 202},
        )

    async def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        # 202 means cancellation was requested, not that execution has stopped.
        return await self._request(
            "POST",
            f"/invocations/{_path_component(invocation_id, 'invocation_id')}/cancel",
            payload=None,
            expected_statuses={200, 202},
        )

    async def stop_session(self, invocation_id: str) -> dict[str, Any]:
        """Request Session termination; acceptance is not a terminal state."""
        return await self._request(
            "POST",
            f"/invocations/{_path_component(invocation_id, 'invocation_id')}/stop",
            payload=None,
            expected_statuses={200, 202},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected_statuses: set[int],
    ) -> dict[str, Any]:
        from PhyAgentOS.forge.interaction.client import check_transport_dispatch
        check_transport_dispatch(self, method, path)
        try:
            response = await self._client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise ForgeToolAPITimeoutError(
                f"Forge Gateway Tool API {method} {path} timed out; remote state is unknown"
            ) from exc
        except httpx.HTTPError as exc:
            raise ForgeToolAPIError(
                f"Forge Gateway Tool API {method} {path} transport failed: {exc}"
            ) from exc

        data = _decode_object(response, path)
        if response.status_code not in expected_statuses or data.get("ok") is not True:
            error = data.get("error")
            error_data = error if isinstance(error, dict) else {}
            message = (
                error_data.get("message")
                or data.get("msg")
                or data.get("message")
                or f"unexpected HTTP {response.status_code}"
            )
            raise ForgeToolAPIError(
                f"Forge Gateway Tool API {path} rejected request: {message}",
                status_code=response.status_code,
                error_code=_optional_str(error_data.get("code")),
                retryable=(
                    error_data.get("retryable")
                    if isinstance(error_data.get("retryable"), bool)
                    else None
                ),
                payload=data,
            )
        if not isinstance(data.get("data"), dict):
            raise ForgeToolAPIError(
                f"Forge Gateway Tool API {path} returned invalid data",
                status_code=response.status_code,
                payload=data,
            )
        return data


def _decode_object(response: httpx.Response, path: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ForgeToolAPIError(
            f"Forge Gateway Tool API {path} returned non-JSON response",
            status_code=response.status_code,
        ) from exc
    if not isinstance(data, dict):
        raise ForgeToolAPIError(
            f"Forge Gateway Tool API {path} returned a non-object payload",
            status_code=response.status_code,
        )
    return data


def _path_component(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return quote(value, safe="")


def _invoke_payload(
    arguments: dict[str, Any] | None,
    *,
    caller_id: str | None,
    timeout_ms: int | None,
) -> dict[str, Any]:
    if arguments is not None and not isinstance(arguments, dict):
        raise TypeError("arguments must be a dict")
    payload: dict[str, Any] = {"arguments": arguments or {}}
    if caller_id is not None:
        if not isinstance(caller_id, str) or not caller_id.strip():
            raise ValueError("caller_id must be a non-empty string")
        payload["caller_id"] = caller_id
    if timeout_ms is not None:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms must be a positive integer")
        payload["timeout_ms"] = timeout_ms
    return payload


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


# Compatibility aliases for concise and service-oriented imports.
ForgeToolAPITimeout = ForgeToolAPITimeoutError
GatewayToolClient = ForgeToolClient
ForgeGatewayToolClient = ForgeToolClient


__all__ = [
    "ForgeToolAPIError",
    "ForgeToolAPITimeout",
    "ForgeToolAPITimeoutError",
    "ForgeToolClient",
    "ForgeGatewayToolClient",
    "GatewayToolClient",
]

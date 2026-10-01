"""Bounded Forge transport. Business receipts and invocation states stay distinct."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol


class InteractionClient(Protocol):
    async def start(self, arguments: dict) -> dict: ...
    async def snapshot(self, arguments: dict) -> dict: ...
    async def submit(self, arguments: dict) -> dict: ...
    async def status(self, session_id: str) -> dict: ...
    async def stop(self, session_id: str) -> dict: ...


class InteractionTransportError(RuntimeError):
    def __init__(
        self, message: str, *, invocation_id: str | None = None, gateway_status: str | None = None
    ):
        super().__init__(message)
        self.invocation_id = invocation_id
        self.gateway_status = gateway_status


class GatewayUnknownError(InteractionTransportError):
    def __init__(self, message: str, *, invocation_id: str | None = None):
        super().__init__(message, invocation_id=invocation_id, gateway_status="unknown")


def query_outputs(body: dict) -> dict:
    """Query replies use a different envelope from invocation results."""
    try:
        result = body["data"]["response"]["result"]
        outputs = result["outputs"]
    except (KeyError, TypeError) as exc:
        raise InteractionTransportError("Malformed Forge Query result") from exc
    if result.get("status") not in (None, "succeeded") or not isinstance(outputs, dict):
        raise InteractionTransportError("Query did not return successful business outputs")
    from .contracts import canonical_json

    if len(canonical_json(outputs).encode()) > 1024 * 1024:
        raise InteractionTransportError("interaction query output exceeds limit")
    return outputs


def invocation_outputs(body: dict) -> dict | None:
    try:
        data = body["data"]
        if data["status"] == "pending":
            return None
        if data["status"] != "available":
            raise ValueError("Unsupported result status")
        result = data["result"]
        if not isinstance(result, dict):
            raise ValueError("Malformed invocation result")
        if result.get("status") == "unknown":
            raise GatewayUnknownError("Gateway reports unknown invocation result")
        if result["status"] != "succeeded" or not isinstance(result["outputs"], dict):
            raise ValueError("Submission tool did not succeed")
        return result["outputs"]
    except (KeyError, TypeError, ValueError) as exc:
        raise InteractionTransportError("Malformed or failed Forge invocation result") from exc


# Only the trusted adapter enters this context, for one bounded operation.

_SCOPE = ContextVar("interaction_call", default=None)


@contextmanager
def trusted_call(coordinator, task_id, runtime):
    token = _SCOPE.set((coordinator, task_id, runtime))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def trusted_runtime(coordinator, task_id):
    scope = _SCOPE.get()
    if scope and scope[0] is coordinator and scope[1] == task_id:
        return scope[2]
    return None


class CoordinatorInteractionClient:
    """The only interaction transport: existing intent/identity governance."""

    def __init__(self, service, task_id, runtime, extension):
        self.service, self.coordinator = service, service.coordinator
        self.task_id, self.runtime = task_id, runtime
        self.contract = extension["contract"]
        self.observer = None

    def observe_invocations(self, observer):
        self.observer = observer

    async def identity(self):
        return await self.service.validate(self.task_id, self.runtime)

    async def start(self, arguments):
        await self.identity()
        async with self.service.lock(self.task_id):
            with trusted_call(self.coordinator, self.task_id, self.runtime):
                body = await self.coordinator.start_session(
                    self.task_id, self.contract["session_tool"], arguments
                )
            invocation = body["data"]["invocation_id"]
            if self.observer:
                self.observer("run", arguments, invocation)
            return {"session_invocation_id": invocation, "gateway": body["data"]}

    async def snapshot(self, arguments):
        from .contracts import validate_query

        validate_query(arguments)
        await self.identity()
        task = self.coordinator.get_task(self.task_id)
        if arguments.get("task_id") != self.task_id:
            raise ValueError("cross-task query")
        runs = self.service.store.runs(self.task_id)
        run = next(
            (
                r
                for r in runs
                if r["interaction_run_id"] == arguments.get("interaction_run_id")
                or r["start_arguments"]["start_id"] == arguments.get("start_id")
            ),
            None,
        )
        if run is None or arguments["revision_id"] != run["revision_id"]:
            raise ValueError("cross-run query")
        if arguments.get("session_invocation_id") not in (None, run["session_invocation_id"]):
            raise ValueError("cross-session query")
        tool = task.primary_skill_binding.tool(self.contract["snapshot_tool"])
        if tool is None or tool.semantics != "query":
            raise ValueError("snapshot is not bound")
        body = await self.runtime.client.invoke_query_tool(
            tool.tool_id, arguments, caller_id=f"paos:{self.task_id}:interaction-monitor"
        )
        return query_outputs(body)

    async def submit(self, arguments):
        await self.identity()
        async with self.service.lock(self.task_id):
            self.service.assert_dispatch(self.task_id)
            with trusted_call(self.coordinator, self.task_id, self.runtime):
                body = await self.coordinator.start_action(
                    self.task_id, self.contract["submission_tool"], arguments
                )
            invocation = body["data"]["invocation_id"]
            if self.observer:
                self.observer("submit_decision", arguments, invocation)
        for _ in range(50):
            result = await self.runtime.client.invocation_result(invocation)
            with trusted_call(self.coordinator, self.task_id, self.runtime):
                self.coordinator.observe_action(self.task_id, invocation, result)
            outputs = invocation_outputs(result)
            if outputs is not None:
                return {**outputs, "transport_invocation_id": invocation}
            await asyncio.sleep(self.coordinator.config.poll_interval_s)
        raise InteractionTransportError("submission receipt timeout", invocation_id=invocation)

    async def status(self, invocation):
        task = self.coordinator.get_task(self.task_id)
        record = next((r for r in task.execution_records if r.invocation_id == invocation), None)
        if record is None:
            raise GatewayUnknownError(
                "invocation has no original Coordinator intent", invocation_id=invocation
            )
        try:
            body = await self.runtime.client.invocation_status(invocation)
        except Exception as exc:
            raise GatewayUnknownError(
                "original invocation unavailable", invocation_id=invocation
            ) from exc
        with trusted_call(self.coordinator, self.task_id, self.runtime):
            if record.semantics == "session":
                self.coordinator.observe_session(self.task_id, invocation, body)
            else:
                self.coordinator.observe_action(self.task_id, invocation, body)
        return body["data"]

    async def stop(self, invocation):
        # Never follow DynamicForgeToolClient to a replacement Runtime.
        async with self.service.lock(self.task_id):
            with trusted_call(self.coordinator, self.task_id, self.runtime):
                return await self.coordinator.stop_session(self.task_id, invocation)


def check_transport_dispatch(client, method, path):
    """Last synchronous check after ToolSpec GET, immediately before HTTP dispatch."""
    scope = _SCOPE.get()
    if scope is None or method != "POST" or not path.endswith(":invoke"):
        return
    coordinator, task_id, runtime = scope
    if runtime.client is not client:
        raise RuntimeError("interaction attempted an unpinned transport")
    current = coordinator.binding_resolver.runtime_registry.current()
    if current is None or (
        current.runtime_instance_id,
        current.gateway_identity,
        current.client,
    ) != (runtime.runtime_instance_id, runtime.gateway_identity, runtime.client):
        raise RuntimeError("interaction Runtime changed before dispatch")
    coordinator.interaction.assert_dispatch(task_id)

"""Host capabilities, pinned transport and query-only uncertain-delivery recovery."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from PhyAgentOS.forge.interaction.client import invocation_outputs, query_outputs, trusted_call
from PhyAgentOS.forge.interaction.contracts import digest

_SCOPE = ContextVar("benchmark_capability", default=None)
SCOPE_KEYS = (
    "task_id",
    "run_id",
    "subtask_id",
    "attempt_id",
    "worker_id",
    "world_epoch",
    "binding_digest",
)


@dataclass(frozen=True)
class Capability:
    owner: object
    task_id: str
    child_id: str | None = None


def scope_for(service, task_id):
    value = _SCOPE.get()
    return value if value and value[0].service is service and value[1].task_id == task_id else None


class BenchmarkClient:
    def __init__(self, service, task_id, runtime, owner):
        self.service, self.task_id, self.runtime, self.owner = service, task_id, runtime, owner
        self.coordinator, self.store = service.coordinator, service.store
        self.binding = self.store.binding(task_id)
        self.contract = self.binding["extension"]["contract"]
        self.root_capability = Capability(owner, task_id)

    def child_capability(self, child):
        if child["task_id"] != self.task_id or not self.store.get("subtask", child["subtask_id"]):
            raise RuntimeError("unreserved child")
        return Capability(self.owner, self.task_id, child["subtask_id"])

    def check(self, cap, arguments, *, cleanup=False):
        if (
            cap.owner is not self.owner
            or cap.task_id != self.task_id
            or not self.store.owns(self.owner)
        ):
            raise RuntimeError("invalid benchmark capability")
        run = self.store.get("run", self.task_id)
        if run is None or self.store.binding(self.task_id) != self.binding:
            raise RuntimeError("benchmark binding changed")
        for key in ("task_id", "run_id", "binding_digest"):
            if arguments.get(key) != run[key]:
                raise RuntimeError("cross-root benchmark call")
        if cap.child_id:
            child = self.store.get("subtask", cap.child_id)
            if child is None or any(arguments.get(k) != child[k] for k in SCOPE_KEYS):
                raise RuntimeError("cross-episode capability")
            if not cleanup and (
                child["settled"] or child["cancelled"] or child["state"] == "unknown"
            ):
                raise RuntimeError("episode admission closed")
            if arguments.get("operation") == "decision.submit":
                last = child.get("snapshot")
                if not last or (
                    arguments.get("observation_version"),
                    arguments.get("observation_digest"),
                ) != (last["observation_version"], digest(last["observation"])):
                    raise RuntimeError("stale decision observation")
        elif "subtask_id" in arguments:
            raise RuntimeError("root authority cannot impersonate an episode")
        if not cleanup:
            self.service.assert_dispatch(self.task_id)

    @contextmanager
    def scope(self, cap, arguments, *, cleanup=False):
        self.check(cap, arguments, cleanup=cleanup)
        token = _SCOPE.set((self, cap, arguments, cleanup))
        try:
            with trusted_call(self.coordinator, self.task_id, self.runtime):
                yield
        finally:
            _SCOPE.reset(token)

    async def start(self, arguments):
        await self.service.validate(self.task_id, self.runtime)
        with self.scope(self.root_capability, arguments):
            return await self.coordinator.start_session(
                self.task_id, self.contract["session_tool"], arguments
            )

    async def submit(self, cap, arguments, *, cleanup=False):
        if not cleanup:
            await self.service.validate(self.task_id, self.runtime)
        elif arguments.get("operation") != "episode.stop":
            raise RuntimeError("cleanup capability permits stop only")
        self.check(cap, arguments, cleanup=cleanup)
        request_id = arguments["request_id"]
        existing = self.store.get("intent", request_id)
        if existing:
            raise RuntimeError("intent already exists; reconcile instead of resending")
        self.store.put(
            "intent",
            request_id,
            self.task_id,
            {
                "request_id": request_id,
                "arguments": arguments,
                "digest": digest(arguments),
                "invocation_id": None,
                "receipt": None,
                "state": "dispatching",
            },
            create=True,
        )
        try:
            with self.scope(cap, arguments, cleanup=cleanup):
                if cleanup:
                    # Cancelled roots cannot append new ordinary task intentions. The stop
                    # intent above is a distinct, bounded control obligation in this journal.
                    response = await self.runtime.client.invoke_action(
                        self.contract["submission_tool"],
                        arguments,
                        caller_id=f"paos:{self.task_id}:benchmark-stop:{request_id}",
                    )
                else:
                    response = await self.coordinator.start_action(
                        self.task_id, self.contract["submission_tool"], arguments
                    )
            invocation = response["data"]["invocation_id"]
            self.store.update("intent", request_id, invocation_id=invocation, state="accepted")
            result = await self.runtime.client.invocation_result(invocation)
            if not cleanup:
                with trusted_call(self.coordinator, self.task_id, self.runtime):
                    self.coordinator.observe_action(self.task_id, invocation, result)
            receipt = invocation_outputs(result)
            if receipt is None:
                raise RuntimeError("receipt pending")
            self.accept_receipt(request_id, receipt)
            return receipt
        except BaseException:
            self.store.update("intent", request_id, state="unknown")
            raise

    def accept_receipt(self, request_id, receipt):
        intent = self.store.get("intent", request_id)
        args = intent["arguments"]
        if (
            receipt.get("request_id") != request_id
            or receipt.get("payload_digest") != intent["digest"]
            or any(receipt.get(k) != args[k] for k in SCOPE_KEYS)
            or receipt.get("accepted") is not True
        ):
            raise RuntimeError("receipt does not match original intent")
        self.store.update("intent", request_id, receipt=receipt, state="reconciled")

    async def reconcile_invocation(self, request_id):
        """Record separate GET evidence; never rewrite a lost original invocation fact."""
        intent = self.store.get("intent", request_id)
        receipt = intent["receipt"]
        invocation = intent["invocation_id"] or receipt.get("transport_invocation_id")
        if not isinstance(invocation, str) or not invocation:
            raise RuntimeError("receipt has no Gateway execution reference")
        status = await self.runtime.client.invocation_status(invocation)
        data = status["data"]
        if (data.get("phase") or data.get("status")) != "succeeded":
            raise RuntimeError("Gateway invocation remains unresolved")
        result = await self.runtime.client.invocation_result(invocation)
        if invocation_outputs(result) != receipt:
            raise RuntimeError("Gateway result does not corroborate scoped receipt")
        self.store.update(
            "intent",
            request_id,
            reconciliation={
                "invocation_id": invocation,
                "gateway_status": "succeeded",
                "receipt_digest": digest(receipt),
                "status_response": status,
                "result_response": result,
            },
        )
        self.runtime.invocation_ids.discard(invocation)

    async def query(self, cap, arguments):
        self.check(cap, arguments, cleanup=True)
        # Monitoring stays on the ORIGINAL authorized transport even after cancellation
        # or Runtime replacement. No diagnostic Query path is used.
        response = await self.runtime.client.invoke_query_tool(
            self.contract["snapshot_tool"],
            arguments,
            caller_id=f"paos:{self.task_id}:benchmark-monitor",
        )
        return query_outputs(response)

    async def read_invocation(self, invocation, semantics, operation):
        task = self.coordinator.get_task(self.task_id)
        if not any(
            r.invocation_id == invocation and r.semantics == semantics
            for r in task.execution_records
        ):
            raise RuntimeError("invocation does not belong to benchmark root")
        response = await (
            self.runtime.client.invocation_status(invocation)
            if operation == "status"
            else self.runtime.client.invocation_result(invocation)
        )
        with trusted_call(self.coordinator, self.task_id, self.runtime):
            observer = (
                self.coordinator.observe_session
                if semantics == "session"
                else self.coordinator.observe_action
            )
            observer(self.task_id, invocation, response)
        return response

    async def stop(self, invocation):
        with trusted_call(self.coordinator, self.task_id, self.runtime):
            return await self.coordinator.stop_session(self.task_id, invocation)


def check_dispatch(coordinator, task_id, client):
    scope = scope_for(coordinator.benchmark, task_id)
    if scope is None:
        raise RuntimeError("benchmark dispatch lacks host capability")
    adapter, cap, arguments, cleanup = scope
    if adapter.runtime.client is not client:
        raise RuntimeError("benchmark transport changed")
    adapter.check(cap, arguments, cleanup=cleanup)
    if not cleanup:
        runtime = coordinator.binding_resolver.runtime_registry.current()
        if runtime is not adapter.runtime:
            raise RuntimeError("benchmark Runtime changed before dispatch")

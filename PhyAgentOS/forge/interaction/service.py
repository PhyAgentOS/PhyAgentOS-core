"""Opt-in interaction governance shared by tasks, tools and the supervisor."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import httpx

from .binding import InteractionBinding, read_contract, verified_extension
from .client import trusted_call, trusted_runtime
from .contracts import digest
from .runner_store import RunnerStore

SAFE_TOOLS = frozenset(
    {
        "activate_skill",
        "forge_task_create",
        "forge_task_get",
        "forge_task_cancel",
        "forge_task_finalize",
        "forge_task_begin_revision",
        "forge_tool_start_session",
        "forge_tool_session_status",
        "forge_tool_session_result",
        "forge_tool_stop_session",
        "message",
    }
)


class InProcessInteractionDeployment:
    """Test-only execution authority; never authorizes a network transport.

    Production deployments must supply a separately reviewed isolation adapter.
    This release deliberately has no boolean or URL that enables real control.
    """

    def __init__(self, *, worlds, decide, limits=None):
        self.worlds, self.decide, self.limits = worlds, decide, limits

    def authorize(self, runtime, world_id):
        transport = runtime.client._client._transport
        if type(transport) is not httpx.MockTransport or world_id not in self.worlds:
            raise RuntimeError("only an explicitly injected in-process fake world is authorized")
        return dict(self.worlds[world_id])


class InteractionService:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.path = coordinator.workspace / ".paos" / "interaction" / "interaction.sqlite3"
        self._store = None
        self._locks = {}
        self.unsafe_calls = 0
        self.supervisor = None
        self.deployment = None
        self.long_lived = False
        self.closing = False

    @property
    def store(self):
        if self._store is None:
            self._store = RunnerStore(self.path)
        elif not self.path.exists():
            raise RuntimeError("interaction journal disappeared; refusing downgrade")
        return self._store

    def lock(self, task_id):
        return self._locks.setdefault(task_id, asyncio.Lock())

    def extension(self, task=None):
        resolver = self.coordinator.binding_resolver
        if resolver is None:
            return None
        if task is not None and task.primary_skill_binding is not None:
            name = task.primary_skill_binding.skill_name
        else:
            runtime = resolver.runtime_registry.current()
            if runtime is None:
                return None
            name = runtime.skill_name
        return verified_extension(resolver.catalog.get(name))

    def managed(self, task_id):
        if self.coordinator.store.has_event(task_id, "interaction_bound"):
            if not self.path.exists() or self.store.binding(task_id) is None:
                raise RuntimeError("interaction binding journal missing; refusing downgrade")
            return True
        if self._store is not None:
            self.store  # Verify the open journal still exists.
        if self.path.exists() and self.store.binding(task_id) is not None:
            return True
        return self.extension(self.coordinator.get_task(task_id)) is not None

    def restricted(self):
        try:
            if self._store is not None:
                self.store
            if self.path.exists() and any(not r.get("settled") for r in self.store.runs()):
                return True
            resolver = self.coordinator.binding_resolver
            runtime = resolver.runtime_registry.current() if resolver else None
            return bool(
                runtime and read_contract(resolver.catalog.get(runtime.skill_name).bundle_root)
            )
        except Exception:
            return True

    def allows(self, name):
        return not self.restricted() or name in SAFE_TOOLS

    @contextmanager
    def tool_call(self, name):
        if not self.allows(name):
            raise RuntimeError("tool is unavailable in governed interaction mode")
        unsafe = name not in SAFE_TOOLS
        self.unsafe_calls += int(unsafe)
        interrupted = False
        try:
            yield
        except asyncio.CancelledError:
            interrupted = unsafe
            raise
        finally:
            # Cancelling a coroutine is not proof its subprocess/network operation ended.
            if not interrupted:
                self.unsafe_calls -= int(unsafe)

    def guard_call(self, task_id, tool_id, semantics):
        if self.managed(task_id):
            if trusted_runtime(self.coordinator, task_id) is None:
                raise RuntimeError("interaction Tool requires the trusted Runner")
            binding = self.store.binding(task_id)
            if binding is None:
                raise RuntimeError("interaction binding is missing")
            key = {
                "session": "session_tool",
                "action": "submission_tool",
                "query": "snapshot_tool",
            }[semantics]
            if binding["extension"]["contract"][key] != tool_id:
                raise RuntimeError("tool is outside interaction scope")

    async def freeze(self, task):
        extension = self.extension(task)
        if extension is None:
            return
        if self.path.exists() and any(not r.get("settled") for r in self.store.runs()):
            raise RuntimeError("unresolved interaction prevents a new root task")
        if not self.coordinator.config.interaction.enabled:
            raise RuntimeError("forge.interaction.enabled is false")
        if task.verification.mode not in {"enforce", "recovery"}:
            raise RuntimeError("interaction requires enforce/recovery root verification")
        binding = task.primary_skill_binding
        if binding is None or not binding.gateway_identity:
            raise RuntimeError("interaction requires a live Runtime/Gateway binding")
        runtime = self.coordinator.binding_resolver.runtime_registry.current()
        endpoints = set()
        specs = {}
        for key, semantics in [
            ("session_tool", "session"),
            ("snapshot_tool", "query"),
            ("submission_tool", "action"),
        ]:
            tool_id = extension["contract"][key]
            await self.coordinator.binding_resolver.validate_tool(
                binding, tool_id, semantics, runtime=runtime
            )
            spec = (await runtime.client.get_tool(tool_id))["data"]
            endpoints.add(spec.get("endpoint_id"))
            specs[tool_id] = digest(spec)
        if len(endpoints) != 1 or None in endpoints:
            raise RuntimeError("interaction operations must share an endpoint")
        value = {
            "task_id": task.task_id,
            "binding_id": binding.binding_id,
            "extension": extension,
            "specs": specs,
        }
        value["binding_digest"] = digest(value)
        self.store.bind(task.task_id, InteractionBinding.model_validate(value).model_dump())

    async def validate(self, task_id, runtime):
        task = self.coordinator.get_task(task_id)
        saved = self.store.binding(task_id)
        if not saved or saved["binding_id"] != task.primary_skill_binding.binding_id:
            raise RuntimeError("missing or mismatched interaction binding")
        if self.extension(task) != saved["extension"]:
            raise RuntimeError("interaction bundle changed")
        resolver = self.coordinator.binding_resolver
        current = resolver.runtime_registry.current()
        if current is None or (
            current.runtime_instance_id,
            current.gateway_identity,
            current.client,
        ) != (runtime.runtime_instance_id, runtime.gateway_identity, runtime.client):
            raise RuntimeError("interaction Runtime changed")
        for key, semantics in [
            ("session_tool", "session"),
            ("snapshot_tool", "query"),
            ("submission_tool", "action"),
        ]:
            await resolver.validate_tool(
                task.primary_skill_binding,
                saved["extension"]["contract"][key],
                semantics,
                runtime=runtime,
            )
        return {
            "binding_digest": saved["binding_digest"],
            "runtime_instance_id": runtime.runtime_instance_id,
            "gateway_identity": runtime.gateway_identity,
        }

    def assert_dispatch(self, task_id):
        task = self.coordinator.get_task(task_id)
        if self.closing or task.cancellation_requested or task.status.value != "executing":
            raise RuntimeError("interaction task no longer accepts dispatch")
        if not self.coordinator.config.interaction.enabled:
            raise RuntimeError("interaction disabled; only reconciliation is allowed")
        if any(
            r.get("settled") is not True
            and (
                r.get("cancelled")
                or r.get("recovery_required")
                or r.get("gateway_uncertain")
                or r["state"] == "blocked"
            )
            for r in self.store.runs(task_id)
        ):
            raise RuntimeError("unresolved interaction prevents dispatch")

    def assert_settled(self, task_id):
        if not self.managed(task_id):
            return
        runs = self.store.runs(task_id)
        task = self.coordinator.get_task(task_id)
        if not runs and task.cancellation_requested and not task.execution_records:
            return
        if not runs or any(not r.get("settled") for r in runs):
            raise RuntimeError("interaction still requires stop/reconciliation")
        task = self.coordinator.get_task(task_id)
        if task.verification.mode not in {"enforce", "recovery"}:
            raise RuntimeError("interaction verification cannot be downgraded")
        binding = task.primary_skill_binding
        current = self.coordinator.binding_resolver.runtime_registry.current()
        if current is None or (current.runtime_instance_id, current.gateway_identity) != (
            binding.runtime_instance_id,
            binding.gateway_identity,
        ):
            raise RuntimeError("interaction Runtime changed before finalization")
        if self.extension(task) != self.store.binding(task_id)["extension"]:
            raise RuntimeError("interaction evidence binding changed")
        for run in runs:
            if digest(run.get("last_snapshot")) != run.get("settled_snapshot_digest"):
                raise RuntimeError("interaction evidence changed after settlement")

    def summary(self, task_id):
        if not self.managed(task_id):
            return None
        return [
            {
                "interaction_run_id": r["interaction_run_id"],
                "state": r["state"],
                "reason": r.get("reason"),
                "settled": r.get("settled", False),
                "model_calls": r["model_calls"],
            }
            for r in self.store.runs(task_id)
        ]

    def enable_in_process(self, deployment, *, long_lived=True, notify=None):
        from PhyAgentOS.agent.interaction.supervisor import InteractionSupervisor

        if type(deployment) is not InProcessInteractionDeployment:
            raise RuntimeError("production isolation adapters are not enabled in this release")
        self.deployment, self.long_lived = deployment, long_lived
        self.supervisor = InteractionSupervisor(self, notify=notify)
        return self.supervisor

    async def start(self, task_id, tool_id, arguments, ownership):
        if self.supervisor is None or self.deployment is None:
            raise RuntimeError("interaction control-plane deployment is not authorized")
        if not self.long_lived:
            raise RuntimeError(
                "use gateway or interactive CLI; one-shot CLI cannot monitor a Session"
            )
        if ownership != "task" or self.unsafe_calls:
            raise RuntimeError("interaction requires task ownership and no unsafe tools in flight")
        saved = self.store.binding(task_id)
        if saved is None or tool_id != saved["extension"]["contract"]["session_tool"]:
            raise RuntimeError("interaction Session is not frozen")
        if set(arguments) != {"world_id"}:
            raise ValueError(
                "interaction start accepts only world_id; identity and budgets are host-owned"
            )
        if any(not r.get("settled") for r in self.store.runs()):
            raise RuntimeError("a prior interaction still needs reconciliation")
        task = self.coordinator.get_task(task_id)
        if any(r["revision_id"] == task.active_revision_id for r in self.store.runs(task_id)):
            raise RuntimeError("a new interaction Session requires a new PlanRevision")
        self.assert_dispatch(task_id)
        return await self.supervisor.start(task_id, arguments["world_id"])

    async def cancel(self, task_id, reason):
        from PhyAgentOS.forge.task import AgentTaskStatus

        current = self.coordinator.get_task(task_id)
        if current.terminal and all(r.get("settled") for r in self.store.runs(task_id)):
            return current

        # No await before the durable gate closes. All subsequently admitted work checks it.
        never_started = not self.store.runs(task_id) and not current.execution_records

        def mark(task):
            if task.terminal:
                return
            task.cancellation_requested = True
            task.status = AgentTaskStatus.CANCELLED if never_started else AgentTaskStatus.CANCELLING

        self.coordinator.store.update(task_id, mark, event_type="interaction_cancel_requested")
        for run in self.store.runs(task_id):
            if not run.get("settled"):
                self.store.update_run(run["interaction_run_id"], cancelled=True, needs_stop=True)
        if self.supervisor:
            await self.supervisor.cancel(task_id, reason)
        result = self.coordinator.get_task(task_id)
        if never_started:
            self.coordinator._schedule_experience(result)
        return result

    async def validate_finalization(self, task_id):
        if not self.managed(task_id):
            return
        self.assert_settled(task_id)
        runtime = self.coordinator.binding_resolver.runtime_registry.current()
        await self.validate(task_id, runtime)
        self.assert_settled(task_id)

    async def read_invocation(self, task_id, invocation_id, semantics, operation):
        task = self.coordinator.get_task(task_id)
        record = next(
            (
                r
                for r in task.execution_records
                if r.invocation_id == invocation_id and r.semantics == semantics
            ),
            None,
        )
        if record is None:
            raise RuntimeError("invocation does not belong to task")
        if self.supervisor is None:
            raise RuntimeError("original interaction control path unavailable")
        runner = next(
            (r for r in self.supervisor.runners.values() if r.state["task_id"] == task_id), None
        )
        if runner is None:
            raise RuntimeError("original interaction control path unavailable")
        client = runner.client.runtime.client
        response = await (
            client.invocation_status(invocation_id)
            if operation == "status"
            else client.invocation_result(invocation_id)
        )
        observer = (
            self.coordinator.observe_session
            if semantics == "session"
            else self.coordinator.observe_action
        )
        with trusted_call(self.coordinator, task_id, runner.client.runtime):
            observer(task_id, invocation_id, response)
        return response

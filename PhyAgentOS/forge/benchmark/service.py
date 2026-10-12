"""Benchmark governance at existing task and tool boundaries."""

import httpx

from PhyAgentOS.forge.interaction.contracts import digest
from PhyAgentOS.forge.interaction.service import InteractionService

from .binding import verified_extension
from .contracts import BenchmarkPlan
from .store import BenchmarkStore


class InProcessBenchmarkDeployment:
    """Explicit fake-only authority. Network deployments require isolation review."""

    def __init__(
        self,
        *,
        workers,
        decide,
        reflect=None,
        validate_lesson=None,
        artifacts=None,
        poll_interval_s=0.01,
        control_timeout_s=2,
    ):
        if not workers or len(set(workers)) != len(workers):
            raise ValueError("distinct worker slots are required")
        self.workers, self.decide = tuple(workers), decide
        self.reflect, self.validate_lesson = reflect, validate_lesson
        self.artifacts = artifacts
        self.poll_interval_s, self.control_timeout_s = poll_interval_s, control_timeout_s

    def authorize(self, runtime):
        if type(runtime.client._client._transport) is not httpx.MockTransport:
            raise RuntimeError("benchmark authorizes only injected in-process fake transport")


class BenchmarkService(InteractionService):
    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.path = coordinator.workspace / ".paos" / "benchmark" / "benchmark.sqlite3"

    @property
    def store(self):
        if self._store is None:
            self._store = BenchmarkStore(self.path)
        elif not self.path.exists():
            raise RuntimeError("benchmark journal disappeared; refusing downgrade")
        return self._store

    def extension(self, task=None):
        resolver = self.coordinator.binding_resolver
        if resolver is None:
            return None
        runtime = resolver.runtime_registry.current()
        name = (
            task.primary_skill_binding.skill_name
            if task and task.primary_skill_binding
            else runtime.skill_name
            if runtime
            else None
        )
        return verified_extension(resolver.catalog.get(name)) if name else None

    def managed(self, task_id):
        if self.coordinator.store.has_event(task_id, "benchmark_bound"):
            if not self.path.exists() or not self.store.binding(task_id):
                raise RuntimeError("benchmark binding journal missing")
            return True
        if self._store is not None:
            self.store
        if self.path.exists() and self.store.binding(task_id):
            return True
        return self.extension(self.coordinator.get_task(task_id)) is not None

    def restricted(self):
        try:
            return bool((self.path.exists() and self.store.unresolved()) or self.extension())
        except Exception:
            return True

    async def freeze(self, task, plan):
        self.guard_unresolved(task.task_id)
        extension = self.extension(task)
        if extension is None:
            if plan is not None:
                raise ValueError("benchmark_plan requires a benchmark Skill")
            return
        if not self.coordinator.config.benchmark.enabled:
            raise RuntimeError("forge.benchmark.enabled is false")
        plan = BenchmarkPlan.model_validate(plan)
        if (
            task.verification.evidence_policy.profile != "benchmark_evaluation"
            or task.verification.success_criteria != ["evaluation_complete"]
        ):
            raise ValueError(
                "benchmark verification must explicitly require evaluation_complete with benchmark_evaluation profile"
            )
        if task.verification.mode != "enforce":
            raise ValueError("benchmark requires enforce evaluation-completeness verification")
        if self.path.exists() and self.store.unresolved():
            raise RuntimeError("unresolved benchmark prevents new root")
        binding = task.primary_skill_binding
        runtime = self.coordinator.binding_resolver.runtime_registry.current()
        if binding is None or runtime is None or not binding.gateway_identity:
            raise RuntimeError("benchmark requires live binding")
        specs, endpoints = {}, set()
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
            specs[tool_id] = digest(spec)
            endpoints.add(spec.get("endpoint_id"))
        if len(endpoints) != 1 or None in endpoints:
            raise ValueError("benchmark operations must share an endpoint")
        value = {
            "task_id": task.task_id,
            "binding_id": binding.binding_id,
            "extension": extension,
            "specs": specs,
            "plan": plan.model_dump(),
            "verification": task.verification.model_dump(),
        }
        value["binding_digest"] = digest(value)
        self.store.bind(task.task_id, value)

    async def validate(self, task_id, runtime):
        try:
            result = await super().validate(task_id, runtime)
        except Exception:
            self.closing = True
            raise
        if (
            self.coordinator.get_task(task_id).verification.model_dump()
            != self.store.binding(task_id)["verification"]
        ):
            raise RuntimeError("benchmark verification contract changed")
        return result

    def guard_unresolved(self, task_id):
        if self._store is not None:
            self.store
        if self.path.exists() and any(r["task_id"] != task_id for r in self.store.unresolved()):
            raise RuntimeError("unresolved benchmark prevents another task from dispatching")

    def guard_call(self, task_id, tool_id, semantics):
        self.guard_unresolved(task_id)
        if not self.managed(task_id):
            return
        from .client import scope_for

        scope = scope_for(self, task_id)
        if scope is None:
            raise RuntimeError("benchmark Tool requires scoped supervisor authority")
        key = {"session": "session_tool", "query": "snapshot_tool", "action": "submission_tool"}[
            semantics
        ]
        if self.store.binding(task_id)["extension"]["contract"][key] != tool_id:
            raise RuntimeError("tool outside benchmark scope")

    def assert_dispatch(self, task_id):
        task = self.coordinator.get_task(task_id)
        if (
            self.closing
            or self.coordinator.interaction.closing
            or task.cancellation_requested
            or task.status.value != "executing"
            or not self.coordinator.config.benchmark.enabled
        ):
            raise RuntimeError("benchmark admission closed")
        run = self.store.get("run", task_id)
        if run and run["admission_closed"]:
            raise RuntimeError("benchmark requires reconciliation")

    def assert_settled(self, task_id):
        if not self.managed(task_id):
            return
        run = self.store.get("run", task_id)
        task = self.coordinator.get_task(task_id)
        if run is None and task.cancellation_requested and not task.execution_records:
            return
        if run is None or not run["settled"]:
            raise RuntimeError("benchmark requires stop/reconciliation; resources retained")
        from .evidence import unknowns_resolved

        if not unknowns_resolved(self, task_id):
            raise RuntimeError("benchmark has unresolved Gateway facts")
        if any(not c["settled"] for c in self.store.list("subtask", task_id)):
            raise RuntimeError("benchmark child still unresolved")

    async def validate_finalization(self, task_id):
        if not self.managed(task_id):
            return
        self.assert_settled(task_id)
        runtime = self.coordinator.binding_resolver.runtime_registry.current()
        await self.validate(task_id, runtime)
        if not self.coordinator.get_task(task_id).cancellation_requested:
            from .evidence import checked_report

            checked_report(self, task_id)

    def summary(self, task_id):
        if not self.managed(task_id):
            return None
        run = self.store.get("run", task_id)
        return {
            "plan": self.store.binding(task_id)["plan"],
            "run": run,
            "rounds": self.store.list("round", task_id),
        }

    def enable_in_process(self, deployment, *, long_lived=True, notify=None):
        from PhyAgentOS.agent.benchmark.supervisor import BenchmarkSupervisor

        if type(deployment) is not InProcessBenchmarkDeployment:
            raise RuntimeError("production benchmark adapter not enabled")
        self.deployment, self.long_lived = deployment, long_lived
        self.supervisor = BenchmarkSupervisor(self, notify=notify)
        return self.supervisor

    async def start(self, task_id, tool_id, arguments, ownership):
        if self.supervisor is None or self.deployment is None:
            raise RuntimeError("benchmark control-plane deployment not authorized")
        if not self.long_lived:
            raise RuntimeError("one-shot CLI cannot monitor a benchmark Session")
        if ownership != "task" or arguments or self.coordinator.interaction.unsafe_calls:
            raise RuntimeError(
                "benchmark start takes no model arguments and requires idle unsafe tools"
            )
        if tool_id != self.store.binding(task_id)["extension"]["contract"]["session_tool"]:
            raise RuntimeError("benchmark Session is not frozen")
        return await self.supervisor.start(task_id)

    async def cancel(self, task_id, reason):
        from PhyAgentOS.forge.task import AgentTaskStatus

        current = self.coordinator.get_task(task_id)
        run = self.store.get("run", task_id)
        if current.terminal and (run is None or run["settled"]):
            return current

        def mark(task):
            task.cancellation_requested = True
            if not task.terminal:
                task.status = AgentTaskStatus.CANCELLING

        self.coordinator.store.update(task_id, mark, event_type="benchmark_cancel_requested")
        run = self.store.get("run", task_id)
        if run:
            self.store.update("run", task_id, admission_closed=True, needs_stop=True, reason=reason)
            if self.supervisor:
                await self.supervisor.cancel(task_id)
        else:
            self.coordinator.store.update(
                task_id,
                lambda t: setattr(t, "status", AgentTaskStatus.CANCELLED),
                event_type="benchmark_cancelled_before_start",
            )
            self.coordinator._schedule_experience(self.coordinator.get_task(task_id))
        return self.coordinator.get_task(task_id)

    async def read_invocation(self, task_id, invocation_id, semantics, operation):
        if self.supervisor is None or task_id not in self.supervisor.clients:
            raise RuntimeError("original benchmark control path unavailable")
        return await self.supervisor.clients[task_id].read_invocation(
            invocation_id, semantics, operation
        )

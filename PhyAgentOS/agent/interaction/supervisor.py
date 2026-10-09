"""Long-lived interaction tasks, independent of the conversational turn lock."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from PhyAgentOS.forge.interaction.client import CoordinatorInteractionClient
from PhyAgentOS.forge.interaction.contracts import PROTOCOL, digest
from PhyAgentOS.forge.interaction.runner_store import RunnerAlreadyOwnedError

from .runner import TERMINAL_SESSION, TERMINAL_STEP, AgentInteractionRunner


class InteractionSupervisor:
    def __init__(self, service, *, notify=None):
        self.service, self.notify = service, notify
        self.runners = {}
        self.jobs = {}

    def _runner(self, task_id, arguments, runtime):
        service = self.service
        saved = service.store.binding(task_id)
        client = CoordinatorInteractionClient(service, task_id, runtime, saved["extension"])
        task = service.coordinator.get_task(task_id)
        runner = AgentInteractionRunner(
            store=service.store,
            client=client,
            decide=service.deployment.decide,
            goal=task.verification.model_dump(mode="json"),
            proposal_schema=saved["extension"]["proposal_schema"],
            start_arguments=arguments,
            identity_probe=client.identity,
            limits=service.deployment.limits,
        )
        self.runners[runner.run_id] = runner
        return runner

    async def start(self, task_id, world_id):
        service = self.service
        task = service.coordinator.get_task(task_id)
        runtime = service.coordinator.binding_resolver.runtime_registry.current()
        identity = await service.validate(task_id, runtime)
        budget = service.deployment.authorize(runtime, world_id)
        service.assert_dispatch(task_id)
        arguments = {
            "protocol": PROTOCOL,
            "task_id": task_id,
            "revision_id": task.active_revision_id,
            "start_id": "start_" + uuid4().hex,
            "interaction_run_id": "run_" + uuid4().hex,
            "world_id": world_id,
            **identity,
            "budget": budget,
        }
        runner = self._runner(task_id, arguments, runtime)
        try:
            await runner.start()
        except BaseException:
            runner.close()
            raise
        self.jobs[runner.run_id] = asyncio.create_task(self._monitor(runner))
        return {
            "ok": True,
            "data": {
                "invocation_id": runner.state["session_invocation_id"],
                "interaction_run_id": runner.run_id,
                "status": runner.state["state"],
            },
        }

    async def _monitor(self, runner):
        try:
            while not self.service.closing:
                task = self.service.coordinator.get_task(runner.state["task_id"])
                if (
                    task.cancellation_requested
                    or task.terminal
                    or not self.service.coordinator.config.interaction.enabled
                ):
                    await runner.cancel()
                    if runner.state["state"] == "needs_verification":
                        await self._finish(runner)
                    else:
                        await self._announce(runner)
                    return
                state = await runner.tick()
                if state["state"] == "needs_verification":
                    await self._finish(runner)
                    return
                if state["state"] in {"blocked", "reconciling"}:
                    # Blocking proposals never releases the duty to request physical stop.
                    await runner._stop_and_check(state.get("reason", "reconciliation_required"))
                    if runner.state["state"] == "needs_verification":
                        await self._finish(runner)
                    else:
                        await self._announce(runner)
                    return
                if state["state"] in {"finished", "cancelled"}:
                    return
                await asyncio.sleep(runner.limits.poll_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runner.store.update_run(
                runner.run_id, state="blocked", needs_stop=True, reason=type(exc).__name__
            )
            try:
                await runner._stop_and_check("supervisor_error")
            finally:
                await self._announce(runner)
        finally:
            if runner.state.get("settled"):
                runner.close()

    async def _finish(self, runner):
        async with self.service.lock(runner.state["task_id"]):
            observed = await runner._read_state()
            reason = runner._finalization_blocker(observed)
            if reason not in (None, "user_cancelled"):
                runner._block(reason)
                return
            # Cancellation is a goal verdict, not permission to skip physical reconciliation.
            status, snapshot = observed
            if (
                status.get("status", status.get("phase")) not in TERMINAL_SESSION
                or snapshot.get("inflight_execution") is not None
                or any(s["status"] not in TERMINAL_STEP for s in runner.store.steps(runner.run_id))
            ):
                runner._block("physical_execution_not_settled")
                return
            try:
                await self._history(runner, snapshot)
                runner.store.update_run(
                    runner.run_id,
                    settled=True,
                    settled_at=time.time(),
                    settled_snapshot_digest=digest(snapshot),
                    needs_stop=False,
                )
                # Existing Coordinator owns the only authoritative root verdict and episode.
                task = await self.service.coordinator.finalize_task(runner.state["task_id"])
                runner.store.update_run(
                    runner.run_id,
                    state="cancelled" if task.cancellation_requested else "finished",
                    finalization={"task_status": task.status.value},
                )
            except Exception as exc:
                runner.store.update_run(runner.run_id, settled=False)
                runner._block("finalization_evidence_unavailable:" + type(exc).__name__)
        await self._announce(runner)

    async def _history(self, runner, snapshot):
        cursor = 0
        target = snapshot["executor_cursor"]
        history = []
        # A bounded full durable history check; never trust an SSE/HTTP retention window.
        for _ in range(100):
            page = await runner._io(
                runner.client.snapshot(
                    runner._query("history", after_executor_cursor=cursor, limit=100)
                )
            )
            if (
                page.get("gap") is not False
                or page.get("retention_watermark", 1) > cursor
                or not isinstance(page.get("events"), list)
            ):
                raise RuntimeError("interaction history is incomplete")
            for event in page["events"]:
                if type(event.get("cursor")) is not int or event["cursor"] <= cursor:
                    raise RuntimeError("interaction history has a gap")
                cursor = event["cursor"]
                history.append(event)
            if page.get("next_executor_cursor") != cursor:
                raise RuntimeError("interaction history cursor mismatch")
            if cursor >= target:
                if cursor != target:
                    raise RuntimeError("interaction changed during evidence collection")
                with runner.store.transaction():
                    runner.store.update_run(
                        runner.run_id,
                        verified_history=history,
                        verified_history_digest=digest(history),
                    )
                    runner.store.event(runner.run_id, "history_verified", {"cursor": cursor})
                return
            if not page["events"]:
                break
        raise RuntimeError("interaction history did not reach final snapshot")

    async def _announce(self, runner):
        state = runner.state
        notification = digest([state["state"], state.get("reason"), state.get("finalization")])
        if state.get("notification") == notification:
            return
        runner.store.update_run(runner.run_id, notification=notification)
        if self.notify:
            await self.notify(state["task_id"], self.service.summary(state["task_id"]))

    async def cancel(self, task_id, reason):
        for run_id, runner in list(self.runners.items()):
            if runner.state["task_id"] != task_id or runner.state.get("settled"):
                continue
            await runner.cancel()
            job = self.jobs.get(run_id)
            if runner.state["state"] == "needs_verification" and (job is None or job.done()):
                await self._finish(runner)

    async def recover(self):
        service = self.service
        if not service.path.exists():
            return
        for state in service.store.runs():
            if state.get("settled") or state["interaction_run_id"] in self.runners:
                continue
            runtime = service.coordinator.binding_resolver.runtime_registry.current()
            try:
                if not service.long_lived or service.closing:
                    raise RuntimeError("no long-lived supervisor available")
                service.deployment.authorize(runtime, state["start_arguments"]["world_id"])
                await service.validate(state["task_id"], runtime)
                runner = self._runner(state["task_id"], state["start_arguments"], runtime)
                await runner.recover()
                self.jobs[runner.run_id] = asyncio.create_task(self._monitor(runner))
            except RunnerAlreadyOwnedError:
                self.runners.pop(state["interaction_run_id"], None)
                raise
            except Exception as exc:
                service.store.update_run(
                    state["interaction_run_id"],
                    state="blocked",
                    needs_stop=True,
                    reason=type(exc).__name__,
                )

    async def shutdown(self):
        self.service.closing = True
        for runner in self.runners.values():
            if not runner.state.get("settled"):
                await self.service.cancel(runner.state["task_id"], "agent_shutdown")
        for job in self.jobs.values():
            if not job.done():
                job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        for runner in self.runners.values():
            runner.close()

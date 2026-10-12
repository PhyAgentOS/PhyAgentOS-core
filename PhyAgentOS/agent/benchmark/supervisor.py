"""One persistent owner; bounded concurrent children under one Forge Session."""

import asyncio
import time
from uuid import uuid4

from PhyAgentOS.forge.benchmark.client import BenchmarkClient
from PhyAgentOS.forge.interaction.contracts import digest

from .lessons import reflect_round
from .runner import EpisodeRunner


class BenchmarkSupervisor:
    def __init__(self, service, *, notify=None):
        self.service, self.store, self.notify = service, service.store, notify
        self.clients, self.monitors, self.children = {}, {}, {}
        self.closing = False
        self.watchers = {}
        self.reconcile_locks = {}

    async def start(self, task_id):
        # Acquire before any mutation. Another owner must fail Agent startup loudly.
        self.store.acquire_owner(self)
        self.service.assert_dispatch(task_id)
        if self.store.get("run", task_id) or self.store.unresolved():
            raise RuntimeError("benchmark Session already admitted; no automatic retry")
        runtime = self.service.coordinator.binding_resolver.runtime_registry.current()
        self.service.deployment.authorize(runtime)
        await self.service.validate(task_id, runtime)
        binding = self.store.binding(task_id)
        plan = binding["plan"]
        if plan["parallelism"] > len(self.service.deployment.workers):
            raise ValueError("parallelism exceeds authorized worker capacity")
        run = {
            "task_id": task_id,
            "run_id": "benchmark_" + uuid4().hex,
            "binding_digest": binding["binding_digest"],
            "state": "starting",
            "settled": False,
            "needs_stop": True,
            "reconciliation_required": True,
            "admission_closed": False,
            "session_invocation_id": None,
            "created_at": time.time(),
        }
        arguments = {k: run[k] for k in ("task_id", "run_id", "binding_digest")}
        arguments["plan_digest"] = digest(plan)
        run["start_arguments"] = arguments
        self.store.put("run", task_id, task_id, run, create=True)
        # Materialize every episode from the frozen manifest before opening the Session.
        for number in range(plan["rounds"]):
            round_id = task_id + f":round:{number:04d}"
            self.store.put(
                "round",
                round_id,
                task_id,
                {
                    "round_id": round_id,
                    "number": number,
                    "lessons": [],
                    "lessons_digest": digest([]),
                    "state": "pending",
                    "reflection_complete": False,
                },
                create=True,
            )
            for index, case in enumerate(plan["cases"]):
                child_id = round_id + f":episode:{index:04d}"
                self.store.put(
                    "subtask",
                    child_id,
                    task_id,
                    {
                        "task_id": task_id,
                        "run_id": run["run_id"],
                        "subtask_id": child_id,
                        "round_id": round_id,
                        "case": case,
                        "attempt_id": "attempt_" + uuid4().hex,
                        "binding_digest": run["binding_digest"],
                        "worker_id": None,
                        "world_epoch": None,
                        "state": "pending",
                        "settled": False,
                        "needs_stop": False,
                        "cancelled": False,
                        "outcome": None,
                        "model_calls": 0,
                        "lessons_digest": None,
                    },
                    create=True,
                )
        client = BenchmarkClient(self.service, task_id, runtime, self)
        self.clients[task_id] = client
        try:
            response = await asyncio.wait_for(
                client.start(arguments), self.service.deployment.control_timeout_s
            )
            self.store.update(
                "run",
                task_id,
                session_invocation_id=response["data"]["invocation_id"],
                state="running",
            )
        except Exception as exc:
            self.store.update(
                "run", task_id, admission_closed=True, state="unknown", reason=str(exc)
            )
            self._monitor(task_id, self.reconcile(task_id))
            raise
        self._monitor(task_id, self.run_rounds(task_id))
        return response

    def _monitor(self, task_id, coroutine):
        task = asyncio.create_task(coroutine, name="benchmark-" + task_id)
        self.monitors[task_id] = task

        def completed(done):
            if not done.cancelled() and done.exception() is not None:
                self.service.closing = True

        task.add_done_callback(completed)

    async def run_rounds(self, task_id):
        client = self.clients[task_id]
        plan, lessons = client.binding["plan"], []
        try:
            for number in range(plan["rounds"]):
                self.service.assert_dispatch(task_id)
                round_id = task_id + f":round:{number:04d}"
                self.store.update(
                    "round",
                    round_id,
                    lessons=lessons,
                    lessons_digest=digest(lessons),
                    state="running",
                )
                child_ids = [
                    c["subtask_id"]
                    for c in self.store.list("subtask", task_id)
                    if c["round_id"] == round_id
                ]
                for child_id in child_ids:
                    self.store.update("subtask", child_id, lessons_digest=digest(lessons))
                # Workers are explicit slots. An unknown child retains its slot permanently.
                queue = asyncio.Queue()
                for child_id in child_ids:
                    queue.put_nowait(child_id)
                jobs = [
                    asyncio.create_task(self.worker(task_id, worker, queue))
                    for worker in self.service.deployment.workers[: plan["parallelism"]]
                ]
                self.children[task_id] = jobs
                await asyncio.gather(*jobs)
                children = [self.store.get("subtask", k) for k in child_ids]
                if not all(c["settled"] for c in children):
                    raise RuntimeError("round contains unresolved episodes")
                self.service.assert_dispatch(task_id)
                lessons = await reflect_round(self.service, task_id, round_id)
                self.store.update("round", round_id, state="complete")
            self.store.update("run", task_id, admission_closed=True, state="stopping")
        except asyncio.CancelledError:
            self.store.update(
                "run", task_id, admission_closed=True, state="stopping", reason="cancelled"
            )
        except Exception as exc:
            self.store.update(
                "run", task_id, admission_closed=True, state="blocked", reason=str(exc)
            )
        finally:
            await self.reconcile(task_id)

    async def worker(self, task_id, worker, queue):
        while not queue.empty():
            try:
                self.service.assert_dispatch(task_id)
            except RuntimeError:
                return
            child_id = queue.get_nowait()
            self.store.update(
                "subtask",
                child_id,
                worker_id=worker,
                world_epoch="world_" + uuid4().hex,
                state="starting",
                needs_stop=True,
                started_at=time.time(),
            )
            runner = EpisodeRunner(self, self.clients[task_id], child_id)
            await runner.run()
            if not runner.child["settled"]:
                return  # Do not reassign an uncertain worker to the next episode.

    async def reconcile(self, task_id):
        async with self.reconcile_locks.setdefault(task_id, asyncio.Lock()):
            await self._reconcile(task_id)
        if (
            not self.closing
            and self.store.get("run", task_id)["state"] == "blocked"
            and task_id not in self.watchers
        ):
            self.watchers[task_id] = asyncio.create_task(
                self.watch(task_id), name="benchmark-reconcile-" + task_id
            )

    async def watch(self, task_id):
        try:
            while not self.closing and self.store.get("run", task_id)["state"] == "blocked":
                await asyncio.sleep(max(1.0, self.service.deployment.poll_interval_s))
                await self.reconcile(task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Store/binding failures must close admission even when journaling is unavailable.
            self.service.closing = True

    async def _reconcile(self, task_id):
        client, deployment = self.clients[task_id], self.service.deployment
        self.store.update("run", task_id, admission_closed=True, reconciliation_required=True)
        try:
            # Pending reservations never reached the executor; cancellation is locally provable.
            for child in self.store.list("subtask", task_id):
                if child["state"] == "pending":
                    self.store.update(
                        "subtask",
                        child["subtask_id"],
                        settled=True,
                        cancelled=True,
                        state="terminal",
                        outcome="cancelled",
                    )
                elif not child["settled"]:
                    await EpisodeRunner(self, client, child["subtask_id"]).stop_and_check()
            run = self.store.get("run", task_id)
            args = {k: run[k] for k in ("task_id", "run_id", "binding_digest")}
            root = await asyncio.wait_for(
                client.query(client.root_capability, {**args, "view": "root"}),
                deployment.control_timeout_s,
            )
            if any(root.get(k) != v for k, v in args.items()) or root.get("start_digest") != digest(
                run["start_arguments"]
            ):
                raise RuntimeError("root recovery identity mismatch")
            invocation = root["session_invocation_id"]
            if run["session_invocation_id"] not in (None, invocation):
                raise RuntimeError("root Session identity changed")
            self.store.update("run", task_id, session_invocation_id=invocation)
            # A lost initial reply cannot fabricate a Coordinator invocation. Keep the
            # original unknown fact; use pinned, root-validated control to stop it.
            task = self.service.coordinator.get_task(task_id)
            registered = any(r.invocation_id == invocation for r in task.execution_records)
            if not run.get("stop_requested"):
                self.store.update("run", task_id, stop_requested=True)
                if registered:
                    await asyncio.wait_for(client.stop(invocation), deployment.control_timeout_s)
                else:
                    await asyncio.wait_for(
                        client.runtime.client.stop_session(invocation), deployment.control_timeout_s
                    )
            status = await asyncio.wait_for(
                client.runtime.client.invocation_status(invocation), deployment.control_timeout_s
            )
            if registered:
                await client.read_invocation(invocation, "session", "status")
            root = await asyncio.wait_for(
                client.query(client.root_capability, {**args, "view": "root"}),
                deployment.control_timeout_s,
            )
            if (
                any(root.get(k) != v for k, v in args.items())
                or root.get("start_digest") != digest(run["start_arguments"])
                or root.get("session_invocation_id") != invocation
                or root.get("stopped") is not True
                or status["data"].get("status") != "stopped"
                or any(not c["settled"] for c in self.store.list("subtask", task_id))
            ):
                raise RuntimeError("benchmark stop is not yet proven")
            self.store.update("run", task_id, needs_stop=False)
            # Preserve unresolved Gateway facts. Query receipts never rewrite them to success.
            from PhyAgentOS.forge.benchmark.evidence import unknowns_resolved

            if not unknowns_resolved(self.service, task_id):
                raise RuntimeError("original Gateway invocation remains unknown")
            self.store.update(
                "run",
                task_id,
                settled=True,
                needs_stop=False,
                reconciliation_required=False,
                state="complete",
                final_root=root,
                final_root_digest=digest(root),
            )
            await self.service.coordinator.finalize_task(task_id)
        except Exception as exc:
            self.store.update(
                "run", task_id, state="blocked", reason=str(exc), reconciliation_required=True
            )
        await self.emit(task_id)

    async def emit(self, task_id):
        run = self.store.get("run", task_id)
        event_id = (
            task_id
            + ":notification:"
            + digest({"state": run["state"], "reason": run.get("reason")})
        )
        if self.notify and not self.store.get("notification", event_id):
            # Persist before notification; a crash cannot cause duplicate messages.
            self.store.put(
                "notification",
                event_id,
                task_id,
                {"id": event_id, "state": run["state"]},
                create=True,
            )
            await self.notify(task_id, run)

    async def cancel(self, task_id):
        monitor = self.monitors.get(task_id)
        for child in self.store.list("subtask", task_id):
            if not child["settled"]:
                self.store.update("subtask", child["subtask_id"], cancelled=True)
        for job in self.children.get(task_id, []):
            job.cancel()
        if monitor and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        elif task_id in self.clients:
            await self.reconcile(task_id)

    async def recover(self):
        self.store.acquire_owner(self)  # Must propagate duplicate-owner errors unchanged.
        for run in self.store.unresolved():  # Includes terminal AgentTasks.
            task_id = run["task_id"]
            if task_id in self.monitors and not self.monitors[task_id].done():
                continue
            runtime = self.service.coordinator.binding_resolver.runtime_registry.current()
            await self.service.validate(task_id, runtime)
            self.service.deployment.authorize(runtime)
            self.clients[task_id] = BenchmarkClient(self.service, task_id, runtime, self)
            self.store.update(
                "run", task_id, admission_closed=True, reason="restart_requires_reconciliation"
            )
            await self.reconcile(task_id)

    async def shutdown(self):
        self.closing = self.service.closing = True
        for watcher in self.watchers.values():
            watcher.cancel()
        await asyncio.gather(*self.watchers.values(), return_exceptions=True)
        for run in self.store.unresolved():
            self.store.update(
                "run", run["task_id"], admission_closed=True, needs_stop=True, reason="shutdown"
            )
            await self.cancel(run["task_id"])
        self.store.release_owner(self)

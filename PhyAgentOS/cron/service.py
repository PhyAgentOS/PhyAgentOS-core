"""Cron service for scheduling agent tasks."""

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from PhyAgentOS.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore


def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        # Next interval from now
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter
            # Use caller-provided reference time for deterministic scheduling
            base_time = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


class CronService:
    """Service for managing and executing scheduled jobs.

    A running service re-checks the job file at least once per
    :attr:`RELOAD_CHECK_S`, even when no job is due. That is how a job added by
    another process to the same ``jobs.json`` (for example ``paos agent``) is
    picked up without restarting the gateway. A check with nothing due does not
    write the store back, so polling cannot overwrite a job another process
    just added. An unstarted service (the CLI) never arms a timer.
    """

    # Upper bound between store re-checks. It is also the wake interval when
    # no job is scheduled, so an external add is noticed within this window.
    RELOAD_CHECK_S = 1.0

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None
    ):
        self.store_path = store_path
        self.on_job = on_job
        self._store: CronStore | None = None
        # Digest of the file bytes this instance last read or wrote. mtime
        # alone is not enough: on a coarse filesystem two writes can share the
        # same timestamp, which would hide a job another process added.
        self._last_digest: bytes | None = None
        self._timer_task: asyncio.Task | None = None
        self._running = False
        # True while the timer task is inside ``_on_timer``. Re-arming from
        # another caller must not cancel that task and abort a job in flight.
        self._executing = False

    def _read_store_bytes(self) -> bytes | None:
        try:
            if self.store_path.exists():
                return self.store_path.read_bytes()
        except OSError as exc:
            logger.warning("Could not read cron store: {}", exc)
        return None

    def _parse_store(self, raw: bytes) -> CronStore:
        """Parse the store document; raises on a document that is not a store."""
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list):
            raise ValueError("cron store must be an object with a jobs array")
        jobs: list[CronJob] = []
        for j in data.get("jobs", []):
            try:
                jobs.append(CronJob(
                    id=j["id"],
                    name=j["name"],
                    enabled=j.get("enabled", True),
                    schedule=CronSchedule(
                        kind=j["schedule"]["kind"],
                        at_ms=j["schedule"].get("atMs"),
                        every_ms=j["schedule"].get("everyMs"),
                        expr=j["schedule"].get("expr"),
                        tz=j["schedule"].get("tz"),
                    ),
                    payload=CronPayload(
                        kind=j["payload"].get("kind", "agent_turn"),
                        message=j["payload"].get("message", ""),
                        deliver=j["payload"].get("deliver", False),
                        channel=j["payload"].get("channel"),
                        to=j["payload"].get("to"),
                    ),
                    state=CronJobState(
                        next_run_at_ms=j.get("state", {}).get("nextRunAtMs"),
                        last_run_at_ms=j.get("state", {}).get("lastRunAtMs"),
                        last_status=j.get("state", {}).get("lastStatus"),
                        last_error=j.get("state", {}).get("lastError"),
                    ),
                    created_at_ms=j.get("createdAtMs", 0),
                    updated_at_ms=j.get("updatedAtMs", 0),
                    delete_after_run=j.get("deleteAfterRun", False),
                ))
            except Exception as e:
                # One unreadable job must not cost the user the others.
                job_id = j.get("id", "<unknown>") if isinstance(j, dict) else "<unknown>"
                logger.warning("Skipping unreadable cron job {}: {}", job_id, e)
        return CronStore(jobs=jobs)

    def _load_store(self) -> CronStore:
        """Load jobs from disk. Reloads automatically if file was modified externally."""
        raw = self._read_store_bytes()

        if self._store and raw is not None:
            digest = hashlib.sha256(raw).digest()
            if digest != self._last_digest:
                logger.info("Cron: jobs.json modified externally, reloading")
                self._store = None
        if self._store:
            return self._store

        if raw is not None:
            try:
                self._store = self._parse_store(raw)
            except Exception as e:
                # Do not let start()'s following write overwrite the only copy
                # of the file; move it aside so it stays recoverable.
                logger.warning("Failed to parse cron store: {}", e)
                self._quarantine_store()
                self._store = CronStore()
                return self._store
            self._last_digest = hashlib.sha256(raw).digest()
        else:
            self._store = CronStore()
            self._last_digest = None

        return self._store

    def _quarantine_store(self) -> None:
        """Move an unreadable store aside, or raise to prevent overwriting it."""
        for n in range(1, 1000):
            candidate = self.store_path.with_name(f"{self.store_path.name}.corrupt{n}")
            if not candidate.exists():
                try:
                    self.store_path.replace(candidate)
                except OSError as e:
                    logger.warning("Could not set the unreadable cron store aside: {}", e)
                    raise
                logger.warning("Moved the unreadable cron store to {}", candidate.name)
                return
        raise OSError("Too many .corrupt files beside the cron store; refusing to overwrite it")

    @contextmanager
    def _store_lock(self):
        """Serialize store read-modify-write across threads and processes.

        Same advisory lock pattern as the session store's write lock: a
        blocking ``flock`` on ``<store>.lock``. It is held only around the
        synchronous read/modify/write block; never across an awaited model or
        tool call, so a CLI call in the same process cannot deadlock behind a
        running job.
        """
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.store_path.with_name(f"{self.store_path.name}.lock")
        handle = open(lock_path, "a+")
        locked = False
        try:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            except (ImportError, OSError):
                pass
            yield
        finally:
            if locked:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
            handle.close()

    @staticmethod
    def _job_definition(job: CronJob) -> tuple:
        """Identity of a job's user-visible definition, not its runtime state.

        ``enabled`` is handled separately: an execution may disable a one-shot
        job itself, so the merge compares the pre-run value with the file.
        """
        return (
            job.name,
            job.schedule.kind,
            job.schedule.at_ms,
            job.schedule.every_ms,
            job.schedule.expr,
            job.schedule.tz,
            job.payload.kind,
            job.payload.message,
            job.payload.deliver,
            job.payload.channel,
            job.payload.to,
            job.delete_after_run,
        )

    def _job_by_id(self, job_id: str) -> CronJob | None:
        if not self._store:
            return None
        for job in self._store.jobs:
            if job.id == job_id:
                return job
        return None

    def _write_store(self) -> None:
        """Write the in-memory store atomically. The caller holds the lock."""
        if not self._store:
            return

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                    },
                    "state": {
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "lastStatus": j.state.last_status,
                        "lastError": j.state.last_error,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "deleteAfterRun": j.delete_after_run,
                }
                for j in self._store.jobs
            ]
        }

        text = json.dumps(data, indent=2, ensure_ascii=False)
        fd, temp_path = tempfile.mkstemp(
            dir=self.store_path.parent, prefix=f".{self.store_path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temp_path, self.store_path)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        self._last_digest = hashlib.sha256(text.encode("utf-8")).digest()

    def _commit_execution(
        self,
        job: CronJob,
        definition: tuple,
        was_enabled: bool,
        *,
        delete: bool,
    ) -> None:
        """Merge one finished job into the latest store, under the lock.

        The file is read fresh instead of trusting the in-memory object, so the
        engine's own ``enabled``/``state`` changes are not mistaken for an
        external edit. Only this job's runtime state (or its one-shot deletion)
        is applied. A job another process deleted stays deleted, and a
        definition another process changed (schedule, payload, enable) is kept
        as-is; jobs added in the meantime are written back unchanged.
        """
        with self._store_lock():
            raw = self._read_store_bytes()
            if raw is None:
                # The store file was removed externally: do not recreate it.
                logger.info(
                    "Cron: store file is gone; keeping the external removal",
                )
                return
            try:
                latest = self._parse_store(raw)
            except Exception as e:
                logger.warning(
                    "Cron: could not parse the store after a job finished: {}", e
                )
                return
            self._store = latest
            self._last_digest = hashlib.sha256(raw).digest()

            current = self._job_by_id(job.id)
            if current is None:
                logger.info(
                    "Cron: job {} was removed externally; keeping the removal", job.id
                )
                return
            if self._job_definition(current) != definition:
                logger.info(
                    "Cron: job {} changed externally; keeping the external definition",
                    job.id,
                )
                return
            if was_enabled and not current.enabled:
                logger.info(
                    "Cron: job {} was disabled externally; keeping the disable",
                    job.id,
                )
                return
            if delete:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                # Only our own one-shot disable is applied; an external
                # enable/disable decision is never overwritten.
                if was_enabled and not job.enabled:
                    current.enabled = False
                current.state = job.state
                current.updated_at_ms = job.updated_at_ms
            self._write_store()

    async def start(self) -> None:
        """Start the cron service and recompute the schedule once."""
        self._running = True
        with self._store_lock():
            self._load_store()
            self._recompute_next_runs()
            self._write_store()
        self._arm_timer()
        logger.info("Cron service started with {} jobs", len(self._store.jobs if self._store else []))

    def stop(self) -> None:
        """Stop the cron service and leave no timer task behind."""
        self._running = False
        task = self._timer_task
        self._timer_task = None
        if task is None or task.done():
            return
        # Never cancel the task that is currently executing a job from inside
        # that same call; it exits on its own after the wake completes.
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs
                 if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None

    def _next_delay_s(self) -> float:
        """Wait until the next due job, but never longer than one reload check."""
        next_wake = self._get_next_wake_ms()
        if next_wake is None:
            return self.RELOAD_CHECK_S
        remaining_ms = max(0, next_wake - _now_ms())
        return min(remaining_ms / 1000, self.RELOAD_CHECK_S)

    def _arm_timer(self) -> None:
        """Schedule the next wake, or a bounded store re-check.

        The previous timer is cancelled unless this call comes from the timer
        task itself: cancelling the current task would deliver an unexpected
        cancellation into a job that is still executing. When the service is
        not running (the CLI case) nothing is armed.
        """
        if not self._running:
            task = self._timer_task
            self._timer_task = None
            if task is not None and not task.done():
                try:
                    current = asyncio.current_task()
                except RuntimeError:
                    current = None
                if task is not current:
                    task.cancel()
            return

        if self._executing:
            # The running wake re-arms itself when it finishes; cancelling it
            # here would abort the job it is awaiting.
            return

        current = asyncio.current_task()
        if (
            self._timer_task is not None
            and self._timer_task is not current
            and not self._timer_task.done()
        ):
            self._timer_task.cancel()

        delay_s = self._next_delay_s()

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Re-check the store, run due jobs, then re-arm.

        A wake with no due job only reloads and re-arms; it never writes the
        store, so a job another process added is never overwritten by the poll.
        Each executed job commits only its own state afterwards.
        """
        self._executing = True
        try:
            self._load_store()
            if not self._store:
                return

            now = _now_ms()
            due_ids = [
                j.id
                for j in self._store.jobs
                if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
            ]

            for job_id in due_ids:
                # Another process may have changed or removed a queued due job
                # while an earlier one was running; re-read the latest store.
                self._load_store()
                job = self._job_by_id(job_id)
                if job is None or not job.enabled or not job.state.next_run_at_ms:
                    continue
                if _now_ms() < job.state.next_run_at_ms:
                    continue
                await self._execute_job(job)
        finally:
            self._executing = False
            self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job and merge its result into the latest store."""
        start_ms = _now_ms()
        definition = self._job_definition(job)
        was_enabled = job.enabled
        logger.info("Cron: executing job '{}' ({})", job.name, job.id)

        try:
            if self.on_job:
                await self.on_job(job)

            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info("Cron: job '{}' completed", job.name)

        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error("Cron: job '{}' failed: {}", job.name, e)

        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()

        delete = False
        if job.schedule.kind == "at":
            if job.delete_after_run:
                delete = True
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

        # The lock is taken only here, after every await has finished.
        self._commit_execution(job, definition, was_enabled, delete=delete)

    # ========== Public API ==========

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        """Add a new job."""
        _validate_schedule_for_add(schedule)
        with self._store_lock():
            store = self._load_store()
            now = _now_ms()
            job = CronJob(
                id=str(uuid.uuid4())[:8],
                name=name,
                enabled=True,
                schedule=schedule,
                payload=CronPayload(
                    kind="agent_turn",
                    message=message,
                    deliver=deliver,
                    channel=channel,
                    to=to,
                ),
                state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
                created_at_ms=now,
                updated_at_ms=now,
                delete_after_run=delete_after_run,
            )
            store.jobs.append(job)
            self._write_store()
        self._arm_timer()

        logger.info("Cron: added job '{}' ({})", name, job.id)
        return job

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        with self._store_lock():
            store = self._load_store()
            before = len(store.jobs)
            store.jobs = [j for j in store.jobs if j.id != job_id]
            removed = len(store.jobs) < before
            if removed:
                self._write_store()

        if removed:
            self._arm_timer()
            logger.info("Cron: removed job {}", job_id)

        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        found: CronJob | None = None
        with self._store_lock():
            store = self._load_store()
            for job in store.jobs:
                if job.id == job_id:
                    job.enabled = enabled
                    job.updated_at_ms = _now_ms()
                    if enabled:
                        job.state.next_run_at_ms = _compute_next_run(
                            job.schedule, _now_ms()
                        )
                    else:
                        job.state.next_run_at_ms = None
                    self._write_store()
                    found = job
                    break
        if found is not None:
            self._arm_timer()
        return found

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._arm_timer()
                return True
        return False

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }

"""Background shell jobs owned by the shared general-tools capability.

The manager reuses :class:`~PhyAgentOS.tooling.shell.ExecTool` for the safety
guard, working-directory resolution, process-group creation, session
cancellation and bounded termination. It adds only:

* a job identity and state machine (``running``, ``completed``, ``failed``,
  ``timeout``, ``cancelled``);
* a bounded in-memory copy of stdout/stderr plus a per-job and manager-lifetime
  bounded ``.paos/tool_outputs`` spool for overflow; bytes beyond either bound
  are drained and reported as dropped, never as a complete log. Existing
  manager-named spool files are counted read-only at startup and are not
  deleted, so historical files remain on disk;
* session-scoped status/wait/cancel lookups that never accept a caller-supplied
  session;
* one optional completion callback, frozen to the job's session at start.

The manager belongs to one shared capability, not to a turn. A per-turn
``runtime.stop`` does not touch it; an explicit session cancel or capability
close does. Every process stays registered with ``ExecTool`` until it settles,
so shutdown waits for the process group instead of leaving it behind.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PhyAgentOS.tooling.shell import BackgroundExec, ExecTool

__all__ = [
    "JOB_CANCEL_TOOL",
    "JOB_STATUS_TOOL",
    "JOB_WAIT_TOOL",
    "JobManager",
    "JobNotify",
    "JobRecord",
]

JobNotify = Callable[[str, str, str], Awaitable[None]]
_NOTIFY_NOT_PROVIDED = object()

_TERMINAL_STATES = frozenset({"completed", "failed", "timeout", "cancelled"})
_NOTIFY_CANCELLED = "the completion notification was cancelled"
_NOTIFY_SKIPPED = "the completion notification was skipped because the job was stopped"
_DEFAULT_JOB_BUFFER_BYTES = 64 * 1024
_DEFAULT_MAX_SPOOL_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_SPOOL_LIFETIME_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_JOBS = 128
_SPOOL_DIR = Path(".paos") / "tool_outputs"


@dataclass(slots=True)
class _SpoolBudget:
    """One job's stdout+stderr budget, drawn from a manager lifetime budget."""

    limit: int
    reserve_bytes: Callable[[int], int]
    available_bytes: Callable[[], int]
    written: int = 0

    def remaining(self) -> int:
        return min(
            max(0, self.limit - self.written),
            max(0, self.available_bytes()),
        )

    def take(self, requested: int) -> int:
        allowed = min(max(0, requested), self.remaining())
        allowed = self.reserve_bytes(allowed)
        self.written += allowed
        return allowed

JOB_STATUS_TOOL = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": (
                "Background job id returned by exec with background=true. "
                "Omit it to list this session's jobs."
            ),
        },
    },
    "required": [],
}

JOB_WAIT_TOOL = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": "Background job id to wait for.",
        },
        "timeout": {
            "type": "integer",
            "description": (
                "Maximum seconds to wait. When it expires the job is still "
                "running; this is not a job timeout."
            ),
            "minimum": 1,
            "maximum": 600,
        },
    },
    "required": ["job_id"],
}

JOB_CANCEL_TOOL = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": "Background job id to stop.",
        },
    },
    "required": ["job_id"],
}


@dataclass(slots=True)
class _BoundedCapture:
    """Read one pipe with bounded memory and a shared bounded disk spool."""

    path: Path | None
    limit: int
    budget: _SpoolBudget

    _buffer: bytearray = field(default_factory=bytearray)
    _spool: Any = None
    _spool_attempted: bool = False
    total_bytes: int = 0
    truncated: bool = False
    spool_path: Path | None = None
    spooled_bytes: int = 0
    spool_error: str | None = None

    @property
    def dropped_bytes(self) -> int:
        if not self.truncated:
            return 0
        return max(0, self.total_bytes - self.spooled_bytes)

    async def read(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                if self._spool is not None:
                    self._write_spool(chunk)
                    continue
                if len(self._buffer) + len(chunk) <= self.limit:
                    self._buffer.extend(chunk)
                    continue

                # Memory is full. Keep the head and spill what fits in the
                # job's shared disk budget; everything else is drained and
                # counted as dropped rather than written without a bound.
                self.truncated = True
                room = self.limit - len(self._buffer)
                if room > 0:
                    self._buffer.extend(chunk[:room])
                if not self._spool_attempted:
                    self._spool_attempted = True
                    if self.budget.remaining() > 0:
                        self._open_spool()
                if self._spool is not None:
                    self._write_spool(self._buffer)
                    self._write_spool(chunk)
        except Exception:  # noqa: BLE001 - pipe may close first
            pass
        finally:
            self._close_spool()

    def _write_spool(self, data: bytes) -> None:
        if self._spool is None:
            return
        written = self.budget.take(len(data))
        if written:
            self._spool.write(data[:written])
            self.spooled_bytes += written

    def _open_spool(self) -> bool:
        if self._spool is not None:
            return True
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._spool = open(self.path, "wb")  # noqa: SIM115 - closed in _close_spool
            self.spool_path = self.path
            return True
        except Exception as exc:  # noqa: BLE001 - keep the bounded head
            self.spool_error = f"{type(exc).__name__}: {exc}"
            return False

    def _close_spool(self) -> None:
        if self._spool is not None:
            try:
                self._spool.close()
            except Exception:  # noqa: BLE001 - read path must not fail shutdown
                pass
            self._spool = None

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")


@dataclass(slots=True)
class JobRecord:
    """One accepted background command and its currently known outcome."""

    job_id: str
    session_id: str
    turn_id: str
    command: str
    cwd: str
    timeout: int
    handle: BackgroundExec
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    state: str = "running"
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    output_bytes: int = 0
    spool_bytes: int = 0
    spool_dropped_bytes: int = 0
    spool_limit_bytes: int = 0
    spool_truncated: bool = False
    spool_saved_prefix_only: bool = False
    spool_error: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: bool = False
    stopping: bool = False
    callback: JobNotify | None = None
    notified: bool = False
    notify_error: str | None = None
    notify_task: asyncio.Task[None] | None = None

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    def spool_paths(self, workspace: Path) -> tuple[str | None, str | None]:
        def display(path: Path | None) -> str | None:
            if path is None:
                return None
            try:
                return path.relative_to(workspace).as_posix()
            except ValueError:
                return path.as_posix()

        return display(self.stdout_path), display(self.stderr_path)

    def report_text(self, workspace: Path) -> str:
        """Human-readable result for the completion callback."""
        lines = [
            f"[Background job {self.job_id} {self.state}]",
            "",
            f"Command: {self.command}",
        ]
        if self.exit_code is not None:
            lines.append(f"Exit code: {self.exit_code}")
        lines.append(f"Elapsed: {self.elapsed():.1f}s")
        stdout_path, stderr_path = self.spool_paths(workspace)
        if self.stdout:
            lines.extend(["", "Output:", self.stdout])
        if self.stderr:
            lines.extend(["", "STDERR:", self.stderr])
        if self.output_truncated:
            parts = []
            if stdout_path:
                parts.append(f"stdout: {stdout_path}")
            if stderr_path:
                parts.append(f"stderr: {stderr_path}")
            if self.spool_error:
                parts.append(f"spool error: {self.spool_error}")
            if self.spool_truncated:
                lines.extend(
                    [
                        "",
                        "Only a saved output prefix is available; "
                        f"{self.spool_dropped_bytes} later byte(s) were drained "
                        "but not stored because the spool budget was reached. "
                        "Partial spool: "
                        + (", ".join(parts) if parts else "not available"),
                    ]
                )
            else:
                lines.extend(
                    [
                        "",
                        "The in-memory copy is bounded. Full output: "
                        + (", ".join(parts) if parts else "not available"),
                    ]
                )
        if self.error:
            lines.extend(["", f"Error: {self.error}"])
        lines.extend(["", f"Session: {self.session_id}"])
        return "\n".join(lines)

    def to_dict(self, workspace: Path, *, include_output: bool = True) -> dict[str, Any]:
        stdout_path, stderr_path = self.spool_paths(workspace)
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "state": self.state,
            "command": self.command,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "exit_code": self.exit_code,
            "elapsed_seconds": round(self.elapsed(), 3),
            "timeout_seconds": self.timeout,
            "truncated": self.output_truncated,
            "output_bytes": self.output_bytes,
        }
        if self.output_truncated:
            data["spool_bytes"] = self.spool_bytes
            data["spool_dropped_bytes"] = self.spool_dropped_bytes
            data["spool_limit_bytes"] = self.spool_limit_bytes
            data["spool_truncated"] = self.spool_truncated
            data["spool_saved_prefix_only"] = self.spool_saved_prefix_only
            data["spool_complete"] = not self.spool_truncated
        if stdout_path is not None:
            data["stdout_spool_path"] = stdout_path
        if stderr_path is not None:
            data["stderr_spool_path"] = stderr_path
        if include_output:
            data["stdout"] = self.stdout
            data["stderr"] = self.stderr
        if self.spool_error:
            data["spool_error"] = self.spool_error
        if self.error:
            data["error"] = self.error
        return data


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _error(message: str) -> str:
    return _json({"error": message, "state": "rejected"})


class JobManager:
    """Session-scoped background commands with queryable settled results.

    ``notify`` is optional. When present it is called once per settled job with
    ``(session_id, job_id, report_text)``; the session argument is frozen at
    start. A delivery failure is recorded on the job and never changes its
    process outcome. Without a callback, ``job_status``/``job_wait`` still
    return the real result.
    """

    def __init__(
        self,
        exec_tool: ExecTool,
        workspace: Path | str,
        *,
        notify: JobNotify | None = None,
        buffer_bytes: int = _DEFAULT_JOB_BUFFER_BYTES,
        max_spool_bytes: int = _DEFAULT_MAX_SPOOL_BYTES,
        max_spool_lifetime_bytes: int = _DEFAULT_MAX_SPOOL_LIFETIME_BYTES,
        max_jobs: int = _DEFAULT_MAX_JOBS,
    ) -> None:
        if notify is not None and not callable(notify):
            raise TypeError("notify must be callable or None")
        if buffer_bytes < 1:
            raise ValueError("buffer_bytes must be >= 1")
        if max_spool_bytes < 0:
            raise ValueError("max_spool_bytes must be >= 0")
        if max_spool_lifetime_bytes < 0:
            raise ValueError("max_spool_lifetime_bytes must be >= 0")
        if max_jobs < 1:
            raise ValueError("max_jobs must be >= 1")
        self._exec = exec_tool
        self._workspace = Path(workspace)
        self._notify = notify
        self._buffer_bytes = int(buffer_bytes)
        self._max_spool_bytes = int(max_spool_bytes)
        self._max_spool_lifetime_bytes = int(max_spool_lifetime_bytes)
        self._spool_used = self._scan_spool_usage()
        self._max_jobs = int(max_jobs)
        self._jobs: dict[str, JobRecord] = {}
        self._order: deque[str] = deque()
        self._reserved = 0
        self._inflight: dict[asyncio.Task[Any], str] = {}
        self._notifications: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- lifecycle ----------------------------------------------------
    async def start(
        self,
        *,
        command: str,
        session_id: str,
        turn_id: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        notify: JobNotify | None | object = _NOTIFY_NOT_PROVIDED,
    ) -> str:
        """Start one job and return an ``accepted`` handle, never a result.

        ``notify`` is optional per-job. Omitting it uses the manager's default
        callback; passing ``None`` explicitly disables notification for this
        job only. The callback is frozen on the record before the process
        starts.
        """
        if self._closed:
            return _error("Error: the background job manager is closed")
        if not isinstance(command, str) or not command.strip():
            return _error("Error: the background command must not be empty")
        if notify is not _NOTIFY_NOT_PROVIDED and notify is not None:
            if not callable(notify):
                return _error("Error: job notify must be callable or None")
        callback = self._notify if notify is _NOTIFY_NOT_PROVIDED else notify

        if not self._reserve_slot():
            return _error(
                f"Error: at most {self._max_jobs} background jobs are retained "
                "and none can be evicted; nothing was started"
            )
        slot_consumed = False
        current = asyncio.current_task()
        if current is not None:
            self._inflight[current] = session_id
        try:
            if self._closed:
                return _error(
                    "Error: the background job manager was closed while starting"
                )
            started = await self._exec.start_background(
                command,
                working_dir=working_dir,
                timeout=timeout,
                session_id=session_id,
            )
            if isinstance(started, str):
                return _error(started)
            if self._closed:
                # close may have set the flag after the reservation was made.
                # Reap the eventual process before returning a failure.
                await self._exec.terminate_background(started)
                self._exec.release_background(
                    session_id, started.record, started.process
                )
                return _error(
                    "Error: the background job manager was closed while "
                    "starting; the command was stopped"
                )

            job_id = f"job-{uuid.uuid4().hex[:12]}"
            stdout_name = f"{job_id}-stdout.log"
            stderr_name = f"{job_id}-stderr.log"
            record = JobRecord(
                job_id=job_id,
                session_id=session_id,
                turn_id=turn_id,
                command=command,
                cwd=started.cwd,
                timeout=started.timeout,
                handle=started,
                stdout_path=None,
                stderr_path=None,
                callback=callback,
            )
            planned_stdout = self._workspace / _SPOOL_DIR / stdout_name
            planned_stderr = self._workspace / _SPOOL_DIR / stderr_name

            try:
                task = asyncio.create_task(
                    self._drive(record, planned_stdout, planned_stderr),
                    name=f"job:{job_id}",
                )
            except Exception as exc:  # noqa: BLE001 - release the process on failure
                record.stopping = True
                record.cancel_requested = True
                await self._exec.terminate_background(started)
                self._exec.release_background(
                    session_id, started.record, started.process
                )
                return _error(
                    f"Error: could not start the background job task: {exc}"
                )

            record.task = task
            self._exec.bind_background_task(started, task)
            self._jobs[job_id] = record
            self._order.append(job_id)
            # The reserved slot is now represented by the retained record.
            self._reserved -= 1
            slot_consumed = True

            return _json(
                {
                    "status": "accepted",
                    "state": "running",
                    "job_id": job_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "command": command,
                    "message": (
                        "Background job started. This handle is not a completion; "
                        "use job_status or job_wait for the real result."
                    ),
                }
            )
        finally:
            if current is not None:
                self._inflight.pop(current, None)
            if not slot_consumed:
                self._reserved -= 1

    def _scan_spool_usage(self) -> int:
        """Count this manager's existing job spool files read-only.

        The budget survives restarts for files this manager named; nothing is
        deleted, so an external or older file never silently frees capacity.
        """
        directory = self._workspace / _SPOOL_DIR
        total = 0
        try:
            entries = directory.glob("job-*.log")
        except OSError:
            return 0
        for path in entries:
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def _reserve_spool_bytes(self, requested: int) -> int:
        remaining = max(0, self._max_spool_lifetime_bytes - self._spool_used)
        allowed = min(max(0, requested), remaining)
        self._spool_used += allowed
        return allowed

    def _reserve_slot(self) -> bool:
        """Reserve one job slot before any await, including concurrent starts.

        Only terminal records may be evicted. A live or reserved slot is never
        sacrificed, so running jobs and retained results cannot exceed
        ``max_jobs`` even when several ``start`` calls overlap.
        """
        while len(self._jobs) + self._reserved >= self._max_jobs:
            if not self._evict_one_finished():
                break
        if len(self._jobs) + self._reserved >= self._max_jobs:
            return False
        self._reserved += 1
        return True

    async def cancel_session(self, session_id: str) -> None:
        """Stop every running job this manager owns for one session."""
        records = [
            record
            for record in self._jobs.values()
            if record.session_id == session_id and not record.terminal
        ]
        if not records:
            return
        for record in records:
            record.stopping = True
            record.cancel_requested = True
            self._cancel_notification(record)
        await asyncio.gather(
            *(self._exec.terminate_background(record.handle) for record in records),
            return_exceptions=True,
        )
        tasks = tuple(
            record.task for record in records if record.task is not None
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Stop every job and delivery task; idempotent."""
        if self._closed:
            return
        self._closed = True
        inflight = tuple(self._inflight.items())
        inflight_sessions = {session_id for _task, session_id in inflight}
        sessions = {
            record.session_id for record in self._jobs.values()
        } | inflight_sessions
        # Stop retained jobs first so their late notifications are skipped.
        await asyncio.gather(
            *(self.cancel_session(session_id) for session_id in sessions),
            return_exceptions=True,
        )
        # A start may still be inside ExecTool's async process creation and has
        # no JobRecord yet. Its record is registered there under this session;
        # cancel_session marks it, waits for the spawn, and reaps the process
        # group with ExecTool's existing bounded cleanup.
        await asyncio.gather(
            *(self._exec.cancel_session(session_id) for session_id in inflight_sessions),
            return_exceptions=True,
        )
        # Do not return until those start calls have observed closure and
        # released their reservation/handle. Never await the caller's own task.
        current = asyncio.current_task()
        start_tasks = tuple(
            task for task, _session in inflight if task is not current
        )
        if start_tasks:
            await asyncio.gather(*start_tasks, return_exceptions=True)
        notifications = tuple(self._notifications)
        for notification in notifications:
            notification.cancel()
        if notifications:
            await asyncio.gather(*notifications, return_exceptions=True)
        self._notifications.clear()

    # -- lookups ------------------------------------------------------
    def pending(self, session_id: str | None = None) -> int:
        return sum(
            1
            for record in self._jobs.values()
            if not record.terminal
            and (session_id is None or record.session_id == session_id)
        )

    def jobs_for(self, session_id: str) -> tuple[JobRecord, ...]:
        return tuple(
            record
            for record in self._jobs.values()
            if record.session_id == session_id
        )

    async def handle(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        turn_id: str,
    ) -> str:
        """Run one job tool from the owning capability."""
        del turn_id
        if name == "job_status":
            return self._status(arguments, session_id)
        if name == "job_wait":
            return await self._wait(arguments, session_id)
        if name == "job_cancel":
            return await self._cancel(arguments, session_id)
        return _error(f"Error: unknown background job tool: {name}")

    def _status(self, arguments: dict[str, Any], session_id: str) -> str:
        job_id = arguments.get("job_id")
        if job_id is None or (isinstance(job_id, str) and not job_id.strip()):
            jobs = [
                record.to_dict(self._workspace, include_output=False)
                for record in self.jobs_for(session_id)
            ]
            return _json(
                {
                    "state": "listed",
                    "session_id": session_id,
                    "jobs": jobs,
                    "pending": sum(1 for job in jobs if job["state"] == "running"),
                }
            )
        if not isinstance(job_id, str):
            return _error("Error: job_id must be a string")
        record, problem = self._lookup(job_id, session_id)
        if problem is not None:
            return problem
        assert record is not None
        return _json(record.to_dict(self._workspace))

    async def _wait(self, arguments: dict[str, Any], session_id: str) -> str:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            return _error("Error: job_wait requires a job_id")
        record, problem = self._lookup(job_id, session_id)
        if problem is not None:
            return problem
        assert record is not None

        raw_timeout = arguments.get("timeout", 30)
        try:
            timeout = max(1, min(600, int(raw_timeout)))
        except (TypeError, ValueError):
            return _error("Error: timeout must be an integer between 1 and 600")

        if not record.terminal:
            try:
                # No shield: on timeout wait_for cancels the waiter coroutine,
                # while the job's own Event stays untouched.
                await asyncio.wait_for(record.done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                data = record.to_dict(self._workspace)
                data["wait_timed_out"] = True
                data["message"] = (
                    "The wait expired while the job is still running. This is "
                    "not a job timeout and not a result."
                )
                return _json(data)
        return _json(record.to_dict(self._workspace))

    async def _cancel(self, arguments: dict[str, Any], session_id: str) -> str:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            return _error("Error: job_cancel requires a job_id")
        record, problem = self._lookup(job_id, session_id)
        if problem is not None:
            return problem
        assert record is not None

        if record.terminal:
            data = record.to_dict(self._workspace)
            data["message"] = "The job already finished; nothing was stopped."
            return _json(data)

        record.stopping = True
        record.cancel_requested = True
        self._cancel_notification(record)
        await self._exec.terminate_background(record.handle)
        if record.task is not None:
            await asyncio.gather(record.task, return_exceptions=True)
        if not record.terminal:
            return _error(
                f"Error: job {record.job_id} did not settle after cancellation"
            )
        data = record.to_dict(self._workspace)
        data["message"] = "The local process group was stopped."
        return _json(data)

    def _lookup(
        self, job_id: str, session_id: str
    ) -> tuple[JobRecord | None, str | None]:
        record = self._jobs.get(job_id)
        if record is None:
            return None, _error("Error: no such background job in this session")
        if record.session_id != session_id:
            # Do not reveal another session's command, output, or state.
            return None, _error(
                "Error: that background job does not belong to this session"
            )
        return record, None

    # -- execution ----------------------------------------------------
    async def _drive(
        self,
        record: JobRecord,
        planned_stdout: Path,
        planned_stderr: Path,
    ) -> None:
        try:
            await self._run(record, planned_stdout, planned_stderr)
        except asyncio.CancelledError:
            if not record.terminal:
                record.state = "cancelled"
                record.error = "Command cancelled because its job was stopped"
            await self._exec.terminate_background(record.handle)
            raise
        except Exception as exc:  # noqa: BLE001 - report a runner failure
            if not record.terminal:
                record.state = "failed"
                record.error = f"job runner failed: {type(exc).__name__}: {exc}"
            await self._exec.terminate_background(record.handle)
        finally:
            record.finished_at = time.monotonic()
            record.done.set()
            self._exec.release_background(
                record.session_id, record.handle.record, record.handle.process
            )
            self._schedule_notify(record)

    async def _run(
        self,
        record: JobRecord,
        planned_stdout: Path,
        planned_stderr: Path,
    ) -> None:
        process = record.handle.process
        budget = _SpoolBudget(
            limit=self._max_spool_bytes,
            reserve_bytes=self._reserve_spool_bytes,
            available_bytes=lambda: max(
                0, self._max_spool_lifetime_bytes - self._spool_used
            ),
        )
        stdout = _BoundedCapture(
            path=planned_stdout, limit=self._buffer_bytes, budget=budget
        )
        stderr = _BoundedCapture(
            path=planned_stderr, limit=self._buffer_bytes, budget=budget
        )
        readers = (
            asyncio.create_task(stdout.read(process.stdout)),
            asyncio.create_task(stderr.read(process.stderr)),
        )
        timed_out = False
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=record.timeout)
            except asyncio.TimeoutError:
                timed_out = True
                await self._exec.terminate_background(record.handle)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            await asyncio.gather(*readers, return_exceptions=True)
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        record.exit_code = process.returncode
        record.stdout = stdout.text()
        record.stderr = stderr.text()
        record.output_truncated = stdout.truncated or stderr.truncated
        record.output_bytes = stdout.total_bytes + stderr.total_bytes
        if record.output_truncated:
            record.stdout_path = stdout.spool_path
            record.stderr_path = stderr.spool_path
            record.spool_bytes = stdout.spooled_bytes + stderr.spooled_bytes
            record.spool_dropped_bytes = (
                stdout.dropped_bytes + stderr.dropped_bytes
            )
            record.spool_limit_bytes = budget.limit
            record.spool_truncated = record.spool_dropped_bytes > 0
            record.spool_saved_prefix_only = record.spool_truncated
        record.spool_error = stdout.spool_error or stderr.spool_error

        cancelled = record.cancel_requested or self._exec.background_cancelled(
            record.handle
        )
        if cancelled:
            record.state = "cancelled"
            record.error = "Command cancelled because its job or session was stopped"
        elif timed_out:
            record.state = "timeout"
            record.error = f"Command timed out after {record.timeout} seconds"
        elif process.returncode == 0:
            record.state = "completed"
        else:
            record.state = "failed"
            record.error = f"Command exited with code {process.returncode}"

    # -- delivery -----------------------------------------------------
    def _schedule_notify(self, record: JobRecord) -> None:
        if record.callback is None or record.notify_task is not None:
            return
        if record.stopping or self._closed:
            record.notify_error = (
                _NOTIFY_SKIPPED if record.stopping else _NOTIFY_CANCELLED
            )
            return
        task = asyncio.create_task(
            self._notify_once(record), name=f"job-notify:{record.job_id}"
        )
        record.notify_task = task
        # Keep delivery tasks independently of retained records: eviction may
        # remove the record while its callback is still blocked.
        self._notifications.add(task)

    async def _notify_once(self, record: JobRecord) -> None:
        callback = record.callback
        if callback is None:
            return
        try:
            await callback(
                record.session_id,
                record.job_id,
                record.report_text(self._workspace),
            )
        except asyncio.CancelledError:
            record.notify_error = _NOTIFY_CANCELLED
            raise
        except Exception as exc:  # noqa: BLE001 - keep the settled result
            record.notify_error = f"{type(exc).__name__}: {exc}"
        else:
            record.notified = True
        finally:
            current = asyncio.current_task()
            self._notifications.discard(current)
            if record.notify_task is current:
                record.notify_task = None

    def _cancel_notification(self, record: JobRecord) -> None:
        task = record.notify_task
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            record.notify_error = _NOTIFY_CANCELLED

    def _evict_one_finished(self) -> bool:
        """Drop the oldest terminal record; pending notify tasks stay tracked."""
        for job_id in self._order:
            record = self._jobs.get(job_id)
            if record is not None and record.terminal:
                self._order.remove(job_id)
                del self._jobs[job_id]
                return True
        return False

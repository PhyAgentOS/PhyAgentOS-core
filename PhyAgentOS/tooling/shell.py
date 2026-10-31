"""Shell execution tool."""

import asyncio
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PhyAgentOS.tooling.base import Tool

_TERMINATE_GRACE_SECONDS = 5.0
_GROUP_POLL_SECONDS = 0.05

_EXEC_LAUNCH_ERROR_PREFIXES = (
    "Error: Command blocked by safety guard",
    "Error: Command timed out",
    "Error: Command cancelled",
    "Error: Exec tool is closed",
    "Error executing command:",
)
_EXIT_CODE_RE = re.compile(r"(?:^|\n)Exit code: (-?\d+)\s*\Z")


def format_exec_result(
    stdout: str | None, stderr: str | None, returncode: int
) -> str:
    """Build the legacy ``exec`` result text from already-decoded output."""
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr and stderr.strip():
        parts.append(f"STDERR:\n{stderr}")
    parts.append(f"\nExit code: {returncode}")
    return "\n".join(parts) if parts else "(no output)"


def exec_failure_message(result: str) -> str | None:
    """Return the failure message for one legacy ``exec`` result, or ``None``.

    The tool's contract is a string, so the mapping is exact: only strings the
    tool itself emits before or instead of running a command, plus a trailing
    nonzero ``Exit code:`` line, count as failures. Command output that merely
    contains ``Error:`` is not reinterpreted.
    """
    for prefix in _EXEC_LAUNCH_ERROR_PREFIXES:
        if result.startswith(prefix):
            return result.removeprefix("Error: ").removeprefix(
                "Error executing command:"
            ).strip() or result
    match = _EXIT_CODE_RE.search(result)
    if match is not None and int(match.group(1)) != 0:
        return f"Command exited with code {match.group(1)}"
    return None


class _ActiveExec:
    """One tracked ``exec`` call, alive from before spawn until settlement.

    The placeholder exists before ``create_subprocess_shell`` returns, so a
    session cancellation during async process creation can wait for the
    eventual process and stop it instead of missing it.
    """

    __slots__ = (
        "task",
        "spawn_task",
        "process",
        "spawned",
        "cancelled",
        "terminate_lock",
    )

    def __init__(self, task: asyncio.Task[Any] | None) -> None:
        self.task = task
        self.spawn_task: asyncio.Task[Any] | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.spawned = asyncio.Event()
        self.cancelled = False
        self.terminate_lock = asyncio.Lock()


@dataclass(slots=True)
class BackgroundExec:
    """One detached shell already started but not awaited by ``ExecTool``.

    The handle keeps the process and its registration record together so the
    job manager can wait, terminate, and release it through the same process
    group and cancellation machinery as a foreground ``exec``. The process is
    tracked under its owning session until :meth:`ExecTool.release_background`
    is called, so session cancellation and capability shutdown still reap it.
    """

    command: str
    cwd: str
    process: asyncio.subprocess.Process
    record: _ActiveExec
    timeout: int


class ExecTool(Tool):
    """Tool to execute shell commands.

    When the caller passes ``session_id``, running processes are tracked under
    that session so :meth:`cancel_session` and :meth:`aclose` can terminate
    them. That shell starts its own process group on POSIX, so cancellation
    also reaches children instead of leaving them orphaned. Legacy callers
    that do not pass ``session_id`` keep the previous behavior unchanged.
    """

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self._processes: dict[str, set[asyncio.subprocess.Process]] = {}
        self._active: dict[str, set[_ActiveExec]] = {}
        self._cancelled_processes: set[asyncio.subprocess.Process] = set()
        # ``start_new_session`` makes the shell its own group leader, so the
        # group id is the shell pid. Store it while the shell is alive; after
        # the shell exits its pid can no longer be resolved with getpgid.
        self._process_groups: dict[asyncio.subprocess.Process, int] = {}
        self._closed = False
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600). "
                        "For a background job the timeout applies once the job starts."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "When true, start the command as a tracked background job "
                        "and return a job handle immediately. The handle is not a "
                        "result; use job_status, job_wait, or job_cancel to inspect "
                        "or stop it. Default false keeps the old blocking behavior."
                    ),
                },
            },
            "required": ["command"],
        }

    async def execute(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None, *, session_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        if self._closed:
            return "Error: Exec tool is closed"
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        process: asyncio.subprocess.Process | None = None
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
        tracked = session_id is not None
        record: _ActiveExec | None = None
        if tracked:
            record = _ActiveExec(asyncio.current_task())
            self._active.setdefault(str(session_id), set()).add(record)

        try:
            # The creation task is independent of this coroutine's task: a
            # direct cancellation must still be able to retrieve and stop a
            # process that the OS created before the await was interrupted.
            spawn_task = asyncio.create_task(
                asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=tracked and os.name == "posix",
                )
            )
            if record is not None:
                record.spawn_task = spawn_task
            process = await asyncio.shield(spawn_task)
            self._track_process(session_id, process, record)

            if record is not None and record.cancelled:
                self._cancelled_processes.add(process)
                await self._terminate_record(record)
                return "Error: Command cancelled because its session was stopped"

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                if record is not None:
                    await self._terminate_record(record)
                else:
                    await self._terminate(process)
                return f"Error: Command timed out after {effective_timeout} seconds"

            if process in self._cancelled_processes:
                self._cancelled_processes.discard(process)
                return (
                    "Error: Command cancelled because its session was stopped"
                )

            result = format_exec_result(
                stdout.decode("utf-8", errors="replace") if stdout else None,
                stderr.decode("utf-8", errors="replace") if stderr else None,
                process.returncode,
            )

            # Legacy callers keep the fixed head+tail bound. A tracked Core
            # call leaves the full body to ToolResultLimiter, which saves the
            # complete text under .paos/tool_outputs instead of discarding it.
            max_len = None if tracked else self._MAX_OUTPUT
            if max_len is not None and len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            return result

        except asyncio.CancelledError:
            # A direct task/``wait_for`` cancellation while the process is
            # still being created must wait for the shielded creation task,
            # then stop whatever it produced. Otherwise the process would run
            # detached with no handle left to cancel it.
            if process is None and spawn_task is not None:
                process = await self._spawn_result(spawn_task)
                if process is not None:
                    self._track_process(session_id, process, record)
            # Reap the owned group before re-raising so the caller's
            # cancellation still propagates.
            if record is not None:
                await self._terminate_record(record)
            elif process is not None:
                await self._terminate(process)
            raise
        except Exception as e:
            return f"Error executing command: {str(e)}"
        finally:
            if record is not None:
                record.spawned.set()
                self._active.get(str(session_id), set()).discard(record)
                if not self._active.get(str(session_id)):
                    self._active.pop(str(session_id), None)
            if tracked and process is not None:
                processes = self._processes.get(str(session_id))
                if processes is not None:
                    processes.discard(process)
                    if not processes:
                        self._processes.pop(str(session_id), None)
            if process is not None:
                self._cancelled_processes.discard(process)
                self._process_groups.pop(process, None)

    async def start_background(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        *,
        session_id: str,
    ) -> BackgroundExec | str:
        """Start one tracked command and return without waiting for it.

        This is the same guard, environment, cwd resolution, process group,
        and session registration as :meth:`execute`; only the wait is removed.
        A returned string means the command was rejected or could not start.
        The caller owns the handle and must call :meth:`release_background`
        after the process settles.
        """
        if self._closed:
            return "Error: Exec tool is closed"
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)
        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        record = _ActiveExec(asyncio.current_task())
        self._active.setdefault(str(session_id), set()).add(record)
        process: asyncio.subprocess.Process | None = None
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None

        try:
            spawn_task = asyncio.create_task(
                asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=os.name == "posix",
                )
            )
            record.spawn_task = spawn_task
            process = await asyncio.shield(spawn_task)
            self._track_process(session_id, process, record)
            record.spawned.set()

            if record.cancelled:
                self._cancelled_processes.add(process)
                await self._terminate_record(record)
                self.release_background(session_id, record, process)
                return "Error: Command cancelled because its session was stopped"

            return BackgroundExec(
                command=command,
                cwd=cwd,
                process=process,
                record=record,
                timeout=effective_timeout,
            )
        except asyncio.CancelledError:
            if process is None and spawn_task is not None:
                process = await self._spawn_result(spawn_task)
                if process is not None:
                    self._track_process(session_id, process, record)
            record.spawned.set()
            await self._terminate_record(record)
            self.release_background(session_id, record, process)
            raise
        except Exception as exc:  # noqa: BLE001 - report a launch failure
            record.spawned.set()
            self.release_background(session_id, record, process)
            return f"Error executing command: {exc}"

    def bind_background_task(
        self, handle: BackgroundExec, task: asyncio.Task[Any]
    ) -> None:
        """Let session cancellation wait for the job task that owns the handle."""
        handle.record.task = task

    def background_cancelled(self, handle: BackgroundExec) -> bool:
        """True when session cancellation already asked this job to stop."""
        return (
            handle.record.cancelled
            or handle.process in self._cancelled_processes
        )

    async def terminate_background(self, handle: BackgroundExec) -> None:
        """Terminate the job's process group under the existing bounded rules."""
        await self._terminate_record(handle.record)

    def release_background(
        self,
        session_id: str,
        record: _ActiveExec,
        process: asyncio.subprocess.Process | None,
    ) -> None:
        """Drop the job's registration after its process has settled."""
        self._active.get(str(session_id), set()).discard(record)
        if not self._active.get(str(session_id)):
            self._active.pop(str(session_id), None)
        if process is not None:
            processes = self._processes.get(str(session_id))
            if processes is not None:
                processes.discard(process)
                if not processes:
                    self._processes.pop(str(session_id), None)
            self._cancelled_processes.discard(process)
            self._process_groups.pop(process, None)

    async def cancel_session(self, session_id: str) -> None:
        """Terminate every ``exec`` call this tool owns for one session.

        Records are registered before the subprocess is created. A cancellation
        that arrives during async creation marks the record and waits for the
        eventual spawn, then stops it, so no process is left detached. The
        handler itself is not cancelled: it still returns the legacy
        ``"Error: Command cancelled..."`` string expected by callers.
        """
        key = str(session_id)
        records = tuple(self._active.get(key, ()))
        for record in records:
            record.cancelled = True

        if records:
            await asyncio.gather(
                *(record.spawned.wait() for record in records),
                return_exceptions=True,
            )
            for record in records:
                if record.process is not None:
                    self._cancelled_processes.add(record.process)
            await asyncio.gather(
                *(self._terminate_record(record) for record in records),
                return_exceptions=True,
            )

        # Defensive fallback for any process already registered without a
        # record (for example a future caller that bypasses execute).
        processes = tuple(self._processes.pop(key, ()))
        known = {
            record.process for record in records if record.process is not None
        }
        extra = tuple(process for process in processes if process not in known)
        for process in extra:
            self._cancelled_processes.add(process)
        if extra:
            await asyncio.gather(
                *(self._terminate(process) for process in extra),
                return_exceptions=True,
            )

        # Waiting for the handler is what leaves no pending task behind after
        # cancellation. Never await the caller's own task.
        current = asyncio.current_task()
        tasks = tuple(
            record.task
            for record in records
            if record.task is not None and record.task is not current
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Stop all owned work and refuse to start new processes afterwards."""
        self._closed = True
        keys = set(self._active) | set(self._processes)
        for session_id in tuple(keys):
            await self.cancel_session(session_id)

    def _track_process(
        self,
        session_id: str | None,
        process: asyncio.subprocess.Process,
        record: _ActiveExec | None,
    ) -> None:
        if record is not None:
            record.process = process
            record.spawned.set()
        if session_id is not None:
            self._processes.setdefault(str(session_id), set()).add(process)
            if os.name == "posix":
                self._process_groups[process] = process.pid

    @staticmethod
    async def _spawn_result(
        spawn_task: asyncio.Task[asyncio.subprocess.Process],
    ) -> asyncio.subprocess.Process | None:
        """Await an independent creation task after local cancellation."""
        try:
            return await spawn_task
        except (Exception, asyncio.CancelledError):
            return None

    async def _terminate_record(self, record: _ActiveExec) -> None:
        async with record.terminate_lock:
            if record.process is not None:
                await self._terminate(record.process)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        pgid = self._process_groups.get(process)
        if pgid is None:
            # Legacy/untracked process: no owned group, keep the original
            # immediate kill and bounded reaping.
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await self._wait_for_exit(process)
            return

        # Signal the stored group even after the shell exits: a descendant may
        # still hold the pipes or ignore SIGTERM after its parent died.
        self._signal_group(pgid, hard=False)
        await self._wait_for_exit(process)
        if self._group_alive(pgid):
            self._signal_group(pgid, hard=True)
        await self._wait_for_exit(process)
        # Do not return while the group is still alive after SIGKILL: the loop
        # above is bounded, but a killed group normally disappears immediately.
        deadline = asyncio.get_running_loop().time() + _TERMINATE_GRACE_SECONDS
        while self._group_alive(pgid):
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(_GROUP_POLL_SECONDS)

    async def _wait_for_exit(
        self, process: asyncio.subprocess.Process
    ) -> None:
        if process.returncode is not None:
            return
        try:
            await asyncio.wait_for(
                process.wait(), timeout=_TERMINATE_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            # The shell did not die within the grace period. The group kill
            # (or the caller's next step) reports the failure.
            pass

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        if os.name != "posix":
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _signal_group(pgid: int, *, hard: bool) -> None:
        if os.name != "posix":
            return
        try:
            sig = signal.SIGKILL if hard else signal.SIGTERM
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)   # Windows: C:\...
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths

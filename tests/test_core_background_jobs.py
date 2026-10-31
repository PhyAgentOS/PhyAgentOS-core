"""Background shell jobs owned by the shared general-tools capability.

The tests use real local processes. Only the optional completion callback is a
fake. They pin the accepted handle, later real result, cross-session rejection,
process-group cancellation, shutdown cleanup, bounded output, and the rule that
a nonzero exit or timeout is never a successful outcome.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import PhyAgentOS.tooling.shell as shell_module
from PhyAgentOS.agent.capabilities.general_tools_capability import (
    GeneralToolsCapability,
)
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolOutcome,
    ToolStatus,
)
from PhyAgentOS.agent.services.job_manager import JobManager
from PhyAgentOS.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ExecToolConfig,
    ToolsConfig,
    WebSearchConfig,
    WebToolsConfig,
)
from PhyAgentOS.tooling.shell import ExecTool


def _config(workspace: Path) -> Config:
    return Config(
        agents=AgentsConfig(
            defaults=AgentDefaults(workspace=str(workspace)),
        ),
        tools=ToolsConfig(
            exec=ExecToolConfig(timeout=10),
            web=WebToolsConfig(search=WebSearchConfig(api_key="")),
        ),
    )


def _capability(workspace: Path, **kwargs: Any) -> GeneralToolsCapability:
    workspace.mkdir(parents=True, exist_ok=True)
    return GeneralToolsCapability(_config(workspace), **kwargs)


def _direct_manager(workspace: Path, **kwargs: Any) -> JobManager:
    workspace.mkdir(parents=True, exist_ok=True)
    return JobManager(
        ExecTool(working_dir=str(workspace), timeout=10),
        workspace,
        **kwargs,
    )


class _CountingEvent(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0
        self.active_waiters = 0

    async def wait(self) -> bool:
        self.wait_calls += 1
        self.active_waiters += 1
        try:
            return await super().wait()
        finally:
            self.active_waiters -= 1


def _context(session_id: str = "session-a") -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="turn-a",
        step_id="step-a",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


async def _invoke(
    capability: GeneralToolsCapability,
    name: str,
    arguments: dict[str, Any],
    *,
    context: ExecutionContext | None = None,
    call_id: str | None = None,
) -> ToolOutcome:
    registrations = {
        registration.definition.name: registration
        for registration in capability.tool_registrations()
    }
    call = ToolCall(
        tool_call_id=call_id or f"call-{name}",
        name=name,
        arguments=arguments,
    )
    return await registrations[name].handler(call, context or _context())


def _job_id(outcome: ToolOutcome) -> str:
    data = json.loads(outcome.text)
    return data["job_id"]


async def _wait_for_pid(path: Path) -> int:
    for _ in range(400):
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        await asyncio.sleep(0.01)
    raise AssertionError(f"pid file was not written: {path}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_until_dead(pid: int) -> None:
    for _ in range(400):
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"process {pid} survived cancellation")


async def test_background_exec_returns_handle_then_query_returns_real_result(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)
    command = f"{sys.executable} -c 'import time; time.sleep(0.2); print(\"bg-done\")'"
    try:
        accepted = await _invoke(
            capability, "exec", {"command": command, "background": True}
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        data = json.loads(accepted.text)
        assert data["status"] == "accepted"
        assert data["state"] == "running"
        assert data["session_id"] == "session-a"
        assert "stdout" not in data
        assert "exit_code" not in data
        assert "completed" not in accepted.text.lower()
        job_id = data["job_id"]

        listed = await _invoke(capability, "job_status", {})
        assert listed.status is ToolStatus.SUCCEEDED
        listed_data = json.loads(listed.text)
        assert listed_data["state"] == "listed"
        assert [job["job_id"] for job in listed_data["jobs"]] == [job_id]

        waited = await _invoke(
            capability, "job_wait", {"job_id": job_id, "timeout": 5}
        )
        assert waited.status is ToolStatus.SUCCEEDED
        result = json.loads(waited.text)
        assert result["state"] == "completed"
        assert result["exit_code"] == 0
        assert "bg-done" in result["stdout"]
        assert "error" not in result

        again = await _invoke(capability, "job_status", {"job_id": job_id})
        assert again.status is ToolStatus.SUCCEEDED
        assert json.loads(again.text)["state"] == "completed"
    finally:
        await capability.aclose()


async def test_cross_session_status_and_cancel_are_rejected_without_leaking(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)
    command = "sleep 30 & echo $! > owned.pid; wait"
    try:
        accepted = await _invoke(
            capability,
            "exec",
            {"command": command, "background": True},
            context=_context("session-a"),
        )
        job_id = _job_id(accepted)
        pid = await _wait_for_pid(tmp_path / "owned.pid")

        status = await _invoke(
            capability,
            "job_status",
            {"job_id": job_id},
            context=_context("session-b"),
        )
        assert status.status is ToolStatus.FAILED
        assert "does not belong to this session" in (
            status.error.message if status.error else ""
        )
        assert "owned.pid" not in status.text

        cancel = await _invoke(
            capability,
            "job_cancel",
            {"job_id": job_id},
            context=_context("session-b"),
        )
        assert cancel.status is ToolStatus.FAILED
        assert "does not belong to this session" in (
            cancel.error.message if cancel.error else ""
        )
        assert _pid_alive(pid)
        assert capability._jobs.pending("session-a") == 1

        cancelled = await _invoke(
            capability,
            "job_cancel",
            {"job_id": job_id},
            context=_context("session-a"),
        )
        assert cancelled.status is ToolStatus.FAILED
        assert "cancelled" in (cancelled.error.message if cancelled.error else "")
        await _wait_until_dead(pid)
        assert capability._jobs.pending("session-a") == 0
    finally:
        await capability.aclose()


async def test_job_cancel_reaps_the_real_process_group(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)
    command = "sleep 30 & echo $! > cancel.pid; wait"
    try:
        accepted = await _invoke(
            capability, "exec", {"command": command, "background": True}
        )
        job_id = _job_id(accepted)
        pid = await _wait_for_pid(tmp_path / "cancel.pid")

        cancelled = await _invoke(
            capability, "job_cancel", {"job_id": job_id}
        )
        assert cancelled.status is ToolStatus.FAILED
        assert "cancelled" in (cancelled.error.message if cancelled.error else "")
        await _wait_until_dead(pid)
        record = capability._jobs.jobs_for("session-a")[0]
        assert record.terminal
        assert record.state == "cancelled"
        tool = capability._exec_tool
        assert tool._active == {}
        assert tool._processes == {}

        late = await _invoke(capability, "job_cancel", {"job_id": job_id})
        assert "already finished" in late.text
    finally:
        await capability.aclose()


async def test_session_cancel_stops_only_that_sessions_jobs(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)
    command = "sleep 30 & echo $! > {name}.pid; wait"
    try:
        first = await _invoke(
            capability,
            "exec",
            {"command": command.format(name="first"), "background": True},
            context=_context("session-a"),
        )
        second = await _invoke(
            capability,
            "exec",
            {"command": command.format(name="second"), "background": True},
            context=_context("session-b"),
        )
        first_pid = await _wait_for_pid(tmp_path / "first.pid")
        second_pid = await _wait_for_pid(tmp_path / "second.pid")
        assert _job_id(first) != _job_id(second)

        await capability.cancel("session-a", None)
        await _wait_until_dead(first_pid)
        assert _pid_alive(second_pid)
        assert capability._jobs.pending("session-a") == 0
        assert capability._jobs.pending("session-b") == 1

        await capability.aclose()
        await _wait_until_dead(second_pid)
        assert capability._jobs.pending() == 0
    finally:
        await capability.aclose()


async def test_close_cleans_every_job_and_rejects_new_work(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)
    command = "sleep 30 & echo $! > {name}.pid; wait"
    accepted = await _invoke(
        capability,
        "exec",
        {"command": command.format(name="close-a"), "background": True},
        context=_context("session-a"),
    )
    accepted_b = await _invoke(
        capability,
        "exec",
        {"command": command.format(name="close-b"), "background": True},
        context=_context("session-b"),
    )
    assert _job_id(accepted) and _job_id(accepted_b)
    pid_a = await _wait_for_pid(tmp_path / "close-a.pid")
    pid_b = await _wait_for_pid(tmp_path / "close-b.pid")

    await asyncio.wait_for(capability.aclose(), timeout=10)
    await _wait_until_dead(pid_a)
    await _wait_until_dead(pid_b)
    assert capability._jobs.pending() == 0
    assert capability._exec_tool._active == {}

    rejected = await _invoke(
        capability, "exec", {"command": "printf never", "background": True}
    )
    assert rejected.status is ToolStatus.FAILED
    assert "closed" in (rejected.error.message if rejected.error else "")
    missing = await _invoke(capability, "job_status", {"job_id": "job-missing"})
    assert missing.status is ToolStatus.FAILED


async def test_background_output_is_bounded_and_spooled_to_disk(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)
    command = f"{sys.executable} -c 'print(\"A\" * 200000)'"
    try:
        accepted = await _invoke(
            capability,
            "exec",
            {"command": command, "background": True},
            call_id="call-bg-big",
        )
        job_id = _job_id(accepted)
        waited = await _invoke(
            capability, "job_wait", {"job_id": job_id, "timeout": 10}
        )
        assert waited.status is ToolStatus.SUCCEEDED
        assert len(waited.text) <= 8000

        record = capability._jobs.jobs_for("session-a")[0]
        assert record.state == "completed"
        assert record.output_truncated is True
        assert len(record.stdout) <= 64 * 1024
        assert record.output_bytes >= 200_000
        assert record.stdout_path is not None
        assert record.stdout_path.exists()
        assert record.stdout_path.stat().st_size >= 200_000
        assert ".paos/tool_outputs/" in waited.text
    finally:
        await capability.aclose()


async def test_nonzero_exit_and_timeout_are_failed_outcomes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(shell_module, "_TERMINATE_GRACE_SECONDS", 0.2)
    capability = _capability(tmp_path)
    try:
        failed = await _invoke(
            capability,
            "exec",
            {
                "command": f"{sys.executable} -c 'import sys; sys.exit(3)'",
                "background": True,
            },
        )
        failed_job = _job_id(failed)
        waited = await _invoke(
            capability, "job_wait", {"job_id": failed_job, "timeout": 5}
        )
        assert waited.status is ToolStatus.FAILED
        assert waited.error is not None
        assert waited.error.message == "Command exited with code 3"
        assert json.loads(waited.text)["state"] == "failed"

        timed = await _invoke(
            capability,
            "exec",
            {
                "command": "sleep 30 & echo $! > timed.pid; wait",
                "background": True,
                "timeout": 1,
            },
        )
        timed_job = _job_id(timed)
        pid = await _wait_for_pid(tmp_path / "timed.pid")
        timeout = await _invoke(
            capability, "job_wait", {"job_id": timed_job, "timeout": 5}
        )
        assert timeout.status is ToolStatus.FAILED
        assert "timed out" in (timeout.error.message if timeout.error else "")
        assert json.loads(timeout.text)["state"] == "timeout"
        await _wait_until_dead(pid)
    finally:
        await capability.aclose()


async def test_wait_expiry_reports_running_not_completion(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    try:
        accepted = await _invoke(
            capability,
            "exec",
            {"command": "sleep 30", "background": True},
        )
        job_id = _job_id(accepted)
        waited = await _invoke(
            capability, "job_wait", {"job_id": job_id, "timeout": 1}
        )
        assert waited.status is ToolStatus.SUCCEEDED
        data = json.loads(waited.text)
        assert data["state"] == "running"
        assert data["wait_timed_out"] is True
        assert "not a job timeout" in data["message"]
    finally:
        await capability.aclose()


async def test_notify_factory_freezes_route_before_start_and_ignores_rebind(
    tmp_path: Path,
) -> None:
    route = {"target": "chat-A"}
    captured: list[tuple[str, str, str]] = []
    contexts: list[tuple[str, str]] = []
    event = asyncio.Event()

    def factory(context: ExecutionContext):
        contexts.append((context.session_id, context.turn_id))
        frozen_target = route["target"]

        async def send(session_id: str, job_id: str, text: str) -> None:
            captured.append((frozen_target, job_id, text))
            event.set()

        return send

    capability = _capability(tmp_path, job_notify_factory=factory)
    try:
        accepted = await _invoke(
            capability,
            "exec",
            {"command": "sleep 0.2; printf routed", "background": True},
            context=_context("session-a"),
        )
        job_id = _job_id(accepted)
        assert contexts == [("session-a", "turn-a")]

        # A live routing change after acceptance must not move this job.
        route["target"] = "chat-B"
        await asyncio.wait_for(event.wait(), timeout=5)

        assert len(captured) == 1
        target, captured_job, text = captured[0]
        assert target == "chat-A"
        assert captured_job == job_id
        assert "routed" in text
    finally:
        await capability.aclose()


async def test_notify_factory_none_disables_default_and_query_still_works(
    tmp_path: Path,
) -> None:
    default_items: list[tuple[str, str, str]] = []

    async def default_notify(session_id: str, job_id: str, text: str) -> None:
        default_items.append((session_id, job_id, text))

    capability = _capability(
        tmp_path,
        job_notify=default_notify,
        job_notify_factory=lambda _context: None,
    )
    try:
        accepted = await _invoke(
            capability,
            "exec",
            {"command": "printf query-only", "background": True},
        )
        job_id = _job_id(accepted)
        assert capability._jobs.jobs_for("session-a")[0].callback is None

        waited = await _invoke(
            capability, "job_wait", {"job_id": job_id, "timeout": 5}
        )
        assert waited.status is ToolStatus.SUCCEEDED
        assert "query-only" in json.loads(waited.text)["stdout"]
        await asyncio.sleep(0.05)
        assert default_items == []
    finally:
        await capability.aclose()


async def test_close_cancels_a_blocked_job_notification_without_blocking(
    tmp_path: Path,
) -> None:
    callback_started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_notify(_session_id: str, _job_id: str, _text: str) -> None:
        callback_started.set()
        await never.wait()

    capability = _capability(tmp_path, job_notify_factory=lambda _ctx: blocked_notify)
    accepted = await _invoke(
        capability, "exec", {"command": "printf close-notify", "background": True}
    )
    assert _job_id(accepted)
    await asyncio.wait_for(callback_started.wait(), timeout=5)

    await asyncio.wait_for(capability.aclose(), timeout=2)
    record = capability._jobs.jobs_for("session-a")[0]
    assert record.notify_task is None
    assert record.notify_error is not None
    assert "cancelled" in record.notify_error
    assert capability._jobs.pending() == 0


async def test_notify_factory_failure_rejects_before_any_process(
    tmp_path: Path,
) -> None:
    def factory(_context: ExecutionContext):
        raise RuntimeError("route is not bound")

    capability = _capability(tmp_path, job_notify_factory=factory)
    try:
        rejected = await _invoke(
            capability,
            "exec",
            {"command": "printf must-not-run", "background": True},
        )
        assert rejected.status is ToolStatus.FAILED
        assert "route is not bound" in (
            rejected.error.message if rejected.error else ""
        )
        assert capability._jobs.jobs_for("session-a") == ()
    finally:
        await capability.aclose()


async def test_notification_is_delivered_once_with_frozen_session(
    tmp_path: Path,
) -> None:
    delivered: list[tuple[str, str, str]] = []
    event = asyncio.Event()

    async def notify(session_id: str, job_id: str, text: str) -> None:
        delivered.append((session_id, job_id, text))
        event.set()

    capability = _capability(tmp_path, job_notify=notify)
    try:
        accepted = await _invoke(
            capability, "exec", {"command": "printf notified", "background": True}
        )
        job_id = _job_id(accepted)
        await asyncio.wait_for(event.wait(), timeout=5)
        await asyncio.sleep(0.05)

        assert len(delivered) == 1
        session_id, notified_job, text = delivered[0]
        assert session_id == "session-a"
        assert notified_job == job_id
        assert "notified" in text
        assert "completed" in text

        await _invoke(capability, "job_status", {"job_id": job_id})
        await _invoke(capability, "job_wait", {"job_id": job_id, "timeout": 1})
        await asyncio.sleep(0.05)
        assert len(delivered) == 1
    finally:
        await capability.aclose()


async def test_max_jobs_reserves_before_spawn_and_rejects_third(
    tmp_path: Path,
) -> None:
    manager = _direct_manager(tmp_path, max_jobs=2)
    commands = [
        f"sleep 30 & echo $! > slot-{index}.pid; wait" for index in range(6)
    ]
    try:
        results = await asyncio.gather(
            *(
                manager.start(
                    command=command,
                    session_id="session-a",
                    turn_id="turn-a",
                )
                for command in commands
            )
        )
        parsed = [json.loads(result) for result in results]
        accepted = [item for item in parsed if item.get("status") == "accepted"]
        rejected = [item for item in parsed if "error" in item]
        assert len(accepted) == 2
        assert len(rejected) == 4
        assert all("nothing was started" in item["error"] for item in rejected)
        assert manager.pending("session-a") == 2
        assert len(manager.jobs_for("session-a")) == 2

        await asyncio.sleep(0.2)
        pid_files = sorted(tmp_path.glob("slot-*.pid"))
        assert len(pid_files) == 2
    finally:
        await asyncio.wait_for(manager.aclose(), timeout=10)


async def test_repeated_job_wait_timeouts_do_not_leak_event_waiters(
    tmp_path: Path,
) -> None:
    manager = _direct_manager(tmp_path)
    try:
        accepted = await manager.start(
            command="sleep 30", session_id="session-a", turn_id="turn-a"
        )
        job_id = json.loads(accepted)["job_id"]
        record = manager.jobs_for("session-a")[0]
        counter = _CountingEvent()
        record.done = counter

        for _ in range(4):
            waited = await manager.handle(
                "job_wait",
                {"job_id": job_id, "timeout": 1},
                session_id="session-a",
                turn_id="turn-a",
            )
            data = json.loads(waited)
            assert data["wait_timed_out"] is True
            assert counter.active_waiters == 0

        assert counter.wait_calls == 4
        assert counter.active_waiters == 0
    finally:
        await asyncio.wait_for(manager.aclose(), timeout=10)


async def test_blocked_notify_survives_record_eviction_and_close_cancels_it(
    tmp_path: Path,
) -> None:
    manager = _direct_manager(tmp_path, max_jobs=1)
    started: asyncio.Queue[str] = asyncio.Queue()
    never = asyncio.Event()

    async def blocked(_session_id: str, job_id: str, _text: str) -> None:
        await started.put(job_id)
        await never.wait()

    manager._notify = blocked
    try:
        first = json.loads(
            await manager.start(
                command="printf first", session_id="session-a", turn_id="turn-a"
            )
        )
        first_record = manager.jobs_for("session-a")[0]
        await asyncio.wait_for(started.get(), timeout=5)

        second = json.loads(
            await manager.start(
                command="printf second", session_id="session-a", turn_id="turn-a"
            )
        )
        assert first["job_id"] not in {
            record.job_id for record in manager.jobs_for("session-a")
        }
        second_record = manager.jobs_for("session-a")[0]
        assert second_record.job_id == second["job_id"]
        await asyncio.wait_for(started.get(), timeout=5)
        assert len(manager._notifications) == 2

        await asyncio.wait_for(manager.aclose(), timeout=2)
        assert manager._notifications == set()
        assert first_record.notify_error is not None
        assert "cancelled" in first_record.notify_error
        assert second_record.notify_error is not None
        leftovers = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("job-notify:")
        ]
        assert leftovers == []
    finally:
        await asyncio.wait_for(manager.aclose(), timeout=10)


async def test_spool_budget_is_shared_and_reports_partial_output(
    tmp_path: Path,
) -> None:
    manager = _direct_manager(
        tmp_path, buffer_bytes=1024, max_spool_bytes=4096, max_jobs=2
    )
    try:
        accepted = await manager.start(
            command=(
                f"{sys.executable} -c 'import sys; "
                'sys.stdout.write("A" * 3000); '
                'sys.stderr.write("B" * 3000)\''
            ),
            session_id="session-a",
            turn_id="turn-a",
        )
        job_id = json.loads(accepted)["job_id"]
        waited = await manager.handle(
            "job_wait",
            {"job_id": job_id, "timeout": 10},
            session_id="session-a",
            turn_id="turn-a",
        )
        data = json.loads(waited)
        record = manager.jobs_for("session-a")[0]
        assert data["state"] == "completed"
        assert record.output_truncated is True
        assert record.output_bytes >= 6000
        assert record.spool_bytes <= 4096
        assert record.spool_truncated is True
        assert record.spool_dropped_bytes > 0
        assert data["spool_complete"] is False
        assert data["spool_truncated"] is True
        assert data["spool_saved_prefix_only"] is True
        assert data["spool_bytes"] <= 4096

        spool_paths = [path for path in (record.stdout_path, record.stderr_path) if path]
        assert spool_paths
        assert sum(path.stat().st_size for path in spool_paths) <= 4096
        report = record.report_text(tmp_path)
        assert "not stored" in report
        assert "saved output prefix" in report
        assert "Full output" not in report
    finally:
        await asyncio.wait_for(manager.aclose(), timeout=10)


async def test_lifetime_spool_budget_counts_existing_files_without_deleting(
    tmp_path: Path,
) -> None:
    spool_dir = tmp_path / ".paos" / "tool_outputs"
    spool_dir.mkdir(parents=True)
    old_file = spool_dir / "job-old-stdout.log"
    old_file.write_bytes(b"x" * 4096)
    manager = _direct_manager(
        tmp_path,
        buffer_bytes=1024,
        max_spool_bytes=4096,
        max_spool_lifetime_bytes=4096,
        max_jobs=2,
    )
    try:
        assert manager._spool_used == 4096
        accepted = await manager.start(
            command=f"{sys.executable} -c 'print(\"Z\" * 3000)'",
            session_id="session-a",
            turn_id="turn-a",
        )
        job_id = json.loads(accepted)["job_id"]
        waited = await manager.handle(
            "job_wait",
            {"job_id": job_id, "timeout": 10},
            session_id="session-a",
            turn_id="turn-a",
        )
        data = json.loads(waited)
        record = manager.jobs_for("session-a")[0]
        assert record.output_truncated is True
        assert record.spool_bytes == 0
        assert record.stdout_path is None
        assert record.spool_saved_prefix_only is True
        assert data["spool_saved_prefix_only"] is True
        assert data["spool_complete"] is False
        assert manager._spool_used == 4096
        # The startup scan is read-only: the historical file stays untouched.
        assert old_file.read_bytes() == b"x" * 4096
    finally:
        await asyncio.wait_for(manager.aclose(), timeout=10)


async def test_close_during_delayed_spawn_never_accepts_and_reaps_process(
    tmp_path: Path, monkeypatch
) -> None:
    real_create = shell_module.asyncio.create_subprocess_shell
    spawned = asyncio.Event()
    release = asyncio.Event()

    async def delayed_create(*args: Any, **kwargs: Any) -> Any:
        process = await real_create(*args, **kwargs)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(
        shell_module.asyncio, "create_subprocess_shell", delayed_create
    )
    manager = _direct_manager(tmp_path)
    start_task = asyncio.create_task(
        manager.start(
            command="sleep 30 & echo $! > close-race.pid; wait",
            session_id="session-a",
            turn_id="turn-a",
        )
    )
    await asyncio.wait_for(spawned.wait(), timeout=2)
    pid = await _wait_for_pid(tmp_path / "close-race.pid")

    close_task = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0.05)
    assert close_task.done() is False

    release.set()
    accepted = await asyncio.wait_for(start_task, timeout=5)
    await asyncio.wait_for(close_task, timeout=5)

    data = json.loads(accepted)
    assert "error" in data
    assert data.get("status") != "accepted"
    assert manager.pending("session-a") == 0
    assert manager.jobs_for("session-a") == ()
    assert manager._inflight == {}
    assert manager._reserved == 0
    await _wait_until_dead(pid)
    assert manager._exec._active == {}
    assert manager._exec._processes == {}

"""Behavior tests for the Core general-tools capability.

The tests exercise the real filesystem and real child processes, and use a
fake HTTP client where a network call would otherwise be required. They pin
the tool set, legacy-compatible schemas, failure mapping, workspace
restriction, session-scoped cancellation, and oversized-output handling.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import PhyAgentOS.tooling as tooling
import PhyAgentOS.tooling.shell as shell_module
import PhyAgentOS.tooling.web as web_module
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
from PhyAgentOS.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ExecToolConfig,
    ToolsConfig,
    WebSearchConfig,
    WebToolsConfig,
)
from PhyAgentOS.tooling.base import SessionTaskRegistry

EXPECTED_TOOLS = {
    "read_file": ["path"],
    "write_file": ["path", "content"],
    "edit_file": ["path", "old_text", "new_text"],
    "list_dir": ["path"],
    "exec": ["command"],
    "web_search": ["query"],
    "web_fetch": ["url"],
    "query_scene_graph": ["query_type"],
}


def _config(
    workspace: Path,
    *,
    restrict: bool = False,
    api_key: str = "",
) -> Config:
    return Config(
        agents=AgentsConfig(
            defaults=AgentDefaults(workspace=str(workspace)),
        ),
        tools=ToolsConfig(
            restrict_to_workspace=restrict,
            exec=ExecToolConfig(timeout=10),
            web=WebToolsConfig(search=WebSearchConfig(api_key=api_key)),
        ),
    )


def _capability(workspace: Path, **kwargs: Any) -> GeneralToolsCapability:
    workspace.mkdir(parents=True, exist_ok=True)
    return GeneralToolsCapability(_config(workspace, **kwargs))


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


async def _wait_for_pid(path: Path) -> int:
    for _ in range(200):
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
    for _ in range(200):
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"process {pid} survived cancellation")


def test_tool_set_and_schemas_are_preserved(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    registrations = capability.tool_registrations()

    assert capability.name == "general_tools"
    assert capability.prompt_context(_context()) is None
    assert {item.definition.name for item in registrations} == set(EXPECTED_TOOLS)
    for registration in registrations:
        definition = registration.definition
        assert definition.description
        assert definition.parameters["type"] == "object"
        assert definition.parameters["required"] == EXPECTED_TOOLS[definition.name]
        assert registration.parallel_safe is (
            definition.name in {
                "read_file", "list_dir", "web_search", "web_fetch",
                "query_scene_graph",
            }
        )


def test_legacy_paths_reexport_the_shared_tools() -> None:
    from PhyAgentOS.agent_legacy import tool_results as legacy_results
    from PhyAgentOS.agent_legacy.tools import base as legacy_base
    from PhyAgentOS.agent_legacy.tools import filesystem as legacy_fs
    from PhyAgentOS.agent_legacy.tools import scene_graph as legacy_scene
    from PhyAgentOS.agent_legacy.tools import shell as legacy_shell
    from PhyAgentOS.agent_legacy.tools import web as legacy_web

    assert legacy_base.Tool is tooling.Tool
    assert legacy_fs.ReadFileTool is tooling.ReadFileTool
    assert legacy_shell.ExecTool is tooling.ExecTool
    assert legacy_scene.SceneGraphQueryTool is tooling.SceneGraphQueryTool
    assert legacy_web.WebFetchTool is tooling.WebFetchTool
    assert legacy_results.ToolResultLimiter is tooling.ToolResultLimiter
    assert callable(legacy_fs._resolve_path)
    assert callable(legacy_fs._find_match)
    assert callable(legacy_web._validate_url)
    assert isinstance(legacy_web.USER_AGENT, str)
    assert isinstance(legacy_web.MAX_REDIRECTS, int)


async def test_filesystem_round_trip_and_explicit_errors(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    written = await _invoke(
        capability,
        "write_file",
        {"path": "notes/a.txt", "content": "alpha\nbeta\n"},
    )
    assert written.is_success
    assert (tmp_path / "notes" / "a.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"

    read = await _invoke(capability, "read_file", {"path": "notes/a.txt"})
    assert read.is_success
    assert read.text.startswith("1| alpha\n2| beta")

    edited = await _invoke(
        capability,
        "edit_file",
        {"path": "notes/a.txt", "old_text": "beta", "new_text": "gamma"},
    )
    assert edited.is_success
    assert "gamma" in (tmp_path / "notes" / "a.txt").read_text(encoding="utf-8")

    listed = await _invoke(capability, "list_dir", {"path": "notes"})
    assert listed.is_success
    assert "a.txt" in listed.text

    missing = await _invoke(capability, "read_file", {"path": "missing.txt"})
    assert missing.status is ToolStatus.FAILED
    assert missing.error is not None
    assert missing.error.message == "File not found: missing.txt"
    assert missing.text.startswith("Error: File not found")


async def test_restriction_blocks_paths_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    capability = _capability(workspace, restrict=True)

    read = await _invoke(capability, "read_file", {"path": str(outside)})
    assert read.status is ToolStatus.FAILED
    assert "outside allowed directory" in (read.error.message if read.error else "")

    write = await _invoke(
        capability,
        "write_file",
        {"path": "../escape.txt", "content": "nope"},
    )
    assert write.status is ToolStatus.FAILED
    assert not (tmp_path / "escape.txt").exists()

    blocked = await _invoke(capability, "exec", {"command": "cat /etc/passwd"})
    assert blocked.status is ToolStatus.FAILED
    assert "path outside working dir" in (blocked.error.message if blocked.error else "")


async def test_exec_restriction_rejects_cwd_outside_workspace_before_spawn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "sub").mkdir(parents=True)
    capability = _capability(workspace, restrict=True)

    absolute = await _invoke(
        capability,
        "exec",
        {
            "command": "pwd > escaped.txt",
            "working_dir": str(outside),
        },
    )
    assert absolute.status is ToolStatus.FAILED
    assert "outside workspace" in (
        absolute.error.message if absolute.error else ""
    )
    assert not (outside / "escaped.txt").exists()

    relative = await _invoke(
        capability,
        "exec",
        {"command": "pwd > escaped.txt", "working_dir": "../outside"},
    )
    assert relative.status is ToolStatus.FAILED
    assert not (outside / "escaped.txt").exists()

    allowed = await _invoke(
        capability,
        "exec",
        {"command": "pwd > inside.txt", "working_dir": "sub"},
    )
    assert allowed.is_success
    written = (workspace / "sub" / "inside.txt").read_text(encoding="utf-8")
    assert written.strip() == str((workspace / "sub").resolve())


async def test_exec_runs_real_command_and_maps_failures(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    result = await _invoke(capability, "exec", {"command": "printf 'hello\\n'"})
    assert result.is_success
    assert "hello" in result.text
    assert "Exit code: 0" in result.text

    # A nonzero legacy exit line is a failed native outcome; the body keeps
    # the original string.
    non_zero = await _invoke(
        capability,
        "exec",
        {"command": f"{sys.executable} -c 'import sys; sys.exit(3)'"},
    )
    assert non_zero.status is ToolStatus.FAILED
    assert non_zero.error is not None
    assert non_zero.error.message == "Command exited with code 3"
    assert "Exit code: 3" in non_zero.text

    # Command output that merely starts with "Error:" is not a tool failure.
    printed = await _invoke(
        capability,
        "exec",
        {"command": "printf 'Error: command text\\n'"},
    )
    assert printed.is_success
    assert printed.text.startswith("Error: command text")

    dangerous = await _invoke(capability, "exec", {"command": "rm -rf notes"})
    assert dangerous.status is ToolStatus.FAILED
    assert "dangerous pattern" in (dangerous.error.message if dangerous.error else "")


async def test_exec_launch_failure_is_a_failed_outcome(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    missing_cwd = tmp_path / "does-not-exist"

    outcome = await _invoke(
        capability,
        "exec",
        {"command": "printf never-runs", "working_dir": str(missing_cwd)},
    )

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.text.startswith("Error executing command:")


async def test_exec_session_cancellation_scopes_and_reaps_children(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)
    command = "sleep 30 & echo $! > {name}.pid; wait"

    first = asyncio.create_task(
        _invoke(
            capability,
            "exec",
            {"command": command.format(name="first")},
            context=_context("session-a"),
        )
    )
    second = asyncio.create_task(
        _invoke(
            capability,
            "exec",
            {"command": command.format(name="second")},
            context=_context("session-b"),
        )
    )
    first_pid = await _wait_for_pid(tmp_path / "first.pid")
    second_pid = await _wait_for_pid(tmp_path / "second.pid")

    await capability.cancel("session-a", _context("session-a"))

    first_result = await asyncio.wait_for(first, timeout=5)
    assert first_result.status is ToolStatus.FAILED
    assert "cancelled" in (first_result.error.message if first_result.error else "")
    await _wait_until_dead(first_pid)

    assert not second.done()
    assert _pid_alive(second_pid)

    await capability.aclose()
    second_result = await asyncio.wait_for(second, timeout=5)
    assert second_result.status is ToolStatus.FAILED
    await _wait_until_dead(second_pid)


async def test_exec_timeout_kills_sigterm_ignoring_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_module, "_TERMINATE_GRACE_SECONDS", 0.2)
    capability = _capability(tmp_path)
    helper = tmp_path / "ignore_term.py"
    helper.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command = f"{sys.executable} ignore_term.py & echo $! > ignored.pid; wait"

    outcome = await _invoke(
        capability, "exec", {"command": command, "timeout": 1}
    )

    assert outcome.status is ToolStatus.FAILED
    assert "timed out" in (outcome.error.message if outcome.error else "")
    child_pid = await _wait_for_pid(tmp_path / "ignored.pid")
    await _wait_until_dead(child_pid)


async def test_exec_direct_cancellation_reaps_own_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_module, "_TERMINATE_GRACE_SECONDS", 0.2)
    capability = _capability(tmp_path)
    command = "sleep 30 & echo $! > direct.pid; wait"

    task = asyncio.create_task(
        _invoke(
            capability,
            "exec",
            {"command": command},
            context=_context("session-a"),
        )
    )
    child_pid = await _wait_for_pid(tmp_path / "direct.pid")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    await _wait_until_dead(child_pid)


def _install_delayed_spawn(
    monkeypatch: pytest.MonkeyPatch,
    started: asyncio.Event,
    release: asyncio.Event,
) -> None:
    """Spawn a real process, then block before returning it to execute()."""
    real_create = asyncio.create_subprocess_shell

    async def delayed_create(*args: Any, **kwargs: Any) -> Any:
        process = await real_create(*args, **kwargs)
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(
        shell_module.asyncio, "create_subprocess_shell", delayed_create
    )


def _exec_tool(capability: GeneralToolsCapability) -> tooling.ExecTool:
    return next(
        tool for tool in capability._tools
        if isinstance(tool, tooling.ExecTool)
    )


async def test_cancel_during_process_creation_terminates_eventual_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_module, "_TERMINATE_GRACE_SECONDS", 0.2)
    capability = _capability(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    _install_delayed_spawn(monkeypatch, started, release)
    command = "sleep 30 & echo $! > gap-cancel.pid; wait"

    exec_task = asyncio.create_task(
        _invoke(
            capability,
            "exec",
            {"command": command},
            context=_context("session-a"),
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    child_pid = await _wait_for_pid(tmp_path / "gap-cancel.pid")

    cancel_task = asyncio.create_task(
        capability.cancel("session-a", _context("session-a"))
    )
    await asyncio.sleep(0.05)
    assert not cancel_task.done()

    release.set()
    await asyncio.wait_for(cancel_task, 5)
    outcome = await asyncio.wait_for(exec_task, 5)

    assert outcome.status is ToolStatus.FAILED
    assert "cancelled" in (outcome.error.message if outcome.error else "")
    await _wait_until_dead(child_pid)
    tool = _exec_tool(capability)
    assert tool._active == {}
    assert tool._processes == {}


async def test_direct_cancel_during_process_creation_stops_eventual_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_module, "_TERMINATE_GRACE_SECONDS", 0.2)
    capability = _capability(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    _install_delayed_spawn(monkeypatch, started, release)
    command = "sleep 30 & echo $! > gap-direct.pid; wait"

    exec_task = asyncio.create_task(
        _invoke(
            capability,
            "exec",
            {"command": command},
            context=_context("session-a"),
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    child_pid = await _wait_for_pid(tmp_path / "gap-direct.pid")

    exec_task.cancel()
    await asyncio.sleep(0.05)
    assert not exec_task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(exec_task, 5)

    await _wait_until_dead(child_pid)
    assert _exec_tool(capability)._active == {}


async def test_aclose_during_process_creation_cleans_and_rejects_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_module, "_TERMINATE_GRACE_SECONDS", 0.2)
    capability = _capability(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    _install_delayed_spawn(monkeypatch, started, release)
    command = "sleep 30 & echo $! > gap-close.pid; wait"

    exec_task = asyncio.create_task(
        _invoke(
            capability,
            "exec",
            {"command": command},
            context=_context("session-a"),
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    child_pid = await _wait_for_pid(tmp_path / "gap-close.pid")

    close_task = asyncio.create_task(capability.aclose())
    await asyncio.sleep(0.05)
    assert not close_task.done()

    release.set()
    await asyncio.wait_for(close_task, 5)
    outcome = await asyncio.wait_for(exec_task, 5)

    assert outcome.status is ToolStatus.FAILED
    await _wait_until_dead(child_pid)
    assert _exec_tool(capability)._active == {}

    rejected = await _invoke(
        capability, "exec", {"command": "printf should-not-run"}
    )
    assert rejected.status is ToolStatus.FAILED
    assert "closed" in (rejected.error.message if rejected.error else "")


async def test_session_registry_does_not_await_the_calling_task() -> None:
    registry = SessionTaskRegistry()
    other_started = asyncio.Event()
    release = asyncio.Event()

    async def other() -> None:
        with registry.scope("session-a"):
            other_started.set()
            await release.wait()

    async def caller() -> bool:
        with registry.scope("session-a"):
            await other_started.wait()
            await registry.cancel_session("session-a")
            return True

    other_task = asyncio.create_task(other())
    assert await asyncio.wait_for(caller(), 2) is True
    with pytest.raises(asyncio.CancelledError):
        await other_task
    release.set()


async def test_session_registry_close_blocks_late_registration() -> None:
    registry = SessionTaskRegistry()
    started = asyncio.Event()

    async def worker() -> None:
        with registry.scope("session-a"):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    await asyncio.wait_for(started.wait(), 2)

    await registry.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task

    with registry.scope("session-a"):
        pass
    assert registry._tasks == {}
    await registry.aclose()


async def test_oversized_output_is_bounded_and_fully_saved(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    command = f"{sys.executable} -c 'print(\"A\" * 20000)'"

    outcome = await _invoke(
        capability, "exec", {"command": command}, call_id="call-big"
    )

    assert outcome.is_success
    assert len(outcome.text) <= 8000
    assert ".paos/tool_outputs/call-big-" in outcome.text
    saved = list((tmp_path / ".paos" / "tool_outputs").glob("call-big-*.txt"))
    assert len(saved) == 1
    body = saved[0].read_text(encoding="utf-8")
    assert body.count("A") >= 20000
    assert "Exit code: 0" in body


class _FakeResponse:
    def __init__(
        self,
        *,
        data: dict[str, Any] | None = None,
        text: str = "",
        content_type: str = "application/json",
        status: int = 200,
        url: str = "https://example.com/data",
    ) -> None:
        self._data = data or {}
        self.text = text or json.dumps(self._data)
        self.headers = {"content-type": content_type}
        self.status_code = status
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeClient:
    response: _FakeResponse

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        del args, kwargs
        return self.response


async def test_web_search_and_fetch_map_results_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _capability(tmp_path, api_key="test-key")

    _FakeClient.response = _FakeResponse(
        data={"web": {"results": [{"title": "Title", "url": "https://e.test", "description": "D"}]}}
    )
    monkeypatch.setattr(web_module.httpx, "AsyncClient", _FakeClient)
    search = await _invoke(capability, "web_search", {"query": "q"})
    assert search.is_success
    assert "Title" in search.text and "https://e.test" in search.text

    _FakeClient.response = _FakeResponse(data={"ok": True})
    fetch = await _invoke(capability, "web_fetch", {"url": "https://e.test/data"})
    assert fetch.is_success
    assert '"extractor": "json"' in fetch.text

    invalid = await _invoke(capability, "web_fetch", {"url": "ftp://e.test"})
    assert invalid.status is ToolStatus.FAILED
    assert "URL validation failed" in (invalid.error.message if invalid.error else "")

    class _BrokenClient(_FakeClient):
        async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            raise RuntimeError("boom")

    monkeypatch.setattr(web_module.httpx, "AsyncClient", _BrokenClient)
    broken = await _invoke(capability, "web_fetch", {"url": "https://e.test/data"})
    assert broken.status is ToolStatus.FAILED
    assert "boom" in (broken.error.message if broken.error else "")


async def test_web_cancellation_stops_the_inflight_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _capability(tmp_path)
    started = asyncio.Event()

    class _BlockingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def __aenter__(self) -> "_BlockingClient":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(web_module.httpx, "AsyncClient", _BlockingClient)
    task = asyncio.create_task(
        _invoke(
            capability,
            "web_fetch",
            {"url": "https://e.test/slow"},
            context=_context("session-a"),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    await capability.cancel("session-a", _context("session-a"))

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    await capability.aclose()


async def test_scene_graph_queries_real_environment_file(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    (tmp_path / "ENVIRONMENT.md").write_text(
        "# Environment\n\n```json\n"
        + json.dumps(
            {
                "scene_graph": {
                    "nodes": [
                        {"id": "cup", "class": "cup", "center": {"x": 3, "y": 4}}
                    ]
                },
                "map": {"zones": [{"id": "kitchen"}]},
                "robots": {"arm": {"robot_pose": {"x": 0, "y": 0}}},
            }
        )
        + "\n```\n",
        encoding="utf-8",
    )

    nearest = await _invoke(
        capability,
        "query_scene_graph",
        {"query_type": "nearest_to_robot", "robot_id": "arm"},
    )
    assert nearest.is_success
    assert json.loads(nearest.text)["matches"][0]["distance_to_robot"] == 5.0

    zones = await _invoke(
        capability, "query_scene_graph", {"query_type": "list_zones"}
    )
    assert zones.is_success
    assert json.loads(zones.text)["zones"] == [{"id": "kitchen"}]

    missing_robot = await _invoke(
        capability,
        "query_scene_graph",
        {"query_type": "nearest_to_robot", "robot_id": "missing"},
    )
    assert missing_robot.status is ToolStatus.FAILED
    assert "robot pose unavailable" in (
        missing_robot.error.message if missing_robot.error else ""
    )

    unsupported = await _invoke(
        capability, "query_scene_graph", {"query_type": "bogus"}
    )
    assert unsupported.status is ToolStatus.FAILED

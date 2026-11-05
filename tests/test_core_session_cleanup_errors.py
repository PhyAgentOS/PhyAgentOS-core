"""Cleanup failures stay visible even when the cancelling caller stopped waiting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from PhyAgentOS.agent.core import TurnRequest, TurnResult, TurnStatus
from PhyAgentOS.cli.command_groups.agent_commands import CoreAgentSession
from PhyAgentOS.providers.service import RuntimeSelection

SESSION = "cli:test"


class _Runtime:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _Capability:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.closed = False

    async def cancel(self, session_id, context) -> None:
        self.cancelled.append(session_id)

    async def aclose(self) -> None:
        self.closed = True


class _Store:
    async def aclose(self) -> None:
        return None


class _DoneHost:
    async def submit(self, request):
        return TurnResult(request.session_id, "turn-done", TurnStatus.COMPLETED, reply="ok")

    async def cancel(self, session_id: str) -> None:
        return None


class _RunningFailHost:
    """A running turn whose capability cancellation fails after the caller left."""

    def __init__(self, error: Exception) -> None:
        self.submitted = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.release = asyncio.Event()
        self.error = error

    async def submit(self, request):
        self.submitted.set()
        await self.cancelled.wait()
        return TurnResult(request.session_id, "turn-running", TurnStatus.INTERRUPTED)

    async def cancel(self, session_id: str) -> None:
        self.cancelled.set()
        self.cancel_started.set()
        await self.release.wait()
        raise self.error


class _FinishedFailHost:
    """A finished turn whose queued cleanup fails."""

    def __init__(self, error: Exception) -> None:
        self.cancel_started = asyncio.Event()
        self.release = asyncio.Event()
        self.error = error

    async def submit(self, request):
        return TurnResult(request.session_id, "turn-finished", TurnStatus.COMPLETED)

    async def cancel(self, session_id: str) -> None:
        self.cancel_started.set()
        await self.release.wait()
        raise self.error


class _RecordingOutput:
    def __init__(self, *, raise_errors: bool = False) -> None:
        self.errors: list[str] = []
        self.error_calls = 0
        self.reported = asyncio.Event()
        self.raise_errors = raise_errors

    def error(self, text: str) -> None:
        self.error_calls += 1
        self.reported.set()
        if self.raise_errors:
            raise RuntimeError("terminal rendering failed")
        self.errors.append(text)


class _Selections:
    def __init__(self, selection: RuntimeSelection) -> None:
        self.selection = selection

    def get(self, session_id: str) -> RuntimeSelection:
        return self.selection


def _assembly(host, runtime, capability):
    return SimpleNamespace(host=host, runtime=runtime, capabilities=(capability,))


def _selection(model: str) -> RuntimeSelection:
    return RuntimeSelection("fake", model, None, None, object(), 128000)


def _session(build, output) -> tuple[CoreAgentSession, _Selections, _Capability]:
    capability = _Capability()
    seed = _assembly(_DoneHost(), _Runtime(), capability)
    selections = _Selections(_selection("first"))

    def wrapped(selection):
        return build(selection, capability)

    session = CoreAgentSession(
        seed,
        selections.selection,
        build=wrapped,
        store=_Store(),
        sessions=selections,
        output=output,
    )
    return session, selections, capability


async def test_running_cleanup_failure_is_reported_after_caller_stops_waiting():
    error = RuntimeError("capability cancel failed token=secret-value")
    failing = _RunningFailHost(error)
    completed = _DoneHost()
    output = _RecordingOutput()

    def build(selection, capability):
        if selection.model == "first":
            return _assembly(failing, _Runtime(), capability)
        return _assembly(completed, _Runtime(), capability)

    session, selections, capability = _session(build, output)
    try:
        first_task = asyncio.create_task(session.submit(TurnRequest(SESSION, "first")))
        await asyncio.wait_for(failing.submitted.wait(), 2)

        cancel_task = asyncio.create_task(session.cancel(SESSION))
        await asyncio.wait_for(failing.cancel_started.wait(), 2)
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        failing.release.set()

        first = await asyncio.wait_for(first_task, 2)
        assert first.status is TurnStatus.INTERRUPTED
        await asyncio.wait_for(output.reported.wait(), 2)

        assert len(output.errors) == 1
        report = output.errors[0]
        assert "RuntimeError" in report
        assert SESSION in report
        assert "secret-value" not in report
        assert "succeeded" not in report.lower()

        selections.selection = _selection("second")
        second = await asyncio.wait_for(session.submit(TurnRequest(SESSION, "second")), 2)
        assert second.status is TurnStatus.COMPLETED
        assert session._pending == 0
        assert session._idle.is_set()
    finally:
        failing.release.set()
        await session.aclose()

    assert capability.closed


async def test_completed_cleanup_failure_is_reported_after_caller_stops_waiting():
    error = RuntimeError("capability cancel failed token=secret-value")
    finished = _FinishedFailHost(error)
    completed = _DoneHost()
    output = _RecordingOutput()

    def build(selection, capability):
        if selection.model == "first":
            return _assembly(finished, _Runtime(), capability)
        return _assembly(completed, _Runtime(), capability)

    session, selections, capability = _session(build, output)
    try:
        first = await asyncio.wait_for(session.submit(TurnRequest(SESSION, "first")), 2)
        assert first.status is TurnStatus.COMPLETED

        cancel_task = asyncio.create_task(session.cancel(SESSION))
        await asyncio.wait_for(finished.cancel_started.wait(), 2)
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        finished.release.set()

        await asyncio.wait_for(output.reported.wait(), 2)
        assert len(output.errors) == 1
        report = output.errors[0]
        assert "RuntimeError" in report
        assert SESSION in report
        assert "secret-value" not in report
        assert "succeeded" not in report.lower()

        selections.selection = _selection("second")
        second = await asyncio.wait_for(session.submit(TurnRequest(SESSION, "second")), 2)
        assert second.status is TurnStatus.COMPLETED
        assert session._pending == 0
        assert session._idle.is_set()
    finally:
        finished.release.set()
        await session.aclose()

    assert capability.closed


async def test_waiting_caller_still_receives_the_original_cleanup_exception():
    error = RuntimeError("capability cancel failed token=secret-value")
    finished = _FinishedFailHost(error)
    output = _RecordingOutput()

    def build(selection, capability):
        return _assembly(finished, _Runtime(), capability)

    session, _selections, capability = _session(build, output)
    try:
        first = await asyncio.wait_for(session.submit(TurnRequest(SESSION, "first")), 2)
        assert first.status is TurnStatus.COMPLETED

        cancel_task = asyncio.create_task(session.cancel(SESSION))
        await asyncio.wait_for(finished.cancel_started.wait(), 2)
        finished.release.set()

        with pytest.raises(RuntimeError, match="secret-value"):
            await asyncio.wait_for(cancel_task, 2)
        await asyncio.wait_for(output.reported.wait(), 2)
        assert len(output.errors) == 1
        assert "RuntimeError" in output.errors[0]
        assert "secret-value" not in output.errors[0]
    finally:
        finished.release.set()
        await session.aclose()

    assert capability.closed


async def test_output_failure_does_not_break_callbacks_or_the_queue():
    error = RuntimeError("capability cancel failed token=secret-value")
    failing = _RunningFailHost(error)
    completed = _DoneHost()
    output = _RecordingOutput(raise_errors=True)

    def build(selection, capability):
        if selection.model == "first":
            return _assembly(failing, _Runtime(), capability)
        return _assembly(completed, _Runtime(), capability)

    session, selections, capability = _session(build, output)
    loop = asyncio.get_running_loop()
    reported_exceptions: list[str] = []

    def handler(loop, context):
        reported_exceptions.append(context.get("message", ""))

    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(handler)
    try:
        first_task = asyncio.create_task(session.submit(TurnRequest(SESSION, "first")))
        await asyncio.wait_for(failing.submitted.wait(), 2)

        cancel_task = asyncio.create_task(session.cancel(SESSION))
        await asyncio.wait_for(failing.cancel_started.wait(), 2)
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        failing.release.set()

        first = await asyncio.wait_for(first_task, 2)
        assert first.status is TurnStatus.INTERRUPTED
        await asyncio.wait_for(output.reported.wait(), 2)
        assert output.error_calls == 1

        selections.selection = _selection("second")
        second = await asyncio.wait_for(session.submit(TurnRequest(SESSION, "second")), 2)
        assert second.status is TurnStatus.COMPLETED
        assert session._pending == 0
        assert session._idle.is_set()
        assert reported_exceptions == []
    finally:
        loop.set_exception_handler(previous_handler)
        failing.release.set()
        await session.aclose()

    assert capability.closed

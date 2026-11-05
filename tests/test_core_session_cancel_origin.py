"""Cancellation origin: execution-side cancellation must not cancel cleanup.

A turn body can raise ``asyncio.CancelledError`` on its own (for example when
a runtime call is interrupted). That cancels the caller's future, but it is not
a caller cancellation: the turn is already settling, and the runtime stop that
follows must be allowed to finish before the worker releases the next item.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from PhyAgentOS.agent.core import TurnRequest, TurnResult, TurnStatus
from PhyAgentOS.cli.command_groups.agent_commands import CoreAgentSession
from PhyAgentOS.providers.service import RuntimeSelection


class _Output:
    def error(self, text: str) -> None:
        pass


class _FakeStore:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeRuntime:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _GatedStopRuntime:
    """Stop blocks between ``stop_started`` and ``stop_release``."""

    def __init__(self) -> None:
        self.stop_started = asyncio.Event()
        self.stop_release = asyncio.Event()
        self.stopped = False
        self.stop_cancelled = False

    async def stop(self) -> None:
        self.stop_started.set()
        try:
            await self.stop_release.wait()
        except asyncio.CancelledError:
            self.stop_cancelled = True
            raise
        self.stopped = True


class _ExecutionCancelHost:
    """The turn body raises CancelledError on its own, not from a task cancel."""

    def __init__(self) -> None:
        self.submitted = asyncio.Event()
        self.cancel_calls = 0

    async def submit(self, request):
        self.submitted.set()
        raise asyncio.CancelledError()

    async def cancel(self, session_id: str) -> None:
        self.cancel_calls += 1


class _BlockingHost:
    """A running turn that only ends when its execution task is cancelled."""

    def __init__(self) -> None:
        self.submitted = asyncio.Event()
        self.cancel_calls = 0

    async def submit(self, request):
        self.submitted.set()
        await asyncio.Event().wait()

    async def cancel(self, session_id: str) -> None:
        self.cancel_calls += 1


class _RecordingHost:
    def __init__(self) -> None:
        self.submitted = asyncio.Event()
        self.cancel_calls = 0

    async def submit(self, request):
        self.submitted.set()
        return TurnResult(
            request.session_id, "turn-done", TurnStatus.COMPLETED, reply="ok"
        )

    async def cancel(self, session_id: str) -> None:
        self.cancel_calls += 1


class _Selections:
    def __init__(self) -> None:
        self.selection = RuntimeSelection("fake", "first", None, None, object(), 128000)

    def get(self, session_id):
        return self.selection


def _assembly(host, runtime):
    return SimpleNamespace(host=host, runtime=runtime, capabilities=())


async def test_execution_cancel_does_not_cancel_the_started_runtime_stop():
    """An execution-side CancelledError keeps the cleanup gate closed for B."""
    seed = _assembly(_RecordingHost(), _FakeRuntime())
    first_runtime = _GatedStopRuntime()
    first_host = _ExecutionCancelHost()
    second_host = _RecordingHost()
    selections = _Selections()

    def build(selection):
        if selection.model == "first":
            return _assembly(first_host, first_runtime)
        return _assembly(second_host, _FakeRuntime())

    session = CoreAgentSession(
        seed,
        selections.selection,
        build=build,
        store=_FakeStore(),
        sessions=selections,
        output=_Output(),
    )
    second_task = None
    try:
        first_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "first"))
        )
        await asyncio.wait_for(first_host.submitted.wait(), 2)
        await asyncio.wait_for(first_runtime.stop_started.wait(), 2)

        selections.selection = RuntimeSelection(
            "fake", "second", None, None, object(), 128000
        )
        second_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "second"))
        )
        for _ in range(5):
            await asyncio.sleep(0)

        # The execution-side cancellation reaches the caller, but the caller
        # must not cancel the runtime stop that is already settling the turn:
        # neither A nor B may finish while the stop gate is closed.
        for _ in range(10):
            await asyncio.sleep(0)
        assert not first_task.done()
        assert not first_runtime.stop_cancelled
        assert not first_runtime.stopped
        assert not second_host.submitted.is_set()

        first_runtime.stop_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first_task, 2)
        second = await asyncio.wait_for(second_task, 2)
        assert second.status is TurnStatus.COMPLETED
        assert first_runtime.stopped
        assert not first_runtime.stop_cancelled
        assert second_host.submitted.is_set()
    finally:
        first_runtime.stop_release.set()
        if second_task is not None:
            second_task.cancel()
            await asyncio.gather(second_task, return_exceptions=True)
        await session.aclose()


async def test_caller_cancel_still_stops_the_running_turn_and_keeps_the_queue():
    """A real caller cancellation stops A; B still runs after its cleanup."""
    seed = _assembly(_RecordingHost(), _FakeRuntime())
    first_runtime = _FakeRuntime()
    first_host = _BlockingHost()
    second_host = _RecordingHost()
    selections = _Selections()

    def build(selection):
        if selection.model == "first":
            return _assembly(first_host, first_runtime)
        return _assembly(second_host, _FakeRuntime())

    session = CoreAgentSession(
        seed,
        selections.selection,
        build=build,
        store=_FakeStore(),
        sessions=selections,
        output=_Output(),
    )
    try:
        first_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "first"))
        )
        await asyncio.wait_for(first_host.submitted.wait(), 2)

        selections.selection = RuntimeSelection(
            "fake", "second", None, None, object(), 128000
        )
        second_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "second"))
        )

        first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)

        assert first_task.cancelled()
        assert first_runtime.stopped
        assert first_host.cancel_calls == 0

        second = await asyncio.wait_for(second_task, 2)
        assert second.status is TurnStatus.COMPLETED
        assert second_host.submitted.is_set()
        assert second_host.cancel_calls == 0
    finally:
        await session.aclose()


async def test_execution_cancelled_compaction_settles_before_the_next_turn():
    """The compact lane mirrors the origin rule and keeps the queue moving."""
    seed = _assembly(_RecordingHost(), _FakeRuntime())
    host = _RecordingHost()
    selections = _Selections()
    compact_started = asyncio.Event()

    async def compact(session_id, selection):
        compact_started.set()
        raise asyncio.CancelledError()

    def build(selection):
        return _assembly(host, _FakeRuntime())

    session = CoreAgentSession(
        seed,
        selections.selection,
        build=build,
        store=_FakeStore(),
        sessions=selections,
        output=_Output(),
        compact=compact,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(session.compact("cli:test"), 2)
        assert compact_started.is_set()

        result = await asyncio.wait_for(
            session.submit(TurnRequest("cli:test", "after compact")), 2
        )
        assert result.status is TurnStatus.COMPLETED
        assert host.submitted.is_set()
    finally:
        await session.aclose()


async def test_repeated_caller_cancel_does_not_cancel_the_started_runtime_stop():
    """A second cancelling wait must not abort the already-begun runtime stop.

    The first caller cancellation stops the turn body; the execution then
    enters the gated runtime stop. Cancelling the caller's wait again must not
    travel through the wait primitive and cancel that stop, and the worker must
    still hold B until the stop really finishes.
    """
    seed = _assembly(_RecordingHost(), _FakeRuntime())
    first_runtime = _GatedStopRuntime()
    first_host = _BlockingHost()
    second_host = _RecordingHost()
    selections = _Selections()

    def build(selection):
        if selection.model == "first":
            return _assembly(first_host, first_runtime)
        return _assembly(second_host, _FakeRuntime())

    session = CoreAgentSession(
        seed,
        selections.selection,
        build=build,
        store=_FakeStore(),
        sessions=selections,
        output=_Output(),
    )
    second_task = None
    try:
        first_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "first"))
        )
        await asyncio.wait_for(first_host.submitted.wait(), 2)

        selections.selection = RuntimeSelection(
            "fake", "second", None, None, object(), 128000
        )
        second_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "second"))
        )
        await asyncio.sleep(0)

        # First cancel: stop the turn body. The execution then enters the
        # gated stop while the caller waits for that execution to settle.
        first_task.cancel()
        await asyncio.wait_for(first_runtime.stop_started.wait(), 2)
        assert not first_runtime.stop_release.is_set()

        # Second cancel: the caller's wait is cancelled, but the runtime stop
        # it was waiting for must keep running.
        first_task.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not first_task.done()
        assert not first_runtime.stop_cancelled
        assert not first_runtime.stopped
        assert not second_host.submitted.is_set()

        first_runtime.stop_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first_task, 2)
        second = await asyncio.wait_for(second_task, 2)
        assert second.status is TurnStatus.COMPLETED
        assert first_runtime.stopped
        assert not first_runtime.stop_cancelled
        assert second_host.submitted.is_set()
    finally:
        first_runtime.stop_release.set()
        if second_task is not None:
            second_task.cancel()
            await asyncio.gather(second_task, return_exceptions=True)
        await session.aclose()

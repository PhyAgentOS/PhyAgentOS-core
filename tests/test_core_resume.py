"""Tests for explicit continuation of a failed or interrupted turn.

Every store test uses a real ``SQLiteSessionStore`` on a temporary database.
The runtime tests reuse that store with a fake loop driver; no provider,
Forge client, or tool handler is called.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from PhyAgentOS.agent.core.events import EventType, RecoveryReport, RuntimeEvent
from PhyAgentOS.agent.core.runtime import AgentRuntime, RuntimeStorageError
from PhyAgentOS.agent.core.session import (
    ContextCheckpoint,
    ResumeHistory,
    ResumeUnavailableError,
    SQLiteSessionStore,
)
from PhyAgentOS.agent.core.types import (
    TurnRequest,
    TurnResult,
    TurnStart,
    TurnStatus,
)

SESSION = "session-resume"


@asynccontextmanager
async def _open(path: str | Path) -> AsyncIterator[SQLiteSessionStore]:
    store = SQLiteSessionStore(path)
    try:
        yield store
    finally:
        await store.aclose()


def _prepared(
    session_id: str, turn_id: str, step: int, messages: Sequence[Mapping[str, Any]]
) -> RuntimeEvent:
    return RuntimeEvent(
        type=EventType.REQUEST_PREPARED,
        session_id=session_id,
        turn_id=turn_id,
        step_id=f"{turn_id}:step:{step}",
        payload={
            "model_call_id": f"{turn_id}:model:{step}",
            "messages": [dict(message) for message in messages],
            "tools": [],
        },
    )


async def _start_turn(store: SQLiteSessionStore, turn_id: str, text: str) -> None:
    await store.append(
        RuntimeEvent(
            type=EventType.INPUT_ACCEPTED,
            session_id=SESSION,
            payload={"characters": len(text)},
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.TURN_STARTED,
            session_id=SESSION,
            turn_id=turn_id,
            payload={"max_steps": 8},
        )
    )


async def _append_completed_turn(
    store: SQLiteSessionStore,
    turn_id: str,
    text: str,
    reply: str,
    *,
    history: Sequence[Mapping[str, Any]] = (),
    session_id: str = SESSION,
) -> None:
    messages = [
        {"role": "system", "content": "be terse"},
        *[dict(message) for message in history],
        {"role": "user", "content": text},
    ]
    await store.append(
        RuntimeEvent(
            type=EventType.INPUT_ACCEPTED,
            session_id=session_id,
            payload={"characters": len(text)},
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.TURN_STARTED,
            session_id=session_id,
            turn_id=turn_id,
            payload={"max_steps": 8},
        )
    )
    await store.append(_prepared(session_id, turn_id, 1, messages))
    await store.append(
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_STARTED,
            session_id=session_id,
            turn_id=turn_id,
            step_id=f"{turn_id}:step:1",
            payload={"model_call_id": f"{turn_id}:model:1"},
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_COMPLETED,
            session_id=session_id,
            turn_id=turn_id,
            step_id=f"{turn_id}:step:1",
            payload={
                "model_call_id": f"{turn_id}:model:1",
                "finish_reason": "stop",
                "content": reply,
                "tool_calls": [],
            },
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.TURN_COMPLETED,
            session_id=session_id,
            turn_id=turn_id,
            payload={"reply": reply},
        )
    )


def _tool_call(call_id: str, name: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


async def _tool_settled(
    store: SQLiteSessionStore,
    turn_id: str,
    step: int,
    call_id: str,
    name: str,
    status: str,
    text: str,
) -> None:
    await store.append(
        RuntimeEvent(
            type=EventType.TOOL_INTENT,
            session_id=SESSION,
            turn_id=turn_id,
            step_id=f"{turn_id}:step:{step}",
            payload={"tool_call_id": call_id, "tool_name": name},
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.TOOL_DISPATCHED,
            session_id=SESSION,
            turn_id=turn_id,
            step_id=f"{turn_id}:step:{step}",
            payload={"tool_call_id": call_id, "tool_name": name},
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.TOOL_SETTLED,
            session_id=SESSION,
            turn_id=turn_id,
            step_id=f"{turn_id}:step:{step}",
            payload={
                "tool_call_id": call_id,
                "tool_name": name,
                "status": status,
                "text": text,
                "error": None,
                "domain_refs": {},
            },
        )
    )


async def _append_failed_after_tool(
    store: SQLiteSessionStore,
    turn_id: str,
    *,
    terminal: EventType = EventType.TURN_FAILED,
    unknown_second: bool = False,
    unsettled_second: bool = False,
) -> None:
    """A turn whose last snapshot is before an assistant/tool exchange."""
    await _start_turn(store, turn_id, "move the cup")
    base = [{"role": "user", "content": "move the cup"}]
    await store.append(_prepared(SESSION, turn_id, 1, base))
    await store.append(
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_STARTED,
            session_id=SESSION,
            turn_id=turn_id,
            step_id=f"{turn_id}:step:1",
            payload={"model_call_id": f"{turn_id}:model:1"},
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_COMPLETED,
            session_id=SESSION,
            turn_id=turn_id,
            step_id=f"{turn_id}:step:1",
            payload={
                "model_call_id": f"{turn_id}:model:1",
                "finish_reason": "tool_calls",
                "content": None,
                "tool_calls": [
                    _tool_call("call-a", "move"),
                    _tool_call("call-b", "check"),
                ],
            },
        )
    )
    await _tool_settled(
        store, turn_id, 1, "call-a", "move", "succeeded", "moved the cup"
    )
    if unsettled_second:
        await store.append(
            RuntimeEvent(
                type=EventType.TOOL_INTENT,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"tool_call_id": "call-b", "tool_name": "check"},
            )
        )
    else:
        await _tool_settled(
            store,
            turn_id,
            1,
            "call-b",
            "check",
            "unknown" if unknown_second else "succeeded",
            "checked",
        )
    if terminal is EventType.TURN_FAILED:
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "model call failed: boom"},
            )
        )
    else:
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_INTERRUPTED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"reason": "cancelled locally"},
            )
        )


# ----------------------------------------------------------------------
# store projection
# ----------------------------------------------------------------------
async def test_base_snapshot_successful_tool_results_are_not_lost(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-failed"
        await _start_turn(store, turn_id, "move the cup")
        base = [{"role": "user", "content": "move the cup"}]
        await store.append(_prepared(SESSION, turn_id, 1, base))
        assistant = {
            "role": "assistant",
            "content": "working",
            "tool_calls": [_tool_call("call-a", "move")],
        }
        with_tool = [
            *base,
            assistant,
            {
                "role": "tool",
                "tool_call_id": "call-a",
                "name": "move",
                "content": "moved the cup",
            },
        ]
        await store.append(_prepared(SESSION, turn_id, 2, with_tool))
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_STARTED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:2",
                payload={"model_call_id": f"{turn_id}:model:2"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:2",
                payload={
                    "model_call_id": f"{turn_id}:model:2",
                    "error": "provider unavailable",
                },
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "model call failed: provider unavailable"},
            )
        )

        history = await store.read_resume_history(SESSION)

        assert isinstance(history, ResumeHistory)
        assert history.source_turn_id == turn_id
        assert [dict(message) for message in history.messages] == with_tool
        assert any("source turn failed" in warning for warning in history.warnings)
        assert not any("not replayed" in warning for warning in history.warnings)


async def test_assistant_tool_exchange_after_last_snapshot_is_rebuilt(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-interrupted"
        await _append_failed_after_tool(
            store,
            turn_id,
            terminal=EventType.TURN_INTERRUPTED,
            unsettled_second=True,
        )

        history = await store.read_resume_history(SESSION)

        assert history.source_turn_id == turn_id
        assert [message["role"] for message in history.messages] == [
            "user",
            "assistant",
            "tool",
        ]
        assert history.messages[0] == {"role": "user", "content": "move the cup"}
        assistant = history.messages[1]
        assert assistant["content"] is None
        assert [call["id"] for call in assistant["tool_calls"]] == ["call-a"]
        assert history.messages[2] == {
            "role": "tool",
            "tool_call_id": "call-a",
            "name": "move",
            "content": "moved the cup",
        }
        assert any("call-b" in warning for warning in history.warnings)
        assert any("not replayed" in warning for warning in history.warnings)
        assert any("source turn was interrupted" in w for w in history.warnings)


async def test_unknown_and_cancelled_tool_results_keep_pair_and_warning(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-unknown"
        await _append_failed_after_tool(
            store,
            turn_id,
            terminal=EventType.TURN_INTERRUPTED,
            unknown_second=True,
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TOOL_INTENT,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"tool_call_id": "call-c", "tool_name": "wave"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TOOL_DISPATCHED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"tool_call_id": "call-c", "tool_name": "wave"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TOOL_SETTLED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={
                    "tool_call_id": "call-c",
                    "tool_name": "wave",
                    "status": "cancelled",
                    "text": "cancelled locally after dispatch",
                    "error": None,
                    "domain_refs": {},
                },
            )
        )

        history = await store.read_resume_history(SESSION)

        # call-c was not in the assistant response, so it cannot be paired;
        # the global warning still names it. Unknown call-b is paired and kept.
        assert any("call-b" in warning and "unknown" in warning for warning in history.warnings)
        assert any("call-c" in warning and "cancelled" in warning for warning in history.warnings)
        assert any("call-b" in message.get("tool_call_id", "") for message in history.messages)
        assert any("call-c" in warning for warning in history.warnings)
        contents = [message.get("content") for message in history.messages]
        assert any("Tool call result is unknown" in (text or "") for text in contents)


async def test_partial_stream_is_a_warning_not_a_fake_answer(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-partial"
        await _start_turn(store, turn_id, "explain the plan")
        base = [{"role": "user", "content": "explain the plan"}]
        await store.append(_prepared(SESSION, turn_id, 1, base))
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_STARTED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"model_call_id": f"{turn_id}:model:1"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_RESPONSE_DELTA,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={
                    "model_call_id": f"{turn_id}:model:1",
                    "content": "the first half",
                },
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_INTERRUPTED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"model_call_id": f"{turn_id}:model:1", "recovered": True},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_INTERRUPTED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"reason": "cancelled"},
            )
        )

        history = await store.read_resume_history(SESSION)

        assert [dict(message) for message in history.messages] == base
        assert all(
            message["role"] != "assistant" for message in history.messages
        )
        assert any("partial streamed reply" in warning for warning in history.warnings)
        assert any("interrupted" in warning for warning in history.warnings)


async def test_missing_snapshot_is_refused_instead_of_fabricating_a_request(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-no-snapshot"
        await _start_turn(store, turn_id, "never stored")
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_STARTED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"model_call_id": f"{turn_id}:model:1"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"model_call_id": f"{turn_id}:model:1", "error": "boom"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "boom"},
            )
        )

        with pytest.raises(ResumeUnavailableError, match="request.prepared"):
            await store.read_resume_history(SESSION)


async def test_completed_turn_is_not_resume_history(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_completed_turn(store, "turn-done", "question", "answer")

        with pytest.raises(ResumeUnavailableError, match="no failed or interrupted"):
            await store.read_resume_history(SESSION)
        with pytest.raises(ResumeUnavailableError, match="completed"):
            await store.read_resume_history(SESSION, "turn-done")
        assert await store.read_conversation(SESSION) == (
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        )


async def test_reverted_failed_turn_cannot_be_resumed(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_completed_turn(store, "turn-ok", "first", "done")
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        outcome = await store.undo_last_turn(SESSION)
        assert outcome is not None and outcome.turn_id == "turn-failed"

        with pytest.raises(ResumeUnavailableError, match="withdrawn by undo"):
            await store.read_resume_history(SESSION, "turn-failed")


async def test_checkpoint_covered_failed_turn_is_refused(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        head = await store.read_head_sequence(SESSION)
        await store.commit_checkpoint(
            ContextCheckpoint(
                session_id=SESSION,
                covers_sequence=head,
                summary="the failed work was summarized",
            ),
            expected_head_sequence=head,
        )

        with pytest.raises(ResumeUnavailableError, match="context checkpoint"):
            await store.read_resume_history(SESSION)


async def test_unfinished_turn_refuses_resume(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_STARTED,
                session_id=SESSION,
                turn_id="turn-running",
                payload={"max_steps": 8},
            )
        )

        with pytest.raises(ResumeUnavailableError, match="unfinished turn"):
            await store.read_resume_history(SESSION)


async def test_newest_failed_or_interrupted_is_selected(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        await _append_failed_after_tool(
            store, "turn-interrupted", terminal=EventType.TURN_INTERRUPTED
        )

        history = await store.read_resume_history(SESSION)
        assert history.source_turn_id == "turn-interrupted"

        await store.undo_last_turn(SESSION)
        history = await store.read_resume_history(SESSION)
        assert history.source_turn_id == "turn-failed"


async def test_resume_lookup_is_scoped_to_the_requested_session(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        with pytest.raises(ResumeUnavailableError, match="does not exist"):
            await store.read_resume_history("other-session", "turn-failed")


# ----------------------------------------------------------------------
# runtime wiring
# ----------------------------------------------------------------------
class RecordingDriver:
    """Fake loop driver that records TurnStart and persists a legal snapshot."""

    def __init__(self, store: SQLiteSessionStore | None = None) -> None:
        self.store = store
        self.turns: list[TurnStart] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.active = 0
        self.peak = 0

    async def run(self, turn: TurnStart) -> TurnResult:
        self.turns.append(turn)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        await self.release.wait()
        if self.store is not None:
            messages = [
                dict(message) for message in turn.history
            ] + [{"role": "user", "content": turn.request.text}]
            await self.store.append(
                RuntimeEvent(
                    type=EventType.REQUEST_PREPARED,
                    session_id=turn.request.session_id,
                    turn_id=turn.turn_id,
                    step_id=f"{turn.turn_id}:step:1",
                    payload={
                        "model_call_id": f"{turn.turn_id}:model:1",
                        "messages": messages,
                        "tools": [],
                    },
                )
            )
        self.active -= 1
        return TurnResult(
            session_id=turn.request.session_id,
            turn_id=turn.turn_id,
            status=TurnStatus.COMPLETED,
            reply=f"reply:{turn.request.text}",
        )


async def test_runtime_resume_passes_snapshot_and_keeps_new_text(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED, unknown_second=True
        )
        expected = await store.read_resume_history(SESSION)
        driver = RecordingDriver(store)
        runtime = AgentRuntime(driver=driver, store=store)
        try:
            result = await runtime.resume(
                TurnRequest(session_id=SESSION, text="continue with the new instruction"),
                source_turn_id="turn-failed",
            )
        finally:
            await runtime.stop()

        assert result.status is TurnStatus.COMPLETED
        turn = driver.turns[0]
        assert [dict(message) for message in turn.history] == [
            dict(message) for message in expected.messages
        ]
        assert turn.request.text == "continue with the new instruction"
        started = [
            event
            for event in await store.read_session(SESSION)
            if event.type is EventType.TURN_STARTED
            and event.turn_id == result.turn_id
        ]
        assert len(started) == 1
        assert started[0].payload["resumed_from_turn_id"] == "turn-failed"
        assert "max_steps" in started[0].payload
        assert any(
            "not replayed" in warning
            for warning in started[0].payload.get("resume_warnings", [])
        )

        projected = await store.read_conversation(SESSION)
        assert projected[-2] == {
            "role": "user",
            "content": "continue with the new instruction",
        }
        assert projected[-1] == {
            "role": "assistant",
            "content": "reply:continue with the new instruction",
        }


async def test_normal_submit_keeps_completed_only_history(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_completed_turn(store, "turn-done", "first", "done")
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        expected_completed = await store.read_conversation(SESSION)
        driver = RecordingDriver(store)
        runtime = AgentRuntime(driver=driver, store=store)
        try:
            result = await runtime.submit(
                TurnRequest(session_id=SESSION, text="ordinary next")
            )
        finally:
            await runtime.stop()

        assert result.status is TurnStatus.COMPLETED
        assert [dict(message) for message in driver.turns[0].history] == [
            dict(message) for message in expected_completed
        ]
        started = [
            event
            for event in await store.read_session(SESSION)
            if event.type is EventType.TURN_STARTED
            and event.turn_id == result.turn_id
        ]
        assert "resumed_from_turn_id" not in started[0].payload


async def test_runtime_refuses_reverted_or_checkpointed_source_before_opening(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        await store.undo_last_turn(SESSION)
        head = await store.read_head_sequence(SESSION)
        driver = RecordingDriver(store)
        runtime = AgentRuntime(driver=driver, store=store)
        try:
            with pytest.raises(ResumeUnavailableError, match="withdrawn by undo"):
                await runtime.resume(
                    TurnRequest(session_id=SESSION, text="continue"),
                    source_turn_id="turn-failed",
                )
        finally:
            await runtime.stop()

        assert driver.turns == []
        new_events = await store.read_session(SESSION, after_sequence=head)
        assert not any(
            event.type in (EventType.INPUT_ACCEPTED, EventType.TURN_STARTED)
            for event in new_events
        )

    async with _open(tmp_path / "second.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        head = await store.read_head_sequence(SESSION)
        await store.commit_checkpoint(
            ContextCheckpoint(
                session_id=SESSION,
                covers_sequence=head,
                summary="compacted failed work",
            ),
            expected_head_sequence=head,
        )
        driver = RecordingDriver(store)
        runtime = AgentRuntime(driver=driver, store=store)
        try:
            with pytest.raises(ResumeUnavailableError, match="context checkpoint"):
                await runtime.resume(
                    TurnRequest(session_id=SESSION, text="continue"),
                    source_turn_id="turn-failed",
                )
        finally:
            await runtime.stop()
        assert driver.turns == []


async def test_runtime_resume_requires_the_store_capability(tmp_path: Path) -> None:
    class NoResumeStore:
        def __init__(self) -> None:
            self.events: list[RuntimeEvent] = []
            self.closed = 0

        async def append(self, event: RuntimeEvent) -> RuntimeEvent:
            self.events.append(event)
            return event

        async def read_turn(
            self, session_id: str, turn_id: str
        ) -> list[RuntimeEvent]:
            return []

        async def read_session(
            self, session_id: str, *, after_sequence: int = 0
        ) -> list[RuntimeEvent]:
            return []

        async def read_conversation(
            self, session_id: str
        ) -> list[dict[str, Any]]:
            return []

        async def recover(self, session_id: str) -> RecoveryReport:
            return RecoveryReport(session_id=session_id)

        async def aclose(self) -> None:
            self.closed += 1

    store = NoResumeStore()
    driver = RecordingDriver()
    runtime = AgentRuntime(driver=driver, store=store)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="does not support resume"):
        await runtime.resume(TurnRequest(session_id=SESSION, text="continue"))
    assert driver.turns == []
    assert store.events == []


async def test_same_session_resume_and_submit_are_serialized(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        driver = RecordingDriver(store)
        driver.release.clear()
        runtime = AgentRuntime(driver=driver, store=store)
        try:
            resume_task = asyncio.create_task(
                runtime.resume(
                    TurnRequest(session_id=SESSION, text="resume"),
                    source_turn_id="turn-failed",
                )
            )
            await driver.started.wait()
            submit_task = asyncio.create_task(
                runtime.submit(TurnRequest(session_id=SESSION, text="next"))
            )
            await asyncio.sleep(0)
            assert len(driver.turns) == 1
            driver.release.set()
            resumed = await resume_task
            submitted = await submit_task
        finally:
            await runtime.aclose()

        assert resumed.status is TurnStatus.COMPLETED
        assert submitted.status is TurnStatus.COMPLETED
        assert len(driver.turns) == 2
        assert driver.peak == 1
        assert driver.turns[0].request.text == "resume"
        assert driver.turns[1].request.text == "next"


async def test_runtime_resume_read_failure_is_a_storage_error(tmp_path: Path) -> None:
    class FailingResumeStore(SQLiteSessionStore):
        async def read_resume_history(self, session_id: str, turn_id: str | None = None):
            raise OSError("disk unavailable")

    store = FailingResumeStore(tmp_path / "events.sqlite")
    driver = RecordingDriver()
    runtime = AgentRuntime(driver=driver, store=store)
    try:
        with pytest.raises(RuntimeStorageError, match="read_resume_history"):
            await runtime.resume(TurnRequest(session_id=SESSION, text="continue"))
    finally:
        await runtime.aclose()
    assert driver.turns == []

async def test_resume_history_strips_runtime_context_without_losing_the_request(
    tmp_path: Path,
) -> None:
    runtime_tail = (
        "--- Runtime context for this step (live environment state; this is not a "
        "user message and asks for no new task) ---\n"
        "[PhyAgentOS runtime context]\n\n"
        "battery: 80%\n\n"
        "--- End runtime context ---"
    )
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-context"
        await _start_turn(store, turn_id, "walk forward")
        await store.append(
            _prepared(
                SESSION,
                turn_id,
                1,
                [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "walk forward"},
                    {"role": "user", "name": "paos_runtime_context", "content": runtime_tail},
                ],
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_STARTED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"model_call_id": f"{turn_id}:model:1"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "cancelled by provider"},
            )
        )

        history = await store.read_resume_history(SESSION)

        assert [dict(message) for message in history.messages] == [
            {"role": "user", "content": "walk forward"}
        ]
        assert runtime_tail not in "".join(
            str(message.get("content")) for message in history.messages
        )


async def test_resume_ignores_a_real_cache_stable_assembler_tail(
    tmp_path: Path,
) -> None:
    from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl
    from PhyAgentOS.agent.core.types import (
        Budget,
        CancellationToken,
        ExecutionContext,
    )

    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-real-context"
        await _start_turn(store, turn_id, "walk forward")
        context = ExecutionContext(
            session_id=SESSION,
            turn_id=turn_id,
            step_id=f"{turn_id}:step:1",
            cancellation=CancellationToken(),
            budget=Budget(max_steps=4),
        )
        bundle = await PromptAssemblerImpl(
            "static system",
            fragments=[lambda current: "battery: 80%"],
            cache_stable=True,
        ).assemble(
            context,
            history=({"role": "user", "content": "walk forward"},),
            tools=(),
        )
        messages = [dict(message) for message in bundle.messages]
        await store.append(_prepared(SESSION, turn_id, 1, messages))
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_STARTED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"model_call_id": f"{turn_id}:model:1"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "provider stopped"},
            )
        )

        history = await store.read_resume_history(SESSION)

        assert [dict(message) for message in history.messages] == [
            {"role": "user", "content": "walk forward"}
        ]


async def test_failed_turn_followed_by_completed_is_refused_as_a_branch(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_failed_after_tool(
            store, "turn-failed", terminal=EventType.TURN_FAILED
        )
        await _append_completed_turn(store, "turn-done", "after failure", "done")

        with pytest.raises(ResumeUnavailableError, match="branch"):
            await store.read_resume_history(SESSION)
        with pytest.raises(ResumeUnavailableError, match="fork is not supported"):
            await store.read_resume_history(SESSION, "turn-failed")

        # Ordinary history still projects only the completed turn.
        assert await store.read_conversation(SESSION) == (
            {"role": "user", "content": "after failure"},
            {"role": "assistant", "content": "done"},
        )

        withdrawn = await store.undo_last_turn(SESSION)
        assert withdrawn is not None and withdrawn.turn_id == "turn-done"
        history = await store.read_resume_history(SESSION, "turn-failed")
        assert history.source_turn_id == "turn-failed"


async def test_resume_keeps_steered_correction_after_the_last_snapshot(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-steered"
        await _start_turn(store, turn_id, "start")
        await store.append(
            _prepared(
                SESSION,
                turn_id,
                1,
                [{"role": "user", "content": "start"}],
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_STARTED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={"model_call_id": f"{turn_id}:model:1"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.MODEL_ATTEMPT_COMPLETED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:1",
                payload={
                    "model_call_id": f"{turn_id}:model:1",
                    "finish_reason": "tool_calls",
                    "content": None,
                    "tool_calls": [_tool_call("call-a", "move")],
                },
            )
        )
        await _tool_settled(
            store, turn_id, 1, "call-a", "move", "succeeded", "moved"
        )
        await store.append(
            RuntimeEvent(
                type=EventType.INPUT_STEERED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:2",
                payload={"text": "go to the blue box"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "context policy failed before the next request"},
            )
        )

        history = await store.read_resume_history(SESSION)

        assert [dict(message) for message in history.messages] == [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-a", "move")],
            },
            {
                "role": "tool",
                "tool_call_id": "call-a",
                "name": "move",
                "content": "moved",
            },
            {"role": "user", "content": "go to the blue box"},
        ]
        assert any(
            "go to the blue box" in warning
            and "not confirmed as sent to the model" in warning
            for warning in history.warnings
        )
        # The tool result and the correction both keep their order; the
        # correction is not rewritten into an assistant answer.
        assert history.messages[-1]["content"] == "go to the blue box"


async def test_resume_uses_context_compacted_after_the_last_snapshot(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-compacted"
        await _start_turn(store, turn_id, "start")
        await store.append(
            _prepared(
                SESSION,
                turn_id,
                1,
                [
                    {"role": "user", "content": "start"},
                    {"role": "assistant", "content": "stale old answer"},
                ],
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.INPUT_STEERED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:2",
                payload={"text": "go to the blue box"},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.CONTEXT_COMPACTED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={
                    "before_tokens": 100,
                    "after_tokens": 20,
                    "reason": "window",
                    "messages": [
                        {"role": "user", "content": "start"},
                        {"role": "user", "content": "go to the blue box"},
                    ],
                },
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "reassembly failed before request.prepared"},
            )
        )

        history = await store.read_resume_history(SESSION)

        assert [dict(message) for message in history.messages] == [
            {"role": "user", "content": "start"},
            {"role": "user", "content": "go to the blue box"},
        ]
        assert not any(
            message.get("content") == "stale old answer"
            for message in history.messages
        )
        assert any("context.compacted" in warning for warning in history.warnings)
        assert any(
            "remains in the recorded compacted messages" in warning
            for warning in history.warnings
        )


async def test_resume_does_not_duplicate_a_correction_already_in_a_snapshot(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        turn_id = "turn-covered"
        await _start_turn(store, turn_id, "start")
        await store.append(
            _prepared(
                SESSION,
                turn_id,
                1,
                [{"role": "user", "content": "start"}],
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.INPUT_STEERED,
                session_id=SESSION,
                turn_id=turn_id,
                step_id=f"{turn_id}:step:2",
                payload={"text": "go to the blue box"},
            )
        )
        await store.append(
            _prepared(
                SESSION,
                turn_id,
                2,
                [
                    {"role": "user", "content": "start"},
                    {"role": "user", "content": "go to the blue box"},
                ],
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "provider failed"},
            )
        )

        history = await store.read_resume_history(SESSION)

        assert [
            message.get("content")
            for message in history.messages
            if message.get("role") == "user"
        ] == ["start", "go to the blue box"]
        assert not any(
            "correction was accepted" in warning for warning in history.warnings
        )

"""Tests for append-only undo of the newest turn.

Undo must withdraw the last request without deleting or rewriting a row: one
session-scoped ``turn.reverted`` event is appended and the conversation
projection skips the named turn. These tests cover the projection fallback, the
durable marker across reopen, the failure and interrupt branches, the checkpoint
that already folded the turn into a summary, and the refusal to undo while a
turn is still running. A real ``SQLiteSessionStore`` on a temporary database is
used; nothing here mocks storage.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.protocols import SessionStore, UndoTurnStore
from PhyAgentOS.agent.core.session import ContextCheckpoint, SQLiteSessionStore, UndoOutcome

SESSION = "session-undo"

FIRST_HISTORY: tuple[Mapping[str, Any], ...] = (
    {"role": "user", "content": "first question"},
    {"role": "assistant", "content": "first reply"},
)


@asynccontextmanager
async def _open(path: str | Path) -> AsyncIterator[SQLiteSessionStore]:
    store = SQLiteSessionStore(path)
    try:
        yield store
    finally:
        await store.aclose()


async def _append_turn(
    store: SQLiteSessionStore,
    turn_id: str,
    text: str,
    *,
    history: Sequence[Mapping[str, Any]] = (),
    reply: str | None = "done",
    terminal: EventType = EventType.TURN_COMPLETED,
) -> None:
    """Append one whole turn the way the runtime and loop driver would."""
    await store.append(
        RuntimeEvent(
            type=EventType.INPUT_ACCEPTED,
            session_id=SESSION,
            payload={"characters": len(text)},
        )
    )
    await store.append(
        RuntimeEvent(type=EventType.TURN_STARTED, session_id=SESSION, turn_id=turn_id)
    )
    messages = [
        {"role": "system", "content": "be terse"},
        *copy.deepcopy(list(history)),
        {"role": "user", "content": text},
    ]
    await store.append(
        RuntimeEvent(
            type=EventType.REQUEST_PREPARED,
            session_id=SESSION,
            turn_id=turn_id,
            step_id=f"{turn_id}-step",
            payload={
                "model_call_id": f"{turn_id}-model",
                "model": "fake-model",
                "messages": messages,
                "tools": [],
            },
        )
    )
    if terminal is EventType.TURN_COMPLETED:
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_COMPLETED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"reply": reply},
            )
        )
    elif terminal is EventType.TURN_FAILED:
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"error": "model failed"},
            )
        )
    else:
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_INTERRUPTED,
                session_id=SESSION,
                turn_id=turn_id,
                payload={"reason": "cancelled"},
            )
        )


def _reverts(events: Sequence[RuntimeEvent]) -> list[RuntimeEvent]:
    return [event for event in events if event.type is EventType.TURN_REVERTED]


async def _two_completed_turns(store: SQLiteSessionStore) -> None:
    await _append_turn(store, "turn-1", "first question", reply="first reply")
    await _append_turn(
        store,
        "turn-2",
        "second question",
        history=await store.read_conversation(SESSION),
        reply="second reply",
    )


async def test_undo_returns_to_the_previous_completed_turn(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        assert isinstance(store, SessionStore)
        assert isinstance(store, UndoTurnStore)
        await _two_completed_turns(store)

        before = await store.read_conversation(SESSION)
        head_before = await store.read_head_sequence(SESSION)
        assert [message["content"] for message in before] == [
            "first question",
            "first reply",
            "second question",
            "second reply",
        ]

        outcome = await store.undo_last_turn(SESSION)

        assert outcome is not None
        assert isinstance(outcome, UndoOutcome)
        assert outcome.session_id == SESSION
        assert outcome.turn_id == "turn-2"
        assert outcome.status == "turn.completed"
        assert outcome.request_text == "second question"
        assert outcome.context_reverted is True
        assert outcome.block_reason is None

        after = await store.read_conversation(SESSION)
        assert after == FIRST_HISTORY

        # One appended marker, no row removed.
        assert await store.read_head_sequence(SESSION) == head_before + 1
        events = await store.read_session(SESSION)
        assert len(events) == head_before + 1
        markers = _reverts(events)
        assert len(markers) == 1
        assert markers[0].turn_id is None
        assert markers[0].step_id is None
        assert markers[0].payload["reverted_turn_id"] == "turn-2"


async def test_undo_survives_reopening_the_store(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite"
    async with _open(db) as store:
        await _two_completed_turns(store)
        await store.undo_last_turn(SESSION)

    async with _open(db) as reopened:
        assert await reopened.read_conversation(SESSION) == FIRST_HISTORY
        assert len(_reverts(await reopened.read_session(SESSION))) == 1
        second = await reopened.undo_last_turn(SESSION)
        assert second is not None
        assert second.turn_id == "turn-1"
        assert await reopened.read_conversation(SESSION) == ()


async def test_new_turn_after_undo_does_not_see_the_reverted_request(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _two_completed_turns(store)
        await store.undo_last_turn(SESSION)

        restored = await store.read_conversation(SESSION)
        await _append_turn(
            store, "turn-3", "third question", history=restored, reply="third reply"
        )

        events = await store.read_session(SESSION)
        snapshots = [
            event
            for event in events
            if event.type is EventType.REQUEST_PREPARED and event.turn_id == "turn-3"
        ]
        assert len(snapshots) == 1
        stored = snapshots[0].payload["messages"]
        assert not any(
            message.get("content") == "second question" for message in stored
        )
        assert stored[-1] == {"role": "user", "content": "third question"}

        projected = await store.read_conversation(SESSION)
        assert projected[:2] == FIRST_HISTORY
        assert projected[-1] == {"role": "assistant", "content": "third reply"}


async def test_repeated_undo_moves_back_then_reports_nothing_left(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        assert await store.undo_last_turn(SESSION) is None

        await _two_completed_turns(store)
        first = await store.undo_last_turn(SESSION)
        second = await store.undo_last_turn(SESSION)

        assert first is not None and first.turn_id == "turn-2"
        assert second is not None and second.turn_id == "turn-1"
        assert second.status == "turn.completed"
        assert second.request_text == "first question"
        assert second.context_reverted is True
        assert await store.read_conversation(SESSION) == ()
        assert await store.undo_last_turn(SESSION) is None


@pytest.mark.parametrize(
    "terminal", [EventType.TURN_FAILED, EventType.TURN_INTERRUPTED]
)
async def test_failed_or_interrupted_turn_is_marked_without_touching_context(
    tmp_path: Path, terminal: EventType
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_turn(store, "turn-1", "first question", reply="first reply")
        await _append_turn(
            store,
            "turn-2",
            "second question",
            history=await store.read_conversation(SESSION),
            terminal=terminal,
        )

        before = await store.read_conversation(SESSION)
        assert before == FIRST_HISTORY
        head_before = await store.read_head_sequence(SESSION)

        outcome = await store.undo_last_turn(SESSION)

        assert outcome is not None
        assert outcome.turn_id == "turn-2"
        assert outcome.status == terminal.value
        assert outcome.request_text == "second question"
        assert outcome.context_reverted is False
        assert outcome.block_reason is None

        # The marker exists so the next undo can move further back, but the
        # context is untouched because the turn never entered it.
        assert await store.read_head_sequence(SESSION) == head_before + 1
        assert await store.read_conversation(SESSION) == before

        previous = await store.undo_last_turn(SESSION)
        assert previous is not None
        assert previous.turn_id == "turn-1"
        assert previous.context_reverted is True
        assert await store.read_conversation(SESSION) == ()


async def test_undo_after_a_checkpoint_falls_back_to_the_summary(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_turn(store, "turn-1", "first question", reply="first reply")
        head = await store.read_head_sequence(SESSION)
        await store.commit_checkpoint(
            ContextCheckpoint(
                session_id=SESSION,
                covers_sequence=head,
                summary="earlier work",
                retained_messages=({"role": "user", "content": "kept"},),
            ),
            expected_head_sequence=head,
        )
        await _append_turn(
            store,
            "turn-2",
            "second question",
            history=await store.read_conversation(SESSION),
            reply="second reply",
        )

        outcome = await store.undo_last_turn(SESSION)

        assert outcome is not None
        assert outcome.turn_id == "turn-2"
        assert outcome.context_reverted is True
        assert outcome.block_reason is None

        # No completed turn survives after the checkpoint, so the projection
        # falls back to the checkpoint instead of the empty log.
        projected = await store.read_conversation(SESSION)
        assert projected[0]["role"] == "assistant"
        assert "earlier work" in projected[0]["content"]
        assert projected[1] == {"role": "user", "content": "kept"}


async def test_turn_folded_into_a_checkpoint_is_reported_not_reverted(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_turn(store, "turn-1", "first question", reply="first reply")
        await store.commit_checkpoint(
            ContextCheckpoint(
                session_id=SESSION,
                covers_sequence=await store.read_head_sequence(SESSION),
                summary="earlier work",
            ),
            expected_head_sequence=await store.read_head_sequence(SESSION),
        )
        await _append_turn(
            store,
            "turn-2",
            "second question",
            history=await store.read_conversation(SESSION),
            reply="second reply",
        )
        head_covering = await store.read_head_sequence(SESSION)
        await store.commit_checkpoint(
            ContextCheckpoint(
                session_id=SESSION,
                covers_sequence=head_covering,
                summary="both turns",
                retained_messages=({"role": "user", "content": "kept"},),
            ),
            expected_head_sequence=head_covering,
        )

        before = await store.read_conversation(SESSION)
        head_before = await store.read_head_sequence(SESSION)

        outcome = await store.undo_last_turn(SESSION)

        assert outcome is not None
        assert outcome.turn_id == "turn-2"
        assert outcome.status == "turn.completed"
        assert outcome.request_text == "second question"
        assert outcome.context_reverted is False
        assert outcome.block_reason == "checkpoint"

        # Nothing was written and the projection did not move: the summary
        # still contains that turn.
        assert await store.read_head_sequence(SESSION) == head_before
        assert _reverts(await store.read_session(SESSION)) == []
        assert await store.read_conversation(SESSION) == before


async def test_undo_refuses_while_a_turn_is_still_running(tmp_path: Path) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await _append_turn(store, "turn-1", "first question", reply="first reply")
        head_before = await store.read_head_sequence(SESSION)
        await store.append(
            RuntimeEvent(type=EventType.TURN_STARTED, session_id=SESSION, turn_id="turn-2")
        )

        with pytest.raises(RuntimeError, match="unfinished"):
            await store.undo_last_turn(SESSION)

        assert await store.read_head_sequence(SESSION) == head_before + 1
        assert _reverts(await store.read_session(SESSION)) == []

        # A running turn with nothing terminal behind it is refused too.
        async with _open(tmp_path / "other.sqlite") as fresh:
            await fresh.append(
                RuntimeEvent(
                    type=EventType.TURN_STARTED, session_id=SESSION, turn_id="turn-1"
                )
            )
            with pytest.raises(RuntimeError, match="unfinished"):
                await fresh.undo_last_turn(SESSION)
            assert await fresh.undo_last_turn("other-session") is None


async def test_revert_marker_is_session_scoped_and_outside_turn_reads(
    tmp_path: Path,
) -> None:
    marker = RuntimeEvent(
        type=EventType.TURN_REVERTED,
        session_id=SESSION,
        payload={"reverted_turn_id": "turn-9"},
    )
    assert marker.turn_id is None
    assert marker.step_id is None

    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TURN_REVERTED,
            session_id=SESSION,
            step_id="step-1",
            payload={"reverted_turn_id": "turn-9"},
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TURN_REVERTED,
            session_id=SESSION,
            turn_id="turn-9",
            step_id="step-1",
            payload={"reverted_turn_id": "turn-9"},
        )

    async with _open(tmp_path / "events.sqlite") as store:
        await _two_completed_turns(store)
        await store.undo_last_turn(SESSION)

        # The reverted turn keeps its own events; the marker is not one of them.
        turn_events = await store.read_turn(SESSION, "turn-2")
        assert [event.type for event in turn_events] == [
            EventType.TURN_STARTED,
            EventType.REQUEST_PREPARED,
            EventType.TURN_COMPLETED,
        ]
        assert not _reverts(turn_events)

        # Recovery does not read the marker as an open or closed turn.
        snapshot = await store.read_startup_snapshot(SESSION)
        assert snapshot.recovery.unfinished_turns == ()
        assert snapshot.recovery.unfinished_calls == ()
        assert snapshot.conversation == FIRST_HISTORY


async def test_revert_payload_is_copied_and_read_only(tmp_path: Path) -> None:
    payload = {"reverted_turn_id": "turn-1", "note": {"depth": ["x"]}}
    event = RuntimeEvent(
        type=EventType.TURN_REVERTED, session_id=SESSION, payload=payload
    )
    payload["reverted_turn_id"] = "turn-2"
    payload["note"]["depth"].append("y")

    assert event.payload["reverted_turn_id"] == "turn-1"
    assert event.payload["note"] == {"depth": ["x"]}
    with pytest.raises(TypeError):
        event.payload["reverted_turn_id"] = "turn-3"  # type: ignore[index]

    async with _open(tmp_path / "events.sqlite") as store:
        await store.append(event)
        stored = _reverts(await store.read_session(SESSION))
        assert stored[0].payload["reverted_turn_id"] == "turn-1"


async def test_completed_turn_without_request_snapshot_has_no_request_text(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_STARTED, session_id=SESSION, turn_id="turn-1"
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_COMPLETED,
                session_id=SESSION,
                turn_id="turn-1",
                payload={"reply": "done"},
            )
        )

        outcome = await store.undo_last_turn(SESSION)

        assert outcome is not None
        assert outcome.request_text is None
        assert outcome.context_reverted is True
        # The turn is skipped, so the broken snapshot is never projected.
        assert await store.read_conversation(SESSION) == ()


RUNTIME_CONTEXT_TAIL = (
    "--- Runtime context for this step (live environment state; this is not a "
    "user message and asks for no new task) ---\n"
    "[PhyAgentOS runtime context]\n\n"
    "workspace: /tmp/demo\n\n"
    "--- End runtime context ---"
)


async def test_undo_reads_the_real_request_before_a_runtime_context_tail(
    tmp_path: Path,
) -> None:
    async with _open(tmp_path / "events.sqlite") as store:
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_STARTED,
                session_id=SESSION,
                turn_id="turn-cache",
                payload={"max_steps": 8},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.REQUEST_PREPARED,
                session_id=SESSION,
                turn_id="turn-cache",
                step_id="turn-cache:step:1",
                payload={
                    "model_call_id": "turn-cache:model:1",
                    "messages": [
                        {"role": "system", "content": "be terse"},
                        {"role": "user", "content": "move the box"},
                        {"role": "assistant", "content": "working"},
                        {"role": "user", "name": "paos_runtime_context", "content": RUNTIME_CONTEXT_TAIL},
                    ],
                    "tools": [],
                },
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_COMPLETED,
                session_id=SESSION,
                turn_id="turn-cache",
                payload={"reply": "done"},
            )
        )

        projected = await store.read_conversation(SESSION)
        assert projected == (
            {"role": "user", "content": "move the box"},
            {"role": "assistant", "content": "working"},
            {"role": "assistant", "content": "done"},
        )

        outcome = await store.undo_last_turn(SESSION)
        assert outcome is not None
        assert outcome.request_text == "move the box"


async def test_undo_ignores_a_real_cache_stable_assembler_tail(tmp_path: Path) -> None:
    from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl
    from PhyAgentOS.agent.core.types import Budget, CancellationToken, ExecutionContext

    async with _open(tmp_path / "events.sqlite") as store:
        context = ExecutionContext(
            session_id=SESSION,
            turn_id="turn-real-cache",
            step_id="turn-real-cache:step:1",
            cancellation=CancellationToken(),
            budget=Budget(max_steps=4),
        )
        assembler = PromptAssemblerImpl(
            "static system",
            fragments=[lambda current: "workspace: /tmp/live"],
            cache_stable=True,
        )
        bundle = await assembler.assemble(
            context,
            history=({"role": "user", "content": "move the box"},),
            tools=(),
        )
        messages = [dict(message) for message in bundle.messages]
        assert messages[-1]["role"] == "user"
        assert "workspace: /tmp/live" in messages[-1]["content"]

        await store.append(
            RuntimeEvent(
                type=EventType.TURN_STARTED,
                session_id=SESSION,
                turn_id="turn-real-cache",
                payload={"max_steps": 4},
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.REQUEST_PREPARED,
                session_id=SESSION,
                turn_id="turn-real-cache",
                step_id="turn-real-cache:step:1",
                payload={
                    "model_call_id": "turn-real-cache:model:1",
                    "messages": messages,
                    "tools": [],
                },
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_COMPLETED,
                session_id=SESSION,
                turn_id="turn-real-cache",
                payload={"reply": "done"},
            )
        )

        projected = await store.read_conversation(SESSION)
        assert projected == (
            {"role": "user", "content": "move the box"},
            {"role": "assistant", "content": "done"},
        )
        outcome = await store.undo_last_turn(SESSION)
        assert outcome is not None
        assert outcome.request_text == "move the box"

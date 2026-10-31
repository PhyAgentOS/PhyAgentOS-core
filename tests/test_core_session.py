"""Tests for the SQLite session store.

These cover what the store itself must guarantee: WAL on file databases,
per-session contiguous sequences, payload snapshots and strict ordering,
concurrent appends, crash readability, recovery of unfinished model/tool
calls without fabricated success, idempotent recovery, and closed-store
behaviour. Recovery must never call a handler or model; nothing here wires
one.
"""

from __future__ import annotations

import asyncio
import copy
import sqlite3
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from PhyAgentOS.agent.core.events import CallKind, EventType, RecoveryReport, RuntimeEvent
from PhyAgentOS.agent.core.protocols import SessionStore
from PhyAgentOS.agent.core.session import SessionStoreClosedError, SQLiteSessionStore
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ToolStatus,
    TurnRequest,
    TurnStart,
)

SESSION = "session-1"

DEFAULT_MESSAGES: tuple[dict[str, Any], ...] = (
    {"role": "system", "content": "be terse"},
    {"role": "user", "content": "hello"},
)


@asynccontextmanager
async def _open(path: str | Path) -> AsyncIterator[SQLiteSessionStore]:
    store = SQLiteSessionStore(path)
    try:
        yield store
    finally:
        await store.aclose()


def _started(turn: str = "turn-1", session: str = SESSION) -> RuntimeEvent:
    return RuntimeEvent(type=EventType.TURN_STARTED, session_id=session, turn_id=turn)


def _request(
    turn: str = "turn-1",
    step: str = "step-1",
    *,
    messages: Sequence[Mapping[str, Any]] | None = None,
    model_call_id: str = "model-1",
) -> RuntimeEvent:
    snapshot = list(DEFAULT_MESSAGES) if messages is None else copy.deepcopy(list(messages))
    return RuntimeEvent(
        type=EventType.REQUEST_PREPARED,
        session_id=SESSION,
        turn_id=turn,
        step_id=step,
        payload={
            "model_call_id": model_call_id,
            "model": "fake-model",
            "messages": snapshot,
            "tools": [],
        },
    )


def _model_started(
    turn: str = "turn-1", step: str = "step-1", model_call_id: str = "model-1"
) -> RuntimeEvent:
    return RuntimeEvent(
        type=EventType.MODEL_ATTEMPT_STARTED,
        session_id=SESSION,
        turn_id=turn,
        step_id=step,
        payload={"model_call_id": model_call_id},
    )


def _model_completed(
    turn: str = "turn-1", step: str = "step-1", model_call_id: str = "model-1"
) -> RuntimeEvent:
    return RuntimeEvent(
        type=EventType.MODEL_ATTEMPT_COMPLETED,
        session_id=SESSION,
        turn_id=turn,
        step_id=step,
        payload={"model_call_id": model_call_id, "finish_reason": "tool_calls"},
    )


def _completed(turn: str = "turn-1", reply: str | None = "done") -> RuntimeEvent:
    return RuntimeEvent(
        type=EventType.TURN_COMPLETED,
        session_id=SESSION,
        turn_id=turn,
        payload={"reply": reply},
    )


def _tool(
    event_type: EventType,
    call_id: str,
    *,
    turn: str = "turn-1",
    step: str = "step-1",
    **payload: object,
) -> RuntimeEvent:
    return RuntimeEvent(
        type=event_type,
        session_id=SESSION,
        turn_id=turn,
        step_id=step,
        payload={"tool_call_id": call_id, **payload},
    )


async def test_store_satisfies_protocol_and_uses_wal(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite"
    async with _open(db) as store:
        assert isinstance(store, SessionStore)
        await store.append(_started())

    assert db.exists()
    with sqlite3.connect(db) as check:
        assert str(check.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


async def test_store_does_not_create_parent_directories(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "events.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        SQLiteSessionStore(missing)
    assert not missing.parent.exists()


async def test_memory_database_is_supported() -> None:
    async with _open(":memory:") as store:
        stored = await store.append(_started())
        assert stored.sequence == 1
        assert [event.sequence for event in await store.read_session(SESSION)] == [1]


async def test_sequence_is_per_session_and_contiguous() -> None:
    async with _open(":memory:") as store:
        first = [await store.append(_started()) for _ in range(3)]
        second = [
            await store.append(RuntimeEvent(type=EventType.INPUT_ACCEPTED, session_id="other"))
            for _ in range(2)
        ]
        third = [await store.append(_started()) for _ in range(2)]

    assert [event.sequence for event in first] == [1, 2, 3]
    assert [event.sequence for event in second] == [1, 2]
    assert [event.sequence for event in third] == [4, 5]


async def test_payload_and_event_order_round_trip() -> None:
    async with _open(":memory:") as store:
        accepted = await store.append(
            RuntimeEvent(
                type=EventType.INPUT_ACCEPTED,
                session_id=SESSION,
                payload={"characters": 5, "nested": {"reply_target": "cli"}},
            )
        )
        started = await store.append(_started())
        request = await store.append(_request())
        settled = await store.append(
            _tool(EventType.TOOL_SETTLED, "call-1", status=ToolStatus.SUCCEEDED.value, text="ok")
        )

        events = await store.read_session(SESSION)
        turn_events = await store.read_turn(SESSION, "turn-1")
        tail = await store.read_session(SESSION, after_sequence=2)

    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert (accepted.sequence, started.sequence, request.sequence, settled.sequence) == (1, 2, 3, 4)
    assert [event.type for event in events] == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.REQUEST_PREPARED,
        EventType.TOOL_SETTLED,
    ]
    assert events[0].payload["nested"] == {"reply_target": "cli"}
    assert events[2].payload["messages"][1] == {"role": "user", "content": "hello"}
    assert events[3].payload["status"] == ToolStatus.SUCCEEDED.value
    assert events[0].recorded_at.tzinfo is not None
    assert events[0].recorded_at.astimezone(timezone.utc) == events[0].recorded_at
    assert events[0].recorded_at <= events[3].recorded_at
    assert [event.sequence for event in turn_events] == [2, 3, 4]
    assert [event.sequence for event in tail] == [3, 4]


async def test_reads_use_empty_results_for_unknown_identifiers() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        assert await store.read_session("unknown-session") == ()
        assert await store.read_turn(SESSION, "unknown-turn") == ()


async def test_payload_is_snapshotted_at_append_time() -> None:
    nested: dict[str, object] = {"tool": "echo", "arguments": {"text": "hello"}}
    event = RuntimeEvent(
        type=EventType.TOOL_INTENT,
        session_id=SESSION,
        turn_id="turn-1",
        step_id="step-1",
        payload={"tool_call_id": "call-1", "arguments": nested},
    )
    async with _open(":memory:") as store:
        await store.append(event)
        nested["arguments"] = {"text": "mutated after append"}
        stored = (await store.read_session(SESSION))[0]

    assert stored.payload["arguments"] == {"tool": "echo", "arguments": {"text": "hello"}}


async def test_append_rejects_non_json_payload_without_writing() -> None:
    async with _open(":memory:") as store:
        with pytest.raises(TypeError):
            await store.append(
                RuntimeEvent(
                    type=EventType.INPUT_ACCEPTED,
                    session_id=SESSION,
                    payload={"object": object()},
                )
            )
        with pytest.raises(ValueError):
            await store.append(
                RuntimeEvent(
                    type=EventType.INPUT_ACCEPTED,
                    session_id=SESSION,
                    payload={"score": float("nan")},
                )
            )

        assert await store.read_session(SESSION) == ()
        stored = await store.append(_started())
        assert stored.sequence == 1


async def test_concurrent_appends_keep_one_contiguous_sequence() -> None:
    async with _open(":memory:") as store:

        async def write(index: int) -> RuntimeEvent:
            return await store.append(
                RuntimeEvent(
                    type=EventType.INPUT_ACCEPTED,
                    session_id=SESSION,
                    payload={"index": index},
                )
            )

        stored = await asyncio.gather(*(write(index) for index in range(25)))
        other = await asyncio.gather(
            *(
                store.append(RuntimeEvent(type=EventType.INPUT_ACCEPTED, session_id="other"))
                for _ in range(5)
            )
        )
        events = await store.read_session(SESSION)
        other_events = await store.read_session("other")

    assert sorted(event.sequence for event in stored) == list(range(1, 26))
    assert [event.sequence for event in events] == list(range(1, 26))
    assert {event.payload["index"] for event in events} == set(range(25))
    # asyncio.to_thread writers commit in lock-acquisition order, which need
    # not match gather's input order. Both stored streams must be contiguous.
    assert sorted(event.sequence for event in other) == [1, 2, 3, 4, 5]
    assert [event.sequence for event in other_events] == [1, 2, 3, 4, 5]


async def test_events_survive_a_writer_that_never_closed(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite"
    writer = SQLiteSessionStore(db)
    await writer.append(_started())
    await writer.append(_request())
    # Simulated crash: no aclose(), no checkpoint, nothing flushed by hand.

    reader = SQLiteSessionStore(db)
    try:
        events = await reader.read_session(SESSION)
        report = await reader.recover(SESSION)
        recovered = await reader.read_session(SESSION)
    finally:
        await reader.aclose()
        await writer.aclose()

    assert [event.type for event in events] == [
        EventType.TURN_STARTED,
        EventType.REQUEST_PREPARED,
    ]
    assert report.unfinished_turns == ("turn-1",)
    assert report.unfinished_calls == ()
    assert recovered[1].payload["messages"][0]["content"] == "be terse"
    assert [event.type for event in recovered] == [
        EventType.TURN_STARTED,
        EventType.REQUEST_PREPARED,
    ]


async def test_recover_reports_unfinished_model_and_tool_calls() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_request())
        await store.append(_model_started())
        await store.append(_tool(EventType.TOOL_INTENT, "call-1", tool_name="echo"))
        await store.append(_tool(EventType.TOOL_DISPATCHED, "call-1"))
        await store.append(_tool(EventType.TOOL_INTENT, "call-2", tool_name="echo"))
        await store.append(
            _tool(EventType.TOOL_SETTLED, "call-2", status=ToolStatus.SUCCEEDED.value)
        )
        await store.append(_tool(EventType.TOOL_INTENT, "call-3", tool_name="echo"))

        report = await store.recover(SESSION)
        events = await store.read_session(SESSION)

    assert isinstance(report, RecoveryReport)
    assert report.session_id == SESSION
    assert report.unfinished_turns == ("turn-1",)
    assert {(call.kind, call.call_id) for call in report.unfinished_calls} == {
        (CallKind.MODEL, "model-1"),
        (CallKind.TOOL, "call-1"),
        (CallKind.TOOL, "call-3"),
    }
    assert all(call.session_id == SESSION for call in report.unfinished_calls)
    assert all(call.turn_id == "turn-1" for call in report.unfinished_calls)
    assert all(call.step_id == "step-1" for call in report.unfinished_calls)
    assert {call.call_id: call.dispatched for call in report.unfinished_calls} == {
        "model-1": False,
        "call-1": True,
        "call-3": False,
    }

    recovery_events = [event for event in events if event.payload.get("recovered") is True]
    assert [event.type for event in recovery_events] == [
        EventType.MODEL_ATTEMPT_INTERRUPTED,
        EventType.TOOL_SETTLED,
        EventType.TOOL_SETTLED,
    ]
    assert recovery_events[1].payload["status"] == ToolStatus.UNKNOWN.value
    assert recovery_events[1].payload["tool_call_id"] == "call-1"
    assert recovery_events[2].payload["status"] == ToolStatus.FAILED.value
    assert recovery_events[2].payload["tool_call_id"] == "call-3"
    assert all("succeeded" not in str(event.payload).lower() for event in recovery_events)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))

    request = next(event for event in events if event.type is EventType.REQUEST_PREPARED)
    assert request.payload["messages"][1] == {"role": "user", "content": "hello"}


async def test_recover_distinguishes_intent_only_from_dispatched() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_tool(EventType.TOOL_INTENT, "call-intent", tool_name="echo"))
        await store.append(_tool(EventType.TOOL_INTENT, "call-dispatched", tool_name="echo"))
        await store.append(_tool(EventType.TOOL_DISPATCHED, "call-dispatched"))

        report = await store.recover(SESSION)
        events = await store.read_session(SESSION)
        again = await store.recover(SESSION)
        after = await store.read_session(SESSION)

    calls = {call.call_id: call for call in report.unfinished_calls}
    assert calls["call-intent"].kind is CallKind.TOOL
    assert calls["call-intent"].dispatched is False
    assert calls["call-dispatched"].dispatched is True

    settled = {
        event.payload["tool_call_id"]: event.payload
        for event in events
        if event.type is EventType.TOOL_SETTLED
    }
    assert settled["call-intent"]["status"] == ToolStatus.FAILED.value
    assert settled["call-intent"]["error"]["code"] == "not_dispatched"
    assert settled["call-dispatched"]["status"] == ToolStatus.UNKNOWN.value
    assert "error" not in settled["call-dispatched"]

    assert again.unfinished_calls == ()
    assert after == events


async def test_recover_keeps_reused_call_ids_in_separate_turns() -> None:
    async with _open(":memory:") as store:
        await store.append(_started("turn-1"))
        await store.append(_started("turn-2"))
        await store.append(
            _tool(EventType.TOOL_INTENT, "call-same", turn="turn-1", step="turn-1:step:1")
        )
        await store.append(
            _tool(EventType.TOOL_DISPATCHED, "call-same", turn="turn-1", step="turn-1:step:1")
        )
        await store.append(
            _tool(EventType.TOOL_INTENT, "call-same", turn="turn-2", step="turn-2:step:1")
        )

        report = await store.recover(SESSION)
        events = await store.read_session(SESSION)

    calls = {(call.turn_id, call.step_id): call for call in report.unfinished_calls}
    assert set(calls) == {("turn-1", "turn-1:step:1"), ("turn-2", "turn-2:step:1")}
    assert calls[("turn-1", "turn-1:step:1")].dispatched is True
    assert calls[("turn-2", "turn-2:step:1")].dispatched is False

    settled = {
        (event.turn_id, event.step_id): event.payload
        for event in events
        if event.type is EventType.TOOL_SETTLED
    }
    assert settled[("turn-1", "turn-1:step:1")]["status"] == ToolStatus.UNKNOWN.value
    assert settled[("turn-2", "turn-2:step:1")]["status"] == ToolStatus.FAILED.value
    assert settled[("turn-2", "turn-2:step:1")]["error"]["code"] == "not_dispatched"


async def test_recover_does_not_close_turns_or_claim_success() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_tool(EventType.TOOL_INTENT, "call-1", tool_name="echo"))
        await store.append(_tool(EventType.TOOL_DISPATCHED, "call-1"))

        report = await store.recover(SESSION)
        events = await store.read_session(SESSION)

    assert report.unfinished_turns == ("turn-1",)
    types = [event.type for event in events]
    assert EventType.TURN_COMPLETED not in types
    assert EventType.TURN_FAILED not in types
    assert EventType.TURN_INTERRUPTED not in types
    assert [event.payload["status"] for event in events if event.type is EventType.TOOL_SETTLED] == [
        ToolStatus.UNKNOWN.value
    ]


async def test_recover_is_idempotent() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_model_started())
        await store.append(_tool(EventType.TOOL_INTENT, "call-1", tool_name="echo"))

        first = await store.recover(SESSION)
        after_first = await store.read_session(SESSION)
        second = await store.recover(SESSION)
        after_second = await store.read_session(SESSION)

    assert {call.kind for call in first.unfinished_calls} == {CallKind.MODEL, CallKind.TOOL}
    assert second.unfinished_calls == ()
    assert second.unfinished_turns == ("turn-1",)
    assert len(after_first) == 5
    assert len(after_second) == len(after_first)
    assert after_second == after_first


async def test_recover_ignores_settled_attempts() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_model_started())
        await store.append(_model_completed())
        await store.append(_tool(EventType.TOOL_INTENT, "call-1", tool_name="echo"))
        await store.append(_tool(EventType.TOOL_DISPATCHED, "call-1"))
        await store.append(
            _tool(
                EventType.TOOL_SETTLED,
                "call-1",
                status=ToolStatus.FAILED.value,
                error={"code": "E", "message": "boom"},
            )
        )
        before = await store.read_session(SESSION)

        report = await store.recover(SESSION)
        after = await store.read_session(SESSION)

    assert report.unfinished_calls == ()
    assert report.unfinished_turns == ("turn-1",)
    assert after == before


async def test_concurrent_recover_appends_recovery_events_once() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_model_started())
        reports = await asyncio.gather(store.recover(SESSION), store.recover(SESSION))
        events = await store.read_session(SESSION)

    recovery_events = [event for event in events if event.payload.get("recovered") is True]
    assert len(recovery_events) == 1
    assert sum(len(report.unfinished_calls) for report in reports) == 1


async def test_recover_on_empty_session_appends_nothing() -> None:
    async with _open(":memory:") as store:
        report = await store.recover("brand-new")
        assert report.session_id == "brand-new"
        assert report.unfinished_turns == ()
        assert report.unfinished_calls == ()
        assert await store.read_session("brand-new") == ()


async def test_closed_store_is_idempotent_and_rejects_operations() -> None:
    store = SQLiteSessionStore(":memory:")
    await store.append(_started())
    await store.aclose()
    await store.aclose()

    with pytest.raises(SessionStoreClosedError):
        await store.append(_started())
    with pytest.raises(SessionStoreClosedError):
        await store.read_turn(SESSION, "turn-1")
    with pytest.raises(SessionStoreClosedError):
        await store.read_session(SESSION)
    with pytest.raises(SessionStoreClosedError):
        await store.read_conversation(SESSION)
    with pytest.raises(SessionStoreClosedError):
        await store.recover(SESSION)
    assert issubclass(SessionStoreClosedError, RuntimeError)


async def test_read_conversation_rebuilds_completed_turns() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_request())
        await store.append(_completed(reply="first answer"))

        history = await store.read_conversation(SESSION)
        assert list(history) == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "first answer"},
        ]

        await store.append(_started(turn="turn-2"))
        await store.append(
            _request(
                turn="turn-2",
                model_call_id="model-2",
                messages=[
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "again"},
                ],
            )
        )
        await store.append(_completed(turn="turn-2", reply="second answer"))

        history = await store.read_conversation(SESSION)
        assert history[0]["content"] == "hello"
        history[0]["content"] = "mutated by caller"
        reread = await store.read_conversation(SESSION)

    assert list(reread) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "second answer"},
    ]


async def test_read_conversation_uses_last_request_snapshot_with_tool_messages() -> None:
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"text": "hello"}'},
    }
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_request(step="step-1"))
        await store.append(_model_completed(step="step-1"))
        await store.append(_model_started(step="step-2", model_call_id="model-2"))
        await store.append(
            _request(
                step="step-2",
                model_call_id="model-2",
                messages=[
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": None, "tool_calls": [tool_call]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
                ],
            )
        )
        await store.append(_completed(reply="final answer"))

        history = await store.read_conversation(SESSION)

    assert list(history) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        {"role": "assistant", "content": "final answer"},
    ]


async def test_read_conversation_skips_assistant_message_without_reply() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_request())
        await store.append(_completed(reply=None))

        history = await store.read_conversation(SESSION)

    assert list(history) == [{"role": "user", "content": "hello"}]


async def test_read_conversation_is_empty_without_completed_turn() -> None:
    async with _open(":memory:") as store:
        assert await store.read_conversation("brand-new") == ()

        await store.append(_started())
        await store.append(_request())
        await store.append(_model_started())
        assert await store.read_conversation(SESSION) == ()

        await store.append(
            RuntimeEvent(
                type=EventType.TURN_FAILED,
                session_id=SESSION,
                turn_id="turn-1",
                payload={"error": "boom"},
            )
        )
        await store.append(_started(turn="turn-2"))
        assert await store.read_conversation(SESSION) == ()


async def test_read_conversation_ignores_turns_after_the_newest_completed_one() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_request())
        await store.append(_completed(reply="first answer"))
        await store.append(_started(turn="turn-2"))
        await store.append(
            _request(
                turn="turn-2",
                model_call_id="model-2",
                messages=[
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "second"},
                ],
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_INTERRUPTED,
                session_id=SESSION,
                turn_id="turn-2",
                payload={"reason": "stop"},
            )
        )

        history = await store.read_conversation(SESSION)

    assert list(history) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "first answer"},
    ]


async def test_read_conversation_survives_reopen_and_ignores_crashed_turn(
    tmp_path: Path,
) -> None:
    db = tmp_path / "events.sqlite"
    writer = SQLiteSessionStore(db)
    await writer.append(_started())
    await writer.append(
        _request(
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "first"},
            ]
        )
    )
    await writer.append(_completed(reply="first answer"))
    await writer.append(_started(turn="turn-2"))
    await writer.append(
        _request(
            turn="turn-2",
            model_call_id="model-2",
            messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "crashed"},
            ],
        )
    )
    await writer.append(_model_started(turn="turn-2", model_call_id="model-2"))
    # Simulated crash: the writer is never closed.

    reader = SQLiteSessionStore(db)
    try:
        history = await reader.read_conversation(SESSION)
        report = await reader.recover(SESSION)
        after = await reader.read_conversation(SESSION)
    finally:
        await reader.aclose()
        await writer.aclose()

    assert list(history) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]
    assert report.unfinished_turns == ("turn-2",)
    assert after == history


async def test_read_conversation_output_fits_turn_start_history() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_request())
        await store.append(_completed(reply="ok"))
        history = await store.read_conversation(SESSION)

    turn = TurnStart(
        request=TurnRequest(session_id=SESSION, text="next"),
        turn_id="turn-2",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=2),
        history=history,
    )
    assert [dict(message) for message in turn.history] == list(history)


async def test_read_conversation_rejects_completed_turn_without_snapshot() -> None:
    async with _open(":memory:") as store:
        await store.append(_started())
        await store.append(_completed())
        with pytest.raises(ValueError):
            await store.read_conversation(SESSION)


async def test_sequence_is_assigned_to_the_returned_copy_only() -> None:
    async with _open(":memory:") as store:
        event = _started()
        assert event.sequence is None
        stored = await store.append(event)
        assert event.sequence is None
        assert stored.sequence == 1
        assert stored.payload == event.payload
        assert stored.recorded_at == event.recorded_at

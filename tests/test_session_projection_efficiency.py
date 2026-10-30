"""Startup projection efficiency and freshness for the SQLite session store.

These tests use the real ``SQLiteSessionStore``, the real ``AgentRuntimeImpl``
and a small driver that only writes the events a driver owns. They do not
replace the store's projection: they count the reads the store actually
performs. The counter subclasses never override ``recover`` unless a test is
explicitly exercising the public fallback, so the snapshot fast path is the
path under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from PhyAgentOS.agent.core import (
    AgentRuntimeImpl,
    EventType,
    RuntimeEvent,
    SQLiteSessionStore,
    TurnRequest,
    TurnResult,
    TurnStatus,
)
from PhyAgentOS.agent.core.runtime import RuntimeStorageError
from PhyAgentOS.agent.core.session import (
    CHECKPOINT_SUMMARY_NOTICE,
    ContextCheckpoint,
    RecoveryConflictError,
)

SESSION = "proj"


def _event(type_, turn_id=None, step_id=None, payload=None):
    return RuntimeEvent(
        type=type_,
        session_id=SESSION,
        turn_id=turn_id,
        step_id=step_id,
        payload=payload or {},
    )


async def _seed_completed_turn(
    store: SQLiteSessionStore,
    turn_id: str,
    text: str,
    reply: str,
    *,
    deltas: int = 0,
) -> None:
    step_id = f"{turn_id}/step-1"
    call_id = f"{step_id}:model"
    await store.append(_event(EventType.INPUT_ACCEPTED, payload={"text": text}))
    await store.append(_event(EventType.TURN_STARTED, turn_id))
    await store.append(
        _event(
            EventType.REQUEST_PREPARED,
            turn_id,
            step_id,
            {
                "model_call_id": call_id,
                "messages": [
                    {"role": "system", "content": "fixed system prompt"},
                    {"role": "user", "content": text},
                ],
                "tools": [],
            },
        )
    )
    for index in range(deltas):
        await store.append(
            _event(
                EventType.MODEL_RESPONSE_DELTA,
                turn_id,
                step_id,
                {"model_call_id": call_id, "content": f"chunk-{index} "},
            )
        )
    await store.append(
        _event(
            EventType.MODEL_ATTEMPT_COMPLETED,
            turn_id,
            step_id,
            {"model_call_id": call_id, "streaming": "native"},
        )
    )
    await store.append(
        _event(EventType.TURN_COMPLETED, turn_id, payload={"reply": reply})
    )


class _StaticDriver:
    """Write the request snapshot a loop driver would write, then complete."""

    def __init__(self, store: SQLiteSessionStore, reply_prefix: str = "current") -> None:
        self._store = store
        self._reply_prefix = reply_prefix
        self.turns = []

    async def run(self, turn):
        self.turns.append(turn)
        step_id = "step-1"
        call_id = f"{step_id}:model"
        await self._store.append(
            _event(
                EventType.REQUEST_PREPARED,
                turn.turn_id,
                step_id,
                {
                    "model_call_id": call_id,
                    "messages": [{"role": "user", "content": turn.request.text}],
                    "tools": [],
                },
            )
        )
        return TurnResult(
            session_id=turn.request.session_id,
            turn_id=turn.turn_id,
            status=TurnStatus.COMPLETED,
            reply=f"{self._reply_prefix}:{turn.request.text}",
        )


class _CountingStore(SQLiteSessionStore):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.select_calls: list[tuple[str, str | None, int]] = []
        self.startup_rows_calls = 0
        self.last_startup_events: tuple[RuntimeEvent, ...] = ()

    def _select_sync(self, session_id, turn_id, after_sequence):
        self.select_calls.append((session_id, turn_id, after_sequence))
        return super()._select_sync(session_id, turn_id, after_sequence)

    def _startup_rows_sync(self, session_id):
        self.startup_rows_calls += 1
        result = super()._startup_rows_sync(session_id)
        self.last_startup_events = result[0]
        return result


def _runtime(store: SQLiteSessionStore, driver) -> AgentRuntimeImpl:
    return AgentRuntimeImpl(driver=driver, store=store, max_steps=2)


async def test_startup_does_one_filtered_read_not_two_full_scans(tmp_path: Path) -> None:
    store = _CountingStore(tmp_path / "efficiency.db")
    for index in range(4):
        await _seed_completed_turn(
            store, f"seed-{index}", f"question-{index}", f"answer-{index}", deltas=50
        )
    driver = _StaticDriver(store)
    runtime = _runtime(store, driver)

    # The old startup shape is still available and still costs two full scans.
    await store.recover(SESSION)
    await store.read_conversation(SESSION)
    assert len(store.select_calls) == 2

    before = [
        event
        for event in await store.read_session(SESSION)
        if event.type is not EventType.MODEL_RESPONSE_DELTA
    ]
    store.select_calls.clear()
    await runtime.submit(TurnRequest(session_id=SESSION, text="warm"))

    assert store.startup_rows_calls == 1
    assert store.select_calls == []
    # The one startup read skips streaming deltas entirely.
    assert store.last_startup_events
    assert all(
        event.type is not EventType.MODEL_RESPONSE_DELTA
        for event in store.last_startup_events
    )
    assert len(store.last_startup_events) == len(before)

    assert [dict(message) for message in driver.turns[0].history] == [
        {"role": "user", "content": "question-3"},
        {"role": "assistant", "content": "answer-3"},
    ]
    await store.aclose()


async def test_external_append_between_turns_is_seen(tmp_path: Path) -> None:
    path = tmp_path / "external.db"
    store = _CountingStore(path)
    await _seed_completed_turn(store, "seed-0", "first", "first-reply")
    driver = _StaticDriver(store)
    runtime = _runtime(store, driver)

    await runtime.submit(TurnRequest(session_id=SESSION, text="local-1"))
    assert [dict(message) for message in driver.turns[0].history] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first-reply"},
    ]

    # A separate connection (another process role) appends a completed turn
    # after the first snapshot. The next startup must not reuse stale rows.
    other = SQLiteSessionStore(path)
    await _seed_completed_turn(other, "other-turn", "external", "external-reply")
    await other.aclose()

    await runtime.submit(TurnRequest(session_id=SESSION, text="local-2"))
    assert [dict(message) for message in driver.turns[1].history] == [
        {"role": "user", "content": "external"},
        {"role": "assistant", "content": "external-reply"},
    ]
    await store.aclose()


class _CheckpointRacingStore(_CountingStore):
    """Commit a checkpoint after the snapshot rows were read, at the same head."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.checkpoint_summary = "racing summary"
        self._committed = False

    def _startup_rows_sync(self, session_id):
        events, checkpoint, head = super()._startup_rows_sync(session_id)
        if not self._committed:
            self._committed = True
            self._commit_checkpoint_sync(
                ContextCheckpoint(
                    session_id=session_id,
                    covers_sequence=head,
                    summary=self.checkpoint_summary,
                    retained_messages=(
                        {"role": "assistant", "content": "retained tail"},
                    ),
                ),
                head,
            )
        return events, checkpoint, head


async def test_checkpoint_change_at_unchanged_head_is_refreshed(
    tmp_path: Path,
) -> None:
    store = _CheckpointRacingStore(tmp_path / "checkpoint-race.db")
    await _seed_completed_turn(store, "seed-0", "old question", "old answer")
    driver = _StaticDriver(store)
    runtime = _runtime(store, driver)

    # The rows read at the start of the snapshot have no checkpoint. The
    # checkpoint is committed before the post-check, at the same head, so only
    # the checkpoint identity can reveal the change. The runtime must still
    # receive the checkpoint projection, not the precomputed old history.
    await runtime.submit(TurnRequest(session_id=SESSION, text="warm"))

    history = [dict(message) for message in driver.turns[0].history]
    assert history[0]["role"] == "assistant"
    assert CHECKPOINT_SUMMARY_NOTICE in history[0]["content"]
    assert "racing summary" in history[0]["content"]
    assert history[1] == {"role": "assistant", "content": "retained tail"}
    await store.aclose()


async def test_snapshot_recovery_matches_closed_log_rules(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "recovery.db")
    await _seed_completed_turn(store, "done-turn", "old", "old-reply")
    await store.append(
        _event(
            EventType.TURN_STARTED,
            "crashed-turn",
        )
    )
    await store.append(
        _event(
            EventType.MODEL_ATTEMPT_STARTED,
            "crashed-turn",
            "step-1",
            {"model_call_id": "crashed-call"},
        )
    )

    snapshot = await store.read_startup_snapshot(SESSION)

    assert snapshot.recovery.unfinished_turns == ("crashed-turn",)
    assert [call.call_id for call in snapshot.recovery.unfinished_calls] == [
        "crashed-call"
    ]
    # Recovery settles the attempt as interrupted, never as a reply and never
    # with a retry instruction.
    events = await store.read_session(SESSION)
    interrupted = [e for e in events if e.type is EventType.MODEL_ATTEMPT_INTERRUPTED]
    assert len(interrupted) == 1
    assert interrupted[0].payload["model_call_id"] == "crashed-call"
    assert all(e.type is not EventType.TURN_COMPLETED for e in events if e.turn_id == "crashed-turn")
    assert [dict(message) for message in snapshot.conversation] == [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old-reply"},
    ]
    await store.aclose()


class _ExternalCompleter:
    """A second connection that completes a call and turn while A is reading."""

    def __init__(self, path) -> None:
        self._store = SQLiteSessionStore(path)

    def complete(self, turn_id: str, step_id: str, call_id: str, reply: str) -> None:
        self._store._append_sync(
            _event(
                EventType.MODEL_ATTEMPT_COMPLETED,
                turn_id,
                step_id,
                {"model_call_id": call_id, "streaming": "native"},
            )
        )
        self.complete_turn(turn_id, reply)

    def complete_turn(self, turn_id: str, reply: str) -> None:
        self._store._append_sync(
            _event(EventType.TURN_COMPLETED, turn_id, payload={"reply": reply})
        )

    def complete_call(self, turn_id: str, step_id: str, call_id: str) -> None:
        self._store._append_sync(
            _event(
                EventType.MODEL_ATTEMPT_COMPLETED,
                turn_id,
                step_id,
                {"model_call_id": call_id, "streaming": "native"},
            )
        )

    def append_delta(self, turn_id: str, step_id: str, call_id: str) -> None:
        self._store._append_sync(
            _event(
                EventType.MODEL_RESPONSE_DELTA,
                turn_id,
                step_id,
                {"model_call_id": call_id, "content": "moving "},
            )
        )

    async def aclose(self) -> None:
        await self._store.aclose()


async def _seed_crashed_turn(store: SQLiteSessionStore, text: str) -> tuple[str, str, str]:
    turn_id = "crashed-turn"
    step_id = "step-1"
    call_id = f"{step_id}:model"
    await store.append(_event(EventType.TURN_STARTED, turn_id))
    await store.append(
        _event(
            EventType.REQUEST_PREPARED,
            turn_id,
            step_id,
            {
                "model_call_id": call_id,
                "messages": [
                    {"role": "system", "content": "fixed system prompt"},
                    {"role": "user", "content": text},
                ],
                "tools": [],
            },
        )
    )
    await store.append(
        _event(
            EventType.MODEL_ATTEMPT_STARTED,
            turn_id,
            step_id,
            {"model_call_id": call_id},
        )
    )
    return turn_id, step_id, call_id


class _ReadRaceStore(_CountingStore):
    """A reads old rows, then B completes the call and turn on another connection."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.external = _ExternalCompleter(path)
        self.injected = False
        self.recovery_attempts = []
        self.recovery_results = []
        self.append_types = []

    def _startup_rows_sync(self, session_id):
        result = super()._startup_rows_sync(session_id)
        if not self.injected:
            self.injected = True
            turn_id, step_id, call_id = self._race
            self.external.complete(turn_id, step_id, call_id, "external reply")
        return result

    async def append(self, event):
        self.append_types.append(event.type)
        return await super().append(event)

    async def append_recovery(self, session_id, events, *, expected_head):
        self.recovery_attempts.append(
            (expected_head, tuple(event.type for event in events))
        )
        result = await super().append_recovery(
            session_id, events, expected_head=expected_head
        )
        self.recovery_results.append(result)
        return result


async def test_read_race_does_not_close_externally_completed_work(tmp_path: Path) -> None:
    path = tmp_path / "read-race.db"
    store = _ReadRaceStore(path)
    store._race = await _seed_crashed_turn(store, "external question")
    store.append_types.clear()

    snapshot = await store.read_startup_snapshot(SESSION)

    assert snapshot.recovery.unfinished_turns == ()
    assert snapshot.recovery.unfinished_calls == ()
    # The first conditional batch was rejected because B moved the head; no
    # interruption was written, and no second batch was needed after re-folding.
    assert len(store.recovery_attempts) == 1
    assert store.recovery_results == [None]
    assert [event.type for event in snapshot.recovery.interruption_events()] == []

    events = await store.read_session(SESSION)
    assert [e.type for e in events].count(EventType.TURN_COMPLETED) == 1
    assert EventType.TURN_INTERRUPTED not in [e.type for e in events]
    assert EventType.MODEL_ATTEMPT_INTERRUPTED not in [e.type for e in events]
    assert [dict(message) for message in snapshot.conversation] == [
        {"role": "user", "content": "external question"},
        {"role": "assistant", "content": "external reply"},
    ]
    # The snapshot itself wrote nothing through the public append hook.
    assert store.append_types == []
    await store.external.aclose()
    await store.aclose()


class _CloseRaceStore(_CountingStore):
    """B completes the turn after the snapshot, before the runtime close CAS."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.external = _ExternalCompleter(path)
        self.injected = False
        self.recovery_attempts = []
        self.recovery_results = []
        self.append_types = []

    async def append(self, event):
        self.append_types.append(event.type)
        return await super().append(event)

    async def append_recovery(self, session_id, events, *, expected_head):
        self.recovery_attempts.append(
            (expected_head, tuple(event.type for event in events))
        )
        if not self.injected and any(
            event.type is EventType.TURN_INTERRUPTED for event in events
        ):
            self.injected = True
            turn_id, _, _ = self._race
            self.external.complete_turn(turn_id, "external reply")
        result = await super().append_recovery(
            session_id, events, expected_head=expected_head
        )
        self.recovery_results.append(result)
        return result


async def test_runtime_close_race_rechecks_instead_of_double_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "close-race.db"
    store = _CloseRaceStore(path)
    store._race = await _seed_crashed_turn(store, "external question")
    store.append_types.clear()
    driver = _StaticDriver(store)
    runtime = _runtime(store, driver)

    await runtime.submit(TurnRequest(session_id=SESSION, text="warm"))

    assert None in store.recovery_results
    events = await store.read_session(SESSION)
    old = [e for e in events if e.turn_id == "crashed-turn"]
    assert [e.type for e in old].count(EventType.TURN_COMPLETED) == 1
    assert EventType.TURN_INTERRUPTED not in [e.type for e in old]
    assert [dict(message) for message in driver.turns[0].history] == [
        {"role": "user", "content": "external question"},
        {"role": "assistant", "content": "external reply"},
    ]
    # Normal turn writes still go through the public append hook.
    assert EventType.INPUT_ACCEPTED in store.append_types
    assert EventType.TURN_STARTED in store.append_types
    assert EventType.TURN_COMPLETED in store.append_types
    await store.external.aclose()
    await store.aclose()


async def test_external_completion_before_read_needs_no_recovery_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "before-read.db"
    store = _ReadRaceStore(path)
    store.injected = True  # no injection inside the read: B already finished
    turn_id, step_id, call_id = await _seed_crashed_turn(store, "external question")
    store.external.complete(turn_id, step_id, call_id, "external reply")
    store.append_types.clear()

    snapshot = await store.read_startup_snapshot(SESSION)

    assert snapshot.recovery.unfinished_turns == ()
    assert snapshot.recovery.unfinished_calls == ()
    assert store.recovery_attempts == []
    assert store.append_types == []
    events = await store.read_session(SESSION)
    assert EventType.MODEL_ATTEMPT_INTERRUPTED not in [e.type for e in events]
    assert EventType.TURN_INTERRUPTED not in [e.type for e in events]
    assert [dict(message) for message in snapshot.conversation] == [
        {"role": "user", "content": "external question"},
        {"role": "assistant", "content": "external reply"},
    ]
    await store.external.aclose()
    await store.aclose()


class _AlwaysMovingStore(_CountingStore):
    """Every startup read is followed by an external append, forever."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.external = _ExternalCompleter(path)
        self.race = ("crashed-turn", "step-1", "step-1:model")
        self.batches = 0

    def _startup_rows_sync(self, session_id):
        result = super()._startup_rows_sync(session_id)
        turn_id, step_id, call_id = self.race
        self.external.append_delta(turn_id, step_id, call_id)
        return result

    async def append_recovery(self, session_id, events, *, expected_head):
        self.batches += 1
        return await super().append_recovery(
            session_id, events, expected_head=expected_head
        )


async def test_snapshot_exhaustion_fails_closed_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "always-moving.db"
    store = _AlwaysMovingStore(path)
    await _seed_crashed_turn(store, "still running")

    with pytest.raises(RecoveryConflictError):
        await store.read_startup_snapshot(SESSION)

    assert store.batches == 4
    events = await store.read_session(SESSION)
    types = [event.type for event in events]
    assert EventType.MODEL_ATTEMPT_INTERRUPTED not in types
    assert EventType.TURN_INTERRUPTED not in types
    assert types.count(EventType.MODEL_ATTEMPT_STARTED) == 1
    # The original call is still open; nothing was turned into a fake terminal.
    assert types.count(EventType.MODEL_ATTEMPT_COMPLETED) == 0
    await store.external.aclose()
    await store.aclose()


class _RuntimeCloseMovingStore(_CountingStore):
    """Every runtime turn-close CAS loses to an external append."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.external = _ExternalCompleter(path)
        self.race = ("crashed-turn", "step-1", "step-1:model")
        self.close_attempts = 0

    async def append_recovery(self, session_id, events, *, expected_head):
        if any(event.type is EventType.TURN_INTERRUPTED for event in events):
            self.close_attempts += 1
            turn_id, step_id, call_id = self.race
            self.external.append_delta(turn_id, step_id, call_id)
        return await super().append_recovery(
            session_id, events, expected_head=expected_head
        )


async def test_runtime_exhaustion_opens_no_turn_and_writes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-moving.db"
    store = _RuntimeCloseMovingStore(path)
    turn_id, step_id, call_id = await _seed_crashed_turn(store, "still running")
    store.external.complete_call(turn_id, step_id, call_id)
    driver = _StaticDriver(store)
    runtime = _runtime(store, driver)

    with pytest.raises(RuntimeStorageError):
        await runtime.submit(TurnRequest(session_id=SESSION, text="must not start"))

    assert store.close_attempts == 4
    assert driver.turns == []
    events = await store.read_session(SESSION)
    types = [event.type for event in events]
    assert types.count(EventType.TURN_STARTED) == 1
    assert EventType.INPUT_ACCEPTED not in types
    assert EventType.TURN_INTERRUPTED not in types
    assert EventType.TURN_COMPLETED not in types
    assert EventType.MODEL_ATTEMPT_INTERRUPTED not in types
    await store.external.aclose()
    await store.aclose()

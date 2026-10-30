"""Tests for manual context compaction.

Covered here: checkpoint persistence across reopen, projection from the
checkpoint, a later completed turn winning with its own snapshot, failure and
CAS rejection leaving history untouched, complete tool pairs in the retained
tail, bounded summary input, refusal when the summary would not shrink the
conversation, per-session usage accumulation, and failed turns staying out of
the projection.
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.session import (
    CHECKPOINT_SUMMARY_NOTICE,
    CheckpointConflictError,
    ContextCheckpoint,
    SQLiteSessionStore,
)
from PhyAgentOS.agent.services.context_compaction import (
    CompactionError,
    compact_session,
)

SESSION = "cli:20261030-01"
OTHER = "cli:20261030-02"
PROVIDER_USAGE = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}


class FakeProvider:
    """Answers with a fixed summary and records the request it received."""

    def __init__(
        self,
        replies: str | list[str] = "## Objective\nDone.",
        *,
        usage: dict[str, int] | None = None,
        before: Any = None,
        error: Exception | None = None,
        finish_reason: str = "stop",
    ) -> None:
        self.replies = [replies] if isinstance(replies, str) else list(replies)
        self.usage = dict(PROVIDER_USAGE if usage is None else usage)
        self.before = before
        self.error = error
        self.finish_reason = finish_reason
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        if self.before is not None:
            await self.before()
        if self.error is not None:
            raise self.error
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(
            content=reply,
            finish_reason=self.finish_reason,
            usage=dict(self.usage),
            tool_calls=[],
        )


def _event(
    event_type: EventType,
    turn_id: str,
    *,
    step_id: str | None = None,
    payload: dict[str, Any] | None = None,
    session: str = SESSION,
) -> RuntimeEvent:
    return RuntimeEvent(
        type=event_type,
        session_id=session,
        turn_id=turn_id,
        step_id=step_id,
        payload=payload or {},
    )


async def _append_turn(
    store: SQLiteSessionStore,
    turn_id: str,
    history: list[dict[str, Any]],
    user_text: str,
    reply: str | None,
    *,
    session: str = SESSION,
) -> list[dict[str, Any]]:
    messages = [
        {"role": "system", "content": "base instructions"},
        *history,
        {"role": "user", "content": user_text},
    ]
    await store.append(_event(EventType.TURN_STARTED, turn_id, session=session))
    await store.append(
        _event(
            EventType.REQUEST_PREPARED,
            turn_id,
            step_id="step-1",
            session=session,
            payload={
                "model_call_id": f"model-{turn_id}",
                "messages": messages,
                "tools": [],
            },
        )
    )
    await store.append(
        _event(
            EventType.TURN_COMPLETED,
            turn_id,
            session=session,
            payload={"reply": reply},
        )
    )
    return [
        *history,
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply},
    ]


async def _seed_turns(
    store: SQLiteSessionStore, count: int, *, session: str = SESSION
) -> list[dict[str, Any]]:
    """Seed realistic turns so a summary can actually be smaller than them."""
    history: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        history = await _append_turn(
            store,
            f"turn-{index}",
            history,
            f"task {index}: " + "context " * 20,
            f"answer {index}: " + "result " * 10,
            session=session,
        )
    return history


async def _snapshot(store: SQLiteSessionStore, session: str = SESSION) -> list[tuple]:
    return [
        (event.sequence, event.type, dict(event.payload))
        for event in await store.read_session(session)
    ]


@pytest.mark.asyncio
async def test_compaction_stores_summary_and_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "core.db"
    store = SQLiteSessionStore(db)
    seeded = await _seed_turns(store, 4)
    before_events = await _snapshot(store)
    provider = FakeProvider("## Objective\nFinish task 4.")

    result = await compact_session(
        store,
        provider,
        SESSION,
        model="fake-model",
        effort="high",
        provider_name="fake",
    )

    assert provider.calls[0]["tools"] is None
    assert provider.calls[0]["kwargs"] == {"model": "fake-model", "reasoning_effort": "high"}
    assert result.checkpoint.covers_sequence == 12
    assert result.checkpoint.summary == "## Objective\nFinish task 4."
    assert result.checkpoint.selection == {
        "provider": "fake",
        "model": "fake-model",
        "effort": "high",
    }
    assert result.after_tokens < result.before_tokens
    assert result.usage == PROVIDER_USAGE
    assert result.attempt.status == "completed"
    assert result.attempt.attempt_id is not None
    assert result.omitted_prefix_messages == 0
    assert result.truncated_prefix_messages == 0
    assert result.warnings == ()

    # Raw events are append-only: compaction adds no event at all.
    assert await _snapshot(store) == before_events

    history = list(await store.read_conversation(SESSION))
    assert history[0]["role"] == "assistant"
    assert CHECKPOINT_SUMMARY_NOTICE in history[0]["content"]
    assert "Finish task 4." in history[0]["content"]
    assert history[1:] == seeded[4:]
    assert await store.get_compaction_usage(SESSION) == PROVIDER_USAGE
    attempts = await store.list_compaction_attempts(SESSION)
    assert len(attempts) == 1
    assert attempts[0].status == "completed"
    assert attempts[0].model == "fake-model"
    assert attempts[0].effort == "high"
    checkpoint = await store.read_checkpoint(SESSION)
    assert checkpoint is not None and checkpoint.checkpoint_id == result.checkpoint.checkpoint_id
    await store.aclose()

    reopened = SQLiteSessionStore(db)
    try:
        assert list(await reopened.read_conversation(SESSION)) == history
        read_checkpoint = await reopened.read_checkpoint(SESSION)
        assert read_checkpoint is not None
        assert read_checkpoint.summary == "## Objective\nFinish task 4."
        assert read_checkpoint.created_at.tzinfo is not None
        assert read_checkpoint.created_at.utcoffset() == timezone.utc.utcoffset(None)
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_completed_turn_after_checkpoint_wins_with_its_snapshot(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 4)
        await compact_session(
            store, FakeProvider("short summary"), SESSION, model="fake-model"
        )
        projected = [dict(message) for message in await store.read_conversation(SESSION)]

        await _append_turn(store, "turn-5", projected, "task 5", "answer 5")
        history = [dict(message) for message in await store.read_conversation(SESSION)]
    finally:
        await store.aclose()

    assert history == [
        *projected,
        {"role": "user", "content": "task 5"},
        {"role": "assistant", "content": "answer 5"},
    ]
    assert history[0]["role"] == "assistant"
    assert CHECKPOINT_SUMMARY_NOTICE in history[0]["content"]


@pytest.mark.asyncio
async def test_failed_summary_leaves_history_and_projection_unchanged(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 3)
        before_events = await _snapshot(store)
        before_history = list(await store.read_conversation(SESSION))
        provider = FakeProvider(error=RuntimeError("provider down"))

        with pytest.raises(CompactionError, match="provider call failed"):
            await compact_session(store, provider, SESSION, model="fake-model")

        assert await _snapshot(store) == before_events
        assert list(await store.read_conversation(SESSION)) == before_history
        assert await store.read_checkpoint(SESSION) is None
        assert await store.get_compaction_usage(SESSION) == {}
        attempts = await store.list_compaction_attempts(SESSION)
        assert len(attempts) == 1
        assert attempts[0].status == "failed"
        assert attempts[0].reason.startswith("provider_error:")
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_new_event_after_snapshot_rejects_the_checkpoint(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 3)
        before_history = list(await store.read_conversation(SESSION))

        async def append_during_summary() -> None:
            await store.append(_event(EventType.TURN_STARTED, "turn-new"))

        provider = FakeProvider("short summary", before=append_during_summary)
        with pytest.raises(CheckpointConflictError, match="head moved"):
            await compact_session(store, provider, SESSION, model="fake-model")

        assert await store.read_checkpoint(SESSION) is None
        assert await store.get_compaction_usage(SESSION) == PROVIDER_USAGE
        attempts = await store.list_compaction_attempts(SESSION)
        assert len(attempts) == 1
        assert attempts[0].status == "failed"
        assert "head moved" in attempts[0].reason
        events = await store.read_session(SESSION)
        assert events[-1].type is EventType.TURN_STARTED
        assert list(await store.read_conversation(SESSION)) == before_history
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_open_turn_blocks_a_checkpoint(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 3)
        await store.append(_event(EventType.TURN_STARTED, "turn-open"))
        head = await store.read_head_sequence(SESSION)
        checkpoint = ContextCheckpoint(
            session_id=SESSION,
            covers_sequence=head,
            summary="summary",
            retained_messages=tuple(await store.read_conversation(SESSION)),
        )
        with pytest.raises(CheckpointConflictError, match="unfinished work"):
            await store.commit_checkpoint(checkpoint, expected_head_sequence=head)
        assert await store.read_checkpoint(SESSION) is None
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_retained_tail_keeps_complete_tool_pairs(tmp_path: Path) -> None:
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"text": "hello"}'},
    }
    conversation = [
        {"role": "user", "content": "old task " + "x" * 400},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "task with tool"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
        {"role": "assistant", "content": "tool answer"},
        {"role": "user", "content": "final task"},
        {"role": "assistant", "content": "final answer"},
    ]
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await store.append(_event(EventType.TURN_STARTED, "turn-1"))
        await store.append(
            _event(
                EventType.REQUEST_PREPARED,
                "turn-1",
                step_id="step-1",
                payload={
                    "model_call_id": "m1",
                    "messages": [{"role": "system", "content": "sys"}, *conversation],
                    "tools": [],
                },
            )
        )
        await store.append(
            _event(EventType.TURN_COMPLETED, "turn-1", payload={"reply": None})
        )

        result = await compact_session(
            store,
            FakeProvider("## Objective\nKeep the tool work."),
            SESSION,
            model="fake-model",
        )
        retained = [dict(message) for message in result.checkpoint.retained_messages]
    finally:
        await store.aclose()

    assert [message["content"] for message in retained[:2]] == [
        "task with tool",
        None,
    ]
    assert retained[2] == {"role": "tool", "tool_call_id": "call-1", "content": "tool output"}
    declared = {
        call["id"]
        for message in retained
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    }
    tool_ids = [
        message["tool_call_id"]
        for message in retained
        if message.get("role") == "tool"
    ]
    assert tool_ids == ["call-1"]
    assert set(tool_ids) <= declared


@pytest.mark.asyncio
async def test_oversized_retained_tail_fails_before_any_model_call(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        history: list[dict[str, Any]] = []
        history = await _append_turn(store, "turn-1", history, "first", "one")
        history = await _append_turn(store, "turn-2", history, "second", "two")
        history = await _append_turn(store, "turn-3", history, "third", "r" * 4000)
        before_events = await _snapshot(store)
        provider = FakeProvider("short")

        with pytest.raises(CompactionError, match="retained budget"):
            await compact_session(
                store,
                provider,
                SESSION,
                model="fake-model",
                max_retained_chars=1000,
            )

        assert provider.calls == []
        assert await _snapshot(store) == before_events
        assert await store.read_checkpoint(SESSION) is None
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_summary_input_is_bounded_and_says_what_was_omitted(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        history: list[dict[str, Any]] = []
        for index in range(1, 5):
            history = await _append_turn(
                store, f"turn-{index}", history, f"task {index} " + "x" * 1200, f"a{index}"
            )
        provider = FakeProvider("small summary")

        result = await compact_session(
            store,
            provider,
            SESSION,
            model="fake-model",
            max_summary_input_chars=800,
            max_message_chars=800,
        )

        sent = provider.calls[0]["messages"][1]["content"]
        assert len(sent) <= 800 + 400  # transcript budget plus the wrapper/note
        assert "omitted" in sent or "[truncated]" in sent
        assert result.omitted_prefix_messages > 0 or result.truncated_prefix_messages > 0
        assert result.after_tokens < result.before_tokens
        assert await store.read_checkpoint(SESSION) is not None
        assert result.warnings
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_omitted_middle_groups_keep_the_first_task_and_warn(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        history: list[dict[str, Any]] = []
        history = await _append_turn(
            store,
            "turn-1",
            history,
            "FIRST_CONSTRAINT: never delete user data",
            "understood",
        )
        history = await _append_turn(
            store, "turn-2", history, "middle two " + "m" * 1200, "middle answer two"
        )
        history = await _append_turn(
            store, "turn-3", history, "middle three " + "m" * 1200, "middle answer three"
        )
        history = await _append_turn(
            store, "turn-4", history, "recent task " + "r" * 200, "recent answer"
        )
        await _append_turn(store, "turn-5", history, "latest task", "latest answer")

        provider = FakeProvider("## Objective\nKeep the first constraint.")
        result = await compact_session(
            store,
            provider,
            SESSION,
            model="fake-model",
            max_summary_input_chars=500,
            max_message_chars=500,
        )

        sent = provider.calls[0]["messages"][1]["content"]
        assert "FIRST_CONSTRAINT: never delete user data" in sent
        assert "middle two" not in sent
        assert result.omitted_prefix_messages > 0
        assert any("first task" in warning for warning in result.warnings)
        assert "first task and its constraints" in sent
        assert await store.read_checkpoint(SESSION) is not None
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_summary_prompt_is_checked_against_the_model_window(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        history = await _append_turn(store, "turn-1", [], "first task", "one")
        await _append_turn(store, "turn-2", history, "second task", "two")
        provider = FakeProvider("small summary")

        with pytest.raises(CompactionError, match="exceeds the model context"):
            await compact_session(
                store,
                provider,
                SESSION,
                model="fake-model",
                context_window_tokens=50,
                reserve_tokens=10,
                max_summary_input_chars=1000,
            )

        assert provider.calls == []
        assert await store.list_compaction_attempts(SESSION) == ()
        assert await store.read_checkpoint(SESSION) is None
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_attempt_rows_are_durable_and_close_once(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        attempt = await store.begin_compaction_attempt(
            SESSION, provider="fake", model="fake-model", effort="high"
        )
        assert attempt.attempt_id is not None
        assert attempt.status == "started"
        assert attempt.usage == {}
        assert await store.get_compaction_usage(SESSION) == {}

        finished = await store.finish_compaction_attempt(
            attempt.attempt_id,
            status="failed",
            usage={"prompt_tokens": 3},
            reason="invalid_summary",
        )
        assert finished.status == "failed"
        assert finished.usage == {"prompt_tokens": 3}
        # No fake completion/total zero was added for an unknown value.
        assert await store.get_compaction_usage(SESSION) == {"prompt_tokens": 3}

        with pytest.raises(ValueError, match="already finished"):
            await store.finish_compaction_attempt(
                attempt.attempt_id, status="completed", usage={}
            )

        attempts = await store.list_compaction_attempts(SESSION)
        assert [item.status for item in attempts] == ["failed"]
        assert attempts[0].reason == "invalid_summary"
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_summary_that_does_not_shrink_the_history_is_rejected(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 2)
        provider = FakeProvider("y" * 4000)

        with pytest.raises(CompactionError, match="would not shrink"):
            await compact_session(store, provider, SESSION, model="fake-model")

        assert await store.read_checkpoint(SESSION) is None
        # The provider call really happened, so its known usage is kept even
        # though the projection was not replaced.
        assert await store.get_compaction_usage(SESSION) == PROVIDER_USAGE
        attempts = await store.list_compaction_attempts(SESSION)
        assert [item.status for item in attempts] == ["failed"]
        assert "would not shrink" in attempts[0].reason
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_real_token_estimate_must_shrink_even_when_characters_do(
    tmp_path: Path,
) -> None:
    """A shorter character count is not evidence that the next prompt shrank."""
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 4)
        before = list(await store.read_conversation(SESSION))
        provider = FakeProvider("short summary")

        def token_estimator(messages) -> int:
            # Exactly the reported case: the summary adds the checkpoint
            # notice, so the real token estimate grows while char/4 falls.
            has_notice = any(
                CHECKPOINT_SUMMARY_NOTICE in str(message.get("content") or "")
                for message in messages
            )
            return 2000 if has_notice else 1000

        with pytest.raises(CompactionError, match="would not shrink"):
            await compact_session(
                store,
                provider,
                SESSION,
                model="fake-model",
                token_estimator=token_estimator,
            )

        assert await store.read_checkpoint(SESSION) is None
        assert await store.get_compaction_usage(SESSION) == PROVIDER_USAGE
        attempts = await store.list_compaction_attempts(SESSION)
        assert [item.status for item in attempts] == ["failed"]
        assert "would not shrink" in attempts[0].reason
        # Nothing in the projection moved.
        assert list(await store.read_conversation(SESSION)) == before
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_before_and_after_use_the_supplied_token_estimator(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 4)
        provider = FakeProvider("short summary")

        def token_estimator(messages) -> int:
            has_notice = any(
                CHECKPOINT_SUMMARY_NOTICE in str(message.get("content") or "")
                for message in messages
            )
            return 500 if has_notice else 1000

        result = await compact_session(
            store,
            provider,
            SESSION,
            model="fake-model",
            token_estimator=token_estimator,
        )

        assert (result.before_tokens, result.after_tokens) == (1000, 500)
        projected = list(await store.read_conversation(SESSION))
        assert CHECKPOINT_SUMMARY_NOTICE in projected[0]["content"]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_truncated_summary_response_is_not_accepted(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 3)
        provider = FakeProvider("cut off", finish_reason="length")

        with pytest.raises(CompactionError, match="not complete"):
            await compact_session(store, provider, SESSION, model="fake-model")

        assert await store.read_checkpoint(SESSION) is None
        assert await store.get_compaction_usage(SESSION) == PROVIDER_USAGE
        attempts = await store.list_compaction_attempts(SESSION)
        assert [item.status for item in attempts] == ["failed"]
        assert "not complete" in attempts[0].reason
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_usage_accumulates_per_session_without_reset(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 4)
        await _seed_turns(store, 3, session=OTHER)
        other_before = list(await store.read_conversation(OTHER))

        provider = FakeProvider(["first summary", "second summary"])
        await compact_session(store, provider, SESSION, model="fake-model")
        projected = tuple(await store.read_conversation(SESSION))
        await _append_turn(store, "turn-5", list(projected), "follow-up task", "follow-up answer")
        await compact_session(store, provider, SESSION, model="fake-model")

        assert [call["kwargs"]["model"] for call in provider.calls] == [
            "fake-model",
            "fake-model",
        ]
        assert await store.get_compaction_usage(SESSION) == {
            "prompt_tokens": 22,
            "completion_tokens": 10,
            "total_tokens": 32,
        }
        assert await store.get_compaction_usage(OTHER) == {}
        assert list(await store.read_conversation(OTHER)) == other_before

        await compact_session(
            store,
            FakeProvider("only other", usage={"prompt_tokens": 1, "total_tokens": 1}),
            OTHER,
            model="fake-model",
        )
        assert await store.get_compaction_usage(OTHER) == {
            "prompt_tokens": 1,
            "total_tokens": 1,
        }
        assert await store.get_compaction_usage(SESSION) == {
            "prompt_tokens": 22,
            "completion_tokens": 10,
            "total_tokens": 32,
        }
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_failed_turn_after_checkpoint_stays_out_but_is_traceable(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 3)
        await compact_session(store, FakeProvider("small"), SESSION, model="fake-model")
        projected = [dict(message) for message in await store.read_conversation(SESSION)]

        await store.append(_event(EventType.TURN_STARTED, "turn-failed"))
        await store.append(
            _event(
                EventType.REQUEST_PREPARED,
                "turn-failed",
                step_id="step-1",
                payload={
                    "model_call_id": "mf",
                    "messages": [
                        {"role": "system", "content": "sys"},
                        *projected,
                        {"role": "user", "content": "broken task"},
                    ],
                    "tools": [],
                },
            )
        )
        await store.append(
            _event(
                EventType.TURN_FAILED,
                "turn-failed",
                payload={"error": "boom"},
            )
        )

        assert list(await store.read_conversation(SESSION)) == projected
        turn_events = await store.read_turn(SESSION, "turn-failed")
        assert [event.type for event in turn_events] == [
            EventType.TURN_STARTED,
            EventType.REQUEST_PREPARED,
            EventType.TURN_FAILED,
        ]

        await _append_turn(store, "turn-next", projected, "next task", "next answer")
        assert list(await store.read_conversation(SESSION)) == [
            *projected,
            {"role": "user", "content": "next task"},
            {"role": "assistant", "content": "next answer"},
        ]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_compaction_requires_a_complete_conversation(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider("summary")
        with pytest.raises(CompactionError, match="no complete conversation"):
            await compact_session(store, provider, "empty-session", model="fake-model")

        await _seed_turns(store, 1)
        with pytest.raises(CompactionError, match="at least two complete user turns"):
            await compact_session(store, provider, SESSION, model="fake-model")
        assert provider.calls == []
        assert await store.read_checkpoint(SESSION) is None
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_a_second_checkpoint_at_the_same_head_is_rejected(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turns(store, 3)
        head = await store.read_head_sequence(SESSION)
        retained = tuple(await store.read_conversation(SESSION))
        first = await store.commit_checkpoint(
            ContextCheckpoint(
                session_id=SESSION,
                covers_sequence=head,
                summary="first summary",
                retained_messages=retained,
            ),
            expected_head_sequence=head,
        )
        with pytest.raises(CheckpointConflictError, match="does not move past"):
            await store.commit_checkpoint(
                ContextCheckpoint(
                    session_id=SESSION,
                    covers_sequence=head,
                    summary="second summary",
                    retained_messages=retained,
                ),
                expected_head_sequence=head,
            )
        latest = await store.read_checkpoint(SESSION)
        assert latest is not None
        assert latest.checkpoint_id == first.checkpoint_id
        assert latest.summary == "first summary"
    finally:
        await store.aclose()

"""Usage accounting and context estimates for the Core CLI.

These are pure event/ledger tests; no provider, Gateway or robot is contacted.
"""

from __future__ import annotations

import pytest

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.services.usage import (
    ContextSnapshot,
    UsageLedger,
    estimate_context,
    format_session_totals,
    format_turn_summary,
)


def _event(
    event_type: EventType,
    *,
    session: str = "cli:test",
    turn: str = "turn-1",
    step: str | None = "step-1",
    payload: dict | None = None,
) -> RuntimeEvent:
    turn_scope = {
        EventType.TURN_STARTED,
        EventType.TURN_COMPLETED,
        EventType.TURN_FAILED,
        EventType.TURN_INTERRUPTED,
        EventType.CONTEXT_COMPACTED,
    }
    session_scope = {EventType.INPUT_ACCEPTED, EventType.SESSION_SNAPSHOT_CREATED}
    return RuntimeEvent(
        type=event_type,
        session_id=session,
        turn_id=None if event_type in session_scope else turn,
        step_id=None if event_type in session_scope or event_type in turn_scope else step,
        payload=payload or {},
    )


def _started(call_id: str, *, turn="turn-1", step="step-1"):
    return _event(
        EventType.MODEL_ATTEMPT_STARTED,
        turn=turn,
        step=step,
        payload={"model_call_id": call_id},
    )


def _completed(call_id: str, usage: dict, *, turn="turn-1", step="step-1"):
    return _event(
        EventType.MODEL_ATTEMPT_COMPLETED,
        turn=turn,
        step=step,
        payload={"model_call_id": call_id, "usage": usage, "content": "answer"},
    )


def test_same_model_call_id_in_another_turn_or_step_is_counted_twice():
    ledger = UsageLedger()
    ledger.ingest(_started("m1", turn="turn-1", step="step-1"))
    ledger.ingest(
        _completed(
            "m1",
            {"prompt_tokens": 10, "completion_tokens": 2},
            turn="turn-1",
            step="step-1",
        )
    )
    # The Core contract allows the same model_call_id in another scope.
    ledger.ingest(_started("m1", turn="turn-1", step="step-2"))
    ledger.ingest(
        _completed(
            "m1",
            {"prompt_tokens": 20, "completion_tokens": 3},
            turn="turn-1",
            step="step-2",
        )
    )
    total = ledger.session("cli:test")
    assert total.model_calls == 2
    assert (total.input_tokens, total.output_tokens, total.total_tokens) == (30, 5, 35)
    assert total.complete


def test_duplicate_terminal_event_for_one_attempt_is_not_added_twice():
    ledger = UsageLedger()
    ledger.ingest(_started("m1"))
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 40},
    }
    ledger.ingest(_completed("m1", usage))
    ledger.ingest(_completed("m1", usage))
    total = ledger.session("cli:test")
    assert total.model_calls == 1
    assert total.input_tokens == 100
    assert total.output_tokens == 10
    assert total.total_tokens == 110
    assert total.cache_hit_percent == pytest.approx(40.0)


def test_mixed_missing_fields_keep_known_sums_and_mark_incomplete():
    ledger = UsageLedger()
    # call 1 knows only total; call 2 knows prompt and completion.
    ledger.ingest(_started("m1", step="step-1"))
    ledger.ingest(_completed("m1", {"total_tokens": 110}, step="step-1"))
    ledger.ingest(_started("m2", step="step-2"))
    ledger.ingest(
        _completed(
            "m2",
            {"prompt_tokens": 20, "completion_tokens": 5},
            step="step-2",
        )
    )
    total = ledger.session("cli:test")
    assert total.model_calls == 2
    assert total.input_tokens == 20
    assert total.output_tokens == 5
    # Each call is normalized first: 110 and (20 + 5), then summed.
    assert total.total_tokens == 135
    assert total.complete is False


def test_failed_call_without_usage_marks_history_incomplete_without_fake_zero():
    ledger = UsageLedger()
    ledger.ingest(_started("m1"))
    ledger.ingest(
        _event(
            EventType.MODEL_ATTEMPT_FAILED,
            payload={"model_call_id": "m1", "error": "connection reset"},
        )
    )
    total = ledger.session("cli:test")
    assert total.model_calls == 1
    assert total.input_tokens is None
    assert total.output_tokens is None
    assert total.total_tokens is None
    assert total.complete is False
    assert total.missing_usage_calls == 1


def test_cancelled_stream_uses_terminal_known_usage_and_never_invents_the_rest():
    ledger = UsageLedger()
    ledger.ingest(_started("m1"))
    ledger.ingest(
        _event(
            EventType.MODEL_ATTEMPT_INTERRUPTED,
            payload={
                "model_call_id": "m1",
                "usage": {"prompt_tokens": 50, "completion_tokens": 4},
            },
        )
    )
    total = ledger.session("cli:test")
    assert (total.input_tokens, total.output_tokens) == (50, 4)
    assert total.total_tokens == 54
    # Both required counts were reported, so this attempt is complete even
    # though the turn was interrupted. The rest of the turn is not invented.
    assert total.complete is True


def test_cache_hit_is_unknown_when_any_call_lacks_cache_fields():
    unknown = UsageLedger()
    unknown.ingest(_started("m1"))
    unknown.ingest(_completed("m1", {"prompt_tokens": 10, "completion_tokens": 1}))
    assert unknown.session("cli:test").cache_hit_percent is None

    explicit_zero = UsageLedger()
    explicit_zero.ingest(_started("m2"))
    explicit_zero.ingest(
        _completed(
            "m2",
            {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        )
    )
    assert explicit_zero.session("cli:test").cache_hit_percent == 0.0


def test_cumulative_usage_reports_are_merged_not_added():
    ledger = UsageLedger()
    ledger.ingest(_started("m1"))
    ledger.ingest(_completed("m1", {"prompt_tokens": 100, "completion_tokens": 10}))
    ledger.ingest(_completed("m1", {"prompt_tokens": 120, "completion_tokens": 15}))
    total = ledger.session("cli:test")
    assert total.model_calls == 1
    assert (total.input_tokens, total.output_tokens) == (120, 15)


def test_compaction_attempts_are_imported_by_stable_id_and_not_double_counted():
    ledger = UsageLedger()
    ledger.record_compaction_attempt(
        "cli:test", 7, "completed", {"prompt_tokens": 30, "completion_tokens": 6}
    )
    ledger.record_compaction_attempt(
        "cli:test", 8, "failed", {"prompt_tokens": 10, "completion_tokens": 2}
    )
    # Re-reading the same attempt rows is idempotent.
    ledger.record_compaction_attempt(
        "cli:test", 7, "completed", {"prompt_tokens": 30, "completion_tokens": 6}
    )
    total = ledger.session("cli:test")
    assert total.model_calls == 2
    assert (total.input_tokens, total.output_tokens, total.total_tokens) == (40, 8, 48)
    assert total.complete is True


def test_compaction_started_attempt_is_counted_but_incomplete():
    ledger = UsageLedger()
    ledger.record_compaction_attempt("cli:test", 9, "started", {})
    total = ledger.session("cli:test")
    assert total.model_calls == 1
    assert total.complete is False
    assert total.input_tokens is None


def test_turn_summary_marks_incomplete_and_missing_cache():
    ledger = UsageLedger()
    ledger.ingest(_started("m1"))
    ledger.ingest(
        _completed(
            "m1",
            {"prompt_tokens": 10, "completion_tokens": 2},
        )
    )
    turn = ledger.turn("cli:test", "turn-1")
    text = format_turn_summary(turn, 12.4)
    assert text.startswith("本轮12.4s")
    assert "模型轮次1" in text
    assert "缓存命中--%" in text
    # prompt+completion are known, so only the total-less cache is missing.
    assert "不完整" not in text

    session = format_session_totals(ledger.session("cli:test"))
    assert "会话累计 输入10 · 输出2 · 合计12" in session


def test_context_is_exact_only_while_the_measured_request_is_the_last_event():
    request = _event(
        EventType.REQUEST_PREPARED,
        payload={
            "model_call_id": "m1",
            "messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "hi"}],
            "tools": [],
        },
    )
    completed = _event(
        EventType.MODEL_ATTEMPT_COMPLETED,
        payload={"model_call_id": "m1", "content": "hello", "usage": {"prompt_tokens": 500}},
    )
    assert estimate_context([request, completed], capacity=1000).exact is True
    assert estimate_context([request, completed], capacity=1000).label() == "context 50% exact"

    terminal = _event(EventType.TURN_COMPLETED, payload={"reply": "hello"})
    snapshot = estimate_context([request, completed, terminal], capacity=1000)
    assert snapshot.exact is False
    assert snapshot.label().startswith("context ≈")


def test_context_over_100_percent_is_not_clamped():
    request = _event(
        EventType.REQUEST_PREPARED,
        payload={
            "model_call_id": "m1",
            "messages": [{"role": "user", "content": "x" * 4000}],
            "tools": [],
        },
    )
    snapshot = estimate_context([request], capacity=100)
    assert snapshot.percent is not None and snapshot.percent > 100
    assert "%" in snapshot.label() and "100" not in snapshot.label().split("%")[0][-3:]


def test_context_capacity_unknown_prints_double_dash():
    assert ContextSnapshot(tokens=None, capacity=None).label() == "context --%"
    assert ContextSnapshot(tokens=10, capacity=None).label() == "context --%"


async def test_reload_from_the_store_keeps_cumulative_usage(tmp_path):
    from PhyAgentOS.agent.core.session import SQLiteSessionStore

    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        for event in (
            _started("m1"),
            _completed(
                "m1",
                {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            ),
        ):
            await store.append(event)
        first = UsageLedger()
        first.seed(await store.read_session("cli:test"))
        second = UsageLedger()
        second.seed(await store.read_session("cli:test"))
        assert second.session("cli:test") == first.session("cli:test")
        assert second.session("cli:test").input_tokens == 100
    finally:
        await store.aclose()

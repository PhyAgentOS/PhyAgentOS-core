"""Tests for the optional per-turn budget tracker.

The configuration is shared; the mutable counters belong to one turn. Missing
usage is never turned into zero, and only an identical no-progress tool result
reaches the repetition limit.
"""

from __future__ import annotations

import pytest

from PhyAgentOS.agent.services.turn_budget import TurnBudget, TurnBudgetTracker


def test_config_validation_and_fresh_state_per_turn() -> None:
    with pytest.raises(ValueError):
        TurnBudget(max_tokens=0)
    with pytest.raises(ValueError):
        TurnBudget(repeated_call_limit=0)
    with pytest.raises(ValueError):
        TurnBudget(reserve_steps=-1)
    with pytest.raises(TypeError):
        TurnBudget(max_tokens="5")  # type: ignore[arg-type]

    config = TurnBudget(max_tokens=10)
    first = config.begin_turn()
    second = config.begin_turn()
    assert isinstance(first, TurnBudgetTracker)
    assert first is not second
    assert first.spent_tokens == second.spent_tokens == 0
    assert first.config is config


def test_usage_prefers_total_then_prompt_plus_completion() -> None:
    tracker = TurnBudget().begin_turn()

    tracker.record_usage({"total_tokens": 7, "prompt_tokens": 5, "completion_tokens": 2})
    tracker.record_usage({"prompt_tokens": 4, "completion_tokens": 6})
    tracker.record_usage({"input_tokens": 1, "output_tokens": 2})

    assert tracker.spent_tokens == 20
    assert tracker.unknown_usage_calls == 0


def test_missing_usage_is_not_counted_as_zero() -> None:
    tracker = TurnBudget(max_tokens=1).begin_turn()

    tracker.record_usage({})
    tracker.record_usage({"prompt_tokens": 3})
    tracker.record_usage(None)

    assert tracker.spent_tokens == 0
    assert tracker.unknown_usage_calls == 3
    # Nothing proves the budget was reached, so the driver must not stop.
    assert not tracker.token_budget_exhausted()

    tracker.record_usage({"total_tokens": 0})
    assert tracker.spent_tokens == 0
    assert tracker.unknown_usage_calls == 3
    assert not tracker.token_budget_exhausted()


def test_token_budget_exhaustion_requires_a_configured_limit() -> None:
    unlimited = TurnBudget().begin_turn()
    unlimited.record_usage({"total_tokens": 10_000})
    assert not unlimited.token_budget_exhausted()

    tracker = TurnBudget(max_tokens=10).begin_turn()
    tracker.record_usage({"total_tokens": 6})
    assert not tracker.token_budget_exhausted()
    tracker.record_usage({"total_tokens": 4})
    assert tracker.token_budget_exhausted()


def test_child_turn_counters_can_be_merged() -> None:
    parent = TurnBudget().begin_turn()
    child = TurnBudget().begin_turn()
    parent.record_usage({"total_tokens": 3})
    child.record_usage({"total_tokens": 4})
    child.record_usage({})

    parent.merge_from(child)

    assert parent.spent_tokens == 7
    assert parent.unknown_usage_calls == 1
    with pytest.raises(TypeError):
        parent.merge_from(object())  # type: ignore[arg-type]


def test_summary_step_is_the_single_reserved_final_step() -> None:
    one = TurnBudget(reserve_steps=1).begin_turn()
    assert [one.is_summary_step(step, 4) for step in range(1, 5)] == [
        False,
        False,
        False,
        True,
    ]

    two = TurnBudget(reserve_steps=2).begin_turn()
    assert [two.is_summary_step(step, 4) for step in range(1, 5)] == [
        False,
        False,
        True,
        False,
    ]

    disabled = TurnBudget(reserve_steps=0).begin_turn()
    assert not any(disabled.is_summary_step(step, 3) for step in range(1, 4))


def test_one_step_turn_still_gets_its_normal_model_request() -> None:
    tracker = TurnBudget(reserve_steps=1).begin_turn()

    assert not tracker.is_summary_step(1, 1)
    # The second step is a real summary step once the first one ran.
    assert tracker.is_summary_step(2, 2)


def test_identical_no_progress_results_reach_the_limit() -> None:
    tracker = TurnBudget(repeated_call_limit=3).begin_turn()

    assert not tracker.observe_tool_outcome("echo", {"text": "a"}, "failed", "boom")
    assert not tracker.observe_tool_outcome("echo", {"text": "a"}, "failed", "boom")
    assert tracker.observe_tool_outcome("echo", {"text": "a"}, "failed", "boom")
    # A different result is progress and is not conflated with the old one.
    assert not tracker.observe_tool_outcome("echo", {"text": "a"}, "failed", "other")


def test_polling_with_changing_results_is_not_blocked() -> None:
    tracker = TurnBudget(repeated_call_limit=2).begin_turn()

    for index in range(10):
        assert not tracker.observe_tool_outcome(
            "read_status", {"id": 1}, "succeeded", f"state-{index}"
        )


def test_different_arguments_or_tool_names_reset_the_identical_run() -> None:
    tracker = TurnBudget(repeated_call_limit=2).begin_turn()

    assert not tracker.observe_tool_outcome("echo", {"text": "a"}, "failed", "x")
    assert not tracker.observe_tool_outcome("echo", {"text": "b"}, "failed", "x")
    assert not tracker.observe_tool_outcome("other", {"text": "a"}, "failed", "x")
    # A/B/A must not add to the first A run; only consecutive repeats count.
    assert not tracker.observe_tool_outcome("echo", {"text": "a"}, "failed", "x")
    assert tracker.observe_tool_outcome("echo", {"text": "a"}, "failed", "x")


def test_explicit_poll_tools_never_trigger_on_unchanged_text() -> None:
    for name in ("job_status", "action_status", "read_status", "task_wait", "wait"):
        tracker = TurnBudget(repeated_call_limit=2).begin_turn()
        for _ in range(10):
            assert not tracker.observe_tool_outcome(
                name, {"id": 1}, "succeeded", "still running"
            )


def test_a_poll_between_reads_resets_the_read_run() -> None:
    tracker = TurnBudget(repeated_call_limit=2).begin_turn()
    read = ("read_file", {"path": "x"}, "succeeded", "same bytes")

    assert not tracker.observe_tool_outcome(*read)
    assert not tracker.observe_tool_outcome(
        "job_status", {"id": 1}, "succeeded", "running"
    )
    assert not tracker.observe_tool_outcome(*read)


def test_repetition_check_can_be_disabled() -> None:
    tracker = TurnBudget(repeated_call_limit=None).begin_turn()

    for _ in range(10):
        assert not tracker.observe_tool_outcome("echo", {}, "failed", "boom")

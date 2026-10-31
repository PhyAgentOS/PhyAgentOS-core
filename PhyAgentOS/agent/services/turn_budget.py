"""Optional per-turn usage and no-progress budget.

The configuration object (:class:`TurnBudget`) is immutable and may be shared
by a composition root and a child driver across many turns and sessions. Every
mutable counter lives in a :class:`TurnBudgetTracker` created by
:meth:`TurnBudget.begin_turn`, so two turns can never accumulate into each
other. The loop driver creates one tracker per ``run`` when a budget is
injected and behaves exactly as before when none is.

Token accounting uses only numbers the provider reported. A missing usage
field is not turned into ``0``: the call is counted as usage-unknown and a
known partial total is never presented as if it were complete. Nothing here
claims actual billing cost.

No-progress detection is narrow on purpose: only *consecutive* identical
(tool name, arguments, terminal status, full result text) results reach the
limit. A different call in between resets the run, so A/B/A never accumulates.
A poll whose result changes is never blocked, and tools whose names mark an
explicit status/wait poll (``*_status``, ``*_wait``, ``wait``) are exempt even
when their text is unchanged. Only the last signature and its count are kept.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["TurnBudget", "TurnBudgetRegistry", "TurnBudgetTracker"]


def _usage_total(usage: Mapping[str, Any]) -> int | None:
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if (
        isinstance(prompt, int)
        and not isinstance(prompt, bool)
        and prompt >= 0
        and isinstance(completion, int)
        and not isinstance(completion, bool)
        and completion >= 0
    ):
        return prompt + completion
    return None


def _status_text(status: object) -> str:
    value = getattr(status, "value", status)
    return value if isinstance(value, str) else str(value)


def _arguments_key(arguments: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(arguments), sort_keys=True, ensure_ascii=False, default=str
    )


def _is_poll_tool(tool_name: str) -> bool:
    """Names whose unchanged text is a legitimate wait, not a stuck loop."""
    lowered = tool_name.lower()
    return (
        lowered == "wait"
        or lowered.endswith("_wait")
        or lowered.endswith("_status")
    )


class TurnBudgetRegistry:
    """Bounded attribution of child usage to the exact originating parent turn.

    Missing or evicted parents fail closed for further child requests. A cap
    stops subsequent calls after observed usage reaches it; already in-flight
    provider calls cannot be retroactively cancelled or priced in advance.
    """

    def __init__(self, max_turns: int = 4096) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self._max_turns = max_turns
        self._trackers: OrderedDict[tuple[str, str], TurnBudgetTracker] = OrderedDict()

    def register(self, session_id: str, turn_id: str, tracker: TurnBudgetTracker) -> None:
        key = (session_id, turn_id)
        self._trackers[key] = tracker
        self._trackers.move_to_end(key)
        while len(self._trackers) > self._max_turns:
            self._trackers.popitem(last=False)

    def observe(self, session_id: str, turn_id: str, usage: Mapping[str, Any]) -> None:
        tracker = self._trackers.get((session_id, turn_id))
        if tracker is not None:
            tracker.record_usage(usage)

    def permits(self, session_id: str, turn_id: str) -> bool:
        key = (session_id, turn_id)
        tracker = self._trackers.get(key)
        if tracker is None:
            return False
        self._trackers.move_to_end(key)
        return not tracker.token_budget_exhausted()


@dataclass(frozen=True, slots=True)
class TurnBudget:
    """Immutable budget configuration shared across turns.

    ``max_tokens`` is an optional total-token ceiling for one turn.
    ``repeated_call_limit`` stops a turn after this many identical no-progress
    tool results (``None`` disables the check). ``reserve_steps`` reserves the
    final step(s) for a summary request with no tools; ``0`` disables the
    reserved summary.
    """

    max_tokens: int | None = None
    repeated_call_limit: int | None = 3
    reserve_steps: int = 1
    registry: TurnBudgetRegistry | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_tokens is not None:
            if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool):
                raise TypeError("max_tokens must be an int or None")
            if self.max_tokens < 1:
                raise ValueError("max_tokens must be >= 1")
        if self.repeated_call_limit is not None:
            if not isinstance(self.repeated_call_limit, int) or isinstance(
                self.repeated_call_limit, bool
            ):
                raise TypeError("repeated_call_limit must be an int or None")
            if self.repeated_call_limit < 1:
                raise ValueError("repeated_call_limit must be >= 1")
        if not isinstance(self.reserve_steps, int) or isinstance(self.reserve_steps, bool):
            raise TypeError("reserve_steps must be an int")
        if self.reserve_steps < 0:
            raise ValueError("reserve_steps must be >= 0")

    def begin_turn(self) -> "TurnBudgetTracker":
        """Return fresh mutable state for exactly one turn."""
        return TurnBudgetTracker(self)


@dataclass(slots=True)
class TurnBudgetTracker:
    """Mutable counters for one turn, created by :meth:`TurnBudget.begin_turn`."""

    config: TurnBudget
    spent_tokens: int = 0
    unknown_usage_calls: int = 0
    _last_signature: tuple[str, str, str, str] | None = field(
        default=None, repr=False
    )
    _consecutive: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.config, TurnBudget):
            raise TypeError("config must be a TurnBudget")

    def record_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Add one provider usage snapshot; a missing total is not zero."""
        total = _usage_total(usage or {})
        if total is None:
            self.unknown_usage_calls += 1
            return
        self.spent_tokens += total

    def merge_from(self, child: "TurnBudgetTracker") -> None:
        """Absorb a completed child turn's counters (main-agent wiring hook)."""
        if not isinstance(child, TurnBudgetTracker):
            raise TypeError("child must be a TurnBudgetTracker")
        self.spent_tokens += child.spent_tokens
        self.unknown_usage_calls += child.unknown_usage_calls

    def token_budget_exhausted(self) -> bool:
        """True only when a budget exists and the reported total reached it."""
        limit = self.config.max_tokens
        return limit is not None and self.spent_tokens >= limit

    def is_summary_step(self, step_number: int, max_steps: int) -> bool:
        """True on the final reserved step, but never on the first step.

        An entry with ``max_steps=1`` must still get its one normal model
        request; a summary can only replace a step after at least one earlier
        model step (and its tools, if any) has run.
        """
        reserve = self.config.reserve_steps
        if reserve <= 0 or step_number <= 1:
            return False
        return step_number == max_steps - reserve + 1

    def observe_tool_outcome(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        status: str,
        text: str,
    ) -> bool:
        """Count a consecutive identical no-progress result; True at the limit.

        Only the last signature and its run length are stored. A different
        call, even one that later repeats, starts a new run instead of adding
        to an old count. Explicit poll tools never reach the stop condition;
        their unchanged text is expected while waiting.
        """
        limit = self.config.repeated_call_limit
        if limit is None:
            return False
        key = (
            tool_name,
            _arguments_key(arguments),
            _status_text(status),
            text,
        )
        if key == self._last_signature:
            self._consecutive += 1
        else:
            self._last_signature = key
            self._consecutive = 1
        if _is_poll_tool(tool_name):
            return False
        return self._consecutive >= limit

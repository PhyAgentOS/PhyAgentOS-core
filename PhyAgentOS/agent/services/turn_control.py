"""A bounded, turn-scoped correction queue for step boundaries.

A correction is not ordinary input: it must never open another turn, never be
inserted into a model answer that is already streaming, and never change a
request whose model call has already started. The loop driver consumes it at
the next safe step boundary with :meth:`TurnControl.drain` and writes one
``input.steered`` event for what it actually used.

The queue is deliberately small and in-memory. It is keyed by the exact
``(session_id, turn_id)`` pair, so a correction for an ended turn cannot leak
into the next turn or another session. ``enqueue`` only accepts a turn the
runtime has opened; ``end_turn`` returns anything left unconsumed and clears
it, so the runtime can record the text as unapplied instead of pretending it
was used. This is a local instruction queued for the next step, not an
acknowledgement that a robot action or remote request changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PhyAgentOS.agent.core.types import require_non_empty

__all__ = [
    "TurnControl",
    "TurnControlClosedError",
    "TurnControlError",
    "TurnControlInactiveError",
    "TurnControlLimitError",
]


class TurnControlError(RuntimeError):
    """A correction cannot be queued for the requested turn."""


class TurnControlClosedError(TurnControlError):
    """The control was closed and no longer accepts corrections."""


class TurnControlInactiveError(TurnControlError):
    """The session has no open turn with that turn id."""


class TurnControlLimitError(TurnControlError, ValueError):
    """The bounded queue or text limit was exceeded."""


@dataclass(slots=True)
class _TurnQueue:
    turn_id: str
    texts: list[str] = field(default_factory=list)
    characters: int = 0


class TurnControl:
    """Queue bounded corrections for one open turn and drain them in order.

    Defaults keep one turn's pending corrections small: at most 16 items and
    4000 characters in total, with a 2000-character limit for a single item.
    ``begin_turn`` opens the only turn a session may queue into; the runtime
    calls it when it publishes an active turn. ``close`` is terminal.
    """

    def __init__(
        self,
        *,
        max_pending: int = 16,
        max_text_chars: int = 2000,
        max_total_chars: int = 4000,
    ) -> None:
        for name, value in (
            ("max_pending", max_pending),
            ("max_text_chars", max_text_chars),
            ("max_total_chars", max_total_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an int")
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
        self._max_pending = max_pending
        self._max_text_chars = max_text_chars
        self._max_total_chars = max_total_chars
        self._turns: dict[str, _TurnQueue] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def begin_turn(self, session_id: str, turn_id: str) -> None:
        """Open the turn a session may queue corrections into.

        A new begin replaces any stale queue for that session. The runtime
        serializes turns per session, so a live queue for another turn is
        already obsolete at this point.
        """
        if self._closed:
            raise TurnControlClosedError("turn control is closed")
        require_non_empty("session_id", session_id)
        require_non_empty("turn_id", turn_id)
        self._turns[session_id] = _TurnQueue(turn_id=turn_id)

    def end_turn(self, session_id: str, turn_id: str) -> tuple[str, ...]:
        """Close one turn and return corrections it never consumed.

        Nothing is reported as applied: an undrained correction simply ends
        with the turn and is returned to the caller for an explicit warning.
        A mismatched turn id leaves the newer queue untouched and returns an
        empty tuple, so a correction never moves to the next turn.
        """
        queue = self._turns.get(session_id)
        if queue is None or queue.turn_id != turn_id:
            return ()
        del self._turns[session_id]
        return tuple(queue.texts)

    def enqueue(self, session_id: str, turn_id: str, text: str) -> None:
        """Queue one correction for exactly this open turn.

        Empty or whitespace-only text is refused. The queue is bounded by item
        count, per-item characters, and total characters; exceeding any limit
        raises instead of growing without limit. Queueing is not applying:
        the driver consumes the text only at a later step boundary.
        """
        if self._closed:
            raise TurnControlClosedError("turn control is closed")
        require_non_empty("session_id", session_id)
        require_non_empty("turn_id", turn_id)
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text.strip():
            raise ValueError("correction text must not be empty")
        queue = self._turns.get(session_id)
        if queue is None or queue.turn_id != turn_id:
            raise TurnControlInactiveError(
                f"session {session_id!r} has no active turn {turn_id!r}"
            )
        if len(text) > self._max_text_chars:
            raise TurnControlLimitError(
                f"correction is {len(text)} characters; the per-item limit is "
                f"{self._max_text_chars}"
            )
        if len(queue.texts) >= self._max_pending:
            raise TurnControlLimitError(
                f"turn {turn_id!r} already has {len(queue.texts)} pending "
                f"corrections; the limit is {self._max_pending}"
            )
        if queue.characters + len(text) > self._max_total_chars:
            raise TurnControlLimitError(
                f"turn {turn_id!r} already has {queue.characters} pending "
                f"characters; the limit is {self._max_total_chars}"
            )
        queue.texts.append(text)
        queue.characters += len(text)

    def drain(self, session_id: str, turn_id: str) -> tuple[str, ...]:
        """Return and clear this turn's corrections in queue order.

        A session/turn mismatch, an ended turn, or a closed control returns an
        empty tuple. Nothing from another turn or session is ever returned.
        """
        if self._closed:
            return ()
        if not isinstance(session_id, str) or not session_id.strip():
            return ()
        if not isinstance(turn_id, str) or not turn_id.strip():
            return ()
        queue = self._turns.get(session_id)
        if queue is None or queue.turn_id != turn_id:
            return ()
        texts = tuple(queue.texts)
        queue.texts.clear()
        queue.characters = 0
        return texts

    def unapplied(self, session_id: str, turn_id: str) -> tuple[str, ...]:
        """Return this turn's queued text without consuming it.

        Use :meth:`end_turn` to close the turn and clear the queue.
        """
        queue = self._turns.get(session_id)
        if queue is None or queue.turn_id != turn_id:
            return ()
        return tuple(queue.texts)

    def active_turn_id(self, session_id: str) -> str | None:
        """Return the session's open turn id, or ``None`` when there is none."""
        queue = self._turns.get(session_id)
        return None if queue is None else queue.turn_id

    def pending(self, session_id: str, turn_id: str) -> int:
        """Return the number of queued corrections for exactly this turn."""
        queue = self._turns.get(session_id)
        if queue is None or queue.turn_id != turn_id:
            return 0
        return len(queue.texts)

    def close(self) -> None:
        """Reject further queueing and clear every pending correction."""
        self._closed = True
        self._turns.clear()

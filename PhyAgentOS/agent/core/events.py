"""Runtime event contract for the agent core.

Events are the durable record of a turn, so their identity rules are strict:
a session-level event may exist before a turn starts, but every turn- or
step-level event must carry the identity of the thing it describes. That is
what stops an unattributable event from entering the log. Whether a turn is
complete is a reading rule: it requires ``turn.started`` plus one terminal
``turn.*`` event with the same ``turn_id``; no single event may be read as a
complete turn on its own.

Ordering has two explicit levels: ``sequence`` is assigned by the session
store on append and is the only ordering authority inside a session;
``recorded_at`` is a wall-clock timestamp used for display and debugging. An
event that has not been stored yet has ``sequence=None``.

Each event type has exactly one writer, so a transition is never recorded
twice:

* ``AgentRuntime``: ``input.accepted``, ``turn.started``, terminal ``turn.*``
  events, and ``session.snapshot.created``;
* ``LoopDriver``: ``request.prepared``, ``model.attempt.*``, and
  ``context.compacted`` (it owns the message history being compacted);
* ``ToolExecutor``: ``tool.intent``, ``tool.dispatched``, ``tool.settled``.

One event must not claim more than was observed. In particular:

* an unfinished model attempt is reopened as ``model.attempt.interrupted``;
* an unfinished tool call that was dispatched is settled as ``tool.settled``
  with status ``unknown``, never ``succeeded``;
* an unfinished tool call that only reached ``intent`` is settled as
  ``failed`` with error code ``not_dispatched``: no handler was called, so
  its side effect cannot be unknown;
* recovery never implies that a call should be resent.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from .types import ToolStatus, require_non_empty

__all__ = [
    "CallKind",
    "EventType",
    "RecoveryReport",
    "RuntimeEvent",
    "UnfinishedCall",
]


class EventType(str, Enum):
    """Event kinds and the lifecycle they describe.

    ``input.accepted`` arrives before a turn exists. Turn, request, model and
    tool events belong to a turn; request/model/tool events also belong to one
    step. ``model.attempt.interrupted`` is the recovery-only terminal state
    for an attempt that was started but never finished.
    """

    INPUT_ACCEPTED = "input.accepted"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_INTERRUPTED = "turn.interrupted"
    REQUEST_PREPARED = "request.prepared"
    MODEL_ATTEMPT_STARTED = "model.attempt.started"
    MODEL_ATTEMPT_COMPLETED = "model.attempt.completed"
    MODEL_ATTEMPT_FAILED = "model.attempt.failed"
    MODEL_ATTEMPT_INTERRUPTED = "model.attempt.interrupted"
    TOOL_INTENT = "tool.intent"
    TOOL_DISPATCHED = "tool.dispatched"
    TOOL_SETTLED = "tool.settled"
    CONTEXT_COMPACTED = "context.compacted"
    SESSION_SNAPSHOT_CREATED = "session.snapshot.created"


_SESSION_SCOPE = frozenset({EventType.INPUT_ACCEPTED, EventType.SESSION_SNAPSHOT_CREATED})
_TURN_SCOPE = frozenset(
    {
        EventType.TURN_STARTED,
        EventType.TURN_COMPLETED,
        EventType.TURN_FAILED,
        EventType.TURN_INTERRUPTED,
        EventType.CONTEXT_COMPACTED,
    }
)
# Everything not listed above is step-scoped; listing it explicitly would only
# add a second place to forget when a new event type is introduced.
_STEP_SCOPE = frozenset(set(EventType) - _SESSION_SCOPE - _TURN_SCOPE)

_MODEL_CALL_EVENTS = frozenset(
    {
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.MODEL_ATTEMPT_FAILED,
        EventType.MODEL_ATTEMPT_INTERRUPTED,
    }
)
_TOOL_CALL_EVENTS = frozenset(
    {EventType.TOOL_INTENT, EventType.TOOL_DISPATCHED, EventType.TOOL_SETTLED}
)

# Successful terminal events must not smuggle an error field, otherwise a log
# reader cannot tell a failure recorded under the wrong type from a success.
_SUCCESS_EVENTS = frozenset({EventType.TURN_COMPLETED, EventType.MODEL_ATTEMPT_COMPLETED})


def _payload_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One append-only event.

    ``payload`` is deep-copied and made read-only on construction: nested
    containers no longer alias the caller's objects. Its schema is
    owned by the event type: model events carry ``model_call_id``, tool events
    carry ``tool_call_id``, and ``tool.settled`` carries a terminal
    :class:`ToolStatus`. Domain state is never copied here; only references
    (for example ``task_id``) are.

    Storing an event assigns ``sequence``; the store must assign it under the
    same session lock as the durable write so the sequence reflects commit
    order, not construction order.
    """

    type: EventType
    session_id: str
    turn_id: str | None = None
    step_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    sequence: int | None = None
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        event_type = self.type if isinstance(self.type, EventType) else EventType(self.type)
        object.__setattr__(self, "type", event_type)
        require_non_empty("session_id", self.session_id)

        if event_type in _SESSION_SCOPE:
            if self.step_id is not None:
                raise ValueError(f"{event_type.value} is session-scoped and must not carry step_id")
        elif event_type in _TURN_SCOPE:
            if self.turn_id is None:
                raise ValueError(f"{event_type.value} requires turn_id")
            if self.step_id is not None:
                raise ValueError(f"{event_type.value} is turn-scoped and must not carry step_id")
        elif event_type in _STEP_SCOPE:
            if self.turn_id is None:
                raise ValueError(f"{event_type.value} requires turn_id")
            if self.step_id is None:
                raise ValueError(f"{event_type.value} requires step_id")

        if self.turn_id is not None:
            require_non_empty("turn_id", self.turn_id)
        if self.step_id is not None:
            require_non_empty("step_id", self.step_id)

        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        payload = MappingProxyType(deepcopy(dict(self.payload)))
        object.__setattr__(self, "payload", payload)

        if event_type in _MODEL_CALL_EVENTS and not _payload_text(payload, "model_call_id"):
            raise ValueError(f"{event_type.value} requires payload.model_call_id")
        if event_type in _TOOL_CALL_EVENTS and not _payload_text(payload, "tool_call_id"):
            raise ValueError(f"{event_type.value} requires payload.tool_call_id")
        if event_type is EventType.TOOL_SETTLED:
            settled = payload.get("status")
            try:
                status = settled if isinstance(settled, ToolStatus) else ToolStatus(settled)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "tool.settled payload.status must be a valid ToolStatus"
                ) from exc
            if status is ToolStatus.RUNNING:
                raise ValueError("tool.settled cannot record the non-terminal status 'running'")
        if event_type in _SUCCESS_EVENTS and payload.get("error"):
            raise ValueError(f"{event_type.value} must not record an error")
        if event_type in (EventType.TURN_FAILED, EventType.MODEL_ATTEMPT_FAILED):
            if not _payload_text(payload, "error"):
                raise ValueError(f"{event_type.value} requires payload.error")

        if self.sequence is not None:
            if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
                raise TypeError("sequence must be an int or None")
            if self.sequence < 0:
                raise ValueError("sequence must be >= 0")
        if not isinstance(self.recorded_at, datetime):
            raise TypeError("recorded_at must be a datetime")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")


class CallKind(str, Enum):
    """Which call an unfinished record refers to."""

    MODEL = "model"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class UnfinishedCall:
    """A model or tool call that was started and never settled on disk.

    ``dispatched`` is only meaningful for tool calls: it is true when
    ``tool.dispatched`` was durably written before the process stopped, which
    means the handler may have run. An intent-only call was never handed to a
    handler, so recovery must not mark it unknown. Model calls leave it false.
    """

    session_id: str
    turn_id: str
    step_id: str
    kind: CallKind
    call_id: str
    dispatched: bool = False

    def __post_init__(self) -> None:
        require_non_empty("session_id", self.session_id)
        require_non_empty("turn_id", self.turn_id)
        require_non_empty("step_id", self.step_id)
        require_non_empty("call_id", self.call_id)
        if not isinstance(self.kind, CallKind):
            object.__setattr__(self, "kind", CallKind(self.kind))
        if not isinstance(self.dispatched, bool):
            raise TypeError("dispatched must be a bool")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What a store found unfinished when a session was reopened.

    The report is descriptive; it does not rewrite history by itself. The
    store must append the events produced by :meth:`interruption_events`
    before serving the session as recovered, so an unfinished attempt never
    stays open and never turns into a fabricated success.

    Recovery only identifies unfinished work and keeps stored request
    snapshots readable. It does not restore Forge tasks, resume execution, or
    rebuild a live conversation; conversation history is a separate store read
    (``read_conversation``).
    """

    session_id: str
    unfinished_turns: tuple[str, ...] = ()
    unfinished_calls: tuple[UnfinishedCall, ...] = ()

    def __post_init__(self) -> None:
        require_non_empty("session_id", self.session_id)
        for turn_id in self.unfinished_turns:
            require_non_empty("turn_id", turn_id)
        for call in self.unfinished_calls:
            if not isinstance(call, UnfinishedCall):
                raise TypeError("unfinished_calls must contain UnfinishedCall instances")
            if call.session_id != self.session_id:
                raise ValueError("unfinished call belongs to a different session")

    def interruption_events(self) -> tuple[RuntimeEvent, ...]:
        """Events that close unfinished work without claiming success.

        A model attempt becomes ``model.attempt.interrupted``. A tool call that
        reached dispatch is settled as ``unknown``; one that only reached
        ``intent`` is settled as ``failed`` with error code ``not_dispatched``,
        because its handler was never called. Nothing here is retryable
        automatically.
        """
        events: list[RuntimeEvent] = []
        for call in self.unfinished_calls:
            if call.kind is CallKind.MODEL:
                events.append(
                    RuntimeEvent(
                        type=EventType.MODEL_ATTEMPT_INTERRUPTED,
                        session_id=call.session_id,
                        turn_id=call.turn_id,
                        step_id=call.step_id,
                        payload={"model_call_id": call.call_id, "recovered": True},
                    )
                )
            elif call.dispatched:
                events.append(
                    RuntimeEvent(
                        type=EventType.TOOL_SETTLED,
                        session_id=call.session_id,
                        turn_id=call.turn_id,
                        step_id=call.step_id,
                        payload={
                            "tool_call_id": call.call_id,
                            "status": ToolStatus.UNKNOWN.value,
                            "recovered": True,
                        },
                    )
                )
            else:
                events.append(
                    RuntimeEvent(
                        type=EventType.TOOL_SETTLED,
                        session_id=call.session_id,
                        turn_id=call.turn_id,
                        step_id=call.step_id,
                        payload={
                            "tool_call_id": call.call_id,
                            "status": ToolStatus.FAILED.value,
                            "error": {
                                "code": "not_dispatched",
                                "message": (
                                    "only tool.intent was recorded before the process "
                                    "stopped; the tool handler was never called"
                                ),
                            },
                            "recovered": True,
                        },
                    )
                )
        return tuple(events)

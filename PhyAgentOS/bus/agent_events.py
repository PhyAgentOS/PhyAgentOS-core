"""Structured events emitted while an agent turn runs."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AgentEventType(StrEnum):
    """Kinds of events a running agent turn can publish."""

    TEXT_DELTA = "text_delta"
    TOOL_PROGRESS = "tool_progress"
    TURN_STARTED = "turn_started"
    TURN_DONE = "turn_done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class AgentEvent:
    """A single event produced by one turn of the agent loop."""

    type: AgentEventType
    content: str | None = None
    tool_name: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None

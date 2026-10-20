"""Message bus module for decoupled channel-agent communication."""

from PhyAgentOS.bus.agent_events import AgentEvent, AgentEventType
from PhyAgentOS.bus.events import InboundMessage, OutboundMessage
from PhyAgentOS.bus.queue import MessageBus

__all__ = [
    "MessageBus",
    "InboundMessage",
    "OutboundMessage",
    "AgentEvent",
    "AgentEventType",
]

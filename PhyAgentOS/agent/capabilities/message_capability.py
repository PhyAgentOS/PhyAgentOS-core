"""Expose outbound chat delivery to the agent core as a native capability.

The tool keeps the legacy external contract (name ``message``, the same
``content``/``channel``/``chat_id``/``media`` arguments) but takes its routing
from the entry's frozen :class:`TurnBinding` plus the explicit
:class:`~PhyAgentOS.agent.core.types.ExecutionContext`. No ``ContextVar`` state
is shared between sessions.

Duplicate-reply rule: when the tool sends to the turn's own target, the
capability records that the turn already answered. The entry point then skips
its final reply for that turn, so the user gets one message. A send to another
target does not suppress the reply.

Thread, reply and metadata are preserved for the default target: the outbound
message carries the inbound ``metadata`` (including ``message_thread_id`` and
``message_id``) and ``reply_to``. Media paths are passed through unchanged; the
capability never drops an attachment because the active model cannot read it,
and the channel itself reports an unsupported attachment.

Scope in this version: gateway/background only. The tool needs a live bus and
channel routing to deliver, so the CLI does not inject it; exposing a ``message``
tool in a process with no outbound consumer would claim a delivery that never
happens.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from PhyAgentOS.agent.services.background_runtime import TurnRoutes
from PhyAgentOS.bus.events import OutboundMessage

from ..core.types import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)

MESSAGE_TOOL = ToolDefinition(
    name="message",
    description="Send a message to the user. Use this when you want to communicate something.",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The message content to send"},
            "channel": {
                "type": "string",
                "description": "Optional: target channel (telegram, discord, etc.)",
            },
            "chat_id": {
                "type": "string",
                "description": "Optional: target chat/user ID",
            },
            "media": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional: list of file paths to attach (images, audio, documents)"
                ),
            },
        },
        "required": ["content"],
    },
)


class MessageCapability:
    """Native Core ``message`` tool over an injected outbound publisher."""

    def __init__(
        self,
        routes: TurnRoutes,
        send: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        self._routes = routes
        self._send = send

    @property
    def name(self) -> str:
        return "message"

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return (ToolRegistration(definition=MESSAGE_TOOL, handler=self._handle),)

    def prompt_context(self, context: ExecutionContext) -> str | None:
        return None

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        # Delivery is per tool call; there is no session-owned work to stop.
        del session_id, context

    async def aclose(self) -> None:
        return None

    async def _handle(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        arguments = call.arguments_dict()
        binding = self._routes.get(context.session_id)
        channel = arguments.get("channel") or (binding.channel if binding else "")
        chat_id = arguments.get("chat_id") or (binding.chat_id if binding else "")
        if not channel or not chat_id:
            return self._failed(
                call, "message", "Error: No target channel/chat specified"
            )

        media = list(arguments.get("media") or [])
        to_default = (
            binding is not None
            and channel == binding.channel
            and chat_id == binding.chat_id
        )
        metadata = dict(binding.metadata) if to_default else {}
        message = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=arguments["content"],
            reply_to=binding.reply_to if to_default else None,
            media=media,
            metadata=metadata,
        )
        try:
            await self._send(message)
        except Exception as exc:  # the publisher's own reason is the useful part
            return self._failed(call, "message", f"Error sending message: {exc}")

        if to_default:
            self._routes.mark_reply_sent(context.session_id, context.turn_id)
        media_info = f" with {len(media)} attachments" if media else ""
        return self._succeeded(call, f"Message sent to {channel}:{chat_id}{media_info}")

    @staticmethod
    def _succeeded(call: ToolCall, text: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=text,
        )

    @staticmethod
    def _failed(call: ToolCall, code: str, text: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.FAILED,
            text=text,
            error=ToolError(code, text.removeprefix("Error: ")),
        )

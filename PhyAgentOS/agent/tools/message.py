"""Message tool for sending messages to users."""

from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from PhyAgentOS.agent.tools.base import Tool
from PhyAgentOS.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels.

    Routing and per-turn send tracking live in ``ContextVar``s: every turn
    runs in its own asyncio task, so one session's context cannot leak into
    another's turn. ``start_turn`` clears the send marker so a later turn in
    the same context is never suppressed by an earlier turn.
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel: ContextVar[str] = ContextVar(
            "message_default_channel", default=default_channel
        )
        self._default_chat_id: ContextVar[str] = ContextVar(
            "message_default_chat_id", default=default_chat_id
        )
        self._default_message_id: ContextVar[str | None] = ContextVar(
            "message_default_message_id", default=default_message_id
        )
        self._session_key: ContextVar[str] = ContextVar("message_session_key", default="")
        self._turn_id: ContextVar[str] = ContextVar("message_turn_id", default="")
        self._sent_key: ContextVar[tuple[str, str] | None] = ContextVar(
            "message_sent_key", default=None
        )

    def set_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        session_key: str = "",
        turn_id: str = "",
    ) -> None:
        """Set the current message context for this turn."""
        self._default_channel.set(channel)
        self._default_chat_id.set(chat_id)
        self._default_message_id.set(message_id)
        self._session_key.set(session_key or f"{channel}:{chat_id}")
        self._turn_id.set(turn_id)

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking for the context installed by set_context."""
        self._sent_key.set(None)

    @property
    def _sent_in_turn(self) -> bool:
        """Whether this turn already sent to its own default target."""
        return self._sent_key.get() == (self._session_key.get(), self._turn_id.get())

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str:
        channel = channel or self._default_channel.get()
        chat_id = chat_id or self._default_chat_id.get()
        message_id = message_id or self._default_message_id.get()

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata={
                "message_id": message_id,
            },
        )

        try:
            await self._send_callback(msg)
            if channel == self._default_channel.get() and chat_id == self._default_chat_id.get():
                self._sent_key.set((self._session_key.get(), self._turn_id.get()))
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"

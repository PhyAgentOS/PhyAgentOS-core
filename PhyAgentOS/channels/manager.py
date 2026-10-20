"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from PhyAgentOS.bus.agent_events import AgentEvent, AgentEventType
from PhyAgentOS.bus.events import OutboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.channels.base import BaseChannel
from PhyAgentOS.config.schema import Config


@dataclass
class _StreamState:
    """Accumulated text for one chat's live stream."""

    channel: str
    chat_id: str
    thread_id: str
    turn_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    sent: int = 0
    flush_task: asyncio.Task | None = None
    closed: bool = False


class _SendQueue:
    """Bounded per-chat queue that rejects new deltas when full.

    Existing items are never evicted: dropped deltas would leave a gap in
    the stream, so a delta that does not fit is dropped by the producer and
    retried later. Terminal events, progress and final replies always append
    (they may briefly exceed the limit) so a long stream cannot starve the
    message that ends the turn.
    """

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.items: deque[OutboundMessage] = deque()
        self._ready = asyncio.Event()

    @staticmethod
    def _droppable(msg: OutboundMessage) -> bool:
        return (
            msg.event is not None
            and msg.event.type == AgentEventType.TEXT_DELTA
        )

    def put(self, msg: OutboundMessage) -> bool:
        if len(self.items) >= self.limit and self._droppable(msg):
            return False
        self.items.append(msg)
        self._ready.set()
        return True

    def has_items(self) -> bool:
        return bool(self.items)

    async def get(self, timeout: float) -> OutboundMessage | None:
        if not self.items:
            self._ready.clear()
            if not self.items:
                try:
                    await asyncio.wait_for(self._ready.wait(), timeout)
                except asyncio.TimeoutError:
                    return None
        return self.items.popleft() if self.items else None


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    - Stream agent events per chat without letting one slow channel block others
    """

    # Merge window and size before a text stream update is handed to a channel.
    STREAM_COALESCE_INTERVAL = 0.08
    STREAM_FLUSH_CHARS = 60
    # Per-chat outbound queue bound, plus idle worker lifetime.
    QUEUE_LIMIT = 64
    WORKER_IDLE_TIMEOUT = 30.0

    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._init_dispatch_state()

        self._init_channels()

    def _init_dispatch_state(self) -> None:
        """Initialize per-chat queues, workers and stream bookkeeping."""
        self._queues: dict[tuple[str, str], _SendQueue] = {}
        self._workers: dict[tuple[str, str], asyncio.Task] = {}
        self._streams: dict[tuple[str, str, str], _StreamState] = {}
        self._finalized: dict[tuple[str, str, str], tuple[str, str | None]] = {}
        self._closed = False

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan."""
        from PhyAgentOS.channels.registry import discover_channel_names, load_channel_class

        groq_key = self.config.providers.groq.api_key

        for modname in discover_channel_names():
            section = getattr(self.config.channels, modname, None)
            if not section or not getattr(section, "enabled", False):
                continue
            try:
                cls = load_channel_class(modname)
                channel = cls(section, self.bus)
                channel.transcription_api_key = groq_key
                self.channels[modname] = channel
                logger.info("{} channel enabled", cls.display_name)
            except ImportError as e:
                logger.warning("{} channel not available: {}", modname, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        self._closed = False
        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")
        self._closed = True

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        for state in list(self._streams.values()):
            self._cancel_flush(state)
        self._streams.clear()

        for task in list(self._workers.values()):
            task.cancel()
        for task in list(self._workers.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._workers.clear()
        self._queues.clear()

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

    async def _dispatch_outbound(self) -> None:
        """Read outbound messages and hand them to per-chat workers."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )
                self._route(msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Outbound routing failed: {}", e)

    def _route(self, msg: OutboundMessage) -> None:
        """Classify and queue one outbound message or agent event."""
        channel = self.channels.get(msg.channel)
        if channel is None:
            logger.warning("Unknown channel: {}", msg.channel)
            return

        event = msg.event
        if event is None:
            self._route_message(channel, msg)
            return

        if event.type == AgentEventType.TEXT_DELTA:
            if not channel.supports_streaming:
                return
            self._on_delta(channel, msg, event)
        elif event.type == AgentEventType.TURN_STARTED:
            if not channel.supports_streaming:
                return
            self._reset_stream(channel, msg, event)
            self._enqueue(msg)
        elif event.type == AgentEventType.TOOL_PROGRESS:
            if (event.metadata or {}).get("phase") == "done":
                return
            progress = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                metadata=dict(msg.metadata or {}, _progress=True, _tool_hint=True),
            )
            if self._allow_progress(progress):
                self._enqueue(progress)
        elif event.type == AgentEventType.TURN_DONE:
            if not channel.supports_streaming:
                return
            state = self._state_for_event(msg, event)
            if state is not None:
                self._finalize_stream(
                    state, state.text, AgentEventType.TURN_DONE, metadata=msg.metadata,
                    full_text=state.text, remember=True,
                )
        elif event.type == AgentEventType.ERROR:
            status = event.content or "Error"
            if not channel.supports_streaming:
                return
            state = self._state_for_event(msg, event)
            if state is not None:
                self._finalize_stream(
                    state, status, AgentEventType.ERROR, metadata=msg.metadata,
                    full_text=state.text, remember=True,
                )
            elif not self._already_finished(msg, event):
                self._enqueue(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=status,
                    metadata=dict(msg.metadata or {}),
                    event=event,
                ))
        elif event.type == AgentEventType.CANCELLED:
            status = event.content or "Cancelled."
            if channel.supports_streaming:
                state = self._state_for_event(msg, event)
                if state is not None:
                    self._finalize_stream(
                        state, status, AgentEventType.CANCELLED,
                        metadata=msg.metadata, full_text=state.text, remember=True,
                    )
                elif not self._already_finished(msg, event):
                    self._enqueue(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=status,
                        metadata=dict(msg.metadata or {}),
                        event=event,
                    ))
            elif not self._already_finished(msg, event):
                self._enqueue(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=status,
                    metadata=dict(msg.metadata or {}),
                ))

    def _route_message(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        """Route a plain message, finalizing only the turn's marked reply."""
        if not self._allow_progress(msg):
            return

        key = self._stream_key(msg)
        is_final = bool(msg.metadata.get("_turn_final"))
        turn_id = msg.metadata.get("turn_id")
        state = self._streams.get(key)
        if (
            is_final
            and state is not None
            and channel.supports_streaming
            and (not turn_id or not state.turn_id or turn_id == state.turn_id)
        ):
            # The final reply is queued before turn_done, so it ends the stream.
            self._finalize_stream(
                state, msg.content, AgentEventType.TURN_DONE,
                metadata=msg.metadata, full_text=state.text, remember=True,
            )
            return

        finished = self._finalized.get(key)
        if (
            is_final
            and finished is not None
            and msg.content
            and finished[0].endswith(msg.content)
        ):
            # turn_done already persisted this text; do not send it twice.
            return
        self._enqueue(msg)

    def _allow_progress(self, msg: OutboundMessage) -> bool:
        """Apply the existing progress/tool-hint switches."""
        if not msg.metadata.get("_progress"):
            return True
        if msg.metadata.get("_tool_hint"):
            return bool(self.config.channels.send_tool_hints)
        return bool(self.config.channels.send_progress)

    @staticmethod
    def _key(msg: OutboundMessage) -> tuple[str, str]:
        return (msg.channel, msg.chat_id)

    @staticmethod
    def _thread_id(msg: OutboundMessage) -> str:
        thread = (msg.metadata or {}).get("message_thread_id")
        return "" if thread is None else str(thread)

    @classmethod
    def _stream_key(cls, msg: OutboundMessage) -> tuple[str, str, str]:
        return (msg.channel, msg.chat_id, cls._thread_id(msg))

    def _state_for_event(
        self, msg: OutboundMessage, event: AgentEvent,
    ) -> _StreamState | None:
        """Return the stream for this chat/topic, enforcing turn identity."""
        state = self._streams.get(self._stream_key(msg))
        if state is None:
            return None
        if event.turn_id and state.turn_id and event.turn_id != state.turn_id:
            return None
        return state

    def _already_finished(self, msg: OutboundMessage, event: AgentEvent) -> bool:
        finished = self._finalized.get(self._stream_key(msg))
        return bool(
            finished is not None
            and event.turn_id
            and finished[1] == event.turn_id
        )

    def _reset_stream(
        self, channel: BaseChannel, msg: OutboundMessage, event: AgentEvent,
    ) -> _StreamState:
        key = self._stream_key(msg)
        old = self._streams.pop(key, None)
        if old is not None:
            self._cancel_flush(old)
        self._finalized.pop(key, None)
        state = _StreamState(
            channel=msg.channel,
            chat_id=msg.chat_id,
            thread_id=self._thread_id(msg),
            turn_id=event.turn_id,
            metadata=dict(msg.metadata or {}),
        )
        self._streams[key] = state
        return state

    def _on_delta(
        self, channel: BaseChannel, msg: OutboundMessage, event: AgentEvent,
    ) -> None:
        key = self._stream_key(msg)
        state = self._streams.get(key)
        if state is None or (
            event.turn_id and state.turn_id and event.turn_id != state.turn_id
        ):
            state = self._reset_stream(channel, msg, event)

        if event.content:
            state.text += event.content
        state.metadata = dict(msg.metadata or state.metadata)

        if len(state.text) - state.sent >= self.STREAM_FLUSH_CHARS:
            self._cancel_flush(state)
            self._flush_now(state)
        else:
            self._schedule_flush(state)

    def _schedule_flush(self, state: _StreamState) -> None:
        if state.flush_task is not None and not state.flush_task.done():
            return
        state.flush_task = asyncio.create_task(self._flush_later(state))

    def _cancel_flush(self, state: _StreamState) -> None:
        task = state.flush_task
        state.flush_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _flush_later(self, state: _StreamState) -> None:
        try:
            await asyncio.sleep(self.STREAM_COALESCE_INTERVAL)
        except asyncio.CancelledError:
            return
        if state.flush_task is asyncio.current_task():
            state.flush_task = None
        self._flush_now(state)

    def _flush_now(self, state: _StreamState) -> None:
        """Hand the merged new text to the channel as one incremental chunk."""
        if state.closed or len(state.text) <= state.sent:
            return
        chunk = state.text[state.sent:]
        event = AgentEvent(
            type=AgentEventType.TEXT_DELTA,
            content=chunk,
            turn_id=state.turn_id,
        )
        queued = self._enqueue(OutboundMessage(
            channel=state.channel,
            chat_id=state.chat_id,
            content=chunk,
            metadata=dict(state.metadata),
            event=event,
        ))
        if queued:
            state.sent = len(state.text)

    def _finalize_stream(
        self,
        state: _StreamState,
        content: str,
        terminal_type: AgentEventType,
        *,
        metadata: dict[str, Any] | None,
        full_text: str,
        remember: bool,
    ) -> None:
        """Cancel pending merges and queue the terminal event for one stream."""
        key = (state.channel, state.chat_id, state.thread_id)
        if self._streams.get(key) is state:
            self._streams.pop(key, None)
        state.closed = True
        self._cancel_flush(state)

        event = AgentEvent(
            type=terminal_type,
            content=content,
            turn_id=state.turn_id,
            metadata={"_stream_text": full_text},
        )
        merged = dict(state.metadata)
        merged.update(metadata or {})
        merged["_stream_text"] = full_text
        self._enqueue(OutboundMessage(
            channel=state.channel,
            chat_id=state.chat_id,
            content=content,
            metadata=merged,
            event=event,
        ))
        if remember:
            self._finalized[key] = (full_text, state.turn_id)

    def _enqueue(self, msg: OutboundMessage) -> bool:
        key = self._key(msg)
        queue = self._queues.get(key)
        if queue is None:
            queue = _SendQueue(self.QUEUE_LIMIT)
            self._queues[key] = queue

        if not queue.put(msg):
            return False

        worker = self._workers.get(key)
        if worker is None or worker.done():
            self._workers[key] = asyncio.create_task(self._worker(key))
        return True

    async def _worker(self, key: tuple[str, str]) -> None:
        """Send one chat's queue in order until it stays idle."""
        queue = self._queues.get(key)
        if queue is None:
            return
        try:
            while not self._closed:
                msg = await queue.get(self.WORKER_IDLE_TIMEOUT)
                if msg is None:
                    if queue.has_items():
                        continue
                    break
                channel = self.channels.get(key[0])
                if channel is None:
                    continue
                try:
                    await self._deliver(channel, msg)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Error sending to {}: {}", key[0], e)
        finally:
            if self._queues.get(key) is queue:
                self._queues.pop(key, None)
            if self._workers.get(key) is asyncio.current_task():
                self._workers.pop(key, None)

    async def _deliver(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        if msg.event is not None and channel.supports_streaming:
            await channel.send_event(msg)
        else:
            await channel.send(msg)

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())

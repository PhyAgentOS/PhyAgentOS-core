"""Shared Core runtime behind the gateway's background sources.

The gateway has three non-terminal inputs: chat channels, cron jobs, and the
heartbeat. They all use one Core session opened by the CLI's own
``open_core_session`` (with the entry's cron/message capabilities added), so
they share one SQLite session store, one Forge coordinator/client, one Skill
activation manager and the same per-turn assembly path as the CLI. Nothing
here builds a second model loop.

The wrapper only adds what the CLI entry cannot know: three parameters of
``open_core_session`` — ``additional_capabilities``, and the optional
``service``/``selected`` injection so the gateway reuses the provider it
already resolved instead of resolving twice. It does not copy the store,
Forge bootstrap, recovery, per-turn build or shutdown.

Two pieces of entry state are added on top of Core:

* :class:`TurnBinding` is the routing the entry froze for the turn before it
  started (channel, chat, thread metadata, reply target). Capabilities read it
  by ``ExecutionContext.session_id`` instead of a shared ``ContextVar``, so a
  later heartbeat or chat turn cannot move an in-flight turn's target.
* :class:`BackgroundCoreRuntime` bounds accepted work. Same-session turns are
  strictly ordered, different sessions run up to ``max_concurrent_turns``, a
  session with more than ``per_session_queue_limit`` queued turns is refused,
  and total unsettled turns are capped by
  ``max_concurrent_turns * (per_session_queue_limit + 1)``. The last bound is
  derived from the two existing settings because configuration has no separate
  global queue key; it exists so incoming messages cannot create unbounded
  asyncio tasks. CoreAgentSession still serializes each session, so this gate
  only decides admission and never lets a second turn start early.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from loguru import logger

from PhyAgentOS.agent.core import TurnRequest, TurnResult, TurnStatus
from PhyAgentOS.agent.core.protocols import Capability
from PhyAgentOS.bus.events import InboundMessage, OutboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.service import ProviderService, RuntimeSelection

__all__ = [
    "BackgroundClosedError",
    "BackgroundCoreRuntime",
    "ChannelTurnDispatcher",
    "TurnBinding",
    "TurnOutcome",
    "TurnRejectedError",
    "TurnRoutes",
    "open_background_core",
]

# The channel path's answer when the model completed without text and the
# message tool did not already deliver one. It matches the legacy channel
# wording so a silent turn is still visible to the user.
EMPTY_REPLY = "I've completed processing but have no response to give."
BUSY_REPLY = (
    "The agent is busy with other work right now. Your message was not "
    "processed; please send it again."
)


def channel_turn_text(msg: InboundMessage) -> str:
    """Return the turn text, keeping explicit attachment references.

    Core has no multimodal message input in this version, so an attachment is
    never silently discarded: its reference is appended to the text and the
    model is told the runtime did not forward the bytes.
    """
    if not msg.media:
        return msg.content
    references = "\n".join(f"- {item}" for item in msg.media)
    return (
        f"{msg.content}\n\n"
        "[Attachments from the channel: this runtime does not forward media "
        f"bytes to the model. Preserved references:]\n{references}"
    )


@dataclass(frozen=True, slots=True)
class TurnBinding:
    """The routing frozen for one turn before it starts."""

    channel: str
    chat_id: str
    reply_to: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # True for a turn started by a cron job: the cron tool must not recursively
    # create more jobs from inside it.
    scheduled: bool = False

    def __post_init__(self) -> None:
        if not self.channel or not self.chat_id:
            raise ValueError("a turn binding requires channel and chat_id")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata or {})))


class TurnRoutes:
    """Per-session turn routing plus the "already answered" markers.

    A binding exists only while its turn runs, and the marker is keyed by
    ``(session_id, turn_id)``. ``BackgroundCoreRuntime`` consumes the marker
    before it releases the session's ordering gate, so the next queued turn of
    the same session cannot clear it before the entry point has read it. The
    marker is removed even when the turn raises, so a rejected or failed turn
    cannot suppress a later reply.
    """

    def __init__(self) -> None:
        self._bound: dict[str, TurnBinding] = {}
        self._reply_sent: set[tuple[str, str]] = set()

    def bind(self, session_id: str, binding: TurnBinding) -> None:
        if session_id in self._bound:
            raise RuntimeError(f"session {session_id!r} already has a bound turn")
        self._bound[session_id] = binding

    def unbind(self, session_id: str) -> None:
        self._bound.pop(session_id, None)

    def get(self, session_id: str) -> TurnBinding | None:
        return self._bound.get(session_id)

    def mark_reply_sent(self, session_id: str, turn_id: str) -> None:
        self._reply_sent.add((session_id, turn_id))

    def consume_reply_sent(self, session_id: str, turn_id: str) -> bool:
        """Return and forget whether the message tool answered this turn."""
        key = (session_id, turn_id)
        if key in self._reply_sent:
            self._reply_sent.discard(key)
            return True
        return False

    def discard_reply_sent(self, session_id: str) -> None:
        """Drop leftover markers of a session whose turn did not settle."""
        self._reply_sent = {
            key for key in self._reply_sent if key[0] != session_id
        }


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """A settled turn plus the entry-facing fact it already answered.

    ``reply_sent`` is read under the session's ordering gate, so a dispatcher
    can decide between the final reply and skipping it without racing the next
    turn of the same session.
    """

    result: TurnResult
    reply_sent: bool


class TurnRejectedError(RuntimeError):
    """Admission refused the turn because the session or process is full."""

    def __init__(self, session_id: str, reason: str) -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(reason)


class BackgroundClosedError(RuntimeError):
    """The runtime started shutting down and refused new input."""

    def __init__(self) -> None:
        super().__init__("background runtime is closed")


@dataclass(slots=True)
class _SessionGate:
    """Ordering for one session plus the number of waiting turns."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: int = 0


class BackgroundCoreRuntime:
    """Admit background turns into one shared Core session and own its close.

    ``run_turn`` is the only way a background source opens a turn. It freezes
    the binding, waits for the session's ordering gate and a cross-session
    slot, then delegates to CoreAgentSession. Rejection raises
    :class:`TurnRejectedError` so each source can decide its own user-visible
    behavior: cron/heartbeat skip with a log, a chat turn answers that it was
    not processed.
    """

    def __init__(
        self,
        session,
        *,
        routes: TurnRoutes | None = None,
        max_concurrent_turns: int = 4,
        per_session_queue_limit: int = 8,
    ) -> None:
        self.session = session
        self.routes = routes or TurnRoutes()
        self._max_concurrent_turns = max(1, int(max_concurrent_turns))
        self._per_session_queue_limit = max(0, int(per_session_queue_limit))
        self._max_pending = self._max_concurrent_turns * (
            self._per_session_queue_limit + 1
        )
        self._semaphore = asyncio.Semaphore(self._max_concurrent_turns)
        self._gates: dict[str, _SessionGate] = {}
        self._pending = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def unsettled(self) -> int:
        """Accepted turns still waiting or running; zero after a clean stop."""
        return self._pending

    async def run_turn(
        self,
        *,
        session_id: str,
        text: str,
        binding: TurnBinding,
        source: str,
    ) -> TurnOutcome:
        if self._closed:
            raise BackgroundClosedError()
        async with self._slot(session_id):
            if self._closed:
                # A turn that waited here must never enter Core after its
                # shutdown began: Core would already be closing its store.
                raise BackgroundClosedError()
            self.routes.bind(session_id, binding)
            try:
                logger.info("Background turn accepted ({} -> {})", source, session_id)
                result = await self.session.submit(TurnRequest(session_id, text))
                # Read the marker before the gate opens again, so the next
                # same-session turn cannot clear it first.
                reply_sent = self.routes.consume_reply_sent(
                    session_id, result.turn_id
                )
                logger.info(
                    "Background turn settled ({} -> {}, {})",
                    source,
                    session_id,
                    result.status.value,
                )
                return TurnOutcome(result=result, reply_sent=reply_sent)
            finally:
                self.routes.unbind(session_id)
                self.routes.discard_reply_sent(session_id)

    @asynccontextmanager
    async def _slot(self, session_id: str):
        """Same-session order plus bounded running and queued turns.

        All counters are updated before waiting, so a burst is refused instead
        of queued forever. Both waits release exactly what they acquired on
        cancellation, and the per-session gate object is dropped when its last
        waiter leaves.
        """
        gate = self._gates.setdefault(session_id, _SessionGate())
        gate.pending += 1
        self._pending += 1
        if gate.pending > self._per_session_queue_limit + 1:
            self._release_counters(session_id, gate)
            raise TurnRejectedError(
                session_id,
                f"Session {session_id} already has "
                f"{self._per_session_queue_limit} queued task(s); try again after "
                "the current turn finishes.",
            )
        if self._pending > self._max_pending:
            self._release_counters(session_id, gate)
            raise TurnRejectedError(
                session_id,
                "The gateway already has the maximum number of unsettled turns "
                f"({self._max_pending}); try again after some turns finish.",
            )
        session_held = False
        semaphore_held = False
        try:
            await gate.lock.acquire()
            session_held = True
            await self._semaphore.acquire()
            semaphore_held = True
            yield
        finally:
            if semaphore_held:
                self._semaphore.release()
            if session_held:
                gate.lock.release()
            self._release_counters(session_id, gate)

    def _release_counters(self, session_id: str, gate: _SessionGate) -> None:
        self._pending -= 1
        gate.pending -= 1
        if gate.pending == 0 and not gate.lock.locked():
            # Ended sessions must not accumulate gate objects per chat.
            if self._gates.get(session_id) is gate:
                self._gates.pop(session_id, None)

    async def aclose(self) -> None:
        """Reject new turns, settle accepted ones, then close Core once.

        ``CoreAgentSession.aclose`` requests local cancellation for running
        turns, wakes queued submits instead of starting them, waits for every
        accepted submit, notifies capabilities, and closes the store exactly
        once. Turns still waiting in this gate see :class:`BackgroundClosedError`
        after their wait and never submit into the closing Core.
        """
        if self._closed:
            return
        self._closed = True
        await self.session.aclose()
        logger.info("Background Core runtime closed")


class ChannelTurnDispatcher:
    """Consume channel input from the bus and deliver one reply per turn.

    Intake is a single consumer loop; each message becomes its own turn task so
    one slow session does not block another's inbox. Shutdown stops intake
    first, lets the runtime settle accepted turns, and then reaps the tasks so
    no future is left pending.
    """

    def __init__(
        self,
        runtime: BackgroundCoreRuntime,
        bus: MessageBus,
        *,
        on_input: Callable[[InboundMessage], None] | None = None,
    ) -> None:
        self._runtime = runtime
        self._bus = bus
        self._on_input = on_input
        self._intake: asyncio.Task | None = None
        self._turns: set[asyncio.Task] = set()
        self._closed = False

    async def run(self) -> None:
        self._intake = asyncio.current_task()
        while not self._closed:
            msg = await self._bus.consume_inbound()
            if self._closed:
                return
            task = asyncio.create_task(self._handle(msg))
            self._turns.add(task)
            task.add_done_callback(self._turns.discard)

    async def stop(self) -> None:
        """Stop intake only; accepted turns are settled by the runtime."""
        self._closed = True
        task = self._intake
        self._intake = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def drain(self) -> None:
        """Wait for every accepted channel turn to leave its task."""
        pending = [task for task in self._turns if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._turns.clear()

    async def _handle(self, msg: InboundMessage) -> None:
        session_id = msg.session_key
        if self._on_input is not None:
            self._on_input(msg)
        binding = TurnBinding(
            channel=msg.channel,
            chat_id=msg.chat_id,
            reply_to=msg.metadata.get("reply_to"),
            metadata=msg.metadata,
        )
        try:
            outcome = await self._runtime.run_turn(
                session_id=session_id,
                text=channel_turn_text(msg),
                binding=binding,
                source=f"channel:{msg.channel}",
            )
        except TurnRejectedError as exc:
            logger.warning("Channel turn rejected for {}: {}", session_id, exc.reason)
            await self._publish(msg, BUSY_REPLY, turn_id=None)
            return
        except BackgroundClosedError:
            logger.info("Channel turn skipped for {}: gateway is shutting down", session_id)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Channel turn failed for {}", session_id)
            await self._publish(
                msg, f"Turn failed: {type(exc).__name__}: {exc}", turn_id=None
            )
            return

        result = outcome.result
        if outcome.reply_sent:
            # The message tool already delivered this turn's answer.
            logger.info("Channel reply suppressed for {}: sent by message tool", session_id)
            return
        if result.status is TurnStatus.INTERRUPTED:
            logger.info("Channel turn interrupted for {}; no reply sent", session_id)
            return
        content = result.reply
        if not content:
            content = EMPTY_REPLY if result.status is TurnStatus.COMPLETED else (
                f"Turn failed: {result.error or 'unknown error'}"
            )
        await self._publish(msg, content, turn_id=result.turn_id)

    async def _publish(
        self, msg: InboundMessage, content: str, *, turn_id: str | None
    ) -> None:
        metadata = dict(msg.metadata or {})
        # Mark the one reply that ends this turn so the channel manager can
        # close a live stream instead of duplicating the final text.
        metadata["_turn_final"] = True
        if turn_id is not None:
            metadata["turn_id"] = turn_id
        await self._bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                reply_to=msg.metadata.get("reply_to"),
                metadata=metadata,
            )
        )


class _LogOutput:
    """Narrow output sink: persist-then-log instead of drawing a terminal."""

    def error(self, text: str) -> None:
        logger.error(text)

    def event(self, event) -> None:
        return None


async def _open_core_session_with_entry_state(config, **kwargs):
    """Open the CLI Core session with the gateway's extra capabilities.

    ``open_core_session`` owns the store, Forge recovery, per-turn assembly and
    shutdown. The gateway only adds ``service``/``selected`` (the provider it
    already resolved) and ``additional_capabilities`` (cron and message), which
    are registered after Skill and before Forge in every turn.
    """
    from PhyAgentOS.cli.command_groups import agent_commands

    return agent_commands.open_core_session(config, **kwargs)


@asynccontextmanager
async def open_background_core(
    config: Config,
    *,
    service: ProviderService,
    selected: RuntimeSelection,
    capabilities: Sequence[Capability] = (),
    routes: TurnRoutes | None = None,
    output=None,
):
    """Open one shared Core session for the gateway's background sources.

    This is a wrapper, not a second composition root: the store, Forge
    coordinator/client, recovery, per-turn build and shutdown all stay in the
    CLI's ``open_core_session``. Background entry state is layered on the
    returned session as admission control and turn routing.
    """
    session = await _open_core_session_with_entry_state(
        config,
        output=output or _LogOutput(),
        additional_capabilities=tuple(capabilities),
        service=service,
        selected=selected,
    )
    async with session as core:
        defaults = config.agents.defaults
        runtime = BackgroundCoreRuntime(
            core,
            routes=routes,
            max_concurrent_turns=defaults.max_concurrent_turns,
            per_session_queue_limit=defaults.per_session_queue_limit,
        )
        yield runtime

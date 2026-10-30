"""Shared fakes for the Core background-gateway tests.

Only the process boundary is faked: the model provider, the Forge coordinator
built by ``open_core_session``, and the channel manager. The Core runtime,
per-turn assembly, tools, SQLite store and the cron/heartbeat services stay the
production classes, so a background turn is a real Core request.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from types import SimpleNamespace

from PhyAgentOS.bus.events import InboundMessage
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest
from PhyAgentOS.providers.service import RuntimeSelection

__all__ = [
    "FakeChannel",
    "FakeChannelManager",
    "FakeCoordinator",
    "ScriptedProvider",
    "fake_forge_builder",
    "make_config",
    "make_selection",
    "publish_inbound",
    "response",
    "tool_call",
    "wait_for",
]


def make_config(tmp_path, *, concurrent: int = 4, per_session: int = 8) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    config.agents.defaults.max_concurrent_turns = concurrent
    config.agents.defaults.per_session_queue_limit = per_session
    # Tests trigger heartbeats explicitly; no interval task and no real clock.
    config.gateway.heartbeat.enabled = True
    config.gateway.heartbeat.interval_s = 3600
    return config


def make_selection(provider) -> RuntimeSelection:
    return RuntimeSelection(
        name="fake",
        model="fake-model",
        effort=None,
        endpoint=None,
        provider=provider,
        context_window_tokens=100_000,
    )


def tool_call(
    call_id: str, name: str, arguments: dict, *, finish_reason: str = "tool_calls"
) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)],
        finish_reason=finish_reason,
    )


def response(
    content: str | None = None,
    *,
    tool_calls: list[ToolCallRequest] | None = None,
    finish_reason: str | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=list(tool_calls or []),
        finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
    )


@dataclass
class ScriptedProvider:
    """A provider that answers with a scripted response per call."""

    responder: object
    calls: list[dict] = field(default_factory=list)

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        self.calls.append(
            {
                "messages": copy.deepcopy(list(messages)),
                "tools": copy.deepcopy(tools),
                "kwargs": dict(kwargs),
            }
        )
        return await self.responder(len(self.calls), messages, tools)


class FakeCoordinator:
    """The small coordinator surface Core capabilities and prompts use."""

    def __init__(self, blockers: list[dict] | None = None) -> None:
        self.binding_resolver = None
        self.activation_manager = None
        self.store = SimpleNamespace(latest_for_session=lambda session: None)
        self.reconcile_calls = 0
        self._blockers = list(blockers or [])

    def set_activation_manager(self, manager) -> None:
        self.activation_manager = manager

    def unknown_remote_blockers(self) -> list[dict]:
        return list(self._blockers)

    async def reconcile_nonterminal(self):
        self.reconcile_calls += 1
        return None


def fake_forge_builder(coordinator: FakeCoordinator):
    def build(config, provider, *, write_legacy_lessons=None, resources=None):
        del config, provider, write_legacy_lessons, resources
        return None, None, coordinator, (lambda name: False)

    return build


@dataclass
class FakeChannel:
    name: str
    supports_streaming: bool = False
    sent: list = field(default_factory=list)
    stopped: bool = False
    config: object = field(default_factory=lambda: SimpleNamespace(allow_from=["*"]))

    async def send(self, msg) -> None:
        self.sent.append(msg)

    async def send_event(self, msg) -> None:
        self.sent.append(msg)

    async def start(self) -> None:  # pragma: no cover - never awaited by tests
        return None

    async def stop(self) -> None:
        self.stopped = True


class FakeChannelManager:
    """Deliver outbound messages to fake channels and stay running until stopped."""

    def __init__(self, channels: dict[str, FakeChannel], bus) -> None:
        self.channels = channels
        self.bus = bus
        self._task: asyncio.Task | None = None
        self.started = False
        self.stopped = False

    @property
    def enabled_channels(self) -> list[str]:
        return list(self.channels)

    def get_channel(self, name: str):
        return self.channels.get(name)

    async def start_all(self) -> None:
        self.started = True
        self._task = asyncio.create_task(self._dispatch())
        await asyncio.Event().wait()

    async def _dispatch(self) -> None:
        while True:
            msg = await self.bus.consume_outbound()
            channel = self.channels.get(msg.channel)
            if channel is not None:
                await channel.send(msg)

    async def stop_all(self) -> None:
        self.stopped = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def publish_inbound(
    bus,
    channel: str,
    chat_id: str,
    text: str,
    *,
    metadata: dict | None = None,
    session_key: str | None = None,
    media: list[str] | None = None,
) -> None:
    await bus.publish_inbound(
        InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=text,
            metadata=dict(metadata or {}),
            session_key_override=session_key,
            media=list(media or []),
        )
    )


async def wait_for(predicate, timeout: float = 3.0) -> None:
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout)

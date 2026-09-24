"""LLM failure handling in the agent loop.

A completion that comes back with ``finish_reason="error"`` must end as a
named failure reply — never a crash out of ``process_direct`` (its callers:
CLI single-message mode, cron jobs, heartbeat) — and the partial turn (the
user's message plus any completed tool exchanges) must still be persisted,
so a retry does not start from scratch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from PhyAgentOS.agent.loop import AgentLoop
from PhyAgentOS.bus.events import InboundMessage
from PhyAgentOS.providers.base import LLMCallError, LLMResponse, ToolCallRequest


class _ScriptedProvider:
    """Hands out scripted responses; fails loudly on any unexpected call."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        assert self._responses, "unexpected extra chat_with_retry call"
        return self._responses.pop(0)


class _RecordingBus:
    def __init__(self) -> None:
        self.outbound: list = []

    async def publish_outbound(self, message) -> None:
        self.outbound.append(message)


def _error_response(detail: str = "Error calling LLM: boom") -> LLMResponse:
    return LLMResponse(content=detail, finish_reason="error")


def _loop(tmp_path: Path, responses: list[LLMResponse]) -> AgentLoop:
    return AgentLoop(
        bus=_RecordingBus(),
        provider=_ScriptedProvider(responses),
        workspace=tmp_path,
        # keep token-based memory consolidation far out of reach: the
        # scripted provider must not be consulted for anything but the turn
        context_window_tokens=10**9,
    )


async def test_failed_completion_replies_instead_of_raising(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [_error_response()])

    reply = await loop.process_direct("hello", session_key="cli:test")

    assert reply.startswith("LLM call failed:")
    assert "boom" in reply


async def test_failed_completion_persists_user_message_but_not_error_text(
    tmp_path: Path,
) -> None:
    loop = _loop(tmp_path, [_error_response()])

    await loop.process_direct("hello", session_key="cli:test")

    session = loop.sessions.get_or_create("cli:test")
    assert any(
        m.get("role") == "user" and "hello" in str(m.get("content"))
        for m in session.messages
    )
    # the provider's error text must not masquerade as an assistant reply (#1303)
    assert not any(
        m.get("role") == "assistant" and "boom" in str(m.get("content"))
        for m in session.messages
    )


async def test_failed_completion_after_tool_round_persists_exchanges(
    tmp_path: Path,
) -> None:
    loop = _loop(tmp_path, [
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="no_such_tool", arguments={"x": 1})],
            finish_reason="tool_calls",
        ),
        _error_response(),
    ])

    reply = await loop.process_direct("use the tool", session_key="cli:test")

    assert reply.startswith("LLM call failed:")
    session = loop.sessions.get_or_create("cli:test")
    roles = [m.get("role") for m in session.messages]
    assert "tool" in roles  # the completed tool exchange survived the abort


@pytest.mark.parametrize("system_message", [False, True])
@pytest.mark.parametrize("channel,metadata", [
    ("telegram", {"message_id": 42, "message_thread_id": 7}),
    ("slack", {"slack": {"thread_ts": "123.456", "channel_type": "channel"}}),
])
async def test_failed_completion_preserves_thread_metadata(
    tmp_path: Path, channel: str, metadata: dict, system_message: bool,
) -> None:
    loop = _loop(tmp_path, [_error_response()])
    message = InboundMessage(
        channel="system" if system_message else channel,
        sender_id="test",
        chat_id=f"{channel}:chat" if system_message else "chat",
        content="hello", metadata=metadata,
    )

    reply = await loop._process_message(message)

    assert reply is not None
    assert reply.content.startswith("LLM call failed:")
    assert (reply.channel, reply.chat_id) == (channel, "chat")
    assert reply.metadata == metadata
    assert reply.metadata is not metadata
    session = loop.sessions.get_or_create(f"{channel}:chat")
    assert any(m.get("role") == "user" and "hello" in str(m.get("content"))
               for m in session.messages)
    assert all("boom" not in str(m.get("content")) for m in session.messages)


async def test_dispatch_defensive_llm_handler_preserves_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _loop(tmp_path, [])
    message = InboundMessage(
        channel="telegram", sender_id="test", chat_id="chat", content="hello",
        metadata={"message_id": 42, "message_thread_id": 7},
    )

    async def fail(_message: InboundMessage):
        raise LLMCallError("boom")

    monkeypatch.setattr(loop, "_process_message", fail)
    await loop._dispatch(message)

    reply = loop.bus.outbound[-1]
    assert reply.content == "LLM call failed: boom"
    assert reply.metadata == message.metadata
    assert reply.metadata is not message.metadata

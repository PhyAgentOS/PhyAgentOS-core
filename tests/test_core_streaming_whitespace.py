"""Whitespace-only streaming deltas are body text, not invalid payloads.

Providers may send a space, a newline or a blank line as its own SSE delta.
The core must keep those pieces verbatim: dropping or rejecting them changes
the answer (for example ``**robotics /`` followed by a newline and a Markdown
list) and used to fail the whole turn with ``ValueError: content must be a
non-empty string``. This is a stream-compatibility rule, not a tokenizer
estimate.

The end-to-end case goes through the real ``AsyncOpenAI`` client and an SSE
``MockTransport``, then ``CustomProvider`` -> ``_SelectedProvider`` -> the Core
loop, before re-reading the durable SQLite log.
"""

from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from rich.console import Console

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.host import AgentHostImpl
from PhyAgentOS.agent.core.loop import LoopDriverImpl
from PhyAgentOS.agent.core.model import ModelAdapter
from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl
from PhyAgentOS.agent.core.runtime import AgentRuntimeImpl
from PhyAgentOS.agent.core.session import SQLiteSessionStore
from PhyAgentOS.agent.core.tools import ToolExecutorImpl
from PhyAgentOS.agent.core.types import (
    ModelRequest,
    ModelResponse,
    ModelStreamDelta,
    TurnRequest,
    TurnStatus,
)
from PhyAgentOS.cli.command_groups.agent_commands import _SelectedProvider
from PhyAgentOS.cli.interaction.agent_output import AgentOutput
from PhyAgentOS.providers.custom_provider import CustomProvider
from PhyAgentOS.providers.service import RuntimeSelection


def _delta_event(content: Any) -> RuntimeEvent:
    return RuntimeEvent(
        type=EventType.MODEL_RESPONSE_DELTA,
        session_id="session-1",
        turn_id="turn-1",
        step_id="step-1",
        payload={"model_call_id": "call-1", "content": content},
    )


def test_model_stream_delta_keeps_whitespace_and_rejects_invalid_content() -> None:
    for text in (" ", "\n", "\n\n", "\t", "  \n"):
        assert ModelStreamDelta(text).content == text

    with pytest.raises(ValueError):
        ModelStreamDelta("")
    with pytest.raises(TypeError):
        ModelStreamDelta(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ModelStreamDelta(3)  # type: ignore[arg-type]


def test_response_delta_event_keeps_whitespace_and_rejects_invalid_content() -> None:
    for text in (" ", "\n", "\n\n", "\t", "  \n"):
        assert _delta_event(text).payload["content"] == text

    for invalid in ("", None, 3):
        with pytest.raises(ValueError):
            _delta_event(invalid)

    # Identity fields keep the strict whitespace rule.
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.MODEL_RESPONSE_DELTA,
            session_id=" ",
            turn_id="turn-1",
            step_id="step-1",
            payload={"model_call_id": "call-1", "content": "text"},
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.MODEL_RESPONSE_DELTA,
            session_id="session-1",
            turn_id="turn-1",
            step_id="step-1",
            payload={"model_call_id": " ", "content": "text"},
        )


class _Prompt:
    bottom_toolbar = None
    app = SimpleNamespace(invalidate=lambda: None)


def test_agent_output_keeps_whitespace_deltas_visible() -> None:
    buffer = StringIO()
    output = AgentOutput(
        Console(file=buffer, width=80, color_system=None), markdown=False
    )
    output.attach_prompt(_Prompt())
    output.turn_started("session-1")

    pieces = ["我目前可以使用以下技能（Skills）：", " ", "/", "\n", "\n", "- 机器人\n"]
    for piece in pieces:
        output.event(_delta_event(piece))

    expected = "".join(pieces)
    assert "".join(output._stream_parts) == expected
    assert output.display.stream_text == expected

    output.event(
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_COMPLETED,
            session_id="session-1",
            turn_id="turn-1",
            step_id="step-1",
            payload={
                "model_call_id": "call-1",
                "content": expected,
                "streaming": "native",
                "usage": {},
            },
        )
    )
    assert output._stream_final_text == expected


class _WhitespaceStreamAdapter:
    def __init__(self) -> None:
        self.content = " / 回答"

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise AssertionError("stream must be used")

    def stream(self, request: ModelRequest):
        return self._events()

    async def _events(self):
        yield ModelStreamDelta(" ")
        yield ModelStreamDelta("/")
        yield ModelStreamDelta(" 回答")
        yield ModelResponse(content=self.content, finish_reason="stop")


async def test_loop_persists_a_standalone_whitespace_delta() -> None:
    store = SQLiteSessionStore(":memory:")
    driver = LoopDriverImpl(
        assembler=PromptAssemblerImpl("system"),
        adapter=_WhitespaceStreamAdapter(),  # type: ignore[arg-type]
        executor=ToolExecutorImpl((), store),
        store=store,
        model="fake-model",
    )
    runtime = AgentRuntimeImpl(driver=driver, store=store, max_steps=2)
    try:
        result = await runtime.submit(TurnRequest(session_id="ws:loop", text="hi"))

        assert result.status is TurnStatus.COMPLETED
        assert result.reply == " / 回答"
        events = await store.read_session("ws:loop")
        deltas = [
            event.payload["content"]
            for event in events
            if event.type is EventType.MODEL_RESPONSE_DELTA
        ]
        # The first delta is flushed immediately, so the space is its own
        # durable event and is not folded into the following text.
        assert deltas[0] == " "
        assert "".join(deltas) == " / 回答"
    finally:
        await store.aclose()


class _SSE(httpx.AsyncByteStream):
    """One SSE data frame per payload."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads
        self.closed = False

    async def __aiter__(self):
        for payload in self._payloads:
            yield f"data: {json.dumps(payload)}\n\n".encode()

    async def aclose(self) -> None:
        self.closed = True


def _chunk(
    content: str | None = None,
    *,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-whitespace",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content is not None else {},
                "finish_reason": finish_reason,
            }
        ],
    }


async def test_whitespace_deltas_survive_real_sdk_stream_and_durable_reread(
    tmp_path,
) -> None:
    prefix = "我目前可以使用以下技能（Skills）："
    markdown = "**机器人 / 具身智能**\n\n- 移动\n- 抓取\n"
    pieces = [prefix, " ", "/", "\n", "\n", markdown]
    expected = "".join(pieces)

    payloads = [
        _chunk(piece)
        for piece in pieces[:-1]
    ] + [
        _chunk(pieces[-1], finish_reason="stop"),
        {
            "id": "chatcmpl-whitespace",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fake-model",
            "choices": [],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        },
    ]
    stream = _SSE(payloads)
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    client = AsyncOpenAI(
        api_key="test-key",
        base_url="http://test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = CustomProvider(api_key="test-key", api_base="http://test/v1")
    provider._client = client
    selection = RuntimeSelection(
        "custom", "fake-model", None, "http://test/v1", provider, 128000
    )
    wrapper = _SelectedProvider(selection, 128000, 4096)
    path = tmp_path / "sessions.db"
    store = SQLiteSessionStore(path)
    driver = LoopDriverImpl(
        assembler=PromptAssemblerImpl("system"),
        adapter=ModelAdapter(wrapper),
        executor=ToolExecutorImpl((), store),
        store=store,
        model="fake-model",
    )
    runtime = AgentRuntimeImpl(driver=driver, store=store, max_steps=2)
    host = AgentHostImpl(runtime=runtime, capabilities=())
    try:
        result = await host.submit(TurnRequest(session_id="ws:test", text="hi"))

        assert result.status is TurnStatus.COMPLETED
        assert result.reply == expected
        assert len(posts) == 1
        assert stream.closed

        events = await store.read_session("ws:test")
        deltas = [
            event.payload["content"]
            for event in events
            if event.type is EventType.MODEL_RESPONSE_DELTA
        ]
        assert "".join(deltas) == expected
        assert EventType.TOOL_INTENT not in {event.type for event in events}
        completed = [
            event
            for event in events
            if event.type is EventType.MODEL_ATTEMPT_COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].payload["streaming"] == "native"
        assert completed[0].payload["content"] == expected
        assert completed[0].payload["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
        history = await store.read_conversation("ws:test")
        assert history[-1] == {"role": "assistant", "content": expected}
    finally:
        await host.aclose()
        await client.close()

    # A fresh store reader sees the same deltas and answer.
    reread = SQLiteSessionStore(path)
    try:
        events = await reread.read_session("ws:test")
        deltas = [
            event.payload["content"]
            for event in events
            if event.type is EventType.MODEL_RESPONSE_DELTA
        ]
        assert "".join(deltas) == expected
        history = await reread.read_conversation("ws:test")
        assert history[-1] == {"role": "assistant", "content": expected}
    finally:
        await reread.aclose()

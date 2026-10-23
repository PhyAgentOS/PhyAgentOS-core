"""Integration tests: the real CustomProvider against scripts/fake_llm.py."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from PhyAgentOS.providers.custom_provider import CustomProvider

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fake_llm.py"
FIRST_TEXT = "正在检查文件..."
FINAL_TEXT = "README.md 已读取，内容完整，验收通过。"
TOOL_CALL_ID = "call_fake_1"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _first_round() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "check README"}]


def _second_round() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "check README"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": TOOL_CALL_ID,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "README.md"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": TOOL_CALL_ID, "content": "hello"},
    ]


@pytest.fixture(scope="module")
def fake_server_url() -> Iterator[str]:
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--port",
            str(port),
            "--chunk-delay",
            "0",
            "--final-delay",
            "0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{url}/v1/models", timeout=0.2).read()
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("fake_llm did not start")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@asynccontextmanager
async def _provider(base_url: str) -> AsyncIterator[CustomProvider]:
    provider = CustomProvider(
        api_key="test-key",
        api_base=f"{base_url}/v1",
        default_model="fake-model",
    )
    try:
        yield provider
    finally:
        await provider._client.close()


def _text(events: list[Any]) -> str:
    return "".join(event.content or "" for event in events if event.type == "delta")


async def test_stream_first_round_emits_text_and_complete_tool_call(
    fake_server_url: str,
) -> None:
    async with _provider(fake_server_url) as provider:
        events = [
            event
            async for event in provider.chat_stream(_first_round(), tools=TOOLS)
        ]

    assert _text(events) == FIRST_TEXT
    assert not [event for event in events if event.type == "error"]
    tool_events = [event for event in events if event.type == "tool_call"]
    assert len(tool_events) == 1
    tool_call = tool_events[0].tool_call
    assert tool_call.id == TOOL_CALL_ID
    assert tool_call.name == "read_file"
    assert tool_call.arguments == {"path": "README.md"}

    done = events[-1]
    assert done.type == "done"
    assert done.finish_reason == "tool_calls"
    assert done.usage == {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }


async def test_stream_second_round_after_tool_result_emits_text_and_done(
    fake_server_url: str,
) -> None:
    async with _provider(fake_server_url) as provider:
        events = [
            event
            async for event in provider.chat_stream(_second_round(), tools=TOOLS)
        ]

    assert _text(events) == FINAL_TEXT
    assert not [event for event in events if event.type in ("tool_call", "error")]
    done = events[-1]
    assert done.type == "done"
    assert done.finish_reason == "stop"
    assert done.usage == {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }


async def test_non_stream_chat_matches_stream(fake_server_url: str) -> None:
    async with _provider(fake_server_url) as provider:
        for messages, expected_finish in (
            (_first_round(), "tool_calls"),
            (_second_round(), "stop"),
        ):
            streamed = [
                event
                async for event in provider.chat_stream(messages, tools=TOOLS)
            ]
            direct = await provider.chat(messages, tools=TOOLS)

            assert direct.content == _text(streamed)
            assert direct.finish_reason == expected_finish
            assert streamed[-1].type == "done"
            assert streamed[-1].finish_reason == expected_finish
            assert direct.usage == streamed[-1].usage

            streamed_calls = [
                event.tool_call for event in streamed if event.type == "tool_call"
            ]
            assert [(call.id, call.name, call.arguments) for call in direct.tool_calls] == [
                (call.id, call.name, call.arguments) for call in streamed_calls
            ]

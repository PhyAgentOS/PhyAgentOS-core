"""Tests for scripts/fake_llm.py against stream and non-stream requests."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fake_llm.py"
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--port", str(port), "--chunk-delay", "0", "--final-delay", "0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{url}/v1/models", timeout=0.2).read()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("fake_llm did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=5)


def _post(url: str, payload: dict[str, Any]) -> tuple[str, bytes]:
    request = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.headers.get("Content-Type", ""), response.read()


def _first_round(stream: bool) -> dict[str, Any]:
    return {"model": "fake-model", "messages": [{"role": "user", "content": "check README"}], "stream": stream, "tools": TOOLS}


def _second_round(stream: bool) -> dict[str, Any]:
    return {
        "model": "fake-model",
        "messages": [
            {"role": "user", "content": "check README"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_fake_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "README.md"}'}}]},
            {"role": "tool", "tool_call_id": "call_fake_1", "content": "hello"},
        ],
        "stream": stream,
        "tools": TOOLS,
    }


def _sse_chunks(text: str) -> list[dict[str, Any]]:
    events = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    assert events and events[-1] == "[DONE]"
    return [chunk for chunk in map(json.loads, events[:-1]) if chunk["choices"]]


def test_non_stream_round_one_returns_tool_call(base_url: str) -> None:
    content_type, raw = _post(base_url, _first_round(False))
    assert content_type.startswith("application/json")
    message = json.loads(raw)["choices"][0]["message"]
    assert message["content"] == "正在检查文件..."
    call = message["tool_calls"][0]
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "README.md"}


def test_non_stream_round_two_returns_text(base_url: str) -> None:
    _, raw = _post(base_url, _second_round(False))
    message = json.loads(raw)["choices"][0]["message"]
    assert message["content"]
    assert "tool_calls" not in message


def test_stream_round_one_emits_text_and_tool_call(base_url: str) -> None:
    content_type, raw = _post(base_url, _first_round(True))
    assert content_type.startswith("text/event-stream")
    chunks = _sse_chunks(raw.decode())
    text = "".join(chunk["choices"][0]["delta"].get("content") or "" for chunk in chunks)
    assert text == "正在检查文件..."
    arguments = "".join(
        fragment["function"].get("arguments") or ""
        for chunk in chunks
        for fragment in chunk["choices"][0]["delta"].get("tool_calls") or []
    )
    assert json.loads(arguments) == {"path": "README.md"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_round_two_emits_text_only(base_url: str) -> None:
    content_type, raw = _post(base_url, _second_round(True))
    assert content_type.startswith("text/event-stream")
    chunks = _sse_chunks(raw.decode())
    text = "".join(chunk["choices"][0]["delta"].get("content") or "" for chunk in chunks)
    assert text
    assert not any(chunk["choices"][0]["delta"].get("tool_calls") for chunk in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

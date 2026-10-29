"""B4: bound oversized tool results before they enter the model context."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from PhyAgentOS.agent_legacy.loop import AgentLoop
from PhyAgentOS.agent_legacy.tool_results import DEFAULT_MAX_CHARS, ToolResultLimiter
from PhyAgentOS.providers.base import LLMProvider, StreamEvent, ToolCallRequest
from PhyAgentOS.utils.helpers import estimate_prompt_tokens_chain


class _RecordingBus:
    def __init__(self) -> None:
        self.outbound: list = []

    async def publish_outbound(self, message) -> None:
        self.outbound.append(message)


class _StreamingProvider(LLMProvider):
    def __init__(self, scripts: list[list[StreamEvent]]):
        super().__init__()
        self._scripts = [list(script) for script in scripts]
        self.requests: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat(self, *args, **kwargs):
        raise AssertionError("chat() should not be used when chat_stream exists")

    async def chat_stream(self, **kwargs):
        self.requests.append(deepcopy(kwargs["messages"]))
        assert self._scripts, "unexpected extra chat_stream call"
        for event in self._scripts.pop(0):
            yield event


def _loop(provider, tmp_path, *, context_window_tokens: int = 10**9) -> AgentLoop:
    return AgentLoop(
        bus=_RecordingBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=context_window_tokens,
    )


def _tool_call_script(tool_call_id: str, name: str) -> list[StreamEvent]:
    return [
        StreamEvent(
            type="tool_call",
            tool_call=ToolCallRequest(
                id=tool_call_id, name=name, arguments={"path": "big.txt"},
            ),
        ),
        StreamEvent(type="done", finish_reason="tool_calls"),
    ]


def test_long_text_is_truncated_and_saved(tmp_path) -> None:
    limiter = ToolResultLimiter(tmp_path)
    body = "A" * 20_000 + "TAIL"

    limited = limiter.limit("call-1", body)

    assert len(limited) <= DEFAULT_MAX_CHARS
    assert limited.startswith("A" * 100)
    assert limited.endswith("TAIL")
    assert ".paos/tool_outputs/call-1-" in limited
    saved = _saved_files(tmp_path, "call-1")
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == body


def test_long_json_keeps_critical_fields(tmp_path) -> None:
    payload = {
        "status": "stopped",
        "error": "gateway rejected the action",
        "invocation_id": "inv-1",
        "attempt_id": "att-1",
        "task_id": "task-1",
        "session_id": "session-1",
        "remote_state": {"state": "unknown"},
        "evidence_refs": ["invocation:inv-1"],
        "result": "R" * 20_000,
        "logs": "L" * 20_000,
    }
    raw = json.dumps(payload, ensure_ascii=False)

    limited = ToolResultLimiter(tmp_path).limit("call-2", raw)

    assert len(limited) <= DEFAULT_MAX_CHARS
    data = json.loads(limited)
    assert data["_truncated"]["original_path"].startswith(
        ".paos/tool_outputs/call-2-"
    )
    for key in (
        "status", "error", "invocation_id", "attempt_id",
        "task_id", "session_id", "remote_state", "evidence_refs",
    ):
        assert data[key] == payload[key]
    assert "truncated" in data["result"]
    assert "truncated" in data["logs"]
    saved = _saved_files(tmp_path, "call-2")
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == raw


def test_forge_result_keeps_safety_fields_when_nested(tmp_path) -> None:
    payload = {
        "ok": False,
        "data": {
            "task_id": "task-9",
            "status": "unknown",
            "error": "remote attempt lost",
            "invocation_id": "inv-9",
            "attempt_id": "att-9",
            "remote_state": {"status": "unknown", "detail": "no heartbeat"},
            "evidence_refs": ["invocation:inv-9"],
            "plan": "P" * 30_000,
        },
    }

    limited = ToolResultLimiter(tmp_path).limit("call-3", json.dumps(payload))

    assert len(limited) <= DEFAULT_MAX_CHARS
    data = json.loads(limited)["data"]
    assert data["status"] == "unknown"
    assert data["error"] == "remote attempt lost"
    assert data["invocation_id"] == "inv-9"
    assert data["attempt_id"] == "att-9"
    assert data["task_id"] == "task-9"
    assert data["remote_state"] == payload["data"]["remote_state"]
    assert data["evidence_refs"] == ["invocation:inv-9"]
    assert "truncated" in data["plan"]


def test_short_result_is_unchanged_and_not_saved(tmp_path) -> None:
    limiter = ToolResultLimiter(tmp_path)

    assert limiter.limit("call-4", "small body") == "small body"
    assert not (tmp_path / ".paos").exists()


def test_write_failure_falls_back_to_truncation(tmp_path, monkeypatch) -> None:
    limiter = ToolResultLimiter(tmp_path)

    def fail_write(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail_write)

    limited = limiter.limit("call-5", "X" * 20_000)

    assert len(limited) <= DEFAULT_MAX_CHARS
    assert limited.startswith("X" * 100)
    assert "full output unavailable" in limited


def test_oversized_preserved_fields_still_fit(tmp_path) -> None:
    payload = {
        "status": "unknown",
        "error": "E" * 20_000,
        "remote_state": {"state": "unknown", "detail": "D" * 20_000},
        "evidence_refs": [f"invocation:{index}" for index in range(5_000)],
        "invocation_id": "inv-big",
        "attempt_id": "att-big",
        "task_id": "task-big",
        "session_id": "session-big",
        "logs": "L" * 20_000,
    }

    limited = ToolResultLimiter(tmp_path).limit("call-6", json.dumps(payload))

    assert len(limited) <= DEFAULT_MAX_CHARS
    data = json.loads(limited)
    assert data["status"] == "unknown"
    assert data["invocation_id"] == "inv-big"
    assert data["attempt_id"] == "att-big"
    assert data["task_id"] == "task-big"
    assert data["session_id"] == "session-big"
    assert data["_truncated"]["original_chars"] == len(json.dumps(payload))


def test_same_tool_call_id_with_different_content_keeps_both_files(tmp_path) -> None:
    limiter = ToolResultLimiter(tmp_path)
    first = "A" * 9_000
    second = "B" * 9_000

    limiter.limit("shared-call", first)
    limiter.limit("shared-call", second)

    saved = _saved_files(tmp_path, "shared-call")
    assert len(saved) == 2
    assert {path.read_text(encoding="utf-8") for path in saved} == {first, second}


async def test_limited_result_fits_the_budget_that_original_exceeds(
    tmp_path, monkeypatch
) -> None:
    long_text = "budget line\n" * 20_000
    provider = _StreamingProvider([
        _tool_call_script("call-big", "read_file"),
        [StreamEvent(type="delta", content="answer"), StreamEvent(type="done")],
    ])
    loop = _loop(provider, tmp_path)

    limited = loop.tool_results.limit("call-big", long_text)
    limited_messages = loop.context.build_messages(
        history=[], current_message="go", channel="cli", chat_id="test"
    )
    loop.context.add_assistant_message(
        limited_messages, None, [_tool_call_message("call-big")],
    )
    loop.context.add_tool_result(
        limited_messages, "call-big", "read_file", limited,
    )
    unlimited_messages = loop.context.build_messages(
        history=[], current_message="go", channel="cli", chat_id="test"
    )
    loop.context.add_assistant_message(
        unlimited_messages, None, [_tool_call_message("call-big")],
    )
    loop.context.add_tool_result(
        unlimited_messages, "call-big", "read_file", long_text,
    )

    tool_defs = loop.tools.get_definitions()
    estimated, _ = estimate_prompt_tokens_chain(
        provider, loop.model, limited_messages, tool_defs
    )
    reserve = provider.generation.max_tokens
    loop.context_window_tokens = estimated + reserve + 64

    output_budget, budget_error = loop._request_output_budget(
        provider, loop.model, unlimited_messages, tool_defs
    )
    assert budget_error is not None or (output_budget is not None and output_budget < reserve)

    async def fake_execute(name, params):
        return long_text

    monkeypatch.setattr(loop.tools, "execute", fake_execute)

    assert await loop.process_direct("go", "cli:test") == "answer"

    tool_messages = [
        message for message in provider.requests[1] if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-big"
    assert len(tool_messages[0]["content"]) <= DEFAULT_MAX_CHARS


def _saved_files(tmp_path, tool_call_id: str) -> list[Path]:
    return sorted(
        (tmp_path / ".paos" / "tool_outputs").glob(f"{tool_call_id}-*.txt")
    )


def _tool_call_message(tool_call_id: str) -> dict:
    return {
        "id": tool_call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "big.txt"})},
    }

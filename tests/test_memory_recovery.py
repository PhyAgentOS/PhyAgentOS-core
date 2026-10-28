"""Memory failures must neither overflow requests nor discard unarchived data."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from PhyAgentOS.agent.budget import fit_history, request_budget
from PhyAgentOS.agent.loop import AgentLoop
from PhyAgentOS.agent.memory import MemoryConsolidator, MemoryStore
from PhyAgentOS.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest
from PhyAgentOS.session.manager import SessionManager
from PhyAgentOS.utils import helpers


class ArchivalProvider(LLMProvider):
    def __init__(self):
        super().__init__()
        self.generation = GenerationSettings(max_tokens=128, reasoning_effort="high")
        self.calls: list[dict] = []
        self.fail_at: int | None = None
        self.result: LLMResponse | None = None

    def get_default_model(self):
        return "test-thinking"

    def estimate_prompt_tokens(self, messages, tools, model):
        return max(1, len(json.dumps([messages, tools], ensure_ascii=False)) // 4), "test-counter"

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs.get("tool_choice") != "required", "thinking mode rejects forced tools"
        if self.fail_at == len(self.calls):
            return LLMResponse("secret backend error", finish_reason="error")
        if self.result is not None:
            return self.result
        if kwargs.get("tools") and kwargs["tools"][0]["function"]["name"] == "save_memory":
            return LLMResponse(None, tool_calls=[ToolCallRequest("save", "save_memory", {
                "history_entry": "Preserved facts", "memory_update": "Updated facts",
            })])
        return LLMResponse("Answer")


def consolidator(tmp_path, provider, window=1024):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("cli:test")
    c = MemoryConsolidator(
        tmp_path, provider, provider.get_default_model(), sessions, window,
        lambda history, current_message, **kw: [*history, {"role": "user", "content": current_message}],
        lambda: [],
    )
    return c, session


async def test_thinking_mode_archive_uses_auto_and_preserves_generation(tmp_path):
    p = ArchivalProvider()
    store = MemoryStore(tmp_path)
    assert await store.consolidate([{"role": "user", "content": "remember"}], p, p.get_default_model())
    assert p.calls[0]["tool_choice"] == "auto"
    assert p.calls[0]["reasoning_effort"] is None
    assert p.generation.reasoning_effort == "high"
    assert store.read_long_term() == "Updated facts"


@pytest.mark.parametrize("response", [
    LLMResponse("provider secret", finish_reason="error"),
    LLMResponse("plain text"),
    LLMResponse(None, tool_calls=[ToolCallRequest("bad", "wrong_tool", {"history_entry": "x", "memory_update": "y"})]),
    LLMResponse(None, tool_calls=[ToolCallRequest("bad", "save_memory", {})]),
    LLMResponse(None, tool_calls=[ToolCallRequest("bad", "save_memory", {"history_entry": "x", "memory_update": 3})]),
    LLMResponse(None, tool_calls=[ToolCallRequest("bad", "save_memory", {"history_entry": "", "memory_update": "x"})]),
])
async def test_invalid_archive_does_not_write_or_advance(tmp_path, response):
    p = ArchivalProvider()
    p.result = response
    c, s = consolidator(tmp_path, p)
    s.add_message("user", "hello")
    assert not await c.archive_unconsolidated(s)
    assert s.last_consolidated == 0
    assert not c.store.history_file.exists()
    assert not c.store.memory_file.exists()
    assert c.store.last_error
    assert "secret" not in c.store.last_error


async def test_large_single_message_split_without_loss_or_reordering(tmp_path):
    p = ArchivalProvider()
    c, s = consolidator(tmp_path, p)
    s.add_message("user", "中文🦾" * 6000)
    s.add_message("assistant", None, tool_calls=[{"id": "t", "function": {"name": "move", "arguments": "{}"}}])
    s.add_message("tool", "done", tool_call_id="t")
    original = c.store._format_messages(s.messages)
    assert await c.archive_unconsolidated(s)
    assert len(p.calls) > 2
    fragments = [call["messages"][1]["content"].split("\n## Conversation to Process\n", 1)[1] for call in p.calls]
    assert "".join(fragments) == original
    budget, _ = request_budget(p, p.get_default_model(), 1024, archival=True)
    assert all(p.estimate_prompt_tokens(call["messages"], call["tools"], call["model"])[0] <= budget for call in p.calls)
    assert s.last_consolidated == len(s.messages)


async def test_midway_failure_resumes_from_durable_offset_after_restart(tmp_path):
    p = ArchivalProvider()
    p.fail_at = 2
    c, s = consolidator(tmp_path, p)
    s.add_message("user", "history " * 2000)
    original = c.store._format_messages(s.messages)
    assert not await c.archive_unconsolidated(s)
    assert s.last_consolidated == 0
    offset = s.metadata["memory_archive_progress"]["offset"]
    assert offset > 0
    first = p.calls[0]["messages"][1]["content"].split("\n## Conversation to Process\n", 1)[1]
    q = ArchivalProvider()
    other, _ = consolidator(tmp_path, q)
    restored = other.sessions.get_or_create(s.key)
    assert await other.archive_unconsolidated(restored)
    fragments = [call["messages"][1]["content"].split("\n## Conversation to Process\n", 1)[1] for call in q.calls]
    assert first + "".join(fragments) == original
    assert restored.last_consolidated == 1


async def test_retry_after_memory_commit_before_checkpoint_is_idempotent(tmp_path, monkeypatch):
    p = ArchivalProvider()
    c, s = consolidator(tmp_path, p)
    s.add_message("user", "short history")
    original_save = c.sessions.save

    def fail_checkpoint(session):
        progress = session.metadata.get("memory_archive_progress", {})
        if progress.get("offset", 0) > 0:
            raise OSError("checkpoint unavailable")
        original_save(session)

    monkeypatch.setattr(c.sessions, "save", fail_checkpoint)
    with pytest.raises(OSError):
        await c.archive_unconsolidated(s)
    assert len(p.calls) == 1
    q = ArchivalProvider()
    other, _ = consolidator(tmp_path, q)
    restored = other.sessions.get_or_create(s.key)
    assert await other.archive_unconsolidated(restored)
    assert not q.calls
    assert other.store.history_file.read_text().count("Preserved facts") == 1


async def test_partial_file_commit_replays_without_duplicate_history(tmp_path, monkeypatch):
    p = ArchivalProvider()
    c, s = consolidator(tmp_path, p)
    s.add_message("user", "remember")
    monkeypatch.setattr(c.store, "write_long_term", lambda _: (_ for _ in ()).throw(OSError()))
    assert not await c.archive_unconsolidated(s)
    q = ArchivalProvider()
    other, _ = consolidator(tmp_path, q)
    restored = other.sessions.get_or_create(s.key)
    assert await other.archive_unconsolidated(restored)
    assert not q.calls
    assert other.store.history_file.read_text().count("Preserved facts") == 1
    assert other.store.read_long_term() == "Updated facts"


async def test_failure_notice_and_cooldown_leave_original_history_intact(tmp_path):
    p = ArchivalProvider()
    p.fail_at = 1
    c, s = consolidator(tmp_path, p)
    s.add_message("user", "x" * 6000)
    s.add_message("assistant", "ok")
    s.add_message("user", "latest")
    assert "Memory archival" in await c.maybe_consolidate_by_tokens(s)
    assert "Memory archival" in await c.maybe_consolidate_by_tokens(s)
    assert len(p.calls) == 1
    assert s.last_consolidated == 0
    assert len(s.messages) == 3


def test_tokenizer_failure_has_nonzero_fallback_including_tool_arguments(monkeypatch):
    monkeypatch.setattr(helpers, "tiktoken", None)
    payload = [{"role": "assistant", "content": None, "tool_calls": [{"arguments": "中" * 1000}]}]
    count, source = helpers.estimate_prompt_tokens_chain(object(), "test", payload)
    assert count >= 3000
    assert source == "tiktoken"  # chain remains compatible; protection never returns zero


def test_request_budget_reserves_output_and_clamps_known_model(monkeypatch):
    import litellm
    monkeypatch.setitem(litellm.model_cost, "test-thinking", {"max_input_tokens": 2048})
    p = ArchivalProvider()
    budget, output = request_budget(p, "test-thinking", 10000)
    assert budget + output < 2048


def test_trim_keeps_complete_latest_tool_exchange_without_mutating_history():
    p = ArchivalProvider()
    messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "x" * 8000},
                {"role": "assistant", "content": "old"}, {"role": "user", "content": "move"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
                {"role": "tool", "tool_call_id": "a", "content": "done"}]
    result, reduced = fit_history(messages, p, "test-thinking", 300, [])
    assert reduced and result == [messages[0], *messages[3:]]
    assert len(messages) == 6
    with pytest.raises(ValueError, match="Current turn"):
        fit_history(result, p, "test-thinking", 1, [])


class Bus:
    async def publish_outbound(self, message):
        pass


def agent(tmp_path, p):
    loop = AgentLoop(bus=Bus(), provider=p, workspace=tmp_path, context_window_tokens=4096)
    loop.tools.get_definitions = lambda: []
    return loop


async def test_new_starts_after_archival_failure_and_keeps_raw_backup(tmp_path):
    p = ArchivalProvider()
    loop = agent(tmp_path, p)
    s = loop.sessions.get_or_create("cli:test")
    s.add_message("user", "important fact")
    loop.memory_consolidator.archive_unconsolidated = AsyncMock(return_value=False)
    reply = await loop.process_direct("/new", session_key=s.key)
    assert reply.startswith("New session started.") and "backed up" in reply
    backups = list((tmp_path / "sessions" / "archives").glob("*.jsonl"))
    assert len(backups) == 1 and "important fact" in backups[0].read_text()
    assert not loop.sessions.get_or_create(s.key).messages


async def test_new_does_not_clear_when_backup_fails(tmp_path, monkeypatch):
    p = ArchivalProvider()
    loop = agent(tmp_path, p)
    s = loop.sessions.get_or_create("cli:test")
    s.add_message("user", "important fact")
    monkeypatch.setattr(loop.sessions, "backup", lambda _: (_ for _ in ()).throw(OSError()))
    reply = await loop.process_direct("/new", session_key=s.key)
    assert "backup failed" in reply and "not cleared" in reply
    assert len(s.messages) == 1


async def test_failed_memory_is_visible_in_final_reply_and_main_request_is_bounded(tmp_path):
    p = ArchivalProvider()
    loop = agent(tmp_path, p)
    s = loop.sessions.get_or_create("cli:test")
    s.add_message("user", "old " * 6000)
    s.add_message("assistant", "done")
    loop.memory_consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value="Memory archival unavailable")
    reply = await loop.process_direct("hello", session_key=s.key)
    assert "Answer" in reply and "Memory archival unavailable" in reply
    budget, _ = request_budget(p, p.get_default_model(), loop.context_window_tokens)
    assert all(p.estimate_prompt_tokens(call["messages"], call["tools"], call["model"])[0] <= budget for call in p.calls)
    assert s.messages[0]["content"] == "old " * 6000


async def test_guard_checks_each_iteration_after_large_tool_output(tmp_path):
    p = ArchivalProvider()
    p.result = LLMResponse(None, tool_calls=[ToolCallRequest("t", "test_tool", {})])
    loop = agent(tmp_path, p)
    loop.tools.execute = AsyncMock(return_value="huge " * 20000)
    loop.memory_consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=None)
    reply = await loop.process_direct("use tool", session_key="cli:test")
    assert "context budget" in reply
    assert len(p.calls) == 1
    assert loop.tools.execute.await_count == 1


async def test_committed_receipt_can_finish_after_budget_is_reduced(tmp_path, monkeypatch):
    p = ArchivalProvider()
    c, s = consolidator(tmp_path, p)
    s.add_message("user", "short history")
    save = c.sessions.save

    def fail_checkpoint(session):
        if session.metadata.get("memory_archive_progress", {}).get("offset", 0):
            raise OSError()
        save(session)

    monkeypatch.setattr(c.sessions, "save", fail_checkpoint)
    with pytest.raises(OSError):
        await c.archive_unconsolidated(s)
    q = ArchivalProvider()
    other, _ = consolidator(tmp_path, q, window=1)
    restored = other.sessions.get_or_create(s.key)
    assert await other.archive_unconsolidated(restored)
    assert not q.calls
    assert restored.last_consolidated == 1


async def test_archive_rejects_changed_history_instead_of_skipping_data(tmp_path):
    p = ArchivalProvider()
    p.fail_at = 1
    c, s = consolidator(tmp_path, p)
    s.add_message("user", "original")
    assert not await c.archive_unconsolidated(s)
    s.messages[0]["content"] = "changed"
    assert not await c.archive_unconsolidated(s)
    assert s.last_consolidated == 0
    assert "changed" in c.store.last_error


async def test_empty_new_session_does_not_call_model(tmp_path):
    p = ArchivalProvider()
    loop = agent(tmp_path, p)
    assert await loop.process_direct("/new", session_key="cli:test") == "New session started."
    assert not p.calls


async def test_consolidation_starts_before_main_output_reserve_is_exhausted(tmp_path):
    p = ArchivalProvider()
    p.generation = GenerationSettings(max_tokens=1500)
    c, s = consolidator(tmp_path, p, window=4096)
    s.add_message("user", "old " * 2400)
    s.add_message("assistant", "done")
    s.add_message("user", "latest")
    estimated, _ = c.estimate_session_prompt_tokens(s)
    budget, _ = request_budget(p, p.get_default_model(), 4096)
    assert budget < estimated < 4096
    await c.maybe_consolidate_by_tokens(s)
    assert p.calls
    assert s.last_consolidated > 0


async def test_custom_provider_real_request_accepts_thinking_auto_tools(tmp_path):
    import httpx
    from openai import AsyncOpenAI

    from PhyAgentOS.providers.custom_provider import CustomProvider

    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("tool_choice") == "required":
            return httpx.Response(400, json={"error": {"message": "Thinking mode does not support this tool_choice", "type": "invalid_request_error"}})
        return httpx.Response(200, json={
            "id": "response", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "save", "type": "function", "function": {
                    "name": "save_memory", "arguments": json.dumps({"history_entry": "Fact", "memory_update": "Fact"}),
                }}],
            }}],
        })

    p = CustomProvider(default_model="deepseek-v4.1-flash")
    await p._client.close()
    p._client = AsyncOpenAI(api_key="test", max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        store = MemoryStore(tmp_path)
        assert await store.consolidate([{"role": "user", "content": "remember"}], p, p.default_model)
        assert bodies[0]["tool_choice"] == "auto"
        assert store.read_long_term() == "Fact"
    finally:
        await p._client.close()

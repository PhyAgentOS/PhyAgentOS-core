"""Session compression: local summaries instead of long-term memory writes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from PhyAgentOS.agent.loop import AgentLoop
from PhyAgentOS.agent.memory import MemoryConsolidator
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest
from PhyAgentOS.session.manager import Session, SessionManager

_SUMMARY_MARKER = "compress the older messages"


def _seed(session: Session, marker: str = "seed", turns: int = 3, pad: int = 40) -> None:
    for index in range(turns):
        session.add_message("user", f"{marker}-user-{index} " + "x" * pad)
        session.add_message("assistant", f"{marker}-assistant-{index} " + "y" * pad)


def _consolidator(
    workspace: Path,
    provider: object,
    *,
    context_window_tokens: int = 100,
    sessions: SessionManager | None = None,
) -> MemoryConsolidator:
    return MemoryConsolidator(
        workspace=workspace,
        provider=provider,  # type: ignore[arg-type]
        model="stub-model",
        sessions=sessions or SessionManager(workspace),
        context_window_tokens=context_window_tokens,
        build_messages=lambda history, **_: [
            {"role": "system", "content": "S"},
            *history,
        ],
        get_tool_definitions=lambda: [],
    )


class _SummaryProvider:
    def __init__(self, summary: str = "## Current goal\nfinish the task", *, fail: bool = False):
        self.summary = summary
        self.fail = fail
        self.calls: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub-model"

    def estimate_prompt_tokens(self, messages, tools, model):
        chars = sum(len(str(message.get("content") or "")) for message in messages)
        return chars, "chars"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        system = str(kwargs["messages"][0].get("content") or "")
        if _SUMMARY_MARKER not in system.lower():
            raise AssertionError("unexpected non-summary call")
        self.calls.append(kwargs["messages"])
        if self.fail:
            raise RuntimeError("summary backend down")
        return LLMResponse(content=self.summary)


async def test_over_budget_compresses_into_session_only(tmp_path):
    provider = _SummaryProvider(
        "## Current goal\nPick the red block\n"
        "## Constraints\nKeep the left arm clear\n"
        "## Unfinished work\nPlace the block"
    )
    consolidator = _consolidator(tmp_path, provider)
    session = consolidator.sessions.get_or_create("cli:one")
    _seed(session, "task")

    await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated == 4
    assert "Pick the red block" in session.summary
    assert not (tmp_path / "memory" / "MEMORY.md").exists()

    history = session.get_history(max_messages=0)
    assert history[0]["role"] == "user"
    assert "Pick the red block" in history[0]["content"]
    assert "Physical state must not be inferred from this summary" in history[0]["content"]
    assert "Forge records" in history[0]["content"]
    assert [message["role"] for message in history[1:]] == ["user", "assistant"]

    system_prompt, user_prompt = provider.calls[0][0]["content"], provider.calls[0][1][
        "content"
    ]
    for section in (
        "Current goal",
        "Constraints",
        "Unfinished work",
        "Key tools and results",
        "Physical state",
    ):
        assert section in system_prompt
    assert "TaskStore" in system_prompt
    assert "task-user-0" in user_prompt

    reloaded = SessionManager(tmp_path).get_or_create("cli:one")
    assert reloaded.summary == session.summary
    assert reloaded.last_consolidated == 4


async def test_failed_summary_does_not_advance_and_next_attempt_retries(tmp_path):
    provider = _SummaryProvider("## Current goal\nrecovered", fail=True)
    consolidator = _consolidator(tmp_path, provider)
    session = consolidator.sessions.get_or_create("cli:retry")
    _seed(session)

    await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated == 0
    assert session.summary == ""
    assert len(provider.calls) == 1
    assert not (tmp_path / "memory" / "MEMORY.md").exists()

    provider.fail = False
    await consolidator.maybe_consolidate_by_tokens(session)

    assert len(provider.calls) == 2
    assert session.last_consolidated == 4
    assert "recovered" in session.summary


async def test_empty_summary_does_not_advance(tmp_path):
    provider = _SummaryProvider("   ")
    consolidator = _consolidator(tmp_path, provider)
    session = consolidator.sessions.get_or_create("cli:empty")
    _seed(session)

    await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated == 0
    assert session.summary == ""


async def test_persist_failure_rolls_back_the_offset(tmp_path, monkeypatch):
    provider = _SummaryProvider("## Current goal\nwill not persist")
    consolidator = _consolidator(tmp_path, provider)
    session = consolidator.sessions.get_or_create("cli:disk")
    _seed(session)

    def broken_save(_session):
        raise OSError("disk full")

    monkeypatch.setattr(consolidator.sessions, "save", broken_save)

    await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated == 0
    assert session.summary == ""


class _ChatProvider:
    def __init__(self) -> None:
        self.summary_calls = 0
        self.turn_calls: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub-model"

    def estimate_prompt_tokens(self, messages, tools, model):
        chars = sum(len(str(message.get("content") or "")) for message in messages)
        return chars, "chars"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        system = str(kwargs["messages"][0].get("content") or "")
        if _SUMMARY_MARKER in system.lower():
            self.summary_calls += 1
            return LLMResponse(content="## Current goal\ncontinue the seeded task")
        self.turn_calls.append(kwargs["messages"])
        last = str(kwargs["messages"][-1].get("content") or "").split("\n\n")[-1]
        return LLMResponse(content=f"reply:{last}")


async def test_compressed_session_continues_with_summary_and_latest_turn(tmp_path):
    provider = _ChatProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,  # type: ignore[arg-type]
        workspace=tmp_path,
        context_window_tokens=14_000,
    )
    session = loop.sessions.get_or_create("cli:sess")
    _seed(session, "seed", pad=1600)
    loop.sessions.save(session)

    reply = await loop.process_direct("next step", "cli:sess")

    assert reply == "reply:next step"
    assert provider.summary_calls >= 1
    session = loop.sessions.get_or_create("cli:sess")
    assert "continue the seeded task" in session.summary
    contents = [message.get("content") for message in session.messages]
    assert contents.count("next step") == 1

    history = session.get_history(max_messages=0)
    assert history[0]["role"] == "user"
    assert "continue the seeded task" in history[0]["content"]
    assert history[-2]["content"] == "next step"

    first_turn = provider.turn_calls[0]
    assert "continue the seeded task" in first_turn[1]["content"]
    current_user = [
        message
        for message in first_turn
        if message.get("role") == "user" and "next step" in str(message.get("content"))
    ]
    assert len(current_user) == 1


class _PerSessionSummaryProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub-model"

    def estimate_prompt_tokens(self, messages, tools, model):
        chars = sum(len(str(message.get("content") or "")) for message in messages)
        return chars, "chars"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        system = str(kwargs["messages"][0].get("content") or "")
        if _SUMMARY_MARKER not in system.lower():
            raise AssertionError("unexpected non-summary call")
        user = str(kwargs["messages"][1].get("content") or "")
        marker = "fact-A" if "fact-A" in user else "fact-B"
        self.calls += 1
        await asyncio.sleep(0.01)
        return LLMResponse(content=f"## Current goal\nsummarized {marker}")


async def test_two_sessions_compress_independently(tmp_path):
    provider = _PerSessionSummaryProvider()
    consolidator = _consolidator(tmp_path, provider)
    first = consolidator.sessions.get_or_create("cli:a")
    second = consolidator.sessions.get_or_create("cli:b")
    _seed(first, "fact-A")
    _seed(second, "fact-B")

    await asyncio.gather(
        consolidator.maybe_consolidate_by_tokens(first),
        consolidator.maybe_consolidate_by_tokens(second),
    )

    assert provider.calls == 2
    assert "fact-A" in first.summary and "fact-B" not in first.summary
    assert "fact-B" in second.summary and "fact-A" not in second.summary
    assert not (tmp_path / "memory" / "MEMORY.md").exists()

    reloaded = SessionManager(tmp_path)
    assert "fact-A" in reloaded.get_or_create("cli:a").summary
    assert "fact-B" in reloaded.get_or_create("cli:b").summary


class _ArchiveProvider:
    def __init__(self, summary: str = "## Current goal\ncompressed detail") -> None:
        self.calls = 0
        self.summary_calls = 0
        self.summary = summary

    def get_default_model(self) -> str:
        return "stub-model"

    def estimate_prompt_tokens(self, messages, tools, model):
        chars = sum(len(str(message.get("content") or "")) for message in messages)
        return chars, "chars"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        system = str(kwargs["messages"][0].get("content") or "")
        if _SUMMARY_MARKER in system.lower():
            self.summary_calls += 1
            return LLMResponse(content=self.summary)
        self.calls += 1
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id=f"call-{self.calls}",
                    name="save_memory",
                    arguments={
                        "history_entry": "archived old turn",
                        "memory_update": "Long-term fact from /new",
                    },
                )
            ],
        )


async def test_new_archives_compressed_summary_exactly_once(tmp_path):
    provider = _ArchiveProvider(
        "## Current goal\nCompressed-only detail: ALPHA"
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,  # type: ignore[arg-type]
        workspace=tmp_path,
        context_window_tokens=14_000,
    )
    session = loop.sessions.get_or_create("cli:summary")
    _seed(session, "old", pad=1600)
    loop.sessions.save(session)
    await loop.memory_consolidator.maybe_consolidate_by_tokens(session)
    assert "Compressed-only detail: ALPHA" in session.summary
    assert session.last_consolidated == 4

    reply = await loop.process_direct("/new", "cli:summary")

    assert reply == "New session started."
    history_file = tmp_path / "memory" / "HISTORY.md"
    history = history_file.read_text(encoding="utf-8")
    assert history.count("Compressed-only detail: ALPHA") == 1
    assert "archived old turn" in history
    assert "Long-term fact from /new" in (
        tmp_path / "memory" / "MEMORY.md"
    ).read_text(encoding="utf-8")
    session = loop.sessions.get_or_create("cli:summary")
    assert session.messages == []
    assert session.summary == ""

    # A retry that still carries the summary must not append it again.
    session.summary = "## Current goal\nCompressed-only detail: ALPHA"
    assert await loop.memory_consolidator.archive_unconsolidated(session) is True
    assert history_file.read_text(encoding="utf-8").count(
        "Compressed-only detail: ALPHA"
    ) == 1


async def test_failed_summary_archive_keeps_summary_for_retry(tmp_path, monkeypatch):
    provider = _ArchiveProvider("## Current goal\nretry me")
    consolidator = _consolidator(tmp_path, provider)
    session = consolidator.sessions.get_or_create("cli:retry-archive")
    _seed(session)
    await consolidator.maybe_consolidate_by_tokens(session)
    assert "retry me" in session.summary
    session.last_consolidated = len(session.messages)
    consolidator.sessions.save(session)

    original_append = consolidator.store.append_history

    def broken_append(_entry: str) -> None:
        raise OSError("history is read-only")

    monkeypatch.setattr(consolidator.store, "append_history", broken_append)

    assert await consolidator.archive_unconsolidated(session) is False
    assert "retry me" in session.summary

    monkeypatch.setattr(consolidator.store, "append_history", original_append)
    assert await consolidator.archive_unconsolidated(session) is True
    assert session.summary == ""
    assert (tmp_path / "memory" / "HISTORY.md").read_text(encoding="utf-8").count(
        "retry me"
    ) == 1


async def test_new_still_archives_to_long_term_memory(tmp_path):
    provider = _ArchiveProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,  # type: ignore[arg-type]
        workspace=tmp_path,
        context_window_tokens=100,
    )
    session = loop.sessions.get_or_create("cli:new")
    _seed(session, "old")
    loop.sessions.save(session)

    reply = await loop.process_direct("/new", "cli:new")

    assert reply == "New session started."
    memory = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "Long-term fact from /new" in memory
    session = loop.sessions.get_or_create("cli:new")
    assert session.messages == []
    assert session.summary == ""
    assert session.last_consolidated == 0


def test_codex_conversion_keeps_main_system_prompt_with_summary():
    from PhyAgentOS.providers.openai_codex_provider import _convert_messages

    session = Session(key="cli:codex")
    session.add_message("user", "old question")
    session.add_message("assistant", "old answer")
    session.summary = "## Current goal\ncontinue the task"
    session.last_consolidated = 2

    history = session.get_history(max_messages=0)
    assert history[0]["role"] == "user"
    assert "<session_summary>" in history[0]["content"]

    system_prompt, input_items = _convert_messages(
        [{"role": "system", "content": "MAIN SYSTEM PROMPT"}, *history]
    )

    assert system_prompt == "MAIN SYSTEM PROMPT"
    assert "<session_summary>" in input_items[0]["content"][0]["text"]


class _BandProvider:
    generation = SimpleNamespace(max_tokens=50)

    def get_default_model(self) -> str:
        return "stub-model"

    def estimate_prompt_tokens(self, messages, tools, model):
        chars = sum(len(str(message.get("content") or "")) for message in messages)
        return chars, "chars"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        system = str(kwargs["messages"][0].get("content") or "")
        if _SUMMARY_MARKER not in system.lower():
            raise AssertionError("unexpected non-summary call")
        return LLMResponse(content="## Current goal\nband summary")


async def test_prompt_inside_output_reserve_band_triggers_compression(tmp_path):
    provider = _BandProvider()
    consolidator = _consolidator(tmp_path, provider, context_window_tokens=200)
    session = consolidator.sessions.get_or_create("cli:band")
    session.add_message("user", "u" * 40)
    session.add_message("assistant", "a" * 40)
    session.add_message("user", "u" * 40)
    session.add_message("assistant", "a" * 40)

    estimated, _ = consolidator.estimate_session_prompt_tokens(session)
    assert 150 < estimated < 200

    await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated == 2
    assert "band summary" in session.summary


def test_session_save_failure_keeps_the_old_file(tmp_path, monkeypatch):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("cli:atomic")
    session.add_message("user", "old content")
    sessions.save(session)
    path = sessions._get_session_path("cli:atomic")
    before = path.read_text(encoding="utf-8")

    session.add_message("assistant", "new content")

    def broken_replace(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr("PhyAgentOS.session.manager.os.replace", broken_replace)

    with pytest.raises(OSError):
        sessions.save(session)

    assert path.read_text(encoding="utf-8") == before
    assert list(sessions.sessions_dir.glob("*.tmp")) == []


async def test_identical_summaries_from_two_sessions_are_both_archived(tmp_path):
    provider = _ArchiveProvider("## Current goal\nidentical")
    consolidator = _consolidator(tmp_path, provider)
    summary = "## Current goal\nidentical"

    for key in ("cli:first", "cli:second"):
        session = consolidator.sessions.get_or_create(key)
        _seed(session)
        session.summary = summary
        session.last_consolidated = len(session.messages)
        consolidator.sessions.save(session)
        assert await consolidator.archive_unconsolidated(session) is True

    history = (tmp_path / "memory" / "HISTORY.md").read_text(encoding="utf-8")
    assert history.count("## Current goal\nidentical") == 2

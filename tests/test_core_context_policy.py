"""Automatic context pressure, deterministic trimming and bounded summary.

The tests use a fake provider and a real SQLite store. Token numbers are the
deterministic character estimate injected below, not measured provider savings.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.session import CHECKPOINT_SUMMARY_NOTICE, SQLiteSessionStore
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolDefinition,
)
from PhyAgentOS.agent.services.context_policy import ContextPolicy, ContextReduction

SESSION = "cli:context-policy"


def _context(step: str = "t:step:1") -> ExecutionContext:
    return ExecutionContext(
        session_id=SESSION,
        turn_id="t",
        step_id=step,
        cancellation=CancellationToken(),
        budget=Budget(max_steps=8),
    )


def _tool(name: str = "echo", *, schema_pad: int = 0) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Tool " + name + ("D" * schema_pad),
        parameters={"type": "object", "properties": {}},
    )


def _message_chars(message: Any) -> int:
    content = message.get("content")
    total = len(str(message.get("role") or ""))
    if isinstance(content, str):
        total += len(content)
    elif content is not None:
        total += len(json.dumps(content, ensure_ascii=False, default=str))
    for key in ("tool_calls", "tool_call_id", "name"):
        value = message.get(key)
        if value is not None:
            total += len(json.dumps(value, ensure_ascii=False, default=str))
    return total


def _estimator(messages, tools) -> int:
    chars = sum(_message_chars(message) for message in messages)
    chars += sum(len(json.dumps(tool.to_provider_dict())) for tool in tools)
    return max(1, math.ceil(chars / 4))


class FakeProvider:
    def __init__(
        self,
        reply: str = "## Objective\nkeep going",
        *,
        error: Exception | None = None,
        counter=None,
        usage: dict[str, int] | None = None,
        finish_reason: str = "stop",
    ) -> None:
        self.reply = reply
        self.error = error
        self.counter = counter
        self.usage = {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        } if usage is None else dict(usage)
        self.finish_reason = finish_reason
        self.calls: list[dict[str, Any]] = []
        self.counter_calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            content=self.reply,
            finish_reason=self.finish_reason,
            usage=dict(self.usage),
            tool_calls=[],
        )

    def estimate_prompt_tokens(self, messages, tools, model):
        self.counter_calls.append(
            {"messages": messages, "tools": tools, "model": model}
        )
        return self.counter(messages, tools) if self.counter is not None else 0


def _policy(store: SQLiteSessionStore, provider, **overrides) -> ContextPolicy:
    values = {
        "model": "fake-model",
        "capacity": 100_000,
        "reserve_tokens": 1_000,
        "threshold": 1_000,
        "system_instructions": "SYS",
        "estimator": _estimator,
        "provider_name": "fake",
        "effort": "high",
    }
    values.update(overrides)
    return ContextPolicy(store, provider, **values)


async def _snapshot(store: SQLiteSessionStore, session: str = SESSION) -> list[tuple]:
    return [
        (event.sequence, event.type, dict(event.payload))
        for event in await store.read_session(session)
    ]


async def _seed_turn(
    store: SQLiteSessionStore,
    turn_id: str,
    history: list[dict[str, Any]],
    user_text: str,
    reply: str,
) -> list[dict[str, Any]]:
    messages = [
        {"role": "system", "content": "SYS"},
        *history,
        {"role": "user", "content": user_text},
    ]
    await store.append(
        RuntimeEvent(
            type=EventType.TURN_STARTED,
            session_id=SESSION,
            turn_id=turn_id,
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.REQUEST_PREPARED,
            session_id=SESSION,
            turn_id=turn_id,
            step_id="step-1",
            payload={"model_call_id": f"m-{turn_id}", "messages": messages, "tools": []},
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.TURN_COMPLETED,
            session_id=SESSION,
            turn_id=turn_id,
            payload={"reply": reply},
        )
    )
    return [*history, {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply}]


# -- pressure gate -----------------------------------------------------


@pytest.mark.asyncio
async def test_below_threshold_does_nothing(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider()
        policy = _policy(store, provider)
        history = [{"role": "user", "content": "small task"}]

        assert await policy.prepare(_context(), history, [_tool()]) is None

        assert provider.calls == []
        assert await store.list_compaction_attempts(SESSION) == ()
        assert await _snapshot(store) == []
        assert await store.read_checkpoint(SESSION) is None
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_below_threshold_preflight_does_not_call_model(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        await _seed_turn(store, "turn-1", [], "short task", "done")
        before = await _snapshot(store)
        provider = FakeProvider()
        policy = _policy(store, provider, threshold=10_000)

        assert await policy.preflight(SESSION) is None

        assert provider.calls == []
        assert await _snapshot(store) == before
        assert await store.read_checkpoint(SESSION) is None
    finally:
        await store.aclose()


# -- deterministic trim ------------------------------------------------


@pytest.mark.asyncio
async def test_huge_current_tool_result_is_shortened_and_user_task_stays(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        payload = {
            "ok": True,
            "data": {
                "task_id": "task-1",
                "revision_id": "rev-1",
                "status": "succeeded",
                "blob": "Z" * 20_000,
            },
            "safety_notice": "do not bypass the guard",
            "error": "old non-fatal warning",
        }
        history = [
            {"role": "user", "content": "CURRENT TASK: pick the cube"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call-1", "type": "function",
                                "function": {"name": "forge_tool_query", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "forge_tool_query",
                "content": json.dumps(payload),
            },
        ]
        original = json.dumps(payload)
        provider = FakeProvider()
        policy = _policy(store, provider)

        result = await policy.prepare(_context(), history, [_tool()])

        assert isinstance(result, ContextReduction)
        assert result.reason == "tool_text_trimmed"
        assert result.after_tokens < result.before_tokens
        assert provider.calls == []
        assert len(result.messages) == 3
        assert result.messages[0] == {"role": "user", "content": "CURRENT TASK: pick the cube"}
        assert result.messages[1]["tool_calls"][0]["id"] == "call-1"
        assert result.messages[2]["tool_call_id"] == "call-1"
        replacement = json.loads(result.messages[2]["content"])
        assert len(result.messages[2]["content"]) < len(original)
        assert replacement["truncated_tool_text"]["original_chars"] == len(original)
        assert len(replacement["truncated_tool_text"]["sha256"]) == 64
        assert replacement["truncated_tool_text"]["full_text"] == "session event log"
        assert replacement["kept"]["domain_refs"]["task_id"] == "task-1"
        assert replacement["kept"]["domain_refs"]["revision_id"] == "rev-1"
        assert replacement["kept"]["state"]["status"] == "succeeded"
        assert any("safety_notice" in item for item in replacement["kept"]["safety"])
        assert any("error" in item for item in replacement["kept"]["safety"])
        # The caller's history is not edited.
        assert history[2]["content"] == original
        assert await _snapshot(store) == []
        assert await store.list_compaction_attempts(SESSION) == ()
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_visual_tool_result_is_not_replaced_with_a_fake_path(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        content = json.dumps(
            {"ok": True, "data": {"image_url": "/tmp/frame.png", "blob": "Z" * 20_000}}
        )
        history = [
            {"role": "user", "content": "look at the frame"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "c1", "name": "camera", "content": content},
        ]
        policy = _policy(store, FakeProvider())

        assert await policy.prepare(_context(), history, []) is None
        assert history[2]["content"] == content
        assert "/tmp/frame.png" in history[2]["content"]
    finally:
        await store.aclose()


# -- summary -----------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_keeps_current_task_and_tool_pair_without_checkpoint(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider("## Objective\nFinish the cube task.")
        policy = _policy(store, provider)
        history = [
            {"role": "user", "content": "FIRST TASK: " + "x" * 6_000},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "CURRENT TASK: pick the cube"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "echo", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        before_events = await _snapshot(store)

        result = await policy.prepare(_context(), history, [])

        assert isinstance(result, ContextReduction)
        assert result.reason == "prefix_summarized"
        assert result.after_tokens < result.before_tokens
        assert provider.calls[0]["kwargs"] == {
            "model": "fake-model",
            "reasoning_effort": "high",
        }
        assert provider.calls[0]["tools"] is None
        assert CHECKPOINT_SUMMARY_NOTICE in result.messages[0]["content"]
        assert result.messages[1]["role"] == "user"
        assert result.messages[1]["content"].startswith("CURRENT TASK")
        assert any(message.get("role") == "tool" for message in result.messages)
        # No checkpoint and no session event: the turn owner persists the result.
        assert await store.read_checkpoint(SESSION) is None
        assert await _snapshot(store) == before_events
        attempts = await store.list_compaction_attempts(SESSION)
        assert [attempt.status for attempt in attempts] == ["completed"]
        assert attempts[0].model == "fake-model"
        assert policy.drain_usage() == (
            {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )
        assert policy.drain_usage() == ()
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_summary_failure_keeps_the_deterministic_trim(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider(error=RuntimeError("provider down"))
        policy = _policy(store, provider)
        history = [
            {"role": "user", "content": "FIRST TASK: " + "x" * 4_000},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "CURRENT TASK: " + "y" * 2_000},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "echo", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "Z" * 12_000},
        ]
        before_events = await _snapshot(store)

        result = await policy.prepare(_context(), history, [])

        assert isinstance(result, ContextReduction)
        assert result.reason.startswith("tool_text_trimmed;summary_failed:")
        assert result.after_tokens < result.before_tokens
        assert result.messages[0]["content"].startswith("FIRST TASK")
        assert result.messages[2]["content"].startswith("CURRENT TASK")
        assert "truncated_tool_text" in result.messages[4]["content"]
        assert await store.read_checkpoint(SESSION) is None
        assert await _snapshot(store) == before_events
        attempts = await store.list_compaction_attempts(SESSION)
        assert [attempt.status for attempt in attempts] == ["failed"]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_failed_summary_without_any_reduction_returns_none(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider(error=RuntimeError("provider down"))
        policy = _policy(store, provider, threshold=1)
        history = [
            {"role": "user", "content": "first task"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "current task"},
        ]

        assert await policy.prepare(_context(), history, []) is None

        assert await store.read_checkpoint(SESSION) is None
        attempts = await store.list_compaction_attempts(SESSION)
        assert [attempt.status for attempt in attempts] == ["failed"]
        assert policy.drain_usage() == ({},)
        assert await _snapshot(store) == []
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_cancel_during_summary_interrupts_and_propagates(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider(error=asyncio.CancelledError())
        policy = _policy(store, provider)
        history = [
            {"role": "user", "content": "FIRST TASK: " + "x" * 4_000},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "CURRENT TASK: " + "y" * 2_000},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "echo", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "Z" * 12_000},
        ]
        before_events = await _snapshot(store)

        with pytest.raises(asyncio.CancelledError):
            await policy.prepare(_context(), history, [])

        attempts = await store.list_compaction_attempts(SESSION)
        assert [attempt.status for attempt in attempts] == ["interrupted"]
        assert policy.drain_usage() == ({},)
        assert await store.read_checkpoint(SESSION) is None
        assert await _snapshot(store) == before_events
    finally:
        await store.aclose()


# -- forced overflow and bounded work ---------------------------------


@pytest.mark.asyncio
async def test_force_recovers_below_threshold_with_one_trim(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider()
        policy = _policy(store, provider, threshold=0.8)
        history = [
            {"role": "user", "content": "CURRENT TASK"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "c1", "content": "Z" * 12_000},
        ]

        assert await policy.prepare(_context(), history, []) is None  # below 80k
        result = await policy.prepare(_context(), history, [], force=True)

        assert isinstance(result, ContextReduction)
        assert result.reason == "tool_text_trimmed"
        assert provider.calls == []
        assert len(provider.calls) <= 1
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_force_with_nothing_to_reduce_returns_none_without_model(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider()
        policy = _policy(store, provider)
        history = [
            {"role": "user", "content": "one task"},
            {"role": "assistant", "content": "one answer"},
        ]

        assert await policy.prepare(_context(), history, [], force=True) is None
        assert provider.calls == []
        assert await store.list_compaction_attempts(SESSION) == ()
    finally:
        await store.aclose()


# -- estimator and preflight -----------------------------------------


@pytest.mark.asyncio
async def test_default_estimator_counts_system_and_tool_schema(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        def counter(messages, tools) -> int:
            return 10 + sum(len(json.dumps(item)) for item in messages) // 100 + len(
                json.dumps(tools or [])
            ) // 50

        provider = FakeProvider(counter=counter)
        policy = ContextPolicy(
            store,
            provider,
            model="fake-model",
            capacity=100_000,
            reserve_tokens=1_000,
            threshold=0.8,
            system_instructions="SYSTEM " + "S" * 500,
        )
        history = [{"role": "user", "content": "task"}]

        without = policy.estimated_tokens(history, [])
        with_tool = policy.estimated_tokens(history, [_tool(schema_pad=2_000)])

        assert with_tool > without
        assert provider.counter_calls
        seen_messages = provider.counter_calls[-1]["messages"]
        assert seen_messages[0]["role"] == "system"
        assert seen_messages[0]["content"].startswith("SYSTEM")
        assert provider.counter_calls[-1]["tools"][0]["function"]["name"] == "echo"
        assert provider.counter_calls[-1]["model"] == "fake-model"
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_preflight_uses_compact_session_between_turns_and_commits(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        history: list[dict[str, Any]] = []
        history = await _seed_turn(store, "turn-1", history, "first task " + "x" * 300, "one")
        history = await _seed_turn(store, "turn-2", history, "second task", "two")
        history = await _seed_turn(store, "turn-3", history, "third task", "three")
        before_events = await _snapshot(store)
        provider = FakeProvider("short summary")
        policy = _policy(store, provider, threshold=10)

        result = await policy.preflight(SESSION)

        assert isinstance(result, ContextReduction)
        assert result.reason == "preflight_checkpoint"
        assert result.after_tokens < result.before_tokens
        assert any(
            message.get("content") == "third task" for message in result.messages
        )
        checkpoint = await store.read_checkpoint(SESSION)
        assert checkpoint is not None
        assert checkpoint.summary == "short summary"
        assert await _snapshot(store) == before_events
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_request_overhead_catches_live_fragment_pressure(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider()
        policy = _policy(
            store,
            provider,
            threshold=4_000,
            request_overhead=lambda context: 1_500,
        )
        history = [
            {"role": "user", "content": "CURRENT TASK"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "c1", "content": "Z" * 12_000},
        ]
        base = policy.estimated_tokens(history, [])
        assert base < policy.limit_tokens

        result = await policy.prepare(_context(), history, [])

        assert isinstance(result, ContextReduction)
        assert result.before_tokens >= base + 1_500
        assert result.after_tokens < result.before_tokens
        assert "truncated_tool_text" in result.messages[-1]["content"]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_request_messages_counts_live_runtime_overhead_exactly(tmp_path: Path) -> None:
    from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl, is_runtime_context_message

    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        def live(context: ExecutionContext) -> str:
            return "FORGE BLOCKER: " + "B" * 20_000

        assembler = PromptAssemblerImpl("SYS", fragments=[live], cache_stable=True)
        history = [
            {"role": "user", "content": "CURRENT TASK: pick the cube"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "c1", "content": "Z" * 12_000},
        ]
        context = _context()
        bundle = await assembler.assemble(context, history=history, tools=[_tool()])
        policy = _policy(store, FakeProvider(), threshold=5_000)
        history_only = policy.estimated_tokens(history, bundle.tools)
        assert history_only < policy.limit_tokens

        result = await policy.prepare(
            context,
            history,
            bundle.tools,
            request_messages=bundle.messages,
        )

        assert isinstance(result, ContextReduction)
        assert result.before_tokens > history_only + 4_000
        assert result.after_tokens < result.before_tokens
        assert not any(is_runtime_context_message(m) for m in result.messages)
        assert not any(m.get("role") == "system" for m in result.messages)
        assert not any("FORGE BLOCKER" in str(m.get("content") or "") for m in result.messages)
        assert "truncated_tool_text" in result.messages[-1]["content"]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_summary_never_absorbs_live_runtime_text(tmp_path: Path) -> None:
    from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl

    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        def live(context: ExecutionContext) -> str:
            return "FORGE BLOCKER: keep this live"

        assembler = PromptAssemblerImpl("SYS", fragments=[live], cache_stable=True)
        history = [
            {"role": "user", "content": "FIRST TASK: " + "x" * 6_000},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "CURRENT TASK: pick the cube"},
        ]
        context = _context()
        bundle = await assembler.assemble(context, history=history, tools=[])
        provider = FakeProvider("short summary")
        policy = _policy(store, provider, threshold=1_000)

        result = await policy.prepare(
            context, history, bundle.tools, request_messages=bundle.messages
        )

        assert isinstance(result, ContextReduction)
        assert result.reason == "prefix_summarized"
        assert provider.calls
        summary_input = provider.calls[0]["messages"]
        assert "FORGE BLOCKER: keep this live" not in json.dumps(summary_input)
        assert any(
            message.get("content") == "CURRENT TASK: pick the cube"
            for message in result.messages
        )
        assert not any("FORGE BLOCKER" in str(m.get("content") or "") for m in result.messages)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_replayed_runtime_tail_is_not_summarized_or_returned(tmp_path: Path) -> None:
    from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl

    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        assembler = PromptAssemblerImpl(
            "SYS",
            fragments=[lambda context: "STALE FORGE BLOCKER"],
            cache_stable=True,
        )
        old_request = await assembler.assemble(
            _context("old:step:1"),
            history=[{"role": "user", "content": "FIRST TASK: " + "x" * 6_000}],
            tools=[],
        )
        runtime_tail = dict(old_request.messages[-1])
        history = [
            {"role": "user", "content": "FIRST TASK: " + "x" * 6_000},
            {"role": "assistant", "content": "first answer"},
            runtime_tail,
            {"role": "user", "content": "CURRENT TASK: pick the cube"},
        ]
        provider = FakeProvider("short summary")
        policy = _policy(store, provider, threshold=1_000)

        result = await policy.prepare(_context(), history, [])

        assert isinstance(result, ContextReduction)
        assert "STALE FORGE BLOCKER" not in json.dumps(provider.calls[0]["messages"])
        assert not any("STALE FORGE BLOCKER" in str(m.get("content") or "") for m in result.messages)
        assert any(
            message.get("content") == "CURRENT TASK: pick the cube"
            for message in result.messages
        )
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_trim_win_keeps_usage_from_a_failed_summary_call(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider("y" * 40_000)
        policy = _policy(store, provider)
        history = [
            {"role": "user", "content": "FIRST TASK: " + "x" * 4_000},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "CURRENT TASK: " + "y" * 2_000},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "echo", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "Z" * 12_000},
        ]

        result = await policy.prepare(_context(), history, [])

        assert isinstance(result, ContextReduction)
        assert result.reason.startswith("tool_text_trimmed;summary_failed:")
        assert policy.drain_usage() == (
            {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )
        assert policy.drain_usage() == ()
        attempts = await store.list_compaction_attempts(SESSION)
        assert [attempt.status for attempt in attempts] == ["failed"]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_protected_current_task_and_later_corrections_stay_out_of_summary(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider("short summary")
        policy = _policy(store, provider, threshold=1)
        history = [
            {"role": "user", "content": "OLD TASK: " + "x" * 5_000},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "CURRENT TASK: go left"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "move", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "moved one step"},
            {"role": "user", "content": "CORRECTION: go left now"},
        ]

        result = await policy.prepare(
            _context(), history, [], protected_user_text="CURRENT TASK: go left"
        )

        assert isinstance(result, ContextReduction)
        assert result.reason == "prefix_summarized"
        contents = [str(message.get("content") or "") for message in result.messages]
        assert "CURRENT TASK: go left" in contents
        assert "moved one step" in contents
        assert "CORRECTION: go left now" in contents
        summary_input = json.dumps(provider.calls[0]["messages"])
        assert "CURRENT TASK: go left" not in summary_input
        assert "CORRECTION: go left now" not in summary_input
        assert "moved one step" not in summary_input
        assert "OLD TASK" in summary_input
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_protected_current_task_with_no_safe_prefix_fails_as_none(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider("short summary")
        policy = _policy(store, provider, threshold=1)
        history = [
            {"role": "user", "content": "CURRENT TASK: go left"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "move", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "moved one step"},
            {"role": "user", "content": "CORRECTION: go right"},
        ]

        assert await policy.prepare(
            _context(), history, [], protected_user_text="CURRENT TASK: go left"
        ) is None
        assert provider.calls == []
        assert await store.read_checkpoint(SESSION) is None
        assert await store.list_compaction_attempts(SESSION) == ()
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_repeated_protected_text_keeps_more_history_instead_of_dropping_it(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "core.db")
    try:
        provider = FakeProvider("short summary")
        policy = _policy(store, provider, threshold=1)
        history = [
            {"role": "user", "content": "SAME TASK"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "SAME TASK"},
            {"role": "assistant", "content": "current answer"},
        ]

        assert await policy.prepare(
            _context(), history, [], protected_user_text="SAME TASK"
        ) is None
        assert provider.calls == []
    finally:
        await store.aclose()

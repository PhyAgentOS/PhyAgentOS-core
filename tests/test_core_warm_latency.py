"""Warm same-process turn path: startup read, prompt, request, first delta.

The provider is fake and deterministic: it emits one one-character delta and
then a terminal event with no delay. The store, adapter, assembler, driver and
runtime are the production classes, so the event order here is the real order.
Timing is recorded but not asserted against a machine-dependent threshold; the
external probe in /tmp uses the same marks for before/after comparison.
"""

from __future__ import annotations

import time
from pathlib import Path

from PhyAgentOS.agent.core import (
    AgentRuntimeImpl,
    EventType,
    LoopDriverImpl,
    ModelAdapterImpl,
    PromptAssemblerImpl,
    SQLiteSessionStore,
    ToolExecutorImpl,
    TurnRequest,
    TurnStatus,
)
from PhyAgentOS.providers.base import StreamEvent

SESSION = "warm"


class _FakeStreamProvider:
    supports_native_stream = True

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def chat_with_retry(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("chat_with_retry must not be used when streaming")

    async def chat_stream(self, *, messages, **_kwargs):
        self.calls.append([dict(message) for message in messages])
        yield StreamEvent(type="delta", content="x")
        yield StreamEvent(type="done", finish_reason="stop", usage={"prompt_tokens": 1})


class _TimedAssembler(PromptAssemblerImpl):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.last_done_ns: int | None = None

    async def assemble(self, *args, **kwargs):
        bundle = await super().assemble(*args, **kwargs)
        self.last_done_ns = time.perf_counter_ns()
        return bundle


class _WarmStore(SQLiteSessionStore):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.snapshot_calls = 0
        self.conversation_calls = 0
        self.marks: dict[str, int] = {}
        self.first_delta_sequence: int | None = None
        self.terminal_sequence: int | None = None
        self.last_startup_events = ()

    async def read_startup_snapshot(self, session_id):
        self.snapshot_calls += 1
        snapshot = await super().read_startup_snapshot(session_id)
        self.marks["startup_done"] = time.perf_counter_ns()
        return snapshot

    async def read_conversation(self, session_id):
        self.conversation_calls += 1
        return await super().read_conversation(session_id)

    def _startup_rows_sync(self, session_id):
        result = super()._startup_rows_sync(session_id)
        self.last_startup_events = result[0]
        return result

    async def append(self, event):
        saved = await super().append(event)
        now = time.perf_counter_ns()
        if event.type is EventType.REQUEST_PREPARED:
            self.marks.setdefault("request_done", now)
        elif (
            event.type is EventType.MODEL_RESPONSE_DELTA
            and self.first_delta_sequence is None
        ):
            self.first_delta_sequence = saved.sequence
            self.marks["first_delta_done"] = now
            # The CLI renders only after the retained event is shown; this marks
            # the same point in the core probe.
            self.marks["output_done"] = now
        elif event.type is EventType.TURN_COMPLETED:
            self.terminal_sequence = saved.sequence
        return saved


def _runtime(store, provider):
    assembler = _TimedAssembler("warm system prompt")
    driver = LoopDriverImpl(
        assembler=assembler,
        adapter=ModelAdapterImpl(provider),
        executor=ToolExecutorImpl((), store),
        store=store,
        tools=(),
        model="fake-model",
    )
    return AgentRuntimeImpl(driver=driver, store=store, max_steps=2), assembler


async def test_warm_turns_use_one_snapshot_and_persist_the_first_delta(
    tmp_path: Path,
) -> None:
    store = _WarmStore(tmp_path / "warm.db")
    provider = _FakeStreamProvider()
    runtime, assembler = _runtime(store, provider)

    for turn_no in range(4):
        store.marks = {}
        store.first_delta_sequence = None
        store.terminal_sequence = None
        assembler.last_done_ns = None
        start = time.perf_counter_ns()
        result = await runtime.submit(
            TurnRequest(session_id=SESSION, text=f"turn-{turn_no}")
        )
        assert result.status is TurnStatus.COMPLETED
        assert assembler.last_done_ns is not None
        marks = {"t0": start, **store.marks}
        assert marks["startup_done"] <= assembler.last_done_ns <= marks["request_done"]
        assert marks["request_done"] <= marks["first_delta_done"]
        assert marks["output_done"] == marks["first_delta_done"]
        # A one-character delta is retained immediately, not held until the
        # 64-character batch threshold or a later delta arrives.
        assert store.first_delta_sequence is not None
        assert store.first_delta_sequence < store.terminal_sequence

    assert store.snapshot_calls == 4
    assert store.conversation_calls == 0

    # Warm means later prompts see earlier completed turns, not a fresh session.
    assert len(provider.calls) == 4
    second = provider.calls[1]
    assert {"role": "user", "content": "turn-0"} in second
    assert {"role": "assistant", "content": "x"} in second

    # The startup read still skips streaming deltas.
    assert all(
        event.type is not EventType.MODEL_RESPONSE_DELTA
        for event in store.last_startup_events
    )
    await store.aclose()

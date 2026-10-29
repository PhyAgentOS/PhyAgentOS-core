"""Cross-session concurrency: per-session serialization, bounded overlap, and shared state."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from PhyAgentOS.agent_legacy.loop import AgentLoop, SessionQueueFullError
from PhyAgentOS.agent_legacy.memory import MemoryConsolidator, MemoryStore
from PhyAgentOS.agent_legacy.tools.forge_task import ForgeTaskCreateTool
from PhyAgentOS.bus.events import InboundMessage
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.binding import ForgeSkillBinding
from PhyAgentOS.forge.task import (
    AgentTaskBusyError,
    AgentTaskCoordinator,
    AgentTaskRecord,
    PlanRevision,
)
from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest
from PhyAgentOS.session.manager import SessionManager
from PhyAgentOS.verification.contracts import TaskVerificationContract


def _loop(tmp_path, provider, **kwargs) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=10**9,
        **kwargs,
    )


def _drain(queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def _stop(session_key: str) -> InboundMessage:
    return InboundMessage(
        channel="cli",
        chat_id=session_key.split(":", 1)[-1],
        sender_id="user",
        content="/stop",
        session_key_override=session_key,
    )


class _GatedProvider:
    """Blocks each turn until released, tracking how many run at once."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return LLMResponse(content="done")


async def test_turns_from_two_sessions_overlap(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider, max_concurrent_turns=2)

    first = asyncio.create_task(loop.process_direct("a", "sess-a"))
    second = asyncio.create_task(loop.process_direct("b", "sess-b"))
    await asyncio.wait_for(provider.started.wait(), 2)
    await asyncio.sleep(0.05)

    assert provider.max_active == 2
    provider.release.set()
    assert await asyncio.wait_for(first, 2) == "done"
    assert await asyncio.wait_for(second, 2) == "done"


async def test_short_turn_finishes_while_long_turn_still_runs(tmp_path):
    class _LongShortProvider:
        def __init__(self) -> None:
            self.long_entered = asyncio.Event()
            self.release_long = asyncio.Event()

        def get_default_model(self) -> str:
            return "stub-model"

        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            content = str(kwargs["messages"][-1].get("content"))
            if "long" in content:
                self.long_entered.set()
                await self.release_long.wait()
                return LLMResponse(content="long")
            return LLMResponse(content="short")

    provider = _LongShortProvider()
    loop = _loop(tmp_path, provider, max_concurrent_turns=2)

    long_turn = asyncio.create_task(loop.process_direct("long", "sess-a"))
    await asyncio.wait_for(provider.long_entered.wait(), 2)
    short_turn = asyncio.create_task(loop.process_direct("short", "sess-b"))

    assert await asyncio.wait_for(short_turn, 2) == "short"
    assert not long_turn.done()

    provider.release_long.set()
    assert await asyncio.wait_for(long_turn, 2) == "long"


class _OrderProvider:
    def __init__(self) -> None:
        self.entered = 0
        self.max_active = 0
        self.first_entered = asyncio.Event()
        self.release_first = asyncio.Event()

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.entered += 1
        number = self.entered
        self.max_active = max(self.max_active, number)
        if number == 1:
            self.first_entered.set()
            await self.release_first.wait()
        return LLMResponse(content=f"reply-{number}")


async def test_semaphore_caps_cross_session_turns(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider, max_concurrent_turns=1)

    first = asyncio.create_task(loop.process_direct("a", "sess-a"))
    await asyncio.wait_for(provider.started.wait(), 2)
    second = asyncio.create_task(loop.process_direct("b", "sess-b"))
    await asyncio.sleep(0.05)

    assert provider.max_active == 1
    provider.release.set()
    assert await asyncio.wait_for(first, 2) == "done"
    assert await asyncio.wait_for(second, 2) == "done"


async def test_session_queue_limit_rejects_excess_turns(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider, per_session_queue_limit=1)

    first = asyncio.create_task(loop.process_direct("first", "sess-a"))
    await asyncio.wait_for(provider.started.wait(), 2)
    second = asyncio.create_task(loop.process_direct("second", "sess-a"))
    await asyncio.sleep(0.05)

    with pytest.raises(SessionQueueFullError, match="queued task"):
        await asyncio.wait_for(loop.process_direct("third", "sess-a"), 2)

    assert len(loop._active_tasks.get("sess-a", [])) == 2
    assert loop._session_slots["sess-a"].pending == 2

    provider.release.set()
    assert await asyncio.wait_for(first, 2) == "done"
    assert await asyncio.wait_for(second, 2) == "done"
    assert "sess-a" not in loop._session_slots


async def test_zero_queue_limit_rejects_any_waiter(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider, per_session_queue_limit=0)

    first = asyncio.create_task(loop.process_direct("first", "sess-a"))
    await asyncio.wait_for(provider.started.wait(), 2)

    with pytest.raises(SessionQueueFullError):
        await loop.process_direct("second", "sess-a")
    assert loop._session_slots["sess-a"].pending == 1

    provider.release.set()
    assert await asyncio.wait_for(first, 2) == "done"
    assert "sess-a" not in loop._session_slots


async def test_chat_turns_get_a_friendly_queue_full_reply(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider, per_session_queue_limit=0)

    first = asyncio.create_task(loop.process_direct("first", "sess-a"))
    await asyncio.wait_for(provider.started.wait(), 2)

    reply = await loop._dispatch(InboundMessage(
        channel="cli", chat_id="a", sender_id="user", content="hello",
        session_key_override="sess-a",
    ))

    assert reply is not None and "queued task(s)" in reply.content
    assert reply.metadata["queue_full"] is True
    published = [
        msg for msg in _drain(loop.bus.outbound) if msg.metadata.get("queue_full")
    ]
    assert [msg.content for msg in published] == [reply.content]

    provider.release.set()
    assert await asyncio.wait_for(first, 2) == "done"


async def test_scheduler_skips_a_queue_full_turn(tmp_path):
    from PhyAgentOS.cli import commands

    class _FullAgent:
        async def process_direct(self, **kwargs):
            raise SessionQueueFullError("Session cron:1 already has 8 queued task(s)")

    assert await commands._process_direct_for_scheduler(
        _FullAgent(), source="cron:1", content="run",
    ) is None

    job = SimpleNamespace(payload=SimpleNamespace(
        deliver=True, to="chat", channel="telegram", message="run",
    ))
    delivered = []

    class _Bus:
        async def publish_outbound(self, message):
            delivered.append(message)

    await commands._deliver_cron_response(job, None, _Bus())
    assert delivered == []


class _MemoryProvider:
    """Merges its fact into whatever MEMORY.md held when the turn started."""

    def __init__(self) -> None:
        self.calls = 0

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        prompt = kwargs["messages"][1]["content"]
        current = prompt.split("## Current Long-term Memory\n", 1)[1].split(
            "\n\n## Conversation to Process", 1
        )[0]
        conversation = prompt.split("## Conversation to Process\n", 1)[1]
        fact = "fact-A" if "fact-A" in conversation else "fact-B"
        await asyncio.sleep(0.05)
        merged = fact if current.strip() == "(empty)" else f"{current}\n{fact}"
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id=f"call-{self.calls}",
                    name="save_memory",
                    arguments={"history_entry": f"entry {fact}", "memory_update": merged},
                )
            ],
        )


async def test_concurrent_memory_consolidation_keeps_both_facts(tmp_path):
    provider = _MemoryProvider()
    consolidator = MemoryConsolidator(
        workspace=tmp_path,
        provider=provider,
        model="stub-model",
        sessions=SessionManager(tmp_path),
        context_window_tokens=10**9,
        build_messages=lambda **_: [],
        get_tool_definitions=lambda: [],
    )

    await asyncio.gather(
        consolidator.consolidate_messages([{"role": "user", "content": "fact-A"}]),
        consolidator.consolidate_messages([{"role": "user", "content": "fact-B"}]),
    )

    memory = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "fact-A" in memory
    assert "fact-B" in memory


def _consolidator(tmp_path, provider) -> MemoryConsolidator:
    return MemoryConsolidator(
        workspace=tmp_path,
        provider=provider,
        model="stub-model",
        sessions=SessionManager(tmp_path),
        context_window_tokens=10**9,
        build_messages=lambda **_: [],
        get_tool_definitions=lambda: [],
    )


async def test_memory_store_consolidate_locks_itself(tmp_path):
    provider = _MemoryProvider()
    first = MemoryStore(tmp_path)
    second = MemoryStore(tmp_path)

    await asyncio.gather(
        first.consolidate(
            [{"role": "user", "content": "fact-A"}], provider, "stub-model"
        ),
        second.consolidate(
            [{"role": "user", "content": "fact-B"}], provider, "stub-model"
        ),
    )

    memory = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "fact-A" in memory
    assert "fact-B" in memory


def _task() -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id="task_concurrency",
        task_description="concurrency test",
        verification=TaskVerificationContract(mode="off"),
        revisions=[PlanRevision(revision_id="rev_1", number=1, reason="initial")],
        active_revision_id="rev_1",
    )


class _GatedCoordinator(AgentTaskCoordinator):
    """Makes two creates reach the slot check in the same event-loop turn."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.arrived = 0
        self.gate = asyncio.Event()

    async def create_task(self, **kwargs: Any):
        self.arrived += 1
        if self.arrived == 2:
            self.gate.set()
        await self.gate.wait()
        result = super().create_task(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


async def test_forge_single_slot_rejects_second_session(tmp_path):
    coordinator = _GatedCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(),
        client=object(),  # type: ignore[arg-type]
    )
    tool = ForgeTaskCreateTool(coordinator)

    async def create(session_key: str):
        tool.set_context(session_key)
        return await tool.execute(
            task_description=session_key,
            activation_id="activation_test",
            verification={"mode": "off"},
        )

    results = await asyncio.gather(
        create("sess-a"), create("sess-b"), return_exceptions=True
    )

    assert coordinator.arrived == 2
    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, AgentTaskBusyError) for result in results) == 1
    active = coordinator.store.active()
    assert active is not None
    assert active.origin_session_key in {"sess-a", "sess-b"}


class _Activation:
    binding_candidate_id = "candidate_test"
    content_sha256 = "b" * 64


class _ActivationManager:
    def require_activation(self, **kwargs: Any) -> _Activation:
        return _Activation()


class _StubResolver:
    def __init__(self) -> None:
        self.freeze_calls: list[str] = []

    async def freeze(
        self,
        candidate_id: str,
        *,
        task_id: str,
        activated_document_sha256: str | None = None,
    ) -> ForgeSkillBinding:
        self.freeze_calls.append(task_id)
        return ForgeSkillBinding(
            binding_id=f"binding_{task_id}",
            skill_name="example-forge",
            skill_version="1.0.0",
            manifest_sha256="a" * 64,
            skill_document_sha256="b" * 64,
            runtime_profile="local",
            runtime_instance_id="runtime-test",
            gateway_url="http://127.0.0.1:9",
            required_tools=(),
        )


def _bound_coordinator(tmp_path, monkeypatch, *, fail_after_freeze: bool = False):
    resolver = _StubResolver()
    binding_ids: set[str] = set()
    coordinator = _GatedCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(),
        client=object(),  # type: ignore[arg-type]
        binding_resolver=resolver,  # type: ignore[arg-type]
        activation_manager=_ActivationManager(),
        runtime_task_binding_ids=binding_ids,
    )
    frozen_task_ids: list[str] = []

    async def fake_freeze(task):
        frozen_task_ids.append(task.task_id)
        coordinator.interaction.store.bind(task.task_id, {
            "task_id": task.task_id,
            "binding_id": task.primary_skill_binding.binding_id,
            "extension": {"test": True},
            "specs": {},
            "binding_digest": "0" * 64,
        })
        if fail_after_freeze:
            raise RuntimeError("freeze failed after writing the journal binding")

    monkeypatch.setattr(coordinator.interaction, "freeze", fake_freeze)
    return coordinator, resolver, binding_ids, frozen_task_ids


async def test_bound_creation_rolls_back_the_losing_session(tmp_path, monkeypatch):
    coordinator, resolver, binding_ids, frozen = _bound_coordinator(tmp_path, monkeypatch)
    tool = ForgeTaskCreateTool(coordinator)

    async def create(session_key: str):
        tool.set_context(session_key)
        return await tool.execute(
            task_description=session_key,
            activation_id="activation_test",
            verification={"mode": "off"},
        )

    results = await asyncio.gather(
        create("sess-a"), create("sess-b"), return_exceptions=True
    )

    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, AgentTaskBusyError) for result in results) == 1
    active = coordinator.store.active()
    assert active is not None
    assert active.origin_session_key in {"sess-a", "sess-b"}
    bindings = coordinator.interaction.store.bindings()
    assert [item["task_id"] for item in bindings] == [active.task_id]
    assert frozen == [active.task_id]
    assert binding_ids == {active.primary_skill_binding.binding_id}
    # The losing session resolved a candidate in memory but never reached freeze.
    assert len(resolver.freeze_calls) == 2


async def test_bound_creation_failure_removes_task_and_binding(tmp_path, monkeypatch):
    coordinator, _, binding_ids, frozen = _bound_coordinator(
        tmp_path, monkeypatch, fail_after_freeze=True
    )
    coordinator.gate.set()  # this case creates one task, not a pair

    with pytest.raises(RuntimeError, match="freeze failed"):
        await coordinator.create_task(
            task_description="failing bind",
            activation_id="activation_test",
            verification=TaskVerificationContract(mode="off"),
            origin_session_key="sess-a",
        )

    assert coordinator.store.active() is None
    assert coordinator.interaction.store.bindings() == []
    assert binding_ids == set()
    assert frozen  # the binding was written before the rollback


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.origins: list[str | None] = []

    async def create_task(self, **kwargs: Any) -> AgentTaskRecord:
        self.origins.append(kwargs.get("origin_session_key"))
        await asyncio.sleep(0.01)
        return _task()


class _ForgeToolProvider:
    """Returns a forge_task_create tool call for each session, gated to overlap."""

    def __init__(self) -> None:
        self.arrived = 0
        self.gate = asyncio.Event()

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        last = kwargs["messages"][-1]
        content = str(last.get("content"))
        if last.get("role") == "user" and "use-tool" in content:
            self.arrived += 1
            if self.arrived == 2:
                self.gate.set()
            await self.gate.wait()
            suffix = content.rsplit("use-tool", 1)[1].strip()[-1]
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id=f"call-{suffix}",
                        name="forge_task_create",
                        arguments={
                            "task_description": f"use-tool-{suffix}",
                            "activation_id": "activation_test",
                            "verification": {"mode": "off"},
                        },
                    )
                ],
            )
        return LLMResponse(content="done")


async def test_forge_tool_session_key_is_isolated_through_the_agent_loop(tmp_path):
    coordinator = _RecordingCoordinator()
    loop = _loop(tmp_path, _ForgeToolProvider())
    loop.tools.register(ForgeTaskCreateTool(coordinator))  # type: ignore[arg-type]

    async def turn(channel: str, chat_id: str):
        return await loop._process_message(InboundMessage(
            channel=channel, chat_id=chat_id, sender_id="user", content=f"use-tool-{chat_id}",
        ))

    await asyncio.gather(turn("telegram", "a"), turn("telegram", "b"))

    assert len(coordinator.origins) == 2
    assert set(coordinator.origins) == {"telegram:a", "telegram:b"}


class _TwoSessionProvider:
    def __init__(self) -> None:
        self.entered = {"A": asyncio.Event(), "B": asyncio.Event()}
        self.release = {"A": asyncio.Event(), "B": asyncio.Event()}

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        key = "A" if "task-A" in str(kwargs["messages"][-1]["content"]) else "B"
        self.entered[key].set()
        await self.release[key].wait()
        return LLMResponse(content=f"done-{key}")


async def test_stop_cancels_only_its_own_session(tmp_path):
    provider = _TwoSessionProvider()
    loop = _loop(tmp_path, provider, max_concurrent_turns=2)

    first = asyncio.create_task(loop.process_direct("task-A", "sess-a"))
    second = asyncio.create_task(loop.process_direct("task-B", "sess-b"))
    await asyncio.wait_for(provider.entered["A"].wait(), 2)
    await asyncio.wait_for(provider.entered["B"].wait(), 2)

    await asyncio.wait_for(loop._handle_stop(_stop("sess-a")), 2)

    assert await asyncio.wait_for(first, 2) == ""
    assert not second.done()
    provider.release["B"].set()
    assert await asyncio.wait_for(second, 2) == "done-B"


async def test_stop_finds_a_system_message_without_session_override(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider)
    msg = InboundMessage(
        channel="system", chat_id="cli:test", sender_id="subagent", content="work",
    )

    task = asyncio.create_task(loop._dispatch(msg))
    loop._register_active_task(loop._session_key_for_turn(msg), task)
    await asyncio.wait_for(provider.started.wait(), 2)
    assert "cli:test" in loop._session_slots
    assert "system:cli:test" not in loop._session_slots

    await asyncio.wait_for(
        loop._handle_stop(InboundMessage(
            channel="cli", chat_id="test", sender_id="user", content="/stop",
        )),
        2,
    )

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert "cli:test" not in loop._active_tasks
    assert "cli:test" not in loop._session_slots


async def test_cancel_while_waiting_for_the_session_lock(tmp_path):
    provider = _OrderProvider()
    loop = _loop(tmp_path, provider)

    first = asyncio.create_task(loop.process_direct("first", "sess-a"))
    await asyncio.wait_for(provider.first_entered.wait(), 2)
    second = asyncio.create_task(loop.process_direct("second", "sess-a"))
    await asyncio.sleep(0.05)
    assert loop._session_slots["sess-a"].pending == 2

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert loop._session_slots["sess-a"].pending == 1

    provider.release_first.set()
    assert await asyncio.wait_for(first, 2) == "reply-1"
    assert "sess-a" not in loop._session_slots


async def test_stop_cancels_all_queued_turns_and_releases_the_session(tmp_path):
    class _FirstTurnProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.first_entered = asyncio.Event()
            self.release_first = asyncio.Event()

        def get_default_model(self) -> str:
            return "stub-model"

        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                self.first_entered.set()
                await self.release_first.wait()
            return LLMResponse(content=f"reply-{self.calls}")

    provider = _FirstTurnProvider()
    loop = _loop(tmp_path, provider)

    first = asyncio.create_task(loop.process_direct("first", "sess-a"))
    await asyncio.wait_for(provider.first_entered.wait(), 2)
    second = asyncio.create_task(loop.process_direct("second", "sess-a"))
    await asyncio.sleep(0.05)
    assert len(loop._active_tasks["sess-a"]) == 2

    await asyncio.wait_for(loop._handle_stop(_stop("sess-a")), 2)
    provider.release_first.set()

    assert await asyncio.wait_for(first, 2) == ""
    assert await asyncio.wait_for(second, 2) == ""
    assert "sess-a" not in loop._active_tasks
    assert "sess-a" not in loop._session_slots
    assert loop._turn_semaphore._value == loop.max_concurrent_turns

    assert await asyncio.wait_for(loop.process_direct("third", "sess-a"), 2) == "reply-2"
    assert "sess-a" not in loop._session_slots


async def test_cancel_while_waiting_for_semaphore_releases_the_slot(tmp_path):
    provider = _GatedProvider()
    loop = _loop(tmp_path, provider, max_concurrent_turns=1)

    first = asyncio.create_task(loop.process_direct("a", "sess-a"))
    await asyncio.wait_for(provider.started.wait(), 2)
    second = asyncio.create_task(loop.process_direct("b", "sess-b"))
    await asyncio.sleep(0.05)
    assert not second.done()
    assert loop._turn_semaphore._value == 0

    await asyncio.wait_for(loop._handle_stop(_stop("sess-b")), 2)
    assert await asyncio.wait_for(second, 2) == ""
    assert "sess-b" not in loop._session_slots

    provider.release.set()
    assert await asyncio.wait_for(first, 2) == "done"
    assert loop._turn_semaphore._value == 1
    assert await asyncio.wait_for(loop.process_direct("c", "sess-b"), 2) == "done"


async def test_failed_turn_releases_its_slot(tmp_path):
    class _FailingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def get_default_model(self) -> str:
            return "stub-model"

        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return LLMResponse(content="recovered")

    provider = _FailingProvider()
    loop = _loop(tmp_path, provider)

    assert "encountered an error" in await loop.process_direct("first", "sess-a")
    assert "sess-a" not in loop._session_slots
    assert loop._turn_semaphore._value == loop.max_concurrent_turns
    assert await loop.process_direct("second", "sess-a") == "recovered"

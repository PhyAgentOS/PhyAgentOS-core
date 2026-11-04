"""Request-side projections: large Forge results and hidden tool stages."""

from __future__ import annotations

import asyncio
import json

import pytest

from PhyAgentOS.agent.core.prompt import (
    PromptAssemblerImpl,
    project_forge_tool_results,
    project_live_forge_reads,
)
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolDefinition,
    ToolPolicy,
)


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        session_id="s",
        turn_id="t",
        step_id="t:step:1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _stable_ctx(step: str = "t:step:1", session: str = "s") -> ExecutionContext:
    return ExecutionContext(
        session_id=session,
        turn_id="t",
        step_id=step,
        cancellation=CancellationToken(),
        budget=Budget(max_steps=8),
    )


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, description="d", parameters={"type": "object"})


def test_single_large_result_is_never_trimmed() -> None:
    payload = {
        "ok": True,
        "data": {
            "task_id": "task-1",
            "status": "succeeded",
            "goal": "pick the cube",
            "evidence": [{"image": f"img-{i}"} for i in range(30)],
            "joint_limits": [{"joint": i, "limit": 1.5} for i in range(20)],
            "blob": "Z" * 9000,
        },
    }
    message = {"role": "tool", "tool_call_id": "call-1", "name": "forge_tool_query",
               "content": json.dumps(payload)}
    messages = [message]
    assert project_forge_tool_results(messages) == 0
    assert messages[0]["content"] == json.dumps(payload)


def test_only_older_identical_successful_results_are_replaced() -> None:
    payload = {"ok": True, "data": {"task_id": "t1", "status": "succeeded", "poses": [1.0] * 50}}
    content = json.dumps(payload)
    messages = [
        {"role": "tool", "tool_call_id": "call-1", "name": "forge_tool_query", "content": content},
        {"role": "tool", "tool_call_id": "call-2", "name": "forge_tool_query", "content": content},
    ]
    assert project_forge_tool_results(messages) == 1
    first = json.loads(messages[0]["content"])
    assert first["duplicate_of_later_result"]["sha256"]
    assert len(first["duplicate_of_later_result"]["sha256"]) == 64
    assert messages[1]["content"] == content


def test_errors_unknown_and_accepted_keep_full_text() -> None:
    big = "E" * 6000
    unsafe = [
        {"ok": False, "error": {"message": big}, "data": {"blob": big}},
        {"ok": True, "data": {"status": "unknown", "note": big}},
        {"ok": True, "data": {"status": "accepted", "note": big}},
        {"ok": True, "data": {"status": "running", "nested": {"error": "boom"}}},
        {"ok": True, "data": {"status": "executing", "note": big}},
    ]
    messages = []
    for index, payload in enumerate(unsafe):
        messages.append({"role": "tool", "tool_call_id": f"c{index}",
                         "name": "forge_tool_action_status", "content": json.dumps(payload)})
        messages.append({"role": "tool", "tool_call_id": f"d{index}",
                         "name": "forge_tool_action_status", "content": json.dumps(payload)})
    before = [m["content"] for m in messages]
    assert project_forge_tool_results(messages) == 0
    assert [m["content"] for m in messages] == before


def test_different_successful_results_are_kept_verbatim() -> None:
    first = json.dumps({"ok": True, "data": {"status": "succeeded", "result": "A" * 5000}})
    second = json.dumps({"ok": True, "data": {"status": "succeeded", "result": "B" * 5000}})
    messages = [
        {"role": "tool", "tool_call_id": "1", "name": "forge_tool_query", "content": first},
        {"role": "tool", "tool_call_id": "2", "name": "forge_tool_query", "content": second},
    ]
    assert project_forge_tool_results(messages) == 0
    assert [m["content"] for m in messages] == [first, second]


def test_small_or_other_tools_are_not_projected() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "1", "name": "forge_tool_query",
         "content": json.dumps({"ok": True, "data": {"x": "y"}})},
        {"role": "tool", "tool_call_id": "2", "name": "cron",
         "content": json.dumps({"ok": True, "data": {"blob": "z" * 9000}})},
    ]
    assert project_forge_tool_results(messages) == 0


def _live_context(tool_id: str, description: str, read_at: str) -> dict:
    return {
        "ok": True,
        "data": {
            "tool": {
                "tool_id": tool_id,
                "description": description,
                "input_schema": {"type": "object"},
            },
            "context": {
                "ready": True,
                "gateway_readiness": {"runtime_phase": "running"},
            },
            "source": {"kind": "live_gateway", "read_at": read_at},
        },
    }


def _live_message(call_id: str, payload: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "forge_tool_context",
        "content": json.dumps(payload),
    }


def test_newer_live_read_supersedes_an_older_changed_spec() -> None:
    old = _live_context("sim.move_eef", "old schema " * 80, "2026-11-01T10:00:00+00:00")
    new = _live_context("sim.move_eef", "new schema " * 80, "2026-11-01T11:00:00+00:00")
    messages = [_live_message("1", old), _live_message("2", new)]

    assert project_live_forge_reads(messages) == 1

    reference = json.loads(messages[0]["content"])
    assert reference["superseded_by_newer_read"]["target"] == "sim.move_eef"
    assert reference["superseded_by_newer_read"]["read_at"] == old["data"]["source"]["read_at"]
    assert len(reference["superseded_by_newer_read"]["sha256"]) == 64
    assert messages[1]["content"] == json.dumps(new)


def test_live_read_total_budget_replaces_the_oldest_full_copies() -> None:
    payloads = [
        _live_context(f"sim.tool_{index}", "x" * 12_000, f"2026-11-01T1{index}:00:00+00:00")
        for index in range(5)
    ]
    messages = [_live_message(str(index), payload) for index, payload in enumerate(payloads)]

    assert project_live_forge_reads(messages, max_total_chars=30_000) == 3

    stripped = [json.loads(message["content"]) for message in messages]
    assert [item.get("superseded_by_size_budget") is not None for item in stripped] == [
        True, True, True, False, False,
    ]
    assert messages[-1]["content"] == json.dumps(payloads[-1])


def test_live_read_projection_leaves_failed_reads_verbatim() -> None:
    first = {
        "ok": False,
        "error": {"message": "gateway timeout"},
        "data": {"tool": {"tool_id": "sim.move_eef"}},
    }
    second = {
        "ok": False,
        "error": {"message": "gateway refused the read"},
        "data": {"tool": {"tool_id": "sim.move_eef"}},
    }
    messages = [_live_message("1", first), _live_message("2", second)]

    assert project_live_forge_reads(messages) == 0
    assert messages[0]["content"] == json.dumps(first)
    assert messages[1]["content"] == json.dumps(second)


def test_hidden_tools_filter_request_tools_but_keep_allowed_ones() -> None:
    def hidden(_context: ExecutionContext) -> frozenset[str]:
        return frozenset({"forge_tool_start_action", "forge_task_create"})

    assembler = PromptAssemblerImpl("SYS", hidden_tools=hidden)
    bundle = asyncio.run(assembler.assemble(
        _ctx(),
        history=[{"role": "user", "content": "hi"}],
        tools=[_tool("forge_tool_start_action"), _tool("forge_task_create"), _tool("cron")],
    ))
    assert [tool.name for tool in bundle.tools] == ["cron"]


def test_policy_still_wins_over_visibility() -> None:
    ctx = ExecutionContext(
        session_id="s",
        turn_id="t",
        step_id="t:step:1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
        policy=ToolPolicy(denied_tools=frozenset({"cron"})),
    )
    bundle = asyncio.run(PromptAssemblerImpl("SYS").assemble(
        ctx,
        history=[{"role": "user", "content": "hi"}],
        tools=[_tool("cron"), _tool("forge_task_get")],
    ))
    assert [tool.name for tool in bundle.tools] == ["forge_task_get"]


def test_status_and_compact_share_the_request_projection() -> None:
    import json

    from PhyAgentOS.cli.command_groups.agent_commands import _project_request_messages

    doc = "STEP BODY" + "D" * 400
    activation = {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "activate_skill",
        "content": json.dumps(
            {"ok": True, "activation": {"skill_name": "robot"}, "skill": doc}
        ),
    }
    large = {
        "role": "tool",
        "tool_call_id": "c2",
        "name": "forge_tool_query",
        "content": json.dumps(
            {"ok": True, "data": {"task_id": "t1", "status": "succeeded", "blob": "Z" * 9000}}
        ),
    }
    projected = _project_request_messages(
        [{"role": "system", "content": "SYS"}, activation, dict(activation),
         dict(large), dict(large)]
    )
    assert projected[0] == {"role": "system", "content": "SYS"}
    assert sum(1 for m in projected if m.get("role") == "tool" and "skill" in json.loads(m["content"])) == 1
    # older identical result replaced, newest stays in full
    assert "duplicate_of_later_result" in json.loads(projected[-2]["content"])
    assert projected[-1]["content"] == large["content"]


def _config_with(configured):
    from types import SimpleNamespace

    return SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(context_window_tokens=configured)))


def _selection(capacity, *, source=None, model_cap=None):
    from types import SimpleNamespace

    data = {
        "provider": SimpleNamespace(),
        "model": "fake-model",
        "context_window_tokens": capacity,
        "model_context_window_tokens": model_cap,
    }
    if source is not None:
        data["context_window_source"] = source
    return SimpleNamespace(**data)


def test_effective_window_keeps_explicit_cap_for_source_less_selection() -> None:
    from PhyAgentOS.cli.command_groups.agent_commands import _effective_window

    # Old RuntimeSelection / external embedder: no source, 1M capacity, 64k cap.
    assert _effective_window(_config_with(65536), _selection(1_000_000)) == (65536, "configured")
    # Small model window still wins when it is the smaller one.
    assert _effective_window(_config_with(65536), _selection(32768)) == (32768, "model")
    # Capacity alone is model metadata.
    assert _effective_window(_config_with(None), _selection(32768)) == (32768, "model")


def test_effective_window_trusts_new_source_and_handles_auto_none() -> None:
    from PhyAgentOS.cli.command_groups.agent_commands import _effective_window

    assert _effective_window(_config_with(65536), _selection(1_000_000, source="model")) == (
        1_000_000,
        "model",
    )
    assert _effective_window(_config_with(131072), _selection(65536, source="configured")) == (
        65536,
        "configured",
    )
    # auto (configured=None): capacity is the model answer, no 65536 assumption.
    assert _effective_window(_config_with(None), _selection(1_048_576, source="model")) == (
        1_048_576,
        "model",
    )
    # fallback source stays labelled, never relabelled as model.
    assert _effective_window(_config_with(None), _selection(131072, source="fallback")) == (
        131072,
        "fallback",
    )
    assert _effective_window(_config_with(None), _selection(None, model_cap=262144)) == (
        262144,
        "model",
    )


def test_guard_and_status_use_the_same_effective_window() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.cli.command_groups.agent_commands import (
        _ContextStatus,
        _effective_window,
        _SelectedProvider,
    )

    config = _config_with(65536)
    selection = _selection(1_000_000)
    window, source = _effective_window(config, selection)
    provider = _SelectedProvider(selection, window, 8192, window_source=source)
    assert provider.context_window == 65536

    session = SimpleNamespace(
        config=config,
        instructions="",
        raw_store=None,
        sessions=SimpleNamespace(get=lambda session_id: selection),
    )
    snapshot = _ContextStatus(session, None).snapshot("s")
    assert snapshot.capacity == provider.context_window
    assert snapshot.capacity_source == "configured"


def test_short_repeated_task_result_is_not_grown_by_a_marker() -> None:
    payload = {"ok": True, "data": {"task_id": "t1", "status": "succeeded", "result": "x" * 40}}
    content = json.dumps(payload)
    messages = [
        {"role": "tool", "tool_call_id": "1", "name": "forge_task_get", "content": content},
        {"role": "tool", "tool_call_id": "2", "name": "forge_task_get", "content": content},
    ]
    assert project_forge_tool_results(messages) == 0
    assert [m["content"] for m in messages] == [content, content]


def test_phase_and_cancel_status_block_dedup() -> None:
    payloads = [
        {"ok": True, "data": {"phase": "running", "note": "x" * 500}},
        {"ok": True, "data": {"phase": "accepted", "note": "x" * 500}},
        {"ok": True, "data": {"phase": "unknown", "note": "x" * 500}},
        {"ok": True, "data": {"status": "succeeded", "nested": {"cancel_status": "pending"}}},
    ]
    messages = []
    for index, payload in enumerate(payloads):
        content = json.dumps(payload)
        messages.append({"role": "tool", "tool_call_id": f"a{index}",
                         "name": "forge_tool_action_status", "content": content})
        messages.append({"role": "tool", "tool_call_id": f"b{index}",
                         "name": "forge_tool_action_status", "content": content})
    before = [m["content"] for m in messages]
    assert project_forge_tool_results(messages) == 0
    assert [m["content"] for m in messages] == before


def test_status_reopen_uses_checkpoint_projection_not_old_prepared_snapshot() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
    from PhyAgentOS.cli.command_groups.agent_commands import _ContextStatus

    big_messages = [
        {"role": "user", "content": "old " + "x" * 8000},
        {"role": "assistant", "content": "old answer " + "y" * 8000},
    ]
    event = RuntimeEvent(
        type=EventType.REQUEST_PREPARED,
        session_id="s",
        turn_id="t",
        step_id="t:step:1",
        payload={"model_call_id": "m", "messages": big_messages, "tools": []},
        sequence=5,
    )
    conversation = [{"role": "assistant", "content": "[context checkpoint] short"}]

    class Raw:
        async def read_session(self, session_id):
            return [event]

        async def read_conversation(self, session_id):
            return list(conversation)

        async def read_checkpoint(self, session_id):
            return SimpleNamespace(covers_sequence=5)

        async def list_compaction_attempts(self, session_id):
            return []

    class Output:
        ledger = SimpleNamespace(seed=lambda events: None,
                                 record_compaction_attempt=lambda *args: None)

    session = SimpleNamespace(
        config=_config_with(65536),
        instructions="SYS",
        raw_store=Raw(),
        sessions=SimpleNamespace(get=lambda session_id: _selection(65536, source="configured")),
    )
    status = _ContextStatus(session, Output())
    __import__("asyncio").run(status.load("s"))
    snapshot = status.snapshot("s")
    # The checkpoint covers the old prepared request; the compact summary is
    # used instead of replaying the pre-compaction messages.
    assert snapshot.tokens is not None and snapshot.tokens < 500


def test_prepared_request_newer_than_checkpoint_is_used() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
    from PhyAgentOS.cli.command_groups.agent_commands import _ContextStatus

    messages = [{"role": "user", "content": "new " + "x" * 8000}]
    event = RuntimeEvent(
        type=EventType.REQUEST_PREPARED,
        session_id="s",
        turn_id="t",
        step_id="t:step:1",
        payload={"model_call_id": "m2", "messages": messages, "tools": []},
        sequence=7,
    )

    class Raw:
        async def read_session(self, session_id):
            return [event]

        async def read_conversation(self, session_id):
            return [{"role": "assistant", "content": "short"}]

        async def read_checkpoint(self, session_id):
            return SimpleNamespace(covers_sequence=5)

        async def list_compaction_attempts(self, session_id):
            return []

    class Output:
        ledger = SimpleNamespace(seed=lambda events: None,
                                 record_compaction_attempt=lambda *args: None)

    session = SimpleNamespace(
        config=_config_with(65536),
        instructions="SYS",
        raw_store=Raw(),
        sessions=SimpleNamespace(get=lambda session_id: _selection(65536, source="configured")),
    )
    status = _ContextStatus(session, Output())
    __import__("asyncio").run(status.load("s"))
    assert status.snapshot("s").tokens > 1000


# -- cache-stable layout -------------------------------------------------


def test_cache_stable_keeps_system_fixed_and_puts_live_state_at_the_tail() -> None:
    def live(context: ExecutionContext) -> str:
        return f"blockers: fresh at {context.step_id}"

    assembler = PromptAssemblerImpl(
        "SYS", fragments=[live], cache_stable=True
    )
    bundle = asyncio.run(
        assembler.assemble(
            _stable_ctx("t:step:1"),
            history=[{"role": "user", "content": "pick the cube"}],
            tools=[_tool("echo")],
        )
    )

    assert bundle.messages[0] == {"role": "system", "content": "SYS"}
    assert [message["role"] for message in bundle.messages] == ["system", "user", "user"]
    tail = bundle.messages[-1]["content"]
    assert "blockers: fresh at t:step:1" in tail
    assert "not a user message" in tail
    assert "blockers:" not in bundle.messages[0]["content"]


def test_cache_stable_prefix_is_byte_identical_across_steps() -> None:
    def live(context: ExecutionContext) -> str:
        return f"step={context.step_id}"

    assembler = PromptAssemblerImpl("SYS", fragments=[live], cache_stable=True)
    history: list[dict[str, object]] = [{"role": "user", "content": "do it"}]
    first = asyncio.run(
        assembler.assemble(_stable_ctx("t:step:1"), history=history, tools=[])
    )

    # The driver appends the settled step; the assembler never edits history.
    history.append({"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]})
    history.append({"role": "tool", "tool_call_id": "c1", "content": "ok"})
    second = asyncio.run(
        assembler.assemble(_stable_ctx("t:step:2"), history=history, tools=[])
    )

    assert len(history) == 3
    # Everything sent before the previous runtime block is unchanged.
    assert second.messages[: len(first.messages) - 1] == first.messages[: -1]
    assert second.messages[-1]["content"].endswith("--- End runtime context ---")
    assert "step=t:step:2" in second.messages[-1]["content"]


def test_cache_stable_does_not_stack_runtime_blocks() -> None:
    assembler = PromptAssemblerImpl(
        "SYS", fragments=[lambda context: "live"], cache_stable=True
    )
    history: list[dict[str, object]] = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "one"},
    ]
    first = asyncio.run(
        assembler.assemble(_stable_ctx("t:step:1"), history=history, tools=[])
    )
    # Feed back the previous request as if a caller persisted it.
    replayed = [dict(message) for message in first.messages[1:]]
    second = asyncio.run(
        assembler.assemble(_stable_ctx("t:step:2"), history=replayed, tools=[])
    )

    runtime_blocks = [
        message
        for message in second.messages
        if isinstance(message.get("content"), str)
        and message["content"].startswith("--- Runtime context")
    ]
    assert len(runtime_blocks) == 1
    assert second.messages[-1] is runtime_blocks[0]


def test_cache_stable_keeps_tool_order_and_two_sessions_apart() -> None:
    def live(context: ExecutionContext) -> str:
        return f"session={context.session_id}"

    assembler = PromptAssemblerImpl("SYS", fragments=[live], cache_stable=True)
    tools = [_tool("zeta"), _tool("alpha"), _tool("middle")]
    a = asyncio.run(
        assembler.assemble(
            _stable_ctx("t:step:1", "session-a"),
            history=[{"role": "user", "content": "hi"}],
            tools=tools,
        )
    )
    b = asyncio.run(
        assembler.assemble(
            _stable_ctx("t:step:1", "session-b"),
            history=[{"role": "user", "content": "hi"}],
            tools=tools,
        )
    )
    assert [tool.name for tool in a.tools] == ["zeta", "alpha", "middle"]
    assert "session=session-a" in a.messages[-1]["content"]
    assert "session=session-b" in b.messages[-1]["content"]
    assert "session=session-b" not in a.messages[-1]["content"]


def test_cache_stable_skill_dedup_keeps_the_earliest_full_document() -> None:
    from PhyAgentOS.agent.core.prompt import project_skill_documents

    doc = "SKILL BODY " + "D" * 400
    content = json.dumps(
        {"ok": True, "activation": {"skill_name": "robot"}, "skill": doc}
    )
    messages = [
        {"role": "tool", "tool_call_id": "c1", "name": "activate_skill", "content": content},
        {"role": "tool", "tool_call_id": "c2", "name": "activate_skill", "content": content},
    ]

    assert project_skill_documents(messages, keep_earliest=True) == 1

    assert json.loads(messages[0]["content"])["skill"] == doc
    later = json.loads(messages[1]["content"])
    assert "skill" not in later
    assert len(later["skill_document_omitted"]["sha256"]) == 64


def test_cache_stable_forge_dedup_keeps_earliest_and_safety() -> None:
    from PhyAgentOS.agent.core.prompt import project_forge_tool_results

    content = json.dumps(
        {"ok": True, "data": {"task_id": "t1", "status": "succeeded", "r": "Z" * 400}}
    )
    unsafe = json.dumps({"ok": True, "data": {"status": "unknown", "r": "Z" * 400}})
    messages = [
        {"role": "tool", "tool_call_id": "c1", "name": "forge_tool_query", "content": content},
        {"role": "tool", "tool_call_id": "c2", "name": "forge_tool_query", "content": content},
        {"role": "tool", "tool_call_id": "c3", "name": "forge_tool_query", "content": unsafe},
    ]

    assert project_forge_tool_results(messages, keep_earliest=True) == 1
    assert messages[0]["content"] == content
    marker = json.loads(messages[1]["content"])
    assert marker["duplicate_of_earlier_result"]["sha256"]
    assert messages[2]["content"] == unsafe


def test_live_forge_read_at_does_not_prevent_identical_result_dedup() -> None:
    import hashlib

    def message(call_id: str, read_at: str, ready: bool = True) -> dict:
        payload = {
            "ok": True,
            "data": {
                "tool": {"tool_id": "demo.tool"},
                "context": {"tool_id": "demo.tool", "ready": ready},
                "source": {
                    "kind": "live_gateway",
                    "snapshot": "not_atomic_two_independent_get_requests",
                    "read_at": read_at,
                    "runtime": None,
                },
                "blob": "Z" * 400,
            },
        }
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "forge_tool_context",
            "content": json.dumps(payload),
        }

    first = message("c1", "2026-11-03T00:00:00+00:00")
    second = message("c2", "2026-11-03T00:00:01+00:00")
    first_content = first["content"]
    messages = [first, second]

    assert project_forge_tool_results(messages) == 1
    assert messages[1]["content"] == second["content"]
    marker = json.loads(messages[0]["content"])
    assert marker["duplicate_of_later_result"]["sha256"] == hashlib.sha256(
        first_content.encode("utf-8")
    ).hexdigest()

    # A real context change is still a different result and stays full.
    changed = message("c3", "2026-11-03T00:00:02+00:00", ready=False)
    messages = [second, changed]
    assert project_forge_tool_results(messages) == 0
    assert messages[0]["content"] == second["content"]
    assert messages[1]["content"] == changed["content"]


def test_cache_stable_skill_versions_keep_separate_full_bodies() -> None:
    from PhyAgentOS.agent.core.prompt import project_skill_documents

    old_doc = "OLD SKILL " + "A" * 400
    new_doc = "NEW SKILL " + "B" * 400
    old = json.dumps({"ok": True, "activation": {"skill_name": "robot"}, "skill": old_doc})
    new = json.dumps({"ok": True, "activation": {"skill_name": "robot"}, "skill": new_doc})
    messages = [
        {"role": "tool", "tool_call_id": "c1", "name": "activate_skill", "content": old},
        {"role": "tool", "tool_call_id": "c2", "name": "activate_skill", "content": new},
    ]

    assert project_skill_documents(messages, keep_earliest=True) == 0
    assert json.loads(messages[0]["content"])["skill"] == old_doc
    assert json.loads(messages[1]["content"])["skill"] == new_doc


def test_runtime_context_helper_only_matches_the_exact_protocol() -> None:
    from PhyAgentOS.agent.core.prompt import is_runtime_context_message

    assembler = PromptAssemblerImpl(
        "SYS", fragments=[lambda context: "live"], cache_stable=True
    )
    bundle = asyncio.run(
        assembler.assemble(
            _stable_ctx(),
            history=[{"role": "user", "content": "real task"}],
            tools=[],
        )
    )
    assert is_runtime_context_message(bundle.messages[-1])
    assert not is_runtime_context_message({"role": "user", "content": "real task"})
    assert not is_runtime_context_message(
        {"role": "user", "content": "--- Runtime context for this step ---"}
    )


def test_cross_turn_stored_runtime_tail_is_removed_before_new_state() -> None:
    def live(context: ExecutionContext) -> str:
        return f"blocker: turn {context.turn_id} step {context.step_id}"

    assembler = PromptAssemblerImpl("SYS", fragments=[live], cache_stable=True)
    previous = asyncio.run(
        assembler.assemble(
            _stable_ctx("old:step:1"),
            history=[{"role": "user", "content": "old task"}],
            tools=[],
        )
    )
    # Simulate a driver that replays the last request snapshot minus its leading
    # system header, then appends the real next-turn user request.
    history = [dict(message) for message in previous.messages[1:]]
    history.append({"role": "user", "content": "new real task"})
    assert any("blocker: turn t step old:step:1" in m["content"] for m in history)

    next_context = _stable_ctx("new:step:1")
    next_context = ExecutionContext(
        session_id=next_context.session_id,
        turn_id="turn-2",
        step_id=next_context.step_id,
        cancellation=next_context.cancellation,
        budget=next_context.budget,
    )
    bundle = asyncio.run(assembler.assemble(next_context, history=history, tools=[]))

    assert bundle.messages[0] == {"role": "system", "content": "SYS"}
    assert [m["content"] for m in bundle.messages if m["role"] == "user"] == [
        "old task",
        "new real task",
        bundle.messages[-1]["content"],
    ]
    assert "old:step:1" not in bundle.messages[-1]["content"]
    assert bundle.messages[-1]["content"].count("--- Runtime context") == 1
    assert "turn-2 step new:step:1" in bundle.messages[-1]["content"]
    # The caller's replayed history is untouched.
    assert any("old:step:1" in m["content"] for m in history)


def test_runtime_block_carries_standard_name_and_user_copy_is_preserved() -> None:
    from PhyAgentOS.agent.core.prompt import is_runtime_context_message

    assembler = PromptAssemblerImpl(
        "SYS", fragments=[lambda context: "live"], cache_stable=True
    )
    bundle = asyncio.run(
        assembler.assemble(
            _stable_ctx(),
            history=[{"role": "user", "content": "real task"}],
            tools=[],
        )
    )
    runtime = bundle.messages[-1]
    assert runtime["name"] == "paos_runtime_context"
    assert is_runtime_context_message(runtime)

    pasted = dict(runtime)
    pasted.pop("name")
    assert not is_runtime_context_message(pasted)
    assert not is_runtime_context_message({**runtime, "name": "someone_else"})

    # A real user message that pasted our exact marker text has no name; the
    # assembler must keep it verbatim and only append its own fresh block.
    replayed = asyncio.run(
        assembler.assemble(
            _stable_ctx("t:step:2"),
            history=[{"role": "user", "content": pasted["content"]}],
            tools=[],
        )
    )
    assert replayed.messages[1]["content"] == pasted["content"]
    assert replayed.messages[1].get("name") is None
    assert is_runtime_context_message(replayed.messages[-1])
    assert replayed.messages[-1]["name"] == "paos_runtime_context"


# -- request budget guard ------------------------------------------------


class _FixedCounterProvider:
    def __init__(self, tokens: int, *, answer="ok") -> None:
        self.tokens = tokens
        self.answer = answer
        self.calls = 0

    def estimate_prompt_tokens(self, messages, tools, model):
        self.calls += 1
        return self.tokens, "test_counter"

    async def chat_with_retry(self, **kwargs):
        return self.answer

    async def chat_stream(self, **kwargs):
        yield {"type": "done", "finish_reason": "stop"}


def test_guard_rejects_8520_with_16384_window_and_8192_reply_budget() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.cli.command_groups.agent_commands import (
        _ContextLimitError,
        _SelectedProvider,
    )

    selection = SimpleNamespace(
        provider=_FixedCounterProvider(8520), model="fake-model"
    )
    provider = _SelectedProvider(
        selection, 16384, 8192, window_source="configured"
    )
    with pytest.raises(_ContextLimitError) as error:
        asyncio.run(
            provider.chat_with_retry(
                messages=[{"role": "user", "content": "x"}],
                max_tokens=8192,
            )
        )
    text = str(error.value)
    assert "estimated input 8520" in text
    assert "input limit 8192" in text
    assert "reply budget 8192" in text
    assert "window 16384" in text
    assert "test_counter" in text


def test_guard_accepts_the_exact_input_limit_and_rejects_one_token_more() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.cli.command_groups.agent_commands import (
        _ContextLimitError,
        _SelectedProvider,
    )

    def call(tokens):
        selection = SimpleNamespace(
            provider=_FixedCounterProvider(tokens), model="fake-model"
        )
        provider = _SelectedProvider(selection, 16384, 8192)
        return asyncio.run(
            provider.chat_with_retry(
                messages=[{"role": "user", "content": "x"}], max_tokens=8192
            )
        )

    assert call(8192) == "ok"
    with pytest.raises(_ContextLimitError):
        call(8193)


def test_guard_uses_the_request_max_tokens_not_the_selection_default() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.cli.command_groups.agent_commands import _SelectedProvider

    selection = SimpleNamespace(
        provider=_FixedCounterProvider(8520), model="fake-model"
    )
    provider = _SelectedProvider(selection, 16384, 8192)
    assert (
        asyncio.run(
            provider.chat_with_retry(
                messages=[{"role": "user", "content": "x"}], max_tokens=4096
            )
        )
        == "ok"
    )


def test_fallback_65536_allows_a_52_percent_request() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.cli.command_groups.agent_commands import _SelectedProvider

    selection = SimpleNamespace(
        provider=_FixedCounterProvider(34078), model="fake-model"
    )
    provider = _SelectedProvider(
        selection, 65536, 8192, window_source="fallback"
    )
    assert (
        asyncio.run(
            provider.chat_with_retry(
                messages=[{"role": "user", "content": "x"}], max_tokens=8192
            )
        )
        == "ok"
    )


def test_blocking_and_streaming_share_one_budget_rule() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.cli.command_groups.agent_commands import (
        _ContextLimitError,
        _SelectedProvider,
    )

    async def consume(stream):
        async for _event in stream:
            pass

    selection = SimpleNamespace(
        provider=_FixedCounterProvider(8520), model="fake-model"
    )
    provider = _SelectedProvider(selection, 16384, 8192)
    with pytest.raises(_ContextLimitError):
        asyncio.run(
            provider.chat_with_retry(
                messages=[{"role": "user", "content": "x"}], max_tokens=8192
            )
        )
    with pytest.raises(_ContextLimitError):
        asyncio.run(
            consume(
                provider.chat_stream(
                    messages=[{"role": "user", "content": "x"}], max_tokens=8192
                )
            )
        )


def test_request_budget_snapshot_matches_the_guard_numbers() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.cli.command_groups.agent_commands import _SelectedProvider

    selection = SimpleNamespace(
        provider=_FixedCounterProvider(8520), model="fake-model"
    )
    provider = _SelectedProvider(
        selection, 16384, 8192, window_source="fallback"
    )
    budget = provider.request_budget_snapshot(
        messages=[{"role": "user", "content": "x"}],
        tools=None,
        model="fake-model",
        max_tokens=4096,
    )
    assert budget["estimated_input"] == 8520
    assert budget["context_window"] == 16384
    assert budget["window_source"] == "fallback"
    assert budget["reply_budget"] == 4096
    assert budget["reserve_tokens"] == 4096
    assert budget["input_limit"] == 12288

    input_only = _SelectedProvider(
        selection, 16384, 8192, window_semantics="input"
    )
    budget = input_only.request_budget_snapshot(
        messages=[{"role": "user", "content": "x"}],
        tools=None,
        model="fake-model",
        max_tokens=4096,
    )
    assert budget["reserve_tokens"] == 0
    assert budget["input_limit"] == 16384


def test_prepared_payload_carries_the_request_budget() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.agent.core.loop import _prepared_payload
    from PhyAgentOS.agent.core.model import ModelAdapter
    from PhyAgentOS.agent.core.types import ModelRequest
    from PhyAgentOS.cli.command_groups.agent_commands import _SelectedProvider

    selection = SimpleNamespace(
        provider=_FixedCounterProvider(8520), model="fake-model"
    )
    provider = _SelectedProvider(selection, 16384, 8192)
    adapter = ModelAdapter(provider, stream=False)
    request = ModelRequest(
        "m1",
        ({"role": "user", "content": "x"},),
        model="fake-model",
        max_tokens=4096,
    )
    budget = adapter.request_budget(request)
    assert budget is not None
    payload = _prepared_payload(request, budget)
    assert payload["request_budget"]["estimated_input"] == 8520
    assert payload["request_budget"]["input_limit"] == 12288
    assert payload["request_budget"]["reply_budget"] == 4096


def test_status_reply_budget_does_not_reuse_a_prepared_request_from_another_model() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
    from PhyAgentOS.cli.command_groups.agent_commands import _ContextStatus

    events = [
        RuntimeEvent(
            type=EventType.REQUEST_PREPARED,
            session_id="s",
            turn_id="t",
            step_id="t:step:1",
            payload={"model_call_id": "m", "model": "old-model", "max_tokens": 8192},
        )
    ]
    session = SimpleNamespace(
        config=SimpleNamespace(
            agents=SimpleNamespace(defaults=SimpleNamespace(max_tokens=4096))
        ),
        instructions="SYS",
        raw_store=None,
        sessions=SimpleNamespace(get=lambda session_id: _selection(32768)),
    )
    status = _ContextStatus(session, None)
    status._events["s"] = events
    assert status._reply_budget(events, _selection(32768)) == 4096
    same_model = [
        RuntimeEvent(
            type=EventType.REQUEST_PREPARED,
            session_id="s",
            turn_id="t",
            step_id="t:step:1",
            payload={"model_call_id": "m", "model": "fake-model", "max_tokens": 8192},
        )
    ]
    assert status._reply_budget(same_model, _selection(32768)) == 8192


def test_edited_rules_make_the_old_provider_usage_an_estimate_not_the_send(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    import PhyAgentOS.agent.services.usage as usage
    from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
    from PhyAgentOS.cli.command_groups.agent_commands import _ContextStatus

    seen = {}

    def estimate(provider, model, messages, tools=None):
        seen["messages"] = messages
        return 1234, "tiktoken"

    monkeypatch.setattr(usage, "estimate_prompt_tokens_chain", estimate)

    messages = [
        {"role": "system", "content": "OLD RULES"},
        {"role": "user", "content": "hi"},
    ]
    events = [
        RuntimeEvent(
            type=EventType.REQUEST_PREPARED,
            session_id="s",
            turn_id="t",
            step_id="t:step:1",
            payload={
                "model_call_id": "m",
                "model": "fake-model",
                "messages": messages,
                "tools": [],
            },
        ),
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_COMPLETED,
            session_id="s",
            turn_id="t",
            step_id="t:step:1",
            payload={
                "model_call_id": "m",
                "content": "hello",
                "usage": {"prompt_tokens": 500},
            },
        ),
    ]
    session = SimpleNamespace(
        config=SimpleNamespace(
            agents=SimpleNamespace(defaults=SimpleNamespace(max_tokens=4096))
        ),
        instructions="NEW RULES",
        raw_store=None,
        sessions=SimpleNamespace(get=lambda session_id: _selection(32768)),
    )
    status = _ContextStatus(session, None)
    status._events["s"] = events
    snapshot = status.snapshot("s")
    assert snapshot.exact is False
    assert snapshot.tokens == 1234
    assert snapshot.tokens_source != "provider_usage"
    # The actual measured messages carry the new rules, not only a flag.
    assert any(
        message.get("role") == "system" and message.get("content") == "NEW RULES"
        for message in seen["messages"]
    )
    assert all(
        message.get("content") != "OLD RULES" for message in seen["messages"]
    )
    assert "last request usage" not in snapshot.label()

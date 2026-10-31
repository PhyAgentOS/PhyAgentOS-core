"""Request-side projections: large Forge results and hidden tool stages."""

from __future__ import annotations

import asyncio
import json

from PhyAgentOS.agent.core.prompt import (
    PromptAssemblerImpl,
    project_forge_tool_results,
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

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

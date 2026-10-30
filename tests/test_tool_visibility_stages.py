"""Tool visibility stages and the shared prompt/executor policy."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from PhyAgentOS.agent.capabilities.forge_capability import ForgeCapability
from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl
from PhyAgentOS.agent.core.tools import ToolExecutorImpl
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)

ALWAYS_AVAILABLE = {
    "forge_task_get",
    "forge_task_cancel",
    "forge_tool_context",
    "forge_tool_query",
    "forge_tool_action_status",
    "forge_tool_action_result",
    "forge_tool_cancel_action",
    "forge_tool_session_status",
    "forge_tool_session_result",
    "forge_tool_stop_session",
}


class _Store:
    def __init__(self) -> None:
        self.record = None

    def latest_for_session(self, session_id: str):
        return self.record


class _Coordinator:
    def __init__(self) -> None:
        self.store = _Store()

    def unknown_remote_blockers(self) -> list:
        return []


class _AppendStore:
    async def append(self, event) -> None:
        return None


def _ctx(turn_id: str = "t1") -> ExecutionContext:
    return ExecutionContext(
        session_id="s",
        turn_id=turn_id,
        step_id=f"{turn_id}:step:1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, description="d", parameters={"type": "object"})


def _capability(stage: str) -> ForgeCapability:
    capability = ForgeCapability(_Coordinator())
    capability.set_stage_resolver(lambda _context: stage)
    return capability


@pytest.mark.parametrize("stage", ["none", "activated", "task"])
def test_recovery_and_poll_tools_are_never_hidden(stage: str) -> None:
    hidden = _capability(stage).hidden_tools(_ctx())
    assert hidden.isdisjoint(ALWAYS_AVAILABLE)


def test_stage_sets_match_the_activation_and_task_boundary() -> None:
    assert "forge_task_create" in _capability("none").hidden_tools(_ctx())
    assert "forge_tool_start_action" in _capability("none").hidden_tools(_ctx())
    assert "forge_task_create" not in _capability("activated").hidden_tools(_ctx())
    assert "forge_tool_start_action" in _capability("activated").hidden_tools(_ctx())
    assert _capability("task").hidden_tools(_ctx()) == frozenset()


def test_forged_hidden_action_is_rejected_before_the_handler_runs() -> None:
    called = {"value": False}

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        called["value"] = True
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text="ran",
        )

    registration = ToolRegistration(
        definition=_tool("forge_tool_start_action"), handler=handler
    )
    executor = ToolExecutorImpl([registration], _AppendStore())
    assembler = PromptAssemblerImpl(
        "SYS", hidden_tools=lambda _context: frozenset({"forge_tool_start_action"})
    )
    context = _ctx()
    bundle = asyncio.run(assembler.assemble(
        context, history=[{"role": "user", "content": "hi"}],
        tools=[registration.definition],
    ))
    assert bundle.tools == ()
    assert bundle.policy is not None
    call = ToolCall(tool_call_id="c1", name="forge_tool_start_action", arguments={})
    outcome = asyncio.run(
        executor.execute(call, replace(context, policy=bundle.policy))
    )
    assert outcome.status is ToolStatus.REJECTED
    assert outcome.error is not None and outcome.error.code == "tool_denied"
    assert called["value"] is False


def test_activation_becomes_visible_on_the_next_step_of_the_same_turn() -> None:
    state = {"stage": "none"}
    capability = ForgeCapability(_Coordinator())
    capability.set_stage_resolver(lambda _context: state["stage"])
    assembler = PromptAssemblerImpl("SYS", hidden_tools=capability.hidden_tools)
    tools = [_tool("activate_skill"), _tool("forge_task_create")]
    first = asyncio.run(assembler.assemble(_ctx(), history=[{"role": "user", "content": "hi"}], tools=tools))
    assert [tool.name for tool in first.tools] == ["activate_skill"]
    state["stage"] = "activated"
    second = asyncio.run(assembler.assemble(_ctx(), history=[{"role": "user", "content": "hi"}], tools=tools))
    assert [tool.name for tool in second.tools] == ["activate_skill", "forge_task_create"]


def test_previous_turn_activation_does_not_authorize_a_new_turn() -> None:
    from types import SimpleNamespace

    from PhyAgentOS.agent.capabilities.skill_access_capability import (
        SkillAccessCapability,
    )

    class _Activations:
        def current(self, session_id: str, turn_id: str):
            return () if turn_id != "t1" else (SimpleNamespace(role="primary"),)

    skill = SkillAccessCapability(_Activations())
    assert skill.has_primary_activation("s", "t1") is True
    assert skill.has_primary_activation("s", "t2") is False


def test_stage_guard_rejects_a_hidden_tool_without_calling_forge() -> None:
    capability = _capability("none")
    call = ToolCall(tool_call_id="c1", name="forge_task_create", arguments={})
    blocked = capability._stage_blocked(call, _ctx())
    assert blocked is not None
    assert blocked.status is ToolStatus.REJECTED
    assert blocked.error is not None and blocked.error.code == "tool_stage_blocked"

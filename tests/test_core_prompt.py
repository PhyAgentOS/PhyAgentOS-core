"""Behavior tests for the core prompt assembler.

These pin the layout rules the loop driver relies on: one system message from
injected text, deep-copied history in order, fragments read live per step with
the current context, and a tool list that the policy may narrow but never widen.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from PhyAgentOS.agent.core import (
    Budget,
    CancellationToken,
    ExecutionContext,
    PromptAssembler,
    ToolDefinition,
    ToolPolicy,
)
from PhyAgentOS.agent.core import prompt as prompt_module
from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl


def _context(
    policy: ToolPolicy | None = None,
    *,
    session_id: str = "session-1",
    step_id: str = "step-1",
) -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="turn-1",
        step_id=step_id,
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
        policy=policy or ToolPolicy(),
    )


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool {name}.",
        parameters={"type": "object", "properties": {}},
    )


def _history() -> list[dict[str, object]]:
    return [
        {"role": "user", "content": "pick up the cup"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
    ]


async def test_system_message_precedes_history_in_order() -> None:
    assembler = PromptAssemblerImpl("You are a robot assistant.")

    bundle = await assembler.assemble(_context(), history=_history(), tools=[])

    assert [message["role"] for message in bundle.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert bundle.messages[0]["content"] == "You are a robot assistant."
    assert bundle.messages[1]["content"] == "pick up the cup"
    assert bundle.messages[3]["content"] == "ok"


async def test_history_is_deep_copied() -> None:
    assembler = PromptAssemblerImpl("Base.")
    history: list[dict[str, object]] = [{"role": "user", "content": "hi", "meta": {"n": 1}}]

    bundle = await assembler.assemble(_context(), history=history, tools=[])

    history[0]["content"] = "mutated"
    meta = history[0]["meta"]
    assert isinstance(meta, dict)
    meta["n"] = 2
    history.append({"role": "user", "content": "late"})

    assert len(bundle.messages) == 2
    assert bundle.messages[1]["content"] == "hi"
    assert bundle.messages[1]["meta"] == {"n": 1}
    assert bundle.messages[1] is not history[0]


async def test_empty_fragments_add_nothing() -> None:
    assembler = PromptAssemblerImpl(
        "Base.",
        fragments=[
            lambda context: None,
            lambda context: "",
            lambda context: "  \n\t",
            lambda context: "Live state: idle.",
        ],
    )

    bundle = await assembler.assemble(_context(), history=_history(), tools=[])

    assert bundle.messages[0]["content"] == "Base.\n\nLive state: idle."


async def test_blank_instructions_with_blank_fragments_add_no_system_message() -> None:
    assembler = PromptAssemblerImpl(
        "  ", fragments=[lambda context: None, lambda context: "   "]
    )

    bundle = await assembler.assemble(_context(), history=_history(), tools=[])

    assert [message["role"] for message in bundle.messages] == ["user", "assistant", "tool"]


async def test_fragments_are_read_on_every_step() -> None:
    reads: list[int] = []

    def fragment(context: ExecutionContext) -> str:
        reads.append(len(reads) + 1)
        return f"call {reads[-1]}"

    assembler = PromptAssemblerImpl("Base.", fragments=[fragment])

    first = await assembler.assemble(_context(step_id="step-1"), history=_history(), tools=[])
    second = await assembler.assemble(_context(step_id="step-2"), history=_history(), tools=[])

    assert reads == [1, 2]
    assert "call 1" in first.messages[0]["content"]  # type: ignore[operator]
    assert "call 2" in second.messages[0]["content"]  # type: ignore[operator]


async def test_every_fragment_receives_the_current_step_context() -> None:
    seen: list[tuple[str, str]] = []

    def first(context: ExecutionContext) -> str:
        seen.append((context.session_id, context.step_id))
        return "first"

    def second(context: ExecutionContext) -> str:
        seen.append((context.session_id, context.step_id))
        return "second"

    assembler = PromptAssemblerImpl("Base.", fragments=[first, second])
    context = _context(session_id="session-a", step_id="step-7")

    await assembler.assemble(context, history=_history(), tools=[])

    assert seen == [("session-a", "step-7"), ("session-a", "step-7")]


async def test_two_sessions_receive_their_own_fragment_text() -> None:
    def fragment(context: ExecutionContext) -> str:
        return f"session={context.session_id}"

    assembler = PromptAssemblerImpl("Base.", fragments=[fragment])

    first = await assembler.assemble(
        _context(session_id="session-a"), history=_history(), tools=[]
    )
    second = await assembler.assemble(
        _context(session_id="session-b"), history=_history(), tools=[]
    )

    assert first.messages[0]["content"] == "Base.\n\nsession=session-a"
    assert second.messages[0]["content"] == "Base.\n\nsession=session-b"


async def test_policy_narrows_the_offered_tools() -> None:
    assembler = PromptAssemblerImpl("Base.")
    offered = [_tool("echo"), _tool("plan"), _tool("stop")]

    allowed = await assembler.assemble(
        _context(ToolPolicy(allowed_tools=frozenset({"echo", "plan"}))),
        history=_history(),
        tools=offered,
    )
    assert [tool.name for tool in allowed.tools] == ["echo", "plan"]

    denied = await assembler.assemble(
        _context(
            ToolPolicy(
                allowed_tools=frozenset({"echo", "plan"}),
                denied_tools=frozenset({"plan"}),
            )
        ),
        history=_history(),
        tools=offered,
    )
    assert [tool.name for tool in denied.tools] == ["echo"]


async def test_assembler_cannot_add_a_tool() -> None:
    assembler = PromptAssemblerImpl("Base.")
    offered = [_tool("echo")]

    bundle = await assembler.assemble(_context(), history=_history(), tools=offered)

    assert [tool.name for tool in bundle.tools] == ["echo"]
    assert {tool.name for tool in bundle.tools} <= {tool.name for tool in offered}


async def test_no_tools_yields_an_empty_tool_list() -> None:
    assembler = PromptAssemblerImpl("Base.")

    bundle = await assembler.assemble(_context(), history=_history(), tools=[])

    assert bundle.tools == ()
    assert bundle.to_provider_tools() == []


async def test_repeated_system_instruction_is_not_duplicated() -> None:
    assembler = PromptAssemblerImpl("Base.", fragments=[lambda context: "Live state."])
    history: list[dict[str, object]] = [
        {"role": "system", "content": "Base.\n\nLive state."},
        {"role": "user", "content": "hi"},
    ]

    bundle = await assembler.assemble(_context(), history=history, tools=[])

    system_messages = [message for message in bundle.messages if message["role"] == "system"]
    assert len(system_messages) == 1
    assert bundle.messages[0]["content"] == "Base.\n\nLive state."


async def test_a_different_leading_system_message_keeps_instructions_first() -> None:
    assembler = PromptAssemblerImpl("Base.")
    history: list[dict[str, object]] = [
        {"role": "system", "content": "Provider-specific note."},
        {"role": "user", "content": "hi"},
    ]

    bundle = await assembler.assemble(_context(), history=history, tools=[])

    assert [message["role"] for message in bundle.messages] == ["system", "system", "user"]
    assert bundle.messages[0]["content"] == "Base."
    assert bundle.messages[1]["content"] == "Provider-specific note."


async def test_history_is_required() -> None:
    assembler = PromptAssemblerImpl("Base.")

    with pytest.raises(ValueError):
        await assembler.assemble(_context(), history=[], tools=[])


async def test_fragment_errors_and_bad_return_values_are_not_swallowed() -> None:
    def boom(context: ExecutionContext) -> str:
        raise RuntimeError("fragment failed")

    with pytest.raises(RuntimeError, match="fragment failed"):
        await PromptAssemblerImpl("Base.", fragments=[boom]).assemble(
            _context(), history=_history(), tools=[]
        )

    with pytest.raises(TypeError):
        await PromptAssemblerImpl("Base.", fragments=[lambda context: 3]).assemble(  # type: ignore[list-item]
            _context(), history=_history(), tools=[]
        )


def test_implementation_satisfies_the_protocol() -> None:
    assert isinstance(PromptAssemblerImpl("Base."), PromptAssembler)


def test_module_imports_only_core_types() -> None:
    source = Path(prompt_module.__file__ or "").read_text(encoding="utf-8")
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." * node.level + (node.module or ""))

    unexpected = imports - {
        "__future__", "copy", "hashlib", "json", "collections.abc", "typing", ".types"
    }
    assert unexpected == set()

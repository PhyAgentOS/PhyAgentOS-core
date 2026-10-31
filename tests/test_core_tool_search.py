"""On-demand tool discovery: hidden schemas, session isolation, and policy."""

from __future__ import annotations

from dataclasses import replace

from PhyAgentOS.agent.capabilities.code_execution_capability import (
    CodeExecutionCapability,
)
from PhyAgentOS.agent.capabilities.tool_search_capability import (
    ToolSearchCapability,
)
from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl
from PhyAgentOS.agent.core.tools import ToolExecutorImpl
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolPolicy,
    ToolRegistration,
    ToolStatus,
)


class _Store:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        stored = replace(event, sequence=len(self.events))
        self.events.append(stored)
        return stored


def _context(session_id: str = "s1") -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="t1",
        step_id="t1:step:1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _registration(
    name: str,
    *,
    description: str = "",
    parallel_safe: bool = False,
    calls: list[str] | None = None,
) -> ToolRegistration:
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        del context
        if calls is not None:
            calls.append(name)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=f"{name} ok",
            content={"name": name},
        )

    return ToolRegistration(
        definition=ToolDefinition(
            name=name,
            description=description or f"Tool {name}.",
            parameters={
                "type": "object",
                "properties": {"widget": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        handler=handler,
        parallel_safe=parallel_safe,
    )


def _assembler(capability: ToolSearchCapability) -> PromptAssemblerImpl:
    return PromptAssemblerImpl("SYS", hidden_tools=capability.hidden_tools)


async def _assemble(assembler, context, definitions):
    return await assembler.assemble(
        context,
        history=[{"role": "user", "content": "hi"}],
        tools=definitions,
    )


async def _discover(capability: ToolSearchCapability, session_id: str, query: str):
    registration = capability.tool_registrations()[0]
    executor = ToolExecutorImpl((registration,), _Store())
    context = _context(session_id)
    return await executor.execute(
        ToolCall(
            tool_call_id=f"search-{session_id}",
            name="tool_search",
            arguments={"query": query},
        ),
        context,
    )


async def test_hidden_candidates_are_only_low_frequency_tools() -> None:
    capability = ToolSearchCapability(
        (
            _registration("read_file"),
            _registration("forge_task_get"),
            _registration("mcp_alpha_widget", description="Alpha widget reader"),
            _registration("mcp_beta_widget", description="Beta widget reader"),
        )
    )
    hidden = capability.hidden_tools(_context())
    assert hidden == frozenset({"mcp_alpha_widget", "mcp_beta_widget"})
    assert "read_file" not in hidden
    assert "forge_task_get" not in hidden
    assert "tool_search" not in hidden


async def test_search_reveals_full_schema_on_the_next_step() -> None:
    calls: list[str] = []
    alpha = _registration(
        "mcp_alpha_widget",
        description="Alpha widget reader with a widget argument",
        calls=calls,
    )
    beta = _registration("mcp_beta_widget", description="Beta widget reader")
    basic = _registration("read_file", calls=calls)
    capability = ToolSearchCapability((basic, alpha, beta))
    search_registration = capability.tool_registrations()[0]
    all_registrations = (basic, alpha, beta, search_registration)
    definitions = tuple(item.definition for item in all_registrations)
    executor = ToolExecutorImpl(all_registrations, _Store())
    assembler = _assembler(capability)
    context = _context()

    first = await _assemble(assembler, context, definitions)
    assert [tool.name for tool in first.tools] == ["read_file", "tool_search"]
    assert first.policy is not None

    search_call = ToolCall(
        tool_call_id="c1",
        name="tool_search",
        arguments={"query": "alpha widget"},
    )
    search_outcome = await executor.execute(
        search_call, replace(context, policy=first.policy)
    )
    assert search_outcome.status is ToolStatus.SUCCEEDED
    assert '"widget"' in search_outcome.text
    assert "mcp_alpha_widget" in search_outcome.text
    assert calls == []

    second = await _assemble(
        assembler, context.with_step("t1:step:2"), definitions
    )
    assert [tool.name for tool in second.tools] == [
        "read_file",
        "mcp_alpha_widget",
        "tool_search",
    ]
    assert second.policy is not None
    read_call = ToolCall(
        tool_call_id="c2", name="mcp_alpha_widget", arguments={"widget": "w"}
    )
    read_outcome = await executor.execute(
        read_call, replace(context, policy=second.policy)
    )
    assert read_outcome.status is ToolStatus.SUCCEEDED
    assert calls == ["mcp_alpha_widget"]


async def test_discovery_is_not_execution_authorization() -> None:
    calls: list[str] = []
    alpha = _registration("mcp_alpha_widget", calls=calls)
    capability = ToolSearchCapability((alpha,))
    search_registration = capability.tool_registrations()[0]
    all_registrations = (search_registration, alpha)
    definitions = tuple(item.definition for item in all_registrations)
    executor = ToolExecutorImpl(all_registrations, _Store())
    assembler = _assembler(capability)
    context = _context()

    first = await _assemble(assembler, context, definitions)
    await executor.execute(
        ToolCall(
            tool_call_id="c1",
            name="tool_search",
            arguments={"query": "mcp_alpha_widget"},
        ),
        replace(context, policy=first.policy),
    )

    denied_context = replace(
        context,
        policy=ToolPolicy(denied_tools=frozenset({"mcp_alpha_widget"})),
    )
    second = await _assemble(assembler, denied_context, definitions)
    assert "mcp_alpha_widget" not in [tool.name for tool in second.tools]
    assert second.policy is not None
    forged = await executor.execute(
        ToolCall(
            tool_call_id="c2", name="mcp_alpha_widget", arguments={"widget": "w"}
        ),
        replace(denied_context, policy=second.policy),
    )
    assert forged.status is ToolStatus.REJECTED
    assert forged.error is not None and forged.error.code == "tool_denied"
    assert calls == []


async def test_discovery_does_not_leak_to_another_session() -> None:
    alpha = _registration("mcp_alpha_widget")
    capability = ToolSearchCapability((alpha,))
    search_registration = capability.tool_registrations()[0]
    all_registrations = (search_registration, alpha)
    definitions = tuple(item.definition for item in all_registrations)
    executor = ToolExecutorImpl(all_registrations, _Store())
    assembler = _assembler(capability)
    first_session = _context("s1")
    second_session = _context("s2")

    first = await _assemble(assembler, first_session, definitions)
    await executor.execute(
        ToolCall(
            tool_call_id="c1",
            name="tool_search",
            arguments={"query": "mcp_alpha_widget"},
        ),
        replace(first_session, policy=first.policy),
    )

    assert "mcp_alpha_widget" not in capability.hidden_tools(_context("s1"))
    assert "mcp_alpha_widget" in capability.hidden_tools(second_session)
    second = await _assemble(assembler, second_session, definitions)
    assert "mcp_alpha_widget" not in [tool.name for tool in second.tools]


async def test_search_itself_has_no_external_side_effects() -> None:
    calls: list[str] = []
    alpha = _registration("mcp_alpha_widget", calls=calls)
    capability = ToolSearchCapability((alpha,))
    store = _Store()
    all_registrations = (*capability.tool_registrations(), alpha)
    executor = ToolExecutorImpl(all_registrations, store)
    context = _context()

    outcome = await executor.execute(
        ToolCall(
            tool_call_id="c1",
            name="tool_search",
            arguments={"query": "no such tool"},
        ),
        context,
    )
    assert outcome.status is ToolStatus.SUCCEEDED
    assert calls == []
    assert capability.hidden_tools(context) == frozenset({"mcp_alpha_widget"})
    assert [event.type for event in store.events] == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]


async def test_discovery_is_session_scoped_and_released_explicitly() -> None:
    alpha = _registration("mcp_alpha_widget")
    capability = ToolSearchCapability((alpha,))
    assert "mcp_alpha_widget" in capability.hidden_tools(_context("s1"))
    await _discover(capability, "s1", "mcp_alpha_widget")
    assert "mcp_alpha_widget" not in capability.hidden_tools(_context("s1"))

    capability.release_session("s1")
    assert "mcp_alpha_widget" in capability.hidden_tools(_context("s1"))

    await _discover(capability, "s1", "mcp_alpha_widget")
    await capability.cancel("s1", None)
    assert "mcp_alpha_widget" in capability.hidden_tools(_context("s1"))


async def test_session_lru_evicts_least_recently_used() -> None:
    alpha = _registration("mcp_alpha_widget")
    capability = ToolSearchCapability((alpha,), max_sessions=2)
    await _discover(capability, "s1", "mcp_alpha_widget")
    await _discover(capability, "s2", "mcp_alpha_widget")

    # Touch s1 so s2 is the least recently used entry.
    assert "mcp_alpha_widget" not in capability.hidden_tools(_context("s1"))
    await _discover(capability, "s3", "mcp_alpha_widget")

    assert "mcp_alpha_widget" not in capability.hidden_tools(_context("s1"))
    assert "mcp_alpha_widget" in capability.hidden_tools(_context("s2"))
    assert "mcp_alpha_widget" not in capability.hidden_tools(_context("s3"))


async def test_search_never_returns_non_candidate_schemas() -> None:
    forge = _registration(
        "forge_task_create",
        description="Create a Forge task with secret_schema_field",
    )
    basic = _registration(
        "read_file", description="Read a file with secret_schema_field"
    )
    alpha = _registration("mcp_alpha_widget")
    capability = ToolSearchCapability((forge, basic, alpha))

    forge_result = await _discover(capability, "s1", "forge_task_create")
    assert "No tool matched" in forge_result.text
    assert "secret_schema_field" not in forge_result.text

    read_result = await _discover(capability, "s1", "read_file")
    assert "No tool matched" in read_result.text
    assert "secret_schema_field" not in read_result.text
    assert capability.hidden_tools(_context("s1")) == frozenset(
        {"mcp_alpha_widget"}
    )


async def test_same_step_search_does_not_refresh_code_program_policy() -> None:
    calls: list[str] = []
    alpha = _registration(
        "mcp_alpha_widget",
        calls=calls,
        parallel_safe=True,
    )
    search = ToolSearchCapability((alpha,))
    code = CodeExecutionCapability().for_turn()
    registrations = (
        search.tool_registrations()[0],
        alpha,
        *code.tool_registrations(),
    )
    executor = ToolExecutorImpl(registrations, _Store())
    code.bind_executor(executor)
    definitions = tuple(item.definition for item in registrations)
    assembler = _assembler(search)
    context = _context("s1")

    first = await _assemble(assembler, context, definitions)
    assert "mcp_alpha_widget" not in [tool.name for tool in first.tools]
    assert first.policy is not None
    search_outcome = await executor.execute(
        ToolCall(
            tool_call_id="search-1",
            name="tool_search",
            arguments={"query": "mcp_alpha_widget"},
        ),
        replace(context, policy=first.policy),
    )
    assert search_outcome.status is ToolStatus.SUCCEEDED

    program = (
        'result = await tools.call("mcp_alpha_widget", {})\n'
        'return result["status"]'
    )
    same_step = await executor.execute(
        ToolCall(
            tool_call_id="code-1",
            name="execute_program",
            arguments={"code": program},
        ),
        replace(context, policy=first.policy),
    )
    assert same_step.status is ToolStatus.SUCCEEDED
    assert same_step.content is not None
    assert same_step.content["subcalls"] == [
        {
            "tool_call_id": "code-1:sub:01",
            "tool": "mcp_alpha_widget",
            "status": "rejected",
            "error": "tool_denied",
        }
    ]
    assert calls == []

    second = await _assemble(
        assembler, context.with_step("t1:step:2"), definitions
    )
    assert "mcp_alpha_widget" in [tool.name for tool in second.tools]
    assert second.policy is not None
    next_step = await executor.execute(
        ToolCall(
            tool_call_id="code-2",
            name="execute_program",
            arguments={"code": program},
        ),
        replace(context.with_step("t1:step:2"), policy=second.policy),
    )
    assert next_step.status is ToolStatus.SUCCEEDED
    assert calls == ["mcp_alpha_widget"]


def test_no_candidates_means_no_search_tool_and_no_prompt() -> None:
    capability = ToolSearchCapability(())
    assert capability.has_candidates is False
    assert capability.tool_registrations() == ()
    assert capability.prompt_context(_context()) is None
    assert capability.hidden_tools(_context()) == frozenset()

    basic_only = ToolSearchCapability((_registration("read_file"),))
    assert basic_only.tool_registrations() == ()
    assert basic_only.prompt_context(_context()) is None


def test_prompt_catalog_is_short() -> None:
    capability = ToolSearchCapability(
        tuple(_registration(f"mcp_tool_{index:02d}") for index in range(50)),
        max_catalog_entries=3,
    )
    text = capability.prompt_context(_context())
    assert text is not None
    assert text.count("\n- mcp_tool_") == 3
    assert "- ... and 47 more" in text

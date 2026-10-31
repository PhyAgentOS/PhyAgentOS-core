"""Restricted code execution: filtering, limits, denial, cancellation."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

import PhyAgentOS.tooling.code_execution as code_module
from PhyAgentOS.agent.capabilities.code_execution_capability import (
    CodeExecutionCapability,
)
from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.loop import LoopDriverImpl
from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl
from PhyAgentOS.agent.core.tools import ToolExecutorImpl
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolPolicy,
    ToolRegistration,
    ToolStatus,
    TurnRequest,
    TurnStart,
    TurnStatus,
)
from PhyAgentOS.tooling.code_execution import (
    CodeExecutionLimits,
    ProgramCancelledError,
    ProgramToolResult,
    run_program,
)


class _Store:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        stored = replace(event, sequence=len(self.events))
        self.events.append(stored)
        return stored


class _FaultStore(_Store):
    """Fails one durable append for a chosen tool call id."""

    def __init__(self, event_type: EventType, tool_call_id: str) -> None:
        super().__init__()
        self._event_type = event_type
        self._tool_call_id = tool_call_id
        self.fired = False

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        if (
            not self.fired
            and event.type is self._event_type
            and event.payload.get("tool_call_id") == self._tool_call_id
        ):
            self.fired = True
            raise RuntimeError("injected durable write failure")
        return await super().append(event)


class _RegisteredExecutor(ToolExecutorImpl):
    """Real executor plus the registrations property the contract promises."""

    def __init__(self, registrations, store) -> None:
        self._registrations = tuple(registrations)
        super().__init__(self._registrations, store)

    @property
    def registrations(self):
        return self._registrations


def _context(
    *,
    session_id: str = "s1",
    token: CancellationToken | None = None,
    policy: ToolPolicy | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="t1",
        step_id="t1:step:1",
        cancellation=token if token is not None else CancellationToken(),
        budget=Budget(max_steps=4),
        policy=policy if policy is not None else ToolPolicy(),
    )


def _parameters() -> dict:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def _registration(
    name: str,
    handler,
    *,
    parallel_safe: bool = True,
) -> ToolRegistration:
    return ToolRegistration(
        definition=ToolDefinition(
            name=name, description=f"Tool {name}.", parameters=_parameters()
        ),
        handler=handler,
        parallel_safe=parallel_safe,
    )


def _success(name: str, text: str):
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        del context
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=name,
            status=ToolStatus.SUCCEEDED,
            text=text,
            content={"name": name},
        )

    return handler


def _build(
    registrations,
    *,
    limits: CodeExecutionLimits | None = None,
    store: _Store | None = None,
):
    capability = CodeExecutionCapability(limits=limits)
    all_registrations = (*registrations, *capability.tool_registrations())
    events = store if store is not None else _Store()
    executor = _RegisteredExecutor(all_registrations, events)
    capability.bind_executor(executor)
    definitions = tuple(item.definition for item in all_registrations)
    return capability, executor, events, definitions


async def _execute(executor, definitions, code: str, context):
    call = ToolCall(
        tool_call_id="outer-1",
        name="execute_program",
        arguments={"code": code},
    )
    return await executor.execute(call, context)


async def test_program_filters_tool_output_to_summary() -> None:
    secret = "RAW-SECRET-ALPHA " * 20
    read_a = _registration("read_a", _success("read_a", secret))
    capability, executor, store, definitions = _build((read_a,))
    context = _context()

    outcome = await _execute(
        executor,
        definitions,
        'data = await tools.call("read_a", {})\nreturn {"chars": len(data["text"])}',
        context,
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert "RAW-SECRET-ALPHA" not in outcome.text
    assert "RAW-SECRET-ALPHA" not in json.dumps(dict(outcome.content))
    assert "chars" in outcome.text
    assert outcome.content is not None
    assert outcome.content["subcalls"] == [
        {
            "tool_call_id": "outer-1:sub:01",
            "tool": "read_a",
            "status": "succeeded",
            "error": None,
        }
    ]
    kinds = [
        event.type
        for event in store.events
        if event.payload.get("tool_call_id") == "outer-1:sub:01"
    ]
    assert kinds == [
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ]


async def test_parallel_read_tools_have_unique_subcall_events() -> None:
    calls: list[str] = []
    sleep_gate_started = []

    def handler(name: str):
        async def dispatch(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            del context
            calls.append(name)
            sleep_gate_started.append(name)
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=name,
                status=ToolStatus.SUCCEEDED,
                text=f"{name} data",
            )

        return dispatch

    read_a = _registration("read_a", handler("read_a"))
    read_b = _registration("read_b", handler("read_b"))
    capability, executor, store, definitions = _build((read_a, read_b))
    context = _context()

    outcome = await _execute(
        executor,
        definitions,
        (
            "results = await parallel([\n"
            '    await tools.call("read_a", {}),\n'
            '    await tools.call("read_b", {}),\n'
            "])\n"
            'return [results[0]["status"], results[1]["status"]]'
        ),
        context,
    )

    assert outcome.status is ToolStatus.SUCCEEDED
    assert calls == ["read_a", "read_b"]
    assert len(sleep_gate_started) == 2
    subcall_ids = ["outer-1:sub:01", "outer-1:sub:02"]
    for subcall_id in subcall_ids:
        kinds = [
            event.type
            for event in store.events
            if event.payload.get("tool_call_id") == subcall_id
        ]
        assert kinds == [
            EventType.TOOL_INTENT,
            EventType.TOOL_DISPATCHED,
            EventType.TOOL_SETTLED,
        ]


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [
        ("import os\nreturn os.getcwd()", "program_forbidden_syntax"),
        ("return (1).__class__", "program_forbidden_syntax"),
        ("return (1).__class__.__bases__", "program_forbidden_syntax"),
        ("return getattr(1, 'real')", "program_forbidden_call"),
        ("return eval('1')", "program_forbidden_call"),
    ],
)
async def test_import_attribute_and_reflection_are_rejected(
    code: str, expected_code: str
) -> None:
    read_a = _registration("read_a", _success("read_a", "ok"))
    _, executor, _, definitions = _build((read_a,))
    outcome = await _execute(executor, definitions, code, _context())
    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None and outcome.error.code == expected_code


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [
        (
            "value = 10\nfor i in range(20):\n    value = value * value\nreturn value",
            "program_integer_limit",
        ),
        ("return 2 ** 100", "program_forbidden_syntax"),
        ("return [x for x in range(3)]", "program_forbidden_syntax"),
        ('return "a" * 100000', "program_result_limit"),
        ("return [0] * 100000", "program_result_limit"),
        ("return [[0] * 100] * 100", "program_result_limit"),
        ('return "%1000000000s" % "x"', "program_forbidden_operation"),
    ],
)
async def test_allocation_limits_are_checked_before_building_values(
    code: str, expected_code: str
) -> None:
    read_a = _registration("read_a", _success("read_a", "ok"))
    _, executor, _, definitions = _build((read_a,))
    outcome = await _execute(executor, definitions, code, _context())
    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None and outcome.error.code == expected_code


async def test_oversized_literal_is_rejected_before_evaluation() -> None:
    literal = "[" + ", ".join("0" for _ in range(101)) + "]"
    read_a = _registration("read_a", _success("read_a", "ok"))
    _, executor, _, definitions = _build((read_a,))
    outcome = await _execute(executor, definitions, f"return {literal}", _context())
    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "program_container_limit"


def test_tooling_runner_has_no_agent_runtime_import() -> None:
    tree = ast.parse(Path(code_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(
        name == "PhyAgentOS.agent" or name.startswith("PhyAgentOS.agent.")
        for name in imported
    )


async def test_loop_and_output_limits() -> None:
    read_a = _registration("read_a", _success("read_a", "ok"))
    _, executor, _, definitions = _build((read_a,))
    loop = await _execute(
        executor,
        definitions,
        "for i in range(100000):\n    value = i\nreturn value",
        _context(),
    )
    assert loop.status is ToolStatus.FAILED
    assert loop.error is not None and loop.error.code == "program_loop_limit"

    printed = await _execute(
        executor,
        definitions,
        'print("a" * 3000)\nprint("b" * 3000)',
        _context(),
    )
    assert printed.status is ToolStatus.FAILED
    assert printed.error is not None and printed.error.code == "program_output_limit"


async def test_tool_call_limit() -> None:
    calls: list[str] = []

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        del context
        calls.append(call.tool_call_id)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name="read_a",
            status=ToolStatus.SUCCEEDED,
            text="ok",
        )

    read_a = _registration("read_a", handler)
    _, executor, _, definitions = _build(
        (read_a,),
        limits=CodeExecutionLimits(max_tool_calls=2, max_loop_iterations=5),
    )
    outcome = await _execute(
        executor,
        definitions,
        "for i in range(5):\n    await tools.call(\"read_a\", {})\nreturn i",
        _context(),
    )
    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None and outcome.error.code == "program_tool_call_limit"
    assert calls == ["outer-1:sub:01", "outer-1:sub:02"]


async def test_settled_write_failure_stops_program_before_next_subcall() -> None:
    calls: list[str] = []

    def handler(name: str):
        async def dispatch(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            del context
            calls.append(name)
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=name,
                status=ToolStatus.SUCCEEDED,
                text="ok",
            )

        return dispatch

    read_a = _registration("read_a", handler("read_a"))
    read_b = _registration("read_b", handler("read_b"))
    store = _FaultStore(EventType.TOOL_SETTLED, "outer-1:sub:01")
    _, executor, store, definitions = _build(
        (read_a, read_b), store=store
    )
    outcome = await _execute(
        executor,
        definitions,
        'a = await tools.call("read_a", {})\n'
        'b = await tools.call("read_b", {})\n'
        'return [a["status"], b["status"]]',
        _context(),
    )
    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "tool_settled_write_failed"
    assert calls == ["read_a"]
    assert store.fired is True


async def test_parallel_intent_write_failure_does_not_dispatch_remaining() -> None:
    calls: list[str] = []

    def handler(name: str):
        async def dispatch(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            del context
            calls.append(name)
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=name,
                status=ToolStatus.SUCCEEDED,
                text="ok",
            )

        return dispatch

    read_a = _registration("read_a", handler("read_a"))
    read_b = _registration("read_b", handler("read_b"))
    store = _FaultStore(EventType.TOOL_INTENT, "outer-1:sub:01")
    _, executor, store, definitions = _build(
        (read_a, read_b), store=store
    )
    outcome = await _execute(
        executor,
        definitions,
        "results = await parallel([\n"
        '    await tools.call("read_a", {}),\n'
        '    await tools.call("read_b", {}),\n'
        "])",
        _context(),
    )
    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "tool_intent_write_failed"
    assert calls == []
    assert store.fired is True


async def test_persistence_failure_stops_driver_before_next_model() -> None:
    calls: list[str] = []

    async def read_a_handler(
        call: ToolCall, context: ExecutionContext
    ) -> ToolOutcome:
        del context
        calls.append("read_a")
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name="read_a",
            status=ToolStatus.SUCCEEDED,
            text="ok",
        )

    read_a = _registration("read_a", read_a_handler)
    read_b = _registration("read_b", _success("read_b", "ok"))
    store = _FaultStore(EventType.TOOL_SETTLED, "outer-1:sub:01")
    _, executor, store, definitions = _build(
        (read_a, read_b), store=store
    )
    assembler = PromptAssemblerImpl("SYS")
    adapter = _CountingAdapter()
    driver = LoopDriverImpl(
        assembler=assembler,
        adapter=adapter,
        executor=executor,
        store=store,
        tools=definitions,
    )
    turn = TurnStart(
        request=TurnRequest(session_id="s1", text="run the program"),
        turn_id="t1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=3),
    )
    result = await driver.run(turn)
    assert result.status is TurnStatus.FAILED
    assert adapter.calls == 1
    assert calls == ["read_a"]
    assert result.error is not None
    assert "tool_settled_write_failed" in result.error


class _CountingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request) -> ModelResponse:
        del request
        self.calls += 1
        return ModelResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=(
                ToolCall(
                    tool_call_id="outer-1",
                    name="execute_program",
                    arguments={
                        "code": (
                            'a = await tools.call("read_a", {})\n'
                            'return a["status"]'
                        )
                    },
                ),
            ),
        )


async def test_unknown_tool_result_is_not_success() -> None:
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        del context
        message = "remote outcome unknown"
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name="read_a",
            status=ToolStatus.UNKNOWN,
            text=message,
            error=ToolError("remote_unknown", message),
        )

    read_a = _registration("read_a", handler)
    _, executor, _, definitions = _build((read_a,))
    outcome = await _execute(
        executor,
        definitions,
        'data = await tools.call("read_a", {})\nreturn data["status"]',
        _context(),
    )
    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "program_dependency_unknown"
    assert outcome.content is not None and outcome.content["unknown"] is True


async def test_cancel_before_dispatch_and_during_program() -> None:
    token = CancellationToken()
    token.request("stop")
    read_a = _registration("read_a", _success("read_a", "ok"))
    _, executor, _, definitions = _build((read_a,))
    outcome = await _execute(
        executor, definitions, 'return await tools.call("read_a", {})', _context(token=token)
    )
    assert outcome.status is ToolStatus.CANCELLED

    token = CancellationToken()

    async def dispatch(name, arguments, subcall_id):
        del arguments, subcall_id
        token.request("stop during subcall")
        return ProgramToolResult(status=ToolStatus.SUCCEEDED.value, text="late")

    with pytest.raises(ProgramCancelledError):
        await run_program(
            'await tools.call("read_a", {})',
            is_cancelled=lambda: token.cancelled,
            limits=CodeExecutionLimits(),
            available_tools=frozenset({"read_a"}),
            dispatch=dispatch,
            outer_call_id="outer-1",
        )


async def test_non_exposed_and_policy_denied_tools_are_not_executed() -> None:
    calls: list[str] = []
    loop_calls: list[str] = []

    def counter(name: str, bucket: list[str]):
        async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
            del context
            bucket.append(name)
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=name,
                status=ToolStatus.SUCCEEDED,
                text="ran",
            )

        return handler

    write_tool = _registration(
        "write_file", counter("write_file", calls), parallel_safe=False
    )
    forge_action = _registration(
        "forge_tool_start_action", counter("forge_tool_start_action", calls)
    )
    hidden_read = _registration(
        "mcp_hidden_read", counter("mcp_hidden_read", loop_calls)
    )
    read_a = _registration("read_a", _success("read_a", "ok"))
    allowed = {
        "read_a",
        "write_file",
        "forge_tool_start_action",
        "mcp_hidden_read",
        "execute_program",
    }
    capability, executor, _, definitions = _build(
        (read_a, write_tool, forge_action, hidden_read)
    )
    del capability
    context = _context(
        policy=ToolPolicy(allowed_tools=frozenset(allowed - {"mcp_hidden_read"}))
    )
    outcome = await _execute(
        executor,
        definitions,
        (
            'a = await tools.call("write_file", {})\n'
            'b = await tools.call("forge_tool_start_action", {})\n'
            'c = await tools.call("execute_program", {"code": "return 1"})\n'
            'd = await tools.call("mcp_hidden_read", {})\n'
            'return [a["status"], b["status"], c["status"], d["status"]]'
        ),
        context,
    )
    assert outcome.status is ToolStatus.SUCCEEDED
    assert calls == []
    assert loop_calls == []
    assert outcome.content is not None
    statuses = [
        (item["tool"], item["status"], item["error"])
        for item in outcome.content["subcalls"]
    ]
    assert statuses == [
        ("write_file", "rejected", "code_tool_not_exposed"),
        ("forge_tool_start_action", "rejected", "code_tool_not_exposed"),
        ("execute_program", "rejected", "code_tool_not_exposed"),
        ("mcp_hidden_read", "rejected", "tool_denied"),
    ]


async def test_for_turn_isolates_concurrent_executors() -> None:
    canonical = CodeExecutionCapability()
    first = canonical.for_turn()
    second = canonical.for_turn()
    assert canonical.is_bound is False
    assert first is not second

    read_a = _registration("read_a", _success("read_a", "ok"))
    first_store = _Store()
    second_store = _Store()
    first_executor = _RegisteredExecutor(
        (*first.tool_registrations(), read_a), first_store
    )
    second_executor = _RegisteredExecutor(
        (*second.tool_registrations(), read_a), second_store
    )
    first.bind_executor(first_executor)
    second.bind_executor(second_executor)

    first_outcome = await first_executor.execute(
        ToolCall(
            tool_call_id="outer-1",
            name="execute_program",
            arguments={"code": 'return await tools.call("read_a", {})'},
        ),
        _context(session_id="s1"),
    )
    second_outcome = await second_executor.execute(
        ToolCall(
            tool_call_id="outer-2",
            name="execute_program",
            arguments={"code": 'return await tools.call("read_a", {})'},
        ),
        _context(session_id="s2"),
    )

    assert first_outcome.status is ToolStatus.SUCCEEDED
    assert second_outcome.status is ToolStatus.SUCCEEDED
    assert any(event.session_id == "s1" for event in first_store.events)
    assert all(event.session_id != "s2" for event in first_store.events)
    assert any(event.session_id == "s2" for event in second_store.events)
    assert all(event.session_id != "s1" for event in second_store.events)

    with pytest.raises(RuntimeError, match="already bound"):
        first.bind_executor(second_executor)

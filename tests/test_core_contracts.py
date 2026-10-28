"""Contract tests for the new agent core.

These tests pin down the rules later components must not weaken: non-empty
identity, immutable execution context, cooperative cancellation, distinct
tool outcomes, request snapshots, event identity, and protocol shape.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace

import pytest

from PhyAgentOS.agent.core import (
    AgentHost,
    AgentRuntime,
    Budget,
    CallKind,
    CancellationToken,
    Capability,
    DomainRefs,
    EventType,
    ExecutionContext,
    LoopDriver,
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    OperationCancelledError,
    PromptAssembler,
    PromptBundle,
    RecoveryReport,
    RuntimeEvent,
    SessionStore,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolExecutor,
    ToolOutcome,
    ToolPolicy,
    ToolRegistration,
    ToolStatus,
    TurnRequest,
    TurnResult,
    TurnStart,
    TurnStatus,
    UnfinishedCall,
)


def _context(**overrides: object) -> ExecutionContext:
    values: dict[str, object] = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "step_id": "step-1",
        "cancellation": CancellationToken(),
        "budget": Budget(max_steps=4),
    }
    values.update(overrides)
    return ExecutionContext(**values)  # type: ignore[arg-type]


def _echo_tool() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Echo one string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


@pytest.mark.parametrize(
    "overrides",
    [{"session_id": ""}, {"turn_id": "   "}, {"step_id": ""}],
)
def test_execution_context_rejects_empty_identity(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _context(**overrides)


def test_other_contracts_reject_empty_identity() -> None:
    with pytest.raises(ValueError):
        RuntimeEvent(type=EventType.TURN_STARTED, session_id="", turn_id="turn-1")
    with pytest.raises(ValueError):
        TurnResult(session_id="session-1", turn_id="", status=TurnStatus.COMPLETED)
    with pytest.raises(ValueError):
        ToolOutcome(
            tool_call_id="",
            tool_name="echo",
            status=ToolStatus.SUCCEEDED,
            text="ok",
        )
    with pytest.raises(ValueError):
        ToolCall(tool_call_id="call-1", name="", arguments={})
    with pytest.raises(ValueError):
        ModelRequest(model_call_id="", messages=[{"role": "user", "content": "hi"}])


def test_execution_context_is_frozen_and_steps_share_turn_state() -> None:
    token = CancellationToken()
    policy = ToolPolicy(allowed_tools=frozenset({"echo"}))
    context = _context(cancellation=token, policy=policy, reply_target="cli:1")

    with pytest.raises(FrozenInstanceError):
        context.step_id = "step-2"  # type: ignore[misc]

    next_step = context.with_step("step-2")
    assert next_step.step_id == "step-2"
    assert next_step.session_id == context.session_id
    assert next_step.cancellation is token
    assert next_step.policy is policy
    assert next_step.reply_target == "cli:1"
    assert context.step_id == "step-1"


def test_tool_policy_denies_before_applying_the_allowlist() -> None:
    assert ToolPolicy().allows("anything")
    policy = ToolPolicy(
        allowed_tools=frozenset({"read", "write"}),
        denied_tools=frozenset({"write"}),
    )
    assert policy.allows("read")
    assert not policy.allows("write")
    assert not policy.allows("unlisted")


def test_budget_allows_step_ceiling_and_deadline() -> None:
    with pytest.raises(ValueError):
        Budget(max_steps=0)

    expired = Budget(max_steps=1, deadline=time.monotonic() - 1)
    assert expired.expired()
    assert not Budget(max_steps=1).expired()
    assert not Budget(max_steps=1, deadline=time.monotonic() + 60).expired()


async def test_cancellation_token_is_cooperative_and_keeps_first_reason() -> None:
    token = CancellationToken()
    waiter = asyncio.create_task(token.wait())
    assert not token.cancelled

    token.request("user stop")
    await asyncio.wait_for(waiter, timeout=1)

    assert token.cancelled
    assert token.reason == "user stop"
    with pytest.raises(OperationCancelledError) as excinfo:
        token.raise_if_cancelled()
    assert excinfo.value.reason == "user stop"

    token.request("second reason")
    assert token.reason == "user stop"


def test_tool_statuses_remain_distinct() -> None:
    assert len({status.value for status in ToolStatus}) == 6

    succeeded = ToolOutcome(
        tool_call_id="call-1", tool_name="echo", status=ToolStatus.SUCCEEDED, text="ok"
    )
    assert succeeded.is_success and succeeded.is_terminal

    for status, error in (
        (ToolStatus.FAILED, ToolError("boom", "it broke")),
        (ToolStatus.REJECTED, ToolError("denied", "policy denied the call")),
        (ToolStatus.CANCELLED, None),
        (ToolStatus.UNKNOWN, None),
    ):
        outcome = ToolOutcome(
            tool_call_id="call-1", tool_name="echo", status=status, text="detail", error=error
        )
        assert outcome.is_terminal
        assert not outcome.is_success

    running = ToolOutcome(
        tool_call_id="call-1", tool_name="echo", status=ToolStatus.RUNNING, text="working"
    )
    assert not running.is_terminal
    with pytest.raises(ValueError):
        running.for_model()


def test_tool_outcome_error_rules_keep_states_honest() -> None:
    with pytest.raises(ValueError):
        ToolOutcome(
            tool_call_id="call-1",
            tool_name="echo",
            status=ToolStatus.SUCCEEDED,
            text="ok",
            error=ToolError("boom", "contradiction"),
        )
    with pytest.raises(ValueError):
        ToolOutcome(
            tool_call_id="call-1", tool_name="echo", status=ToolStatus.FAILED, text="no error"
        )


def test_domain_acceptance_is_not_local_success() -> None:
    unknown = ToolOutcome(
        tool_call_id="call-1",
        tool_name="forge_task",
        status=ToolStatus.UNKNOWN,
        text="request accepted; stop not confirmed",
        content={"domain_status": "accepted"},
        domain_refs=DomainRefs(task_id="task-1", revision_id="rev-1"),
    )
    assert not unknown.is_success
    text = unknown.for_model()
    assert "unknown" in text.lower()
    assert "do not retry automatically" in text
    assert "succeeded" not in text

    pending = ToolOutcome(
        tool_call_id="call-2",
        tool_name="forge_task",
        status=ToolStatus.RUNNING,
        text="dispatched",
        domain_refs=DomainRefs(task_id="task-1"),
    )
    assert not pending.is_terminal
    assert not pending.is_success
    assert pending.domain_refs.task_id == "task-1"


def test_model_request_snapshots_messages_and_tools() -> None:
    messages = [{"role": "user", "content": "hello"}]
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    tool = ToolDefinition(name="read_file", description="Read a file.", parameters=parameters)
    request = ModelRequest(
        model_call_id="model-1",
        messages=messages,
        tools=[tool],
        model="fake-model",
        temperature=0.2,
        max_tokens=128,
    )

    messages[0]["content"] = "mutated"
    parameters["properties"]["path"]["type"] = "integer"

    assert request.to_messages() == [{"role": "user", "content": "hello"}]
    provider_tool = request.to_provider_tools()[0]
    assert provider_tool["type"] == "function"
    assert provider_tool["function"]["parameters"]["properties"]["path"]["type"] == "string"

    with pytest.raises(TypeError):
        request.messages[0]["content"] = "nope"  # type: ignore[index]
    with pytest.raises(TypeError):
        tool.parameters["type"] = "array"  # type: ignore[index]


def test_tool_call_and_response_keep_provider_details() -> None:
    call = ToolCall(
        tool_call_id="call-1",
        name="echo",
        arguments={"text": "a"},
        raw_arguments='{"text": "a", "trunc',
        provider_fields={"index": 0},
        function_provider_fields={"signature": "sig"},
    )
    handler_arguments = call.arguments_dict()
    handler_arguments["text"] = "b"
    assert call.arguments["text"] == "a"
    assert call.raw_arguments is not None and call.raw_arguments.endswith('"trunc')
    assert call.function_provider_fields == {"signature": "sig"}

    response = ModelResponse(
        content=None,
        finish_reason="tool_calls",
        tool_calls=(call,),
        usage={"prompt_tokens": 3, "total_tokens": 5},
    )
    assert response.has_tool_calls
    assert response.tool_calls[0].tool_call_id == "call-1"

    with pytest.raises(ValueError):
        ModelResponse(content=None, finish_reason="stop", usage={"total_tokens": -1})


async def test_tool_handler_receives_the_full_validated_call() -> None:
    seen: list[tuple[ToolCall, ExecutionContext]] = []

    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        seen.append((call, context))
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=str(call.arguments_dict()["text"]),
        )

    registration = ToolRegistration(definition=_echo_tool(), handler=handler)
    call = ToolCall(tool_call_id="call-9", name="echo", arguments={"text": "hello"})
    context = _context()

    outcome = await registration.handler(call, context)

    assert seen == [(call, context)]
    assert call.arguments_dict() == {"text": "hello"}
    assert outcome.tool_call_id == call.tool_call_id
    assert outcome.tool_name == call.name
    assert outcome.text == "hello"
    assert registration.parallel_safe is False


def test_model_response_rejects_provider_error_as_finish_reason() -> None:
    with pytest.raises(ValueError):
        ModelResponse(content="provider exploded", finish_reason="error")
    assert ModelResponse(content=None, finish_reason="length").finish_reason == "length"


def test_turn_request_requires_meaningful_text() -> None:
    with pytest.raises(ValueError):
        TurnRequest(session_id="session-1", text="")
    with pytest.raises(ValueError):
        TurnRequest(session_id="session-1", text=" \n\t ")
    assert TurnRequest(session_id="session-1", text="  hi  ").text == "  hi  "


def test_turn_events_require_turn_identity() -> None:
    with pytest.raises(ValueError):
        RuntimeEvent(type=EventType.TURN_COMPLETED, session_id="session-1")
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TURN_STARTED, session_id="session-1", turn_id="turn-1", step_id="step-1"
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_STARTED,
            session_id="session-1",
            turn_id="turn-1",
            payload={"model_call_id": "model-1"},
        )

    accepted = RuntimeEvent(
        type=EventType.INPUT_ACCEPTED, session_id="session-1", payload={"characters": 5}
    )
    assert accepted.turn_id is None
    assert accepted.sequence is None


def test_events_cannot_record_unfinished_work_as_success() -> None:
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TOOL_SETTLED,
            session_id="session-1",
            turn_id="turn-1",
            step_id="step-1",
            payload={"tool_call_id": "call-1", "status": ToolStatus.RUNNING.value},
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TOOL_SETTLED,
            session_id="session-1",
            turn_id="turn-1",
            step_id="step-1",
            payload={"status": ToolStatus.SUCCEEDED.value},
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_FAILED,
            session_id="session-1",
            turn_id="turn-1",
            step_id="step-1",
            payload={"model_call_id": "model-1"},
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TURN_COMPLETED,
            session_id="session-1",
            turn_id="turn-1",
            payload={"error": "model adapter exploded"},
        )


def test_event_payload_is_read_only_and_sequence_belongs_to_the_store() -> None:
    event = RuntimeEvent(
        type=EventType.TOOL_SETTLED,
        session_id="session-1",
        turn_id="turn-1",
        step_id="step-1",
        payload={"tool_call_id": "call-1", "status": ToolStatus.SUCCEEDED},
    )
    with pytest.raises(TypeError):
        event.payload["tool_call_id"] = "call-2"  # type: ignore[index]

    stored = RuntimeEvent(
        type=EventType.TURN_STARTED, session_id="session-1", turn_id="turn-1", sequence=7
    )
    assert stored.sequence == 7
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TURN_STARTED, session_id="session-1", turn_id="turn-1", sequence=-1
        )


def test_turn_result_refuses_contradictory_states() -> None:
    with pytest.raises(ValueError):
        TurnResult(
            session_id="session-1",
            turn_id="turn-1",
            status=TurnStatus.COMPLETED,
            error="boom",
        )
    with pytest.raises(ValueError):
        TurnResult(session_id="session-1", turn_id="turn-1", status=TurnStatus.FAILED)

    interrupted = TurnResult(
        session_id="session-1", turn_id="turn-1", status=TurnStatus.INTERRUPTED
    )
    assert interrupted.status is TurnStatus.INTERRUPTED
    assert interrupted.reply is None


def test_recovery_marks_unfinished_work_without_fabricating_success() -> None:
    report = RecoveryReport(
        session_id="session-1",
        unfinished_turns=("turn-1",),
        unfinished_calls=(
            UnfinishedCall(
                session_id="session-1",
                turn_id="turn-1",
                step_id="step-1",
                kind=CallKind.MODEL,
                call_id="model-1",
            ),
            UnfinishedCall(
                session_id="session-1",
                turn_id="turn-1",
                step_id="step-2",
                kind=CallKind.TOOL,
                call_id="call-1",
                dispatched=True,
            ),
        ),
    )
    events = report.interruption_events()

    assert [event.type for event in events] == [
        EventType.MODEL_ATTEMPT_INTERRUPTED,
        EventType.TOOL_SETTLED,
    ]
    assert events[1].payload["status"] == ToolStatus.UNKNOWN.value
    assert all(event.sequence is None for event in events)
    assert all("succeeded" not in str(event.payload).lower() for event in events)

    with pytest.raises(ValueError):
        RecoveryReport(
            session_id="session-1",
            unfinished_calls=(
                UnfinishedCall(
                    session_id="session-2",
                    turn_id="turn-1",
                    step_id="step-1",
                    kind=CallKind.TOOL,
                    call_id="call-1",
                ),
            ),
        )


def test_recovery_keeps_intent_only_tool_calls_out_of_unknown() -> None:
    report = RecoveryReport(
        session_id="session-1",
        unfinished_calls=(
            UnfinishedCall(
                session_id="session-1",
                turn_id="turn-1",
                step_id="step-1",
                kind=CallKind.TOOL,
                call_id="call-intent",
            ),
            UnfinishedCall(
                session_id="session-1",
                turn_id="turn-1",
                step_id="step-2",
                kind=CallKind.TOOL,
                call_id="call-dispatched",
                dispatched=True,
            ),
        ),
    )
    events = report.interruption_events()

    assert [event.type for event in events] == [
        EventType.TOOL_SETTLED,
        EventType.TOOL_SETTLED,
    ]
    intent_only, dispatched = events
    assert intent_only.payload["status"] == ToolStatus.FAILED.value
    assert intent_only.payload["error"]["code"] == "not_dispatched"
    assert "never called" in intent_only.payload["error"]["message"]
    assert dispatched.payload["status"] == ToolStatus.UNKNOWN.value
    assert "error" not in dispatched.payload

    with pytest.raises(TypeError):
        UnfinishedCall(
            session_id="session-1",
            turn_id="turn-1",
            step_id="step-1",
            kind=CallKind.TOOL,
            call_id="call-1",
            dispatched="yes",  # type: ignore[arg-type]
        )


def test_turn_start_history_is_optional_and_read_only() -> None:
    turn = TurnStart(
        request=TurnRequest(session_id="session-1", text="hi"),
        turn_id="turn-2",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=2),
    )
    assert turn.history == ()

    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
    ]
    turn = TurnStart(
        request=TurnRequest(session_id="session-1", text="hi"),
        turn_id="turn-2",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=2),
        history=history,
    )
    history[0]["content"] = "mutated"
    history.append({"role": "user", "content": "new"})

    assert len(turn.history) == 2
    assert turn.history[0]["content"] == "old"
    with pytest.raises(TypeError):
        turn.history[0]["content"] = "nope"  # type: ignore[index]


async def test_capability_contexts_are_per_call() -> None:
    capability = _FakeCapability()
    first = _context()
    other = _context(session_id="session-2", turn_id="turn-2")

    assert capability.prompt_context(first) == "session=session-1"
    assert capability.prompt_context(other) == "session=session-2"
    assert capability.prompt_contexts == [first, other]

    await capability.cancel("session-2", None)
    await capability.cancel("session-1", first)
    assert capability.cancelled == [("session-2", None), ("session-1", first)]


def test_model_response_preserves_thinking_blocks_as_snapshot() -> None:
    blocks = [{"type": "thinking", "thinking": "plan"}]
    response = ModelResponse(content="ok", finish_reason="stop", thinking_blocks=blocks)

    blocks[0]["thinking"] = "mutated"
    blocks.append({"type": "thinking", "thinking": "extra"})

    assert response.thinking_blocks == ({"type": "thinking", "thinking": "plan"},)
    with pytest.raises(TypeError):
        response.thinking_blocks[0]["thinking"] = "nope"  # type: ignore[index]

    assert ModelResponse(content="ok", finish_reason="stop").thinking_blocks is None


class _FakeAssembler:
    def __init__(self, fragment: Callable[[ExecutionContext], str | None] | None = None) -> None:
        self._fragment = fragment
        self.contexts: list[ExecutionContext] = []

    async def assemble(
        self,
        context: ExecutionContext,
        *,
        history: tuple[dict[str, object], ...] | list[dict[str, object]],
        tools: tuple[ToolDefinition, ...] | list[ToolDefinition],
    ) -> PromptBundle:
        self.contexts.append(context)
        messages: list[dict[str, object]] = list(history)
        if self._fragment is not None:
            text = self._fragment(context)
            if text:
                messages.insert(0, {"role": "system", "content": text})
        return PromptBundle(messages=tuple(messages), tools=tools)


class _FakeModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(content="done", finish_reason="stop")


class _FakeExecutor:
    def __init__(self) -> None:
        self.contexts: list[ExecutionContext] = []

    async def execute(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        self.contexts.append(context)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text="ok",
        )


class _FakeStore:
    def __init__(self, conversation: list[dict[str, object]] | None = None) -> None:
        self.events: list[RuntimeEvent] = []
        self.conversation = list(conversation or [])
        self.closed = False

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        stored = replace(event, sequence=len(self.events))
        self.events.append(stored)
        return stored

    async def read_turn(self, session_id: str, turn_id: str) -> list[RuntimeEvent]:
        return [
            event
            for event in self.events
            if event.session_id == session_id and event.turn_id == turn_id
        ]

    async def read_session(
        self, session_id: str, *, after_sequence: int = 0
    ) -> list[RuntimeEvent]:
        return [
            event
            for event in self.events
            if event.session_id == session_id
            and event.sequence is not None
            and event.sequence >= after_sequence
        ]

    async def read_conversation(self, session_id: str) -> list[dict[str, object]]:
        return list(self.conversation)

    async def recover(self, session_id: str) -> RecoveryReport:
        return RecoveryReport(session_id=session_id)

    async def aclose(self) -> None:
        self.closed = True


class _FakeCapability:
    name = "fake"

    def __init__(self, log: list[str] | None = None) -> None:
        self.cancelled: list[tuple[str, ExecutionContext | None]] = []
        self.prompt_contexts: list[ExecutionContext] = []
        self.closed = False
        self.log = log if log is not None else []

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return ()

    def prompt_context(self, context: ExecutionContext) -> str | None:
        self.prompt_contexts.append(context)
        return f"session={context.session_id}"

    async def cancel(self, session_id: str, context: ExecutionContext | None) -> None:
        self.cancelled.append((session_id, context))
        self.log.append(f"capability.cancel:{session_id}")

    async def aclose(self) -> None:
        self.closed = True
        self.log.append("capability.aclose")


class _FakeDriver:
    async def run(self, turn: TurnStart) -> TurnResult:
        assert turn.cancellation.cancelled is False
        return TurnResult(
            session_id=turn.request.session_id,
            turn_id=turn.turn_id,
            status=TurnStatus.COMPLETED,
            reply=f"echo:{turn.request.text}",
        )


class _FakeRuntime:
    """Minimal runtime showing the injected stop/cancel contract."""

    def __init__(
        self,
        history: list[dict[str, object]] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.submitted: list[TurnRequest] = []
        self.cancelled: list[str] = []
        self.closed = False
        self.stopped = False
        self.log = log if log is not None else []
        self.history = list(history or [])
        self.active_context: ExecutionContext | None = _context()

    async def submit(self, request: TurnRequest) -> TurnResult:
        self.submitted.append(request)
        return TurnResult(
            session_id=request.session_id,
            turn_id="turn-1",
            status=TurnStatus.COMPLETED,
            reply=request.text,
        )

    async def stop(self) -> None:
        self.stopped = True
        self.log.append("runtime.stop")

    async def cancel(self, session_id: str) -> ExecutionContext | None:
        self.cancelled.append(session_id)
        return self.active_context

    async def aclose(self) -> None:
        self.closed = True
        self.log.append("runtime.aclose")


class _FakeHost:
    """Delegating host: no config, no component construction."""

    def __init__(self, runtime: AgentRuntime, capabilities: list[Capability]) -> None:
        self._runtime = runtime
        self._capabilities = capabilities
        self._sessions: set[str] = set()
        self._closed = False

    async def submit(self, request: TurnRequest) -> TurnResult:
        if self._closed:
            raise RuntimeError("host is closed")
        self._sessions.add(request.session_id)
        return await self._runtime.submit(request)

    async def cancel(self, session_id: str) -> None:
        context = await self._runtime.cancel(session_id)
        for capability in self._capabilities:
            await capability.cancel(session_id, context)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # stop() settles every accepted submit, queued turns included, and
        # keeps the store open for the capability stop requests below.
        await self._runtime.stop()
        for session_id in sorted(self._sessions):
            for capability in self._capabilities:
                await capability.cancel(session_id, None)
        for capability in self._capabilities:
            await capability.aclose()
        await self._runtime.aclose()


async def test_protocols_can_be_implemented_and_driven_independently() -> None:
    model = _FakeModel()
    executor = _FakeExecutor()
    store = _FakeStore(conversation=[{"role": "user", "content": "previous"}])
    log: list[str] = []
    capability = _FakeCapability(log)
    assembler = _FakeAssembler(capability.prompt_context)
    runtime = _FakeRuntime(log=log)
    host = _FakeHost(runtime, [capability])

    assert isinstance(assembler, PromptAssembler)
    assert isinstance(model, ModelAdapter)
    assert isinstance(executor, ToolExecutor)
    assert isinstance(store, SessionStore)
    assert isinstance(capability, Capability)
    assert isinstance(runtime, AgentRuntime)
    assert isinstance(host, AgentHost)

    context = _context()
    tool = _echo_tool()
    bundle = await assembler.assemble(
        context, history=[{"role": "user", "content": "hi"}], tools=[tool]
    )
    assert bundle.messages[0]["content"] == "session=session-1"

    # A fragment is evaluated per call with the step context, so the next
    # session gets its own text and no state leaks between sessions.
    second_context = _context(session_id="session-2", turn_id="turn-2")
    await assembler.assemble(
        second_context, history=[{"role": "user", "content": "hi"}], tools=[tool]
    )
    assert capability.prompt_contexts == [context, second_context]

    request = ModelRequest(model_call_id="model-1", messages=bundle.messages, tools=bundle.tools)
    response = await model.complete(request)
    assert response.content == "done"
    assert model.requests[0].model_call_id == "model-1"

    outcome = await executor.execute(ToolCall("call-1", "echo", {"text": "a"}), context)
    assert outcome.is_success
    assert executor.contexts[0].step_id == "step-1"

    first = await store.append(
        RuntimeEvent(
            type=EventType.TOOL_SETTLED,
            session_id="session-1",
            turn_id="turn-1",
            step_id="step-1",
            payload={"tool_call_id": outcome.tool_call_id, "status": outcome.status.value},
        )
    )
    await store.append(
        RuntimeEvent(
            type=EventType.TURN_COMPLETED,
            session_id="session-1",
            turn_id="turn-1",
        )
    )
    assert first.sequence == 0
    assert [event.sequence for event in await store.read_turn("session-1", "turn-1")] == [0, 1]
    assert (await store.recover("session-1")).unfinished_calls == ()

    driver: LoopDriver = _FakeDriver()
    turn = TurnStart(
        request=TurnRequest(session_id="session-1", text="hi"),
        turn_id="turn-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=2),
        history=await store.read_conversation("session-1"),
    )
    assert turn.history[0]["content"] == "previous"
    result = await driver.run(turn)
    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "echo:hi"

    host_result = await host.submit(TurnRequest(session_id="session-1", text="hello"))
    assert host_result.reply == "hello"
    assert runtime.submitted[0].session_id == "session-1"

    await host.cancel("session-1")
    assert runtime.cancelled == ["session-1"]
    assert capability.cancelled == [("session-1", runtime.active_context)]

    # No local turn is active: the host must still notify capabilities, which
    # look up their own persisted domain state by session id.
    runtime.active_context = None
    await host.cancel("session-1")
    assert capability.cancelled[-1] == ("session-1", None)

    await host.aclose()
    await host.aclose()  # idempotent: no second shutdown, no second submit accepted
    # aclose stops the runtime once and never calls runtime.cancel per session.
    assert runtime.cancelled == ["session-1", "session-1"]
    assert runtime.stopped and runtime.closed and capability.closed
    assert capability.cancelled[-1] == ("session-1", None)
    assert log[-4:] == [
        "runtime.stop",
        "capability.cancel:session-1",
        "capability.aclose",
        "runtime.aclose",
    ]
    assert log.count("runtime.stop") == 1

    await store.aclose()
    assert store.closed
    with pytest.raises(RuntimeError):
        await host.submit(TurnRequest(session_id="session-1", text="late"))

"""Integration tests for the assembled agent core.

Every test builds the real seven components from the package exports:

``SQLiteSessionStore`` + ``ToolExecutorImpl`` + ``PromptAssemblerImpl`` +
``ModelAdapterImpl`` + ``LoopDriverImpl`` + ``AgentRuntimeImpl`` +
``AgentHostImpl``.

Only the two genuinely external sides are faked: the provider behind
``ModelAdapter`` and the tool handler behind a ``ToolRegistration``. The store,
executor, assembler, adapter, driver, runtime, and host are the production
classes, wired by hand in the tests (the core has no factory or config lookup).
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from PhyAgentOS.agent.core import (
    AgentHostImpl,
    AgentRuntimeImpl,
    EventType,
    ExecutionContext,
    LoopDriverImpl,
    ModelAdapterImpl,
    PromptAssemblerImpl,
    SQLiteSessionStore,
    ToolCall,
    ToolDefinition,
    ToolExecutorImpl,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
    TurnRequest,
    TurnStatus,
)
from PhyAgentOS.agent.core.session import SessionStoreClosedError

_ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# external fakes: provider and tool handler only
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _ProviderToolCall:
    """The provider's own tool-call shape, before the adapter maps it."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None


@dataclass(frozen=True)
class _ProviderResponse:
    """The provider's own answer shape, before the adapter maps it."""

    content: str | None = None
    finish_reason: str = "stop"
    tool_calls: tuple[_ProviderToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)


_Responder = Callable[
    [int, list[dict[str, Any]], list[dict[str, Any]] | None],
    Awaitable[_ProviderResponse],
]


class _FakeProvider:
    """Stand-in for the external chat provider; records every request."""

    def __init__(self, responder: _Responder) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []
        self.cancelled = asyncio.Event()

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> _ProviderResponse:
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "kwargs": dict(kwargs),
            }
        )
        try:
            return await self._responder(len(self.calls), messages, tools)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _Capability:
    """External domain capability: prompt fragment, tools, cancel/aclose."""

    def __init__(
        self,
        *,
        name: str = "fleet",
        registrations: Sequence[ToolRegistration] = (),
        fragment: Callable[[ExecutionContext], str | None] | None = None,
        on_cancel: Callable[[str, ExecutionContext | None], Awaitable[None]] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.name = name
        self._registrations = tuple(registrations)
        self._fragment = fragment
        self._on_cancel = on_cancel
        self.log = log
        self.prompt_calls: list[ExecutionContext] = []
        self.cancellations: list[tuple[str, ExecutionContext | None]] = []
        self.closed = False

    def tool_registrations(self) -> Sequence[ToolRegistration]:
        return self._registrations

    def prompt_context(self, context: ExecutionContext) -> str | None:
        self.prompt_calls.append(context)
        return None if self._fragment is None else self._fragment(context)

    async def cancel(self, session_id: str, context: ExecutionContext | None) -> None:
        self.cancellations.append((session_id, context))
        if self.log is not None:
            self.log.append(f"cancel:{session_id}")
        if self._on_cancel is not None:
            await self._on_cancel(session_id, context)

    async def aclose(self) -> None:
        self.closed = True
        if self.log is not None:
            self.log.append("capability.aclose")


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
@dataclass
class _Stack:
    host: AgentHostImpl
    runtime: AgentRuntimeImpl
    store: SQLiteSessionStore
    executor: ToolExecutorImpl
    adapter: ModelAdapterImpl
    provider: _FakeProvider
    capabilities: tuple[_Capability, ...]


def _build(
    store: SQLiteSessionStore,
    provider: _FakeProvider,
    *,
    capabilities: Sequence[_Capability] = (),
    max_steps: int = 4,
) -> _Stack:
    """Wire the seven production components from externally supplied parts."""
    capability_tuple = tuple(capabilities)
    registrations = tuple(
        registration
        for capability in capability_tuple
        for registration in capability.tool_registrations()
    )
    definitions = tuple(registration.definition for registration in registrations)

    assembler = PromptAssemblerImpl(
        "You are the integration-test agent.",
        fragments=tuple(capability.prompt_context for capability in capability_tuple),
    )
    adapter = ModelAdapterImpl(provider)
    executor = ToolExecutorImpl(registrations, store)
    driver = LoopDriverImpl(
        assembler=assembler,
        adapter=adapter,
        executor=executor,
        store=store,
        tools=definitions,
        model="fake-model",
    )
    runtime = AgentRuntimeImpl(driver=driver, store=store, max_steps=max_steps)
    host = AgentHostImpl(runtime=runtime, capabilities=capability_tuple)
    return _Stack(
        host=host,
        runtime=runtime,
        store=store,
        executor=executor,
        adapter=adapter,
        provider=provider,
        capabilities=capability_tuple,
    )


def _echo_registration(
    handler: Callable[[ToolCall, ExecutionContext], Awaitable[ToolOutcome]],
    *,
    name: str = "echo",
) -> ToolRegistration:
    return ToolRegistration(
        definition=ToolDefinition(
            name=name,
            description="Echo one string.",
            parameters=_ECHO_SCHEMA,
        ),
        handler=handler,
    )


def _echo_call(*, call_id: str = "call-1", text: str = "ping") -> _ProviderToolCall:
    return _ProviderToolCall(
        id=call_id,
        name="echo",
        arguments={"text": text},
        raw_arguments=json.dumps({"text": text}),
    )


async def _submit(stack: _Stack, session_id: str, text: str):
    return await asyncio.wait_for(
        stack.host.submit(TurnRequest(session_id=session_id, text=text)),
        timeout=_TIMEOUT,
    )


async def _events(store, session_id: str):
    return await store.read_session(session_id)


def _types(events: Sequence[Any]) -> list[EventType]:
    return [event.type for event in events]


def _of_type(events: Sequence[Any], event_type: EventType) -> list[Any]:
    return [event for event in events if event.type is event_type]


async def _wait_for_event(store, session_id: str, event_type: EventType):
    async def poll():
        while True:
            events = await store.read_session(session_id)
            if any(event.type is event_type for event in events):
                return events
            await asyncio.sleep(0.005)

    return await asyncio.wait_for(poll(), timeout=_TIMEOUT)


async def _drain(executor: ToolExecutorImpl) -> None:
    pending = executor.pending_handler_tasks
    if pending:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=_TIMEOUT
        )
    await asyncio.sleep(0)


def _pending_tasks() -> set[asyncio.Task[Any]]:
    current = asyncio.current_task()
    return {
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    }


async def _wait_for_inflight(runtime: AgentRuntimeImpl, expected: int) -> None:
    """Wait until ``expected`` submits have registered with the runtime."""

    async def poll() -> None:
        while runtime._inflight_submits < expected:  # noqa: SLF001 - test timing probe
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=_TIMEOUT)


async def _aclose(host: AgentHostImpl) -> None:
    await asyncio.wait_for(host.aclose(), timeout=_TIMEOUT)


# ---------------------------------------------------------------------------
# 1. direct text answer, real SQLite persistence
# ---------------------------------------------------------------------------
async def test_direct_text_turn_orders_and_persists_events(tmp_path: Path) -> None:
    async def responder(_call_no, _messages, _tools):
        return _ProviderResponse(
            content="hello from model", finish_reason="stop", usage={"total_tokens": 3}
        )

    provider = _FakeProvider(responder)
    capability = _Capability(
        fragment=lambda context: f"scope={context.session_id}"
    )
    path = tmp_path / "sessions.db"
    stack = _build(SQLiteSessionStore(path), provider, capabilities=[capability])

    # Construction reads no config and starts no work.
    assert provider.calls == []
    assert await stack.store.read_session("s1") == ()

    result = await _submit(stack, "s1", "hello")

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "hello from model"
    assert result.error is None

    events = await _events(stack.store, "s1")
    assert _types(events) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.TURN_COMPLETED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5, 6]

    prepared = _of_type(events, EventType.REQUEST_PREPARED)[0]
    assert prepared.payload["messages"][0]["role"] == "system"
    assert "integration-test agent" in prepared.payload["messages"][0]["content"]
    assert "scope=s1" in prepared.payload["messages"][0]["content"]
    assert prepared.payload["messages"][1] == {"role": "user", "content": "hello"}
    assert provider.calls[0]["messages"] == prepared.payload["messages"]
    assert provider.calls[0]["kwargs"]["model"] == "fake-model"
    assert capability.prompt_calls[0].session_id == "s1"

    await _aclose(stack.host)

    reopened = SQLiteSessionStore(path)
    try:
        persisted = await reopened.read_session("s1")
        assert _types(persisted) == _types(events)
        assert _of_type(persisted, EventType.TURN_COMPLETED)[0].payload["reply"] == (
            "hello from model"
        )
    finally:
        await reopened.aclose()


async def test_host_uses_injected_assembly_without_reading_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PhyAgentOS import config as config_package
    from PhyAgentOS.config import loader as config_loader

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the host and its assembly must not read config")

    monkeypatch.setattr(config_package, "load_config", _forbidden)
    monkeypatch.setattr(config_package, "get_config_path", _forbidden)
    monkeypatch.setattr(config_loader, "load_config", _forbidden)
    monkeypatch.setattr(config_loader, "get_config_path", _forbidden)

    async def echo_handler(call: ToolCall, context: ExecutionContext):
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=f"injected:{call.arguments_dict()['text']}",
        )

    async def responder(_call_no, _messages, tools):
        assert tools is not None
        assert [tool["function"]["name"] for tool in tools] == ["echo"]
        return _ProviderResponse(content="configured by injection", finish_reason="stop")

    provider = _FakeProvider(responder)
    capability = _Capability(
        registrations=[_echo_registration(echo_handler)],
        fragment=lambda context: f"injected prompt for {context.session_id}",
    )
    stack = _build(
        SQLiteSessionStore(tmp_path / "injected.db"),
        provider,
        capabilities=[capability],
    )

    result = await _submit(stack, "s1", "use injected parts")

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "configured by injection"
    system = provider.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "injected prompt for s1" in system["content"]
    tools = provider.calls[0]["tools"]
    assert [tool["function"]["name"] for tool in tools] == ["echo"]

    await _aclose(stack.host)


# ---------------------------------------------------------------------------
# 2. tool call, real handler, reply from the tool result
# ---------------------------------------------------------------------------
async def test_tool_round_trip_settles_once_and_returns_to_model(
    tmp_path: Path,
) -> None:
    handler_calls: list[tuple[ToolCall, ExecutionContext]] = []

    async def echo_handler(
        call: ToolCall, context: ExecutionContext
    ) -> ToolOutcome:
        arguments = call.arguments_dict()
        handler_calls.append((call, context))
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=f"echo:{arguments['text']}",
            content={"echoed": arguments["text"]},
        )

    async def responder(call_no, messages, _tools):
        if call_no == 1:
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(text="ping"),),
                usage={"total_tokens": 7},
            )
        tool_messages = [
            message for message in messages if message.get("role") == "tool"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_call_id"] == "call-1"
        assert tool_messages[0]["content"] == "echo:ping"
        return _ProviderResponse(content="final answer", finish_reason="stop")

    provider = _FakeProvider(responder)
    capability = _Capability(registrations=[_echo_registration(echo_handler)])
    stack = _build(SQLiteSessionStore(tmp_path / "tools.db"), provider, capabilities=[capability])

    result = await _submit(stack, "s1", "use the echo tool")

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "final answer"
    assert len(handler_calls) == 1
    received_call = handler_calls[0][0]
    assert received_call.tool_call_id == "call-1"
    assert received_call.name == "echo"
    assert received_call.arguments_dict() == {"text": "ping"}
    assert handler_calls[0][1].session_id == "s1"
    assert len(provider.calls) == 2

    events = await _events(stack.store, "s1")
    assert _types(events) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.TURN_COMPLETED,
    ]
    for event_type in (
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
    ):
        assert len(_of_type(events, event_type)) == 1

    intent = _of_type(events, EventType.TOOL_INTENT)[0]
    assert intent.payload["tool_call_id"] == "call-1"
    assert intent.payload["tool_name"] == "echo"
    assert intent.payload["arguments"] == {"text": "ping"}

    settled = _of_type(events, EventType.TOOL_SETTLED)[0]
    assert settled.payload["tool_call_id"] == "call-1"
    assert settled.payload["status"] == "succeeded"
    assert settled.payload["text"] == "echo:ping"

    second_messages = provider.calls[1]["messages"]
    assistant = [m for m in second_messages if m["role"] == "assistant"][0]
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert assistant["tool_calls"][0]["function"]["arguments"] == json.dumps(
        {"text": "ping"}
    )
    tool_message = [m for m in second_messages if m["role"] == "tool"][0]
    assert tool_message == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "echo",
        "content": "echo:ping",
    }

    await _aclose(stack.host)


# ---------------------------------------------------------------------------
# 3. consecutive turns, restart, and per-session isolation
# ---------------------------------------------------------------------------
async def test_consecutive_turns_survive_restart_and_sessions_do_not_mix(
    tmp_path: Path,
) -> None:
    def responder_for():
        async def responder(_call_no, messages, _tools):
            last_user = [m for m in messages if m.get("role") == "user"][-1]
            return _ProviderResponse(
                content=f"reply to {last_user['content']}", finish_reason="stop"
            )

        return responder

    path = tmp_path / "history.db"
    first_provider = _FakeProvider(responder_for())
    first_capability = _Capability(
        fragment=lambda context: f"scope={context.session_id}"
    )
    first = _build(
        SQLiteSessionStore(path), first_provider, capabilities=[first_capability]
    )

    first_result = await _submit(first, "s1", "first")
    second_result = await _submit(first, "s1", "second")

    assert first_result.reply == "reply to first"
    assert second_result.reply == "reply to second"
    second_messages = first_provider.calls[1]["messages"]
    assert [m["role"] for m in second_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert [m["content"] for m in second_messages[1:]] == [
        "first",
        "reply to first",
        "second",
    ]
    assert sum(1 for m in second_messages if m["role"] == "system") == 1

    await _aclose(first.host)

    # Reopen with new store/runtime/host over the same SQLite file.
    restarted_provider = _FakeProvider(responder_for())
    restarted_capability = _Capability(
        fragment=lambda context: f"scope={context.session_id}"
    )
    restarted = _build(
        SQLiteSessionStore(path),
        restarted_provider,
        capabilities=[restarted_capability],
    )

    third_result = await _submit(restarted, "s1", "third")

    assert third_result.reply == "reply to third"
    third_messages = restarted_provider.calls[0]["messages"]
    assert [m["role"] for m in third_messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert [m["content"] for m in third_messages[1:]] == [
        "first",
        "reply to first",
        "second",
        "reply to second",
        "third",
    ]
    assert sum(1 for m in third_messages if m["role"] == "system") == 1
    assert "scope=s1" in third_messages[0]["content"]

    assert await restarted.store.read_conversation("other") == ()

    other_result = await _submit(restarted, "other", "unrelated")

    assert other_result.reply == "reply to unrelated"
    other_messages = restarted_provider.calls[1]["messages"]
    assert [m["role"] for m in other_messages] == ["system", "user"]
    assert other_messages[1] == {"role": "user", "content": "unrelated"}
    assert "scope=other" in other_messages[0]["content"]
    assert "scope=s1" not in other_messages[0]["content"]
    serialized = json.dumps(other_messages)
    assert "first" not in serialized
    assert "second" not in serialized
    assert "third" not in serialized

    conversation = await restarted.store.read_conversation("other")
    assert [dict(message) for message in conversation] == [
        {"role": "user", "content": "unrelated"},
        {"role": "assistant", "content": "reply to unrelated"},
    ]

    await _aclose(restarted.host)


# ---------------------------------------------------------------------------
# 4. cancel while the model is pending
# ---------------------------------------------------------------------------
async def test_cancel_while_model_pending_interrupts_and_reaps_adapter(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    async def responder(_call_no, _messages, _tools):
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    handler_calls: list[dict[str, Any]] = []

    async def echo_handler(call: ToolCall, context: ExecutionContext):
        handler_calls.append(call.arguments_dict())
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text="should not run",
        )

    provider = _FakeProvider(responder)
    capability = _Capability(registrations=[_echo_registration(echo_handler)])
    stack = _build(SQLiteSessionStore(tmp_path / "cancel-model.db"), provider, capabilities=[capability])

    submit_task = asyncio.create_task(
        stack.host.submit(TurnRequest(session_id="s1", text="wait for me"))
    )
    await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)

    await asyncio.wait_for(stack.host.cancel("s1"), timeout=2.0)
    result = await asyncio.wait_for(submit_task, timeout=_TIMEOUT)

    assert result.status is TurnStatus.INTERRUPTED
    assert result.reply is None
    await asyncio.wait_for(provider.cancelled.wait(), timeout=_TIMEOUT)
    assert handler_calls == []
    assert _pending_tasks() == set()

    events = await _events(stack.store, "s1")
    assert _types(events) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_INTERRUPTED,
        EventType.TURN_INTERRUPTED,
    ]

    assert len(capability.cancellations) == 1
    session_id, context = capability.cancellations[0]
    assert session_id == "s1"
    assert isinstance(context, ExecutionContext)
    assert context.session_id == "s1"
    assert context.turn_id == result.turn_id
    assert context.step_id == "runtime/cancel"

    await _aclose(stack.host)


# ---------------------------------------------------------------------------
# 5. cancel while a tool is pending
# ---------------------------------------------------------------------------
async def test_cancel_while_tool_pending_returns_unknown_without_retry(
    tmp_path: Path,
) -> None:
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()
    handler_finished = asyncio.Event()
    handler_calls: list[dict[str, Any]] = []

    async def blocking_handler(call: ToolCall, context: ExecutionContext):
        handler_calls.append(call.arguments_dict())
        handler_started.set()
        try:
            await handler_release.wait()
        finally:
            handler_finished.set()
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text="late success",
        )

    async def responder(call_no, _messages, _tools):
        if call_no == 1:
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(text="hold"),),
            )
        return _ProviderResponse(content="should not be requested", finish_reason="stop")

    async def release_handler(session_id, _context):
        assert session_id == "s1"
        handler_release.set()

    provider = _FakeProvider(responder)
    capability = _Capability(
        registrations=[_echo_registration(blocking_handler)],
        on_cancel=release_handler,
    )
    stack = _build(SQLiteSessionStore(tmp_path / "cancel-tool.db"), provider, capabilities=[capability])

    submit_task = asyncio.create_task(
        stack.host.submit(TurnRequest(session_id="s1", text="run the tool"))
    )
    await asyncio.wait_for(handler_started.wait(), timeout=_TIMEOUT)
    await _wait_for_event(stack.store, "s1", EventType.TOOL_DISPATCHED)

    await asyncio.wait_for(stack.host.cancel("s1"), timeout=2.0)
    result = await asyncio.wait_for(submit_task, timeout=_TIMEOUT)

    assert result.status is TurnStatus.INTERRUPTED
    assert handler_calls == [{"text": "hold"}]
    assert len(provider.calls) == 1
    assert len(capability.cancellations) == 1
    assert isinstance(capability.cancellations[0][1], ExecutionContext)

    await asyncio.wait_for(handler_finished.wait(), timeout=_TIMEOUT)
    await _drain(stack.executor)

    events = await _events(stack.store, "s1")
    assert _types(events) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_COMPLETED,
        EventType.TOOL_INTENT,
        EventType.TOOL_DISPATCHED,
        EventType.TOOL_SETTLED,
        EventType.TURN_INTERRUPTED,
    ]
    settled = _of_type(events, EventType.TOOL_SETTLED)
    assert len(settled) == 1
    assert settled[0].payload["status"] == ToolStatus.UNKNOWN.value
    assert len(_of_type(events, EventType.TOOL_INTENT)) == 1
    assert len(_of_type(events, EventType.TOOL_DISPATCHED)) == 1
    assert len(provider.calls) == 1
    assert handler_calls == [{"text": "hold"}]
    assert _pending_tasks() == set()

    await _aclose(stack.host)


# ---------------------------------------------------------------------------
# 6. shutdown notifies capabilities even for a completed session
# ---------------------------------------------------------------------------
class _CloseTrackingStore(SQLiteSessionStore):
    def __init__(self, path: Path, log: list[str]) -> None:
        self._close_log = log
        super().__init__(path)

    async def aclose(self) -> None:
        self._close_log.append("store.aclose")
        await super().aclose()


async def test_close_notifies_capabilities_then_closes_runtime_and_store(
    tmp_path: Path,
) -> None:
    async def responder(_call_no, _messages, _tools):
        return _ProviderResponse(content="done", finish_reason="stop")

    log: list[str] = []
    provider = _FakeProvider(responder)
    capability = _Capability(log=log)
    store = _CloseTrackingStore(tmp_path / "close.db", log)
    stack = _build(store, provider, capabilities=[capability])

    result = await _submit(stack, "s1", "finish this")
    assert result.status is TurnStatus.COMPLETED
    assert capability.cancellations == []

    await _aclose(stack.host)

    assert capability.cancellations == [("s1", None)]
    assert capability.closed is True
    assert log == ["cancel:s1", "capability.aclose", "store.aclose"]

    with pytest.raises(SessionStoreClosedError):
        await stack.store.read_session("s1")
    with pytest.raises(RuntimeError):
        await stack.host.submit(TurnRequest(session_id="s1", text="after close"))
    with pytest.raises(RuntimeError):
        await stack.runtime.submit(TurnRequest(session_id="s1", text="after close"))

    # Idempotent: the second close does not notify again.
    await _aclose(stack.host)
    assert capability.cancellations == [("s1", None)]
    assert capability.closed is True


# ---------------------------------------------------------------------------
# 7. real append failures in the SQLite store
# ---------------------------------------------------------------------------
class _FaultStore(SQLiteSessionStore):
    """Real SQLite store that fails the next append of one event type."""

    def __init__(self, path: Path, *, fail_on: EventType) -> None:
        self.fail_on = fail_on
        self.failures_left = 1
        self.failed_appends: list[EventType] = []
        super().__init__(path)

    async def append(self, event):
        if event.type is self.fail_on and self.failures_left:
            self.failures_left -= 1
            self.failed_appends.append(event.type)
            raise OSError(f"injected {event.type.value} append failure")
        return await super().append(event)


async def test_request_prepared_append_failure_prevents_model_call(
    tmp_path: Path,
) -> None:
    async def responder(_call_no, _messages, _tools):
        return _ProviderResponse(content="must not be called", finish_reason="stop")

    provider = _FakeProvider(responder)
    store = _FaultStore(tmp_path / "fault-request.db", fail_on=EventType.REQUEST_PREPARED)
    stack = _build(store, provider, capabilities=[_Capability()])

    result = await _submit(stack, "s1", "go")

    assert result.status is TurnStatus.FAILED
    assert result.error is not None
    assert "request.prepared" in result.error
    assert provider.calls == []
    assert store.failed_appends == [EventType.REQUEST_PREPARED]

    events = await _events(store, "s1")
    assert _types(events) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.TURN_FAILED,
    ]
    assert EventType.TURN_COMPLETED not in _types(events)

    await _aclose(stack.host)


async def test_tool_dispatched_append_failure_skips_handler_and_fails_turn(
    tmp_path: Path,
) -> None:
    handler_calls: list[dict[str, Any]] = []

    async def echo_handler(call: ToolCall, context: ExecutionContext):
        handler_calls.append(call.arguments_dict())
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text="must not run",
        )

    async def responder(call_no, _messages, _tools):
        if call_no == 1:
            return _ProviderResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=(_echo_call(text="danger"),),
            )
        return _ProviderResponse(content="pretend success", finish_reason="stop")

    provider = _FakeProvider(responder)
    store = _FaultStore(tmp_path / "fault-dispatch.db", fail_on=EventType.TOOL_DISPATCHED)
    stack = _build(
        store,
        provider,
        capabilities=[_Capability(registrations=[_echo_registration(echo_handler)])],
    )

    result = await _submit(stack, "s1", "run it")

    assert handler_calls == []
    assert store.failed_appends == [EventType.TOOL_DISPATCHED]
    # ToolExecutor contract (protocols.py): a failed intent/dispatched append
    # means the handler never ran and the driver fails the turn. This assertion
    # pins the documented rule rather than the failure's internal shape.
    assert len(provider.calls) == 1
    assert result.status is TurnStatus.FAILED
    assert result.error is not None

    events = await _events(store, "s1")
    types = _types(events)
    assert EventType.TOOL_DISPATCHED not in types
    assert EventType.TURN_COMPLETED not in types
    assert types[-1] is EventType.TURN_FAILED
    settled = _of_type(events, EventType.TOOL_SETTLED)
    assert len(settled) == 1
    assert settled[0].payload["status"] == ToolStatus.FAILED.value
    assert settled[0].payload["error"]["code"] == "tool_dispatched_write_failed"

    await _aclose(stack.host)


# ---------------------------------------------------------------------------
# 8. cancel while submit is still inside recover/read_conversation
# ---------------------------------------------------------------------------
class _RecoverGateStore(SQLiteSessionStore):
    """Real store that holds a submit inside ``recover`` until released."""

    def __init__(self, path: Path) -> None:
        self.recover_started = asyncio.Event()
        self.recover_release = asyncio.Event()
        super().__init__(path)

    async def recover(self, session_id: str):
        self.recover_started.set()
        await self.recover_release.wait()
        return await super().recover(session_id)


async def test_cancel_before_active_is_published_interrupts_without_model(
    tmp_path: Path,
) -> None:
    async def responder(_call_no, _messages, _tools):
        return _ProviderResponse(content="must not be called", finish_reason="stop")

    provider = _FakeProvider(responder)
    capability = _Capability()
    store = _RecoverGateStore(tmp_path / "startup-cancel.db")
    stack = _build(store, provider, capabilities=[capability])

    submit_task = asyncio.create_task(
        stack.host.submit(TurnRequest(session_id="s1", text="start slowly"))
    )
    await asyncio.wait_for(store.recover_started.wait(), timeout=_TIMEOUT)

    # The submit owns the session but has not published an active turn yet.
    await asyncio.wait_for(stack.host.cancel("s1"), timeout=2.0)
    assert provider.calls == []
    assert len(capability.cancellations) == 1
    cancelled_session, cancelled_context = capability.cancellations[0]
    assert cancelled_session == "s1"
    assert cancelled_context is None

    store.recover_release.set()
    result = await asyncio.wait_for(submit_task, timeout=_TIMEOUT)

    assert result.status is TurnStatus.INTERRUPTED
    assert provider.calls == []

    events = await _events(store, "s1")
    assert _types(events) == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.TURN_INTERRUPTED,
    ]
    assert EventType.REQUEST_PREPARED not in _types(events)
    assert EventType.MODEL_ATTEMPT_STARTED not in _types(events)
    assert _pending_tasks() == set()

    await _aclose(stack.host)


# ---------------------------------------------------------------------------
# 9. close while one turn is model-pending and a second submit is queued
# ---------------------------------------------------------------------------
async def test_close_cancels_active_and_rejects_queued_submit(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    async def responder(_call_no, _messages, _tools):
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    path = tmp_path / "close-queued.db"
    log: list[str] = []
    cancel_snapshots: list[list[EventType]] = []
    store = _CloseTrackingStore(path, log)

    async def record_cancel(session_id: str, _context) -> None:
        events = await store.read_session(session_id)
        cancel_snapshots.append(_types(events))

    provider = _FakeProvider(responder)
    capability = _Capability(log=log, on_cancel=record_cancel)
    stack = _build(store, provider, capabilities=[capability])

    first = asyncio.create_task(
        stack.host.submit(TurnRequest(session_id="s1", text="first"))
    )
    await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)
    second = asyncio.create_task(
        stack.host.submit(TurnRequest(session_id="s1", text="second"))
    )
    await _wait_for_inflight(stack.runtime, 2)

    await asyncio.wait_for(stack.host.aclose(), timeout=5.0)

    first_result = await asyncio.wait_for(first, timeout=2.0)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(second, timeout=2.0)

    assert first_result.status is TurnStatus.INTERRUPTED
    await asyncio.wait_for(provider.cancelled.wait(), timeout=_TIMEOUT)
    assert len(provider.calls) == 1
    assert _pending_tasks() == set()

    # Capability notification and close happened after the local stop and
    # before the store closed.
    assert log == ["cancel:s1", "capability.aclose", "store.aclose"]
    assert len(cancel_snapshots) == 1
    stopped_types = cancel_snapshots[0]
    assert stopped_types.count(EventType.TURN_STARTED) == 1
    assert EventType.TURN_INTERRUPTED in stopped_types
    assert EventType.MODEL_ATTEMPT_INTERRUPTED in stopped_types
    assert EventType.TURN_COMPLETED not in stopped_types

    reopened = SQLiteSessionStore(path)
    try:
        persisted = await reopened.read_session("s1")
    finally:
        await reopened.aclose()

    types = _types(persisted)
    assert types == [
        EventType.INPUT_ACCEPTED,
        EventType.TURN_STARTED,
        EventType.REQUEST_PREPARED,
        EventType.MODEL_ATTEMPT_STARTED,
        EventType.MODEL_ATTEMPT_INTERRUPTED,
        EventType.TURN_INTERRUPTED,
    ]
    assert types.count(EventType.INPUT_ACCEPTED) == 1
    assert types.count(EventType.TURN_STARTED) == 1
    assert types.count(EventType.REQUEST_PREPARED) == 1

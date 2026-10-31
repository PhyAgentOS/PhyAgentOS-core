"""Subagent continuation over the real Core store and child runtime.

Only the provider, the injected general-tools object, and the completion
callback are fakes. The second child run is an ordinary Core turn on the same
child session, so its SQLite history must contain the first task and answer.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import PhyAgentOS.agent.services.subagent_runtime as subagent_runtime_module
from PhyAgentOS.agent.capabilities.spawn_capability import SpawnCapability
from PhyAgentOS.agent.core import (
    Budget,
    CancellationToken,
    ExecutionContext,
    SQLiteSessionStore,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.service import RuntimeSelection

_TIMEOUT = 5.0


@dataclass(frozen=True)
class _ProviderToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class _ProviderResponse:
    content: str | None = None
    finish_reason: str = "stop"
    tool_calls: tuple[_ProviderToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)


class _FakeProvider:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

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
        return await self._responder(len(self.calls), messages, tools)


class _GeneralTools:
    def __init__(self, registrations: tuple[ToolRegistration, ...]) -> None:
        self._registrations = registrations
        self.closed = False

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return self._registrations

    def prompt_context(self, context: ExecutionContext) -> str | None:
        del context
        return None

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        del session_id, context

    async def aclose(self) -> None:
        self.closed = True


class _Notifier:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []
        self.queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue()

    async def __call__(self, parent: str, turn: str, text: str) -> None:
        item = (parent, turn, text)
        self.items.append(item)
        await self.queue.put(item)

    async def next(self) -> tuple[str, str, str]:
        return await asyncio.wait_for(self.queue.get(), _TIMEOUT)


def _definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} stub",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


def _registration(name: str) -> ToolRegistration:
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        del context
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=f"{name} result",
        )

    return ToolRegistration(definition=_definition(name), handler=handler)


def _general_tools() -> _GeneralTools:
    return _GeneralTools(
        (
            _registration("probe"),
            _registration("read_file"),
            _registration("spawn"),
            _registration("forge_task_create"),
        )
    )


def _selection(provider: Any, model: str = "child-model") -> RuntimeSelection:
    return RuntimeSelection("fake", model, None, "http://fake.invalid", provider)


def _config(tmp_path: Path) -> Config:
    config = Config()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    config.agents.defaults.workspace = str(workspace)
    config.agents.defaults.max_concurrent_turns = 4
    config.agents.defaults.per_session_queue_limit = 8
    config.agents.defaults.max_tool_iterations = 6
    config.agents.defaults.temperature = 0.0
    config.agents.defaults.max_tokens = 512
    return config


def _context(parent: str = "parent-a", turn: str = "turn-a") -> ExecutionContext:
    return ExecutionContext(
        session_id=parent,
        turn_id=turn,
        step_id="step-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


async def _spawn(
    capability: SpawnCapability,
    *,
    task: str,
    child_session_id: str | None = None,
    role: str | None = None,
    tool_allowlist: list[str] | None = None,
    context: ExecutionContext | None = None,
    call_id: str = "call-spawn",
) -> ToolOutcome:
    arguments: dict[str, Any] = {"task": task, "label": "child"}
    if child_session_id is not None:
        arguments["child_session_id"] = child_session_id
    if role is not None:
        arguments["role"] = role
    if tool_allowlist is not None:
        arguments["tool_allowlist"] = tool_allowlist
    registrations = {
        registration.definition.name: registration
        for registration in capability.tool_registrations()
    }
    return await registrations["spawn"].handler(
        ToolCall(tool_call_id=call_id, name="spawn", arguments=arguments),
        context or _context(),
    )


async def _tool(
    capability: SpawnCapability,
    name: str,
    arguments: dict[str, Any],
    *,
    context: ExecutionContext | None = None,
) -> ToolOutcome:
    registrations = {
        registration.definition.name: registration
        for registration in capability.tool_registrations()
    }
    return await registrations[name].handler(
        ToolCall(tool_call_id=f"call-{name}", name=name, arguments=arguments),
        context or _context(),
    )


def _offered(provider: _FakeProvider, call_index: int) -> set[str]:
    tools = provider.calls[call_index]["tools"] or []
    return {item["function"]["name"] for item in tools}


async def test_continuation_reuses_child_history_and_delivers_once(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    answers = ["first answer", "second answer"]

    async def respond(
        call_no: int,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        del messages, tools
        return _ProviderResponse(
            content=answers[min(call_no - 1, len(answers) - 1)],
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    provider = _FakeProvider(respond)
    notifier = _Notifier()
    observed: list[tuple[str, str, dict[str, int]]] = []
    resolver_calls: list[str] = []

    def observe(parent: str, child: str, usage: dict[str, int]) -> None:
        observed.append((parent, child, usage))

    def resolve(session_id: str) -> RuntimeSelection:
        resolver_calls.append(session_id)
        return _selection(provider)

    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=resolve,
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
        usage_observer=observe,
    )
    try:
        first = await _spawn(capability, task="first task")
        assert first.status is ToolStatus.SUCCEEDED
        assert first.content["continued"] is False
        child_id = first.content["child_session_id"]
        _parent, turn, text = await notifier.next()
        assert turn == "turn-a"
        assert "first answer" in text

        second = await _spawn(
            capability,
            task="second task",
            child_session_id=child_id,
            context=_context("parent-a", "turn-b"),
            call_id="call-spawn-2",
        )
        assert second.status is ToolStatus.SUCCEEDED
        assert second.content["continued"] is True
        assert second.content["run_index"] == 2
        assert second.content["child_session_id"] == child_id
        _parent, turn, text = await notifier.next()
        assert turn == "turn-b"
        assert "second answer" in text
        await asyncio.sleep(0.05)
        assert len(notifier.items) == 2

        # One child session, two real Core turns: the second request carries the
        # first task and answer as stored history.
        second_messages = provider.calls[1]["messages"]
        user_texts = [
            message.get("content")
            for message in second_messages
            if message.get("role") == "user"
        ]
        assert user_texts == ["first task", "second task"]
        assert {
            "role": "assistant",
            "content": "first answer",
        } in second_messages
        system_messages = [
            message for message in second_messages if message.get("role") == "system"
        ]
        assert len(system_messages) == 1

        assert _offered(provider, 0) == {"probe", "read_file"}
        assert _offered(provider, 1) == {"probe", "read_file"}
        # Each run resolved the parent selection once; the continuation did not
        # invent a second model or silently switch providers at completion.
        assert resolver_calls == ["parent-a", "parent-a"]
        assert [call["kwargs"]["model"] for call in provider.calls] == [
            "child-model",
            "child-model",
        ]

        results = capability.results("parent-a")
        assert len(results) == 2
        assert [item.run_index for item in results] == [1, 2]
        assert [item.parent_turn_id for item in results] == ["turn-a", "turn-b"]
        assert all(item.status == "completed" for item in results)
        assert all(item.notified for item in results)
        assert [item.usage for item in results] == [
            {"prompt_tokens": 10, "completion_tokens": 5},
            {"prompt_tokens": 10, "completion_tokens": 5},
        ]
        assert observed == [
            (
                "parent-a",
                "turn-a",
                {"prompt_tokens": 10, "completion_tokens": 5},
            ),
            (
                "parent-a",
                "turn-b",
                {"prompt_tokens": 10, "completion_tokens": 5},
            ),
        ]

        status = await _tool(
            capability,
            "subagent_status",
            {"child_session_id": child_id},
        )
        assert status.status is ToolStatus.SUCCEEDED
        data = json.loads(status.text)
        assert data["status"] == "completed"
        assert data["run_index"] == 2
        assert data["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}

        # The child conversation is durable and also available after the fact.
        conversation = await store.read_conversation(child_id)
        assert {"role": "user", "content": "second task"} in [
            dict(message) for message in conversation
        ]
        assert conversation[-1] == {
            "role": "assistant",
            "content": "second answer",
        }

        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        # Closing does not delete retained run results or repeat delivery.
        assert len(capability.results("parent-a")) == 2
        assert len(notifier.items) == 2
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_other_parent_and_running_child_are_rejected(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        entered.set()
        await never.wait()
        return _ProviderResponse(content="never reached")

    provider = _FakeProvider(blocked)
    notifier = _Notifier()
    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
    )
    try:
        accepted = await _spawn(
            capability, task="long task", context=_context("parent-a", "turn-a")
        )
        child_id = accepted.content["child_session_id"]
        await asyncio.wait_for(entered.wait(), _TIMEOUT)

        other_parent = _context("parent-b", "turn-b")
        rejected = await _spawn(
            capability,
            task="steal child",
            child_session_id=child_id,
            context=other_parent,
        )
        assert rejected.status is ToolStatus.FAILED
        assert "does not belong to this parent" in (
            rejected.error.message if rejected.error else ""
        )

        status = await _tool(
            capability,
            "subagent_status",
            {"child_session_id": child_id},
            context=other_parent,
        )
        assert status.status is ToolStatus.FAILED
        cancel = await _tool(
            capability,
            "subagent_cancel",
            {"child_session_id": child_id},
            context=other_parent,
        )
        assert cancel.status is ToolStatus.FAILED
        assert notifier.items == []

        running = await _spawn(
            capability,
            task="continue while running",
            child_session_id=child_id,
            context=_context("parent-a", "turn-b"),
        )
        assert running.status is ToolStatus.FAILED
        assert "still running" in (running.error.message if running.error else "")

        stopped = await _tool(
            capability,
            "subagent_cancel",
            {"child_session_id": child_id},
            context=_context("parent-a", "turn-c"),
        )
        assert stopped.status is ToolStatus.SUCCEEDED
        assert json.loads(stopped.text)["status"] == "interrupted"
        assert notifier.items == []
        assert capability.pending("parent-a") == 0
        assert capability.results("parent-a")[0].status == "interrupted"
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_allowlist_and_role_can_only_narrow_child_tools(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    async def respond(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        return _ProviderResponse(content="allowlist answer")

    provider = _FakeProvider(respond)
    notifier = _Notifier()
    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
    )
    try:
        first = await _spawn(
            capability,
            task="review",
            role="reviewer",
            tool_allowlist=["probe", "read_file"],
        )
        assert first.status is ToolStatus.SUCCEEDED
        child_id = first.content["child_session_id"]
        await notifier.next()
        assert _offered(provider, 0) == {"probe", "read_file"}
        system_text = provider.calls[0]["messages"][0]["content"]
        assert "## Role\nreviewer" in system_text

        narrowed = await _spawn(
            capability,
            task="narrow",
            child_session_id=child_id,
            tool_allowlist=["probe"],
            context=_context("parent-a", "turn-b"),
        )
        assert narrowed.status is ToolStatus.SUCCEEDED
        await notifier.next()
        assert _offered(provider, 1) == {"probe"}
        second_system = provider.calls[1]["messages"][0]["content"]
        assert "## Role\nreviewer" in second_system

        expanded = await _spawn(
            capability,
            task="expand",
            child_session_id=child_id,
            tool_allowlist=["probe", "read_file"],
            context=_context("parent-a", "turn-c"),
        )
        assert expanded.status is ToolStatus.FAILED
        assert "only narrow" in (expanded.error.message if expanded.error else "")

        forge = await _spawn(
            capability,
            task="forge",
            child_session_id=child_id,
            tool_allowlist=["forge_task_create"],
            context=_context("parent-a", "turn-d"),
        )
        assert forge.status is ToolStatus.FAILED
        assert "never allowed" in (forge.error.message if forge.error else "")
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_usage_observer_is_not_given_invented_zeros(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    async def respond(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        return _ProviderResponse(content="no usage")

    provider = _FakeProvider(respond)
    notifier = _Notifier()
    observed: list[dict[str, int]] = []
    step_observed: list[dict[str, int]] = []

    def observe(_parent: str, _turn: str, usage: dict[str, int]) -> None:
        observed.append(usage)

    def observe_step(_parent: str, _turn: str, usage: dict[str, int]) -> None:
        step_observed.append(usage)

    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
        usage_observer=observe,
        step_usage_observer=observe_step,
    )
    try:
        accepted = await _spawn(capability, task="no usage task")
        child_id = accepted.content["child_session_id"]
        await notifier.next()
        assert observed == [{}]
        assert step_observed == [{}]
        result = capability.results("parent-a")[0]
        assert result.usage == {}
        assert "prompt_tokens" not in result.usage
        assert "completion_tokens" not in result.usage

        status = await _tool(
            capability,
            "subagent_status",
            {"child_session_id": child_id},
        )
        assert json.loads(status.text)["usage"] == {}
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_each_successful_child_step_observes_usage_once_and_settles_once(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    async def respond(
        call_no: int,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        del messages, tools
        if call_no == 1:
            return _ProviderResponse(
                finish_reason="tool_calls",
                tool_calls=(_ProviderToolCall("probe-1", "probe", {}),),
                usage={"prompt_tokens": 2, "completion_tokens": 1},
            )
        return _ProviderResponse(
            content="done after probe",
            usage={"prompt_tokens": 4, "completion_tokens": 3},
        )

    provider = _FakeProvider(respond)
    notifier = _Notifier()
    step_observed: list[dict[str, int]] = []
    settled_observed: list[dict[str, int]] = []

    def observe_step(_parent: str, _turn: str, usage: dict[str, int]) -> None:
        step_observed.append(usage)

    def observe_settled(_parent: str, _turn: str, usage: dict[str, int]) -> None:
        settled_observed.append(usage)

    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
        step_usage_observer=observe_step,
        usage_observer=observe_settled,
    )
    try:
        await _spawn(capability, task="two step task")
        await notifier.next()
        assert step_observed == [
            {"prompt_tokens": 2, "completion_tokens": 1},
            {"prompt_tokens": 4, "completion_tokens": 3},
        ]
        assert settled_observed == [
            {"prompt_tokens": 6, "completion_tokens": 4}
        ]
        result = capability.results("parent-a")[0]
        assert result.status == "completed"
        assert result.usage == {"prompt_tokens": 6, "completion_tokens": 4}
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_budget_guard_stops_before_the_next_child_model_request(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    async def respond(
        call_no: int,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        del messages, tools
        if call_no == 1:
            return _ProviderResponse(
                finish_reason="tool_calls",
                tool_calls=(_ProviderToolCall("probe-1", "probe", {}),),
                usage={"prompt_tokens": 3, "completion_tokens": 2},
            )
        return _ProviderResponse(
            content="second model call must not run",
            usage={"prompt_tokens": 99, "completion_tokens": 99},
        )

    provider = _FakeProvider(respond)
    notifier = _Notifier()
    guard_calls: list[tuple[str, str]] = []
    step_observed: list[dict[str, int]] = []
    settled_observed: list[tuple[str, str, dict[str, int]]] = []

    def guard(parent: str, turn: str) -> bool:
        guard_calls.append((parent, turn))
        return len(guard_calls) == 1

    def observe_step(parent: str, turn: str, usage: dict[str, int]) -> None:
        assert (parent, turn) == ("parent-a", "turn-a")
        step_observed.append(usage)

    def observe_settled(parent: str, turn: str, usage: dict[str, int]) -> None:
        settled_observed.append((parent, turn, usage))

    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
        step_usage_observer=observe_step,
        usage_observer=observe_settled,
        budget_guard=guard,
    )
    try:
        accepted = await _spawn(capability, task="budget task")
        child_id = accepted.content["child_session_id"]
        _parent, _turn, text = await notifier.next()
        assert "budget" in text
        assert "did not complete" in text

        # The second request was refused before the provider call.
        assert len(provider.calls) == 1
        assert guard_calls == [
            ("parent-a", "turn-a"),
            ("parent-a", "turn-a"),
        ]
        assert step_observed == [{"prompt_tokens": 3, "completion_tokens": 2}]
        assert settled_observed == [
            (
                "parent-a",
                "turn-a",
                {"prompt_tokens": 3, "completion_tokens": 2},
            )
        ]
        result = capability.results("parent-a")[0]
        assert result.child_session_id == child_id
        assert result.status == "failed"
        assert result.error is not None
        assert "budget" in result.error
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_step_usage_keeps_known_error_usage_without_inventing_zeros(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    class _ProviderFailureError(RuntimeError):
        def __init__(self) -> None:
            self.usage = {"prompt_tokens": 7, "completion_tokens": 0}
            super().__init__("provider exploded")

    async def respond(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        raise _ProviderFailureError()

    provider = _FakeProvider(respond)
    notifier = _Notifier()
    step_observed: list[dict[str, int]] = []
    settled_observed: list[dict[str, int]] = []

    def observe_step(_parent: str, _turn: str, usage: dict[str, int]) -> None:
        step_observed.append(usage)

    def observe_settled(_parent: str, _turn: str, usage: dict[str, int]) -> None:
        settled_observed.append(usage)

    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
        step_usage_observer=observe_step,
        usage_observer=observe_settled,
    )
    try:
        await _spawn(capability, task="error usage task")
        _parent, _turn, text = await notifier.next()
        assert "provider exploded" in text
        assert step_observed == [{"prompt_tokens": 7, "completion_tokens": 0}]
        assert settled_observed == [
            {"prompt_tokens": 7, "completion_tokens": 0}
        ]
        result = capability.results("parent-a")[0]
        assert result.status == "failed"
        assert result.usage == {"prompt_tokens": 7, "completion_tokens": 0}
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_guarded_provider_calls_a_keyword_only_chat_provider(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    class _KeywordOnlyProvider:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_with_retry(
            self,
            *,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ) -> _ProviderResponse:
            self.calls.append(
                {"messages": messages, "tools": tools, "kwargs": dict(kwargs)}
            )
            return _ProviderResponse(
                content="keyword-only answer",
                usage={"prompt_tokens": 8, "completion_tokens": 4},
            )

    provider = _KeywordOnlyProvider()
    notifier = _Notifier()
    step_observed: list[dict[str, int]] = []

    def observe_step(_parent: str, _turn: str, usage: dict[str, int]) -> None:
        step_observed.append(usage)

    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
        step_usage_observer=observe_step,
        budget_guard=lambda _parent, _turn: True,
    )
    try:
        accepted = await _spawn(capability, task="keyword-only task")
        assert accepted.status is ToolStatus.SUCCEEDED
        _parent, _turn, text = await notifier.next()
        assert "keyword-only answer" in text
        assert "TypeError" not in text
        assert len(provider.calls) == 1
        result = capability.results("parent-a")[0]
        assert result.status == "completed"
        assert result.usage == {"prompt_tokens": 8, "completion_tokens": 4}
        assert step_observed == [{"prompt_tokens": 8, "completion_tokens": 4}]
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_step_observer_failure_keeps_usage_and_child_result(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    async def respond(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        return _ProviderResponse(
            content="observer survived",
            usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    provider = _FakeProvider(respond)
    notifier = _Notifier()
    settled: list[dict[str, int]] = []

    def broken_step_observer(
        _parent: str, _turn: str, _usage: dict[str, int]
    ) -> None:
        raise RuntimeError("budget observer is down")

    def observe_settled(_parent: str, _turn: str, usage: dict[str, int]) -> None:
        settled.append(usage)

    capability = SpawnCapability(
        _config(tmp_path),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
        step_usage_observer=broken_step_observer,
        usage_observer=observe_settled,
    )
    try:
        await _spawn(capability, task="observer failure task")
        _parent, _turn, text = await notifier.next()
        assert "observer survived" in text
        result = capability.results("parent-a")[0]
        assert result.status == "completed"
        assert result.usage == {"prompt_tokens": 5, "completion_tokens": 2}
        assert settled == [{"prompt_tokens": 5, "completion_tokens": 2}]
        assert "step usage observer failed" in result.report_text
        assert result.usage_observer_error is None
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_child_inherits_parent_performance_and_guarded_context_policy(
    tmp_path: Path, monkeypatch
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    config = _config(tmp_path)
    performance = config.agents.defaults.performance
    performance.cache_stable_prompts = True
    performance.max_parallel_tools = 2
    performance.auto_compact = True
    performance.compaction_threshold = 0.7
    performance.max_turn_tokens = 1234
    performance.repeated_call_limit = 2
    performance.reserve_steps = 1

    provider = _FakeProvider(
        lambda *_args: _AsyncValue(
            _ProviderResponse(
                content="performance child",
                usage={"prompt_tokens": 6, "completion_tokens": 1},
            )
        )
    )
    selection = RuntimeSelection(
        "fake",
        "child-model",
        None,
        "http://fake.invalid",
        provider,
        context_window_tokens=128_000,
        context_window_source="model",
    )
    seen: dict[str, Any] = {}
    original_assembler = subagent_runtime_module.PromptAssemblerImpl
    original_driver = subagent_runtime_module.LoopDriverImpl

    class TrackingAssembler(original_assembler):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["cache_stable"] = kwargs.get("cache_stable")
            super().__init__(*args, **kwargs)

    class TrackingDriver(original_driver):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["max_parallel_tools"] = kwargs.get("max_parallel_tools")
            seen["context_policy"] = kwargs.get("context_policy")
            seen["turn_budget"] = kwargs.get("turn_budget")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        subagent_runtime_module, "PromptAssemblerImpl", TrackingAssembler
    )
    monkeypatch.setattr(subagent_runtime_module, "LoopDriverImpl", TrackingDriver)

    notifier = _Notifier()
    capability = SpawnCapability(
        config,
        provider_resolver=lambda _session: selection,
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _general_tools(),
        budget_guard=lambda _parent, _turn: True,
        step_usage_observer=lambda _parent, _turn, _usage: None,
    )
    try:
        accepted = await _spawn(capability, task="performance task")
        assert accepted.status is ToolStatus.SUCCEEDED
        await notifier.next()

        assert seen["cache_stable"] is True
        assert seen["max_parallel_tools"] == 2
        context_policy = seen["context_policy"]
        assert context_policy is not None
        assert context_policy._capacity == 128_000
        assert (
            type(context_policy._provider).__name__ == "_GuardedProvider"
        ), "summary calls must pass through the guard/observer wrapper"
        turn_budget = seen["turn_budget"]
        assert turn_budget is not None
        assert turn_budget.max_tokens is None
        assert turn_budget.repeated_call_limit == 2
        assert turn_budget.reserve_steps == 1
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


class _AsyncValue:
    """Adapter for a synchronous fake responder to keep this test compact."""

    def __init__(self, value: _ProviderResponse) -> None:
        self._value = value

    def __await__(self):
        async def resolve() -> _ProviderResponse:
            return self._value

        return resolve().__await__()

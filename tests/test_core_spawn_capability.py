"""Native Core ``spawn`` capability: schema, isolation, lifecycle, delivery.

The tests exercise the production capability and the real seven Core
components. Only the provider behind the model adapter, the general-tools
capability, and the completion callback are injected fakes.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import PhyAgentOS.agent.capabilities.spawn_capability as spawn_module
import PhyAgentOS.agent.services.subagent_runtime as subagent_runtime_module
from PhyAgentOS.agent.capabilities.spawn_capability import (
    SPAWN_TOOL,
    SpawnCapability,
)
from PhyAgentOS.agent.core import (
    AgentHostImpl,
    AgentRuntimeImpl,
    Budget,
    CancellationToken,
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
from PhyAgentOS.agent.services.subagent_runtime import (
    CHILD_SYSTEM_PROMPT,
    SubagentOutcome,
)
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.service import RuntimeSelection

_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# external fakes
# ---------------------------------------------------------------------------
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
    """Child general-tools capability with a reusable probe tool."""

    def __init__(
        self,
        registrations: tuple[ToolRegistration, ...] = (),
        *,
        prompt: str | None = None,
    ) -> None:
        self._registrations = registrations
        self._prompt = prompt
        self.contexts: list[ExecutionContext] = []
        self.cancel_calls: list[tuple[str, ExecutionContext | None]] = []
        self.closed = False

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        return self._registrations

    def prompt_context(self, context: ExecutionContext) -> str | None:
        self.contexts.append(context)
        return self._prompt

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        self.cancel_calls.append((session_id, context))

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


def _selection(provider: Any, model: str = "child-model") -> RuntimeSelection:
    return RuntimeSelection("fake", model, None, "http://fake.invalid", provider)


def _config(
    *,
    max_concurrent: int = 4,
    queue_limit: int = 8,
    max_iterations: int = 6,
) -> Config:
    config = Config()
    config.agents.defaults.max_concurrent_turns = max_concurrent
    config.agents.defaults.per_session_queue_limit = queue_limit
    config.agents.defaults.max_tool_iterations = max_iterations
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
        reply_target=f"{parent}:reply",
    )


def _call(
    *,
    call_id: str = "call-1",
    task: str = "inspect the workspace",
    label: str | None = "inspect",
) -> ToolCall:
    arguments: dict[str, Any] = {"task": task}
    if label is not None:
        arguments["label"] = label
    return ToolCall(tool_call_id=call_id, name="spawn", arguments=arguments)


def _probe_definition() -> ToolDefinition:
    return ToolDefinition(
        name="probe",
        description="Record the child execution context.",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


def _spawn_stub_definition() -> ToolDefinition:
    return ToolDefinition(
        name="spawn",
        description="Recursive spawn must be removed from the child.",
        parameters={"type": "object", "properties": {}},
    )


def _probe_registration(
    seen: list[tuple[str, str | None, str]],
    *,
    status: ToolStatus = ToolStatus.SUCCEEDED,
) -> ToolRegistration:
    async def handler(call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        seen.append((context.session_id, context.parent_session_id, context.turn_id))
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=status,
            text="probe result" if status is ToolStatus.SUCCEEDED else "probe unknown",
        )

    return ToolRegistration(definition=_probe_definition(), handler=handler)


def _has_tool_message(messages: list[dict[str, Any]]) -> bool:
    return any(message.get("role") == "tool" for message in messages)


def _parent_host(
    store: SQLiteSessionStore,
    provider: _FakeProvider,
    capability: SpawnCapability,
) -> AgentHostImpl:
    registrations = capability.tool_registrations()
    assembler = PromptAssemblerImpl(
        "parent-agent", fragments=(capability.prompt_context,)
    )
    adapter = ModelAdapterImpl(provider, stream=False)
    executor = ToolExecutorImpl(registrations, store)
    driver = LoopDriverImpl(
        assembler=assembler,
        adapter=adapter,
        executor=executor,
        store=store,
        tools=tuple(item.definition for item in registrations),
        model="parent-model",
        temperature=0.0,
        max_tokens=512,
    )
    runtime = AgentRuntimeImpl(driver=driver, store=store, max_steps=6)
    return AgentHostImpl(runtime=runtime, capabilities=(capability,))


def _parent_responder(task: str, label: str = "inspect"):
    async def respond(
        _call_no: int,
        messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        if _has_tool_message(messages):
            return _ProviderResponse(content="parent done")
        return _ProviderResponse(
            tool_calls=(
                _ProviderToolCall(
                    id="call-1",
                    name="spawn",
                    arguments={"task": task, "label": label},
                ),
            )
        )

    return respond


def _direct_child_responder(reply: str = "child answer"):
    async def respond(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        return _ProviderResponse(content=reply)

    return respond


def _probe_child_responder(reply: str = "child final"):
    async def respond(
        _call_no: int,
        messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        if _has_tool_message(messages):
            return _ProviderResponse(content=reply)
        return _ProviderResponse(
            tool_calls=(
                _ProviderToolCall(id="probe-1", name="probe", arguments={}),
            )
        )

    return respond


async def _wait_entered(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), _TIMEOUT)


# ---------------------------------------------------------------------------
# schema and import boundaries
# ---------------------------------------------------------------------------
def test_spawn_schema_keeps_legacy_required_task_and_adds_optional_continuation() -> None:
    assert SPAWN_TOOL.name == "spawn"
    assert "background" in SPAWN_TOOL.description
    assert SPAWN_TOOL.parameters["required"] == ["task"]
    assert set(SPAWN_TOOL.parameters["properties"]) == {
        "task",
        "label",
        "child_session_id",
        "role",
        "tool_allowlist",
    }
    assert SPAWN_TOOL.parameters["properties"]["task"]["type"] == "string"
    assert SPAWN_TOOL.parameters["properties"]["label"]["type"] == "string"
    assert (
        SPAWN_TOOL.parameters["properties"]["child_session_id"]["type"]
        == "string"
    )
    assert (
        SPAWN_TOOL.parameters["properties"]["tool_allowlist"]["type"]
        == "array"
    )


async def test_default_general_tools_are_built_once_without_recursive_spawn(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    config = _config()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Demo workspace skill\n"
        "---\n\n"
        "# Demo\n",
        encoding="utf-8",
    )
    config.agents.defaults.workspace = str(workspace)
    provider = _FakeProvider(_direct_child_responder("used general tools"))
    notifier = _Notifier()
    capability = SpawnCapability(
        config,
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
    )
    try:
        accepted = await capability.tool_registrations()[0].handler(
            _call(), _context("parent-a", "turn-a")
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        await notifier.next()
        offered = {
            item["function"]["name"] for item in (provider.calls[0]["tools"] or [])
        }
        assert {
            "read_file",
            "write_file",
            "exec",
            "web_fetch",
            "query_scene_graph",
        } <= offered
        assert "spawn" not in offered
        system_text = provider.calls[0]["messages"][0]["content"]
        assert f"## Workspace\n{workspace}" in system_text
        assert "demo-skill" in system_text
        assert "Demo workspace skill" in system_text
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


def test_native_spawn_modules_import_no_legacy_or_forge_or_contextvars() -> None:
    module_dir = Path(spawn_module.__file__).parent
    files = [
        Path(spawn_module.__file__),
        module_dir.parent / "services" / "subagent_runtime.py",
    ]
    forbidden = (
        "PhyAgentOS.agent_legacy",
        "PhyAgentOS.agent.loop",
        "PhyAgentOS.forge",
        "agent_legacy",
    )
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * node.level + (node.module or ""))
        for name in imported:
            assert not any(
                name == root or name.startswith(root + ".") for root in forbidden
            ), (path.name, name)
            assert name != "contextvars", path.name

    code = (
        "import sys\n"
        "import PhyAgentOS.agent.capabilities.spawn_capability\n"
        "bad = sorted(name for name in sys.modules "
        "if name.startswith(('PhyAgentOS.agent_legacy', 'PhyAgentOS.forge')))\n"
        "assert not bad, bad\n"
    )
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        check=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# accepted run, notification, metadata and persisted child history
# ---------------------------------------------------------------------------
async def test_spawn_returns_accepted_and_notifies_completed_child(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    seen: list[tuple[str, str | None, str]] = []
    general = _GeneralTools(
        (
            _probe_registration(seen),
            ToolRegistration(
                definition=_spawn_stub_definition(),
                handler=_probe_registration(seen).handler,
            ),
        ),
        prompt="general child fragment",
    )
    child_provider = _FakeProvider(_probe_child_responder("child answer"))
    selections: list[str] = []

    def resolver(session_id: str) -> RuntimeSelection:
        selections.append(session_id)
        return _selection(child_provider)

    notifier = _Notifier()
    capability = SpawnCapability(
        _config(),
        provider_resolver=resolver,
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: general,
    )
    parent_provider = _FakeProvider(_parent_responder("inspect the workspace"))
    host = _parent_host(store, parent_provider, capability)

    try:
        result = await asyncio.wait_for(
            host.submit(TurnRequest("parent-a", "please delegate")),
            _TIMEOUT,
        )
        assert result.status is TurnStatus.COMPLETED
        assert result.reply == "parent done"

        parent_events = await store.read_session("parent-a")
        settled = [
            event for event in parent_events if event.type is EventType.TOOL_SETTLED
        ]
        assert len(settled) == 1
        accepted_text = settled[0].payload["text"]
        assert "started" in accepted_text
        assert "notify" in accepted_text
        assert "completed" not in accepted_text.lower()
        assert "parent done" not in accepted_text

        parent_turns = {
            event.turn_id
            for event in parent_events
            if event.type is EventType.TURN_STARTED
        }
        assert len(parent_turns) == 1
        parent_turn = parent_turns.pop()

        parent_turn_id, notified_text = _notify_fields(await notifier.next())
        assert parent_turn_id == "parent-a"
        assert notified_text.startswith("[Subagent 'inspect' completed]")
        assert "child answer" in notified_text

        assert selections == ["parent-a"]
        # The resolver was read once; no completion path resolves it again.
        assert capability.results("parent-a") == capability.results("parent-a")
        assert len(capability.results("parent-a")) == 1
        child = capability.results("parent-a")[0]
        assert child.parent_session_id == "parent-a"
        assert child.parent_turn_id == parent_turn
        assert child.status == "completed"
        assert child.notified is True
        assert child.reply == "child answer"
        assert child.child_session_id != "parent-a"
        assert capability.pending("parent-a") == 0

        # Child tools are general-only: probe stays, recursive spawn is gone.
        first_child_call = child_provider.calls[0]
        offered = {
            item["function"]["name"] for item in (first_child_call["tools"] or [])
        }
        assert offered == {"probe"}
        system_text = first_child_call["messages"][0]["content"]
        assert CHILD_SYSTEM_PROMPT.strip() in system_text
        # The parent performance default is cache-stable prompts, so live
        # fragments are a runtime-context tail instead of part of the system.
        assert "general child fragment" not in system_text
        runtime_fragments = [
            message
            for message in first_child_call["messages"]
            if message.get("role") == "user"
            and "general child fragment" in (message.get("content") or "")
        ]
        assert len(runtime_fragments) == 1
        assert first_child_call["messages"][1] == {
            "role": "user",
            "content": "inspect the workspace",
        }
        assert "please delegate" not in system_text

        # Parent metadata reaches the child execution context.
        assert seen
        assert seen[0][0] == child.child_session_id
        assert seen[0][1] == "parent-a"

        # The child turn is stored in its own session, next to the parent's.
        child_conversation = await store.read_conversation(child.child_session_id)
        assert {"role": "user", "content": "inspect the workspace"} in [
            dict(message) for message in child_conversation
        ]
        assert child_conversation[-1] == {"role": "assistant", "content": "child answer"}

        # Parent history does not absorb the child's transcript.
        parent_conversation = await store.read_conversation("parent-a")
        assert all(
            message.get("content") != "child answer" for message in parent_conversation
        )
    finally:
        await asyncio.wait_for(host.aclose(), _TIMEOUT)
        await store.aclose()


def _notify_fields(item: tuple[str, str, str]) -> tuple[str, str]:
    parent, _turn, text = item
    return parent, text


# ---------------------------------------------------------------------------
# provider selection is captured at spawn, not at completion
# ---------------------------------------------------------------------------
async def test_provider_selection_is_frozen_at_spawn(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        entered.set()
        await release.wait()
        return _ProviderResponse(content="from provider A")

    provider_a = _FakeProvider(blocked)
    provider_b = _FakeProvider(_direct_child_responder("from provider B"))
    resolver_calls: list[str] = []

    def resolver(session_id: str) -> RuntimeSelection:
        resolver_calls.append(session_id)
        if len(resolver_calls) == 1:
            return _selection(provider_a, "child-model-a")
        return _selection(provider_b, "child-model-b")

    notifier = _Notifier()
    capability = SpawnCapability(
        _config(),
        provider_resolver=resolver,
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _GeneralTools(),
    )

    try:
        accepted = await capability.tool_registrations()[0].handler(
            _call(), _context("parent-a", "turn-a")
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        await _wait_entered(entered)

        # A later turn could change this entry's live selection; the child must
        # still use the object captured when spawn was accepted.
        assert resolver("parent-a").model == "child-model-b"
        release.set()

        parent, text = _notify_fields(await notifier.next())
        assert parent == "parent-a"
        assert "from provider A" in text
        assert len(provider_a.calls) == 1
        assert provider_b.calls == []
        assert resolver_calls == ["parent-a", "parent-a"]
        child = capability.results("parent-a")[0]
        assert child.reply == "from provider A"
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


# ---------------------------------------------------------------------------
# notify_factory captures routing at spawn; default callback is fallback
# ---------------------------------------------------------------------------
async def test_notify_factory_runs_at_spawn_and_keeps_frozen_route(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    child_provider = _FakeProvider(_direct_child_responder("frozen route result"))
    factory_calls: list[ExecutionContext] = []
    notifications: list[tuple[str, str, str, str]] = []
    live_route = {"target": "live-route"}
    default_notifier = _Notifier()

    def notify_factory(context: ExecutionContext):
        factory_calls.append(context)
        frozen_target = live_route["target"]

        async def send(parent: str, turn: str, text: str) -> None:
            notifications.append((parent, turn, text, frozen_target))
            default_notifier.items.append((parent, turn, text))
            await default_notifier.queue.put((parent, turn, text))

        return send

    capability = SpawnCapability(
        _config(),
        provider_resolver=lambda _session: _selection(child_provider),
        store=store,
        notify=default_notifier,
        notify_factory=notify_factory,
        general_tools_factory=lambda _config: _GeneralTools(),
    )

    try:
        accepted = await capability.tool_registrations()[0].handler(
            _call(), _context("parent-a", "turn-a")
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        assert len(factory_calls) == 1
        assert factory_calls[0].session_id == "parent-a"
        assert factory_calls[0].turn_id == "turn-a"

        live_route["target"] = "changed-after-spawn"
        parent, turn, text = await default_notifier.next()
        assert (parent, turn) == ("parent-a", "turn-a")
        assert "frozen route result" in text
        assert notifications == [("parent-a", "turn-a", text, "live-route")]
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


# ---------------------------------------------------------------------------
# child failure, unknown dispatched work, and isolation/cancel/close
# ---------------------------------------------------------------------------
async def test_child_failure_is_reported_as_failure(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    async def failing(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        raise RuntimeError("provider down")

    provider = _FakeProvider(failing)
    notifier = _Notifier()
    capability = SpawnCapability(
        _config(),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _GeneralTools(),
    )

    try:
        accepted = await capability.tool_registrations()[0].handler(
            _call(), _context("parent-a", "turn-a")
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        parent, text = _notify_fields(await notifier.next())
        assert parent == "parent-a"
        assert text.startswith("[Subagent 'inspect' failed]")
        assert "provider down" in text
        assert "did not complete" in text
        assert "completed]" not in text

        result = capability.results("parent-a")[0]
        assert result.status == "failed"
        assert result.error is not None and "provider down" in result.error
        child_events = await store.read_session(result.child_session_id)
        assert any(
            event.type is EventType.TURN_FAILED for event in child_events
        )
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_unknown_dispatched_work_keeps_unknown_semantics(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    seen: list[tuple[str, str | None, str]] = []
    general = _GeneralTools(
        (_probe_registration(seen, status=ToolStatus.UNKNOWN),)
    )
    provider = _FakeProvider(_probe_child_responder("child continued"))
    notifier = _Notifier()
    capability = SpawnCapability(
        _config(),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: general,
    )

    try:
        accepted = await capability.tool_registrations()[0].handler(
            _call(), _context("parent-a", "turn-a")
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        _parent, text = _notify_fields(await notifier.next())
        assert "child continued" in text
        assert "unknown" in text.lower()
        assert "Do not retry them automatically" in text
        result = capability.results("parent-a")[0]
        assert result.status == "completed"
        assert result.unknown_work is True
        assert seen[0][1] == "parent-a"
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_cancel_stops_only_that_parents_children(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    entered: dict[str, asyncio.Event] = {
        "parent-a": asyncio.Event(),
        "parent-b": asyncio.Event(),
    }
    release: dict[str, asyncio.Event] = {
        "parent-a": asyncio.Event(),
        "parent-b": asyncio.Event(),
    }

    def provider_for(parent: str) -> _FakeProvider:
        async def respond(
            _call_no: int,
            _messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]] | None,
        ) -> _ProviderResponse:
            entered[parent].set()
            await release[parent].wait()
            return _ProviderResponse(content=f"reply from {parent}")

        return _FakeProvider(respond)

    providers = {parent: provider_for(parent) for parent in entered}
    notifier = _Notifier()
    capability = SpawnCapability(
        _config(max_concurrent=2, queue_limit=2),
        provider_resolver=lambda session: _selection(providers[session]),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _GeneralTools(),
    )

    try:
        a = await capability.tool_registrations()[0].handler(
            _call(task="task A"), _context("parent-a", "turn-a")
        )
        b = await capability.tool_registrations()[0].handler(
            _call(task="task B"), _context("parent-b", "turn-b")
        )
        assert a.status is ToolStatus.SUCCEEDED
        assert b.status is ToolStatus.SUCCEEDED
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered.values())),
            _TIMEOUT,
        )

        await capability.cancel("parent-a", None)
        # Stopping a child deliberately skips a new late callback; the
        # interrupted result stays available to the parent prompt/results.
        assert notifier.items == []
        assert capability.pending("parent-a") == 0
        assert capability.pending("parent-b") == 1
        stopped = capability.results("parent-a")[0]
        assert stopped.status == "interrupted"
        assert stopped.notify_error is not None

        release["parent-b"].set()
        parent, text = _notify_fields(await notifier.next())
        assert parent == "parent-b"
        assert "reply from parent-b" in text

        statuses = {
            result.parent_session_id: result.status
            for result in capability.results()
        }
        assert statuses == {"parent-a": "interrupted", "parent-b": "completed"}
        assert capability.results("parent-b")[0].notified is True
        # The shared store is still usable during capability life.
        assert await store.read_session("parent-a") is not None
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_cancel_active_child_skips_late_callback_and_keeps_interrupted(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    entered = asyncio.Event()
    never = asyncio.Event()
    callback_started = asyncio.Event()

    async def blocked_provider(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        entered.set()
        await never.wait()
        return _ProviderResponse(content="never reached")

    async def blocked_notify(_parent: str, _turn: str, _text: str) -> None:
        callback_started.set()
        await never.wait()

    provider = _FakeProvider(blocked_provider)
    capability = SpawnCapability(
        _config(max_concurrent=1, queue_limit=1),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=blocked_notify,
        general_tools_factory=lambda _config: _GeneralTools(),
    )
    try:
        accepted = await capability.tool_registrations()[0].handler(
            _call(task="active task"), _context("parent-a", "turn-a")
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        await _wait_entered(entered)

        # The child is still in the provider call, so no notification task
        # exists yet when the stop begins.
        await asyncio.wait_for(capability.cancel("parent-a", None), _TIMEOUT)
        result = capability.results("parent-a")[0]
        assert result.status == "interrupted"
        assert result.notify_error is not None
        assert "skipped" in result.notify_error
        assert result.notified is False
        assert callback_started.is_set() is False
        assert capability.pending("parent-a") == 0

        child_events = await store.read_session(result.child_session_id)
        assert any(event.type is EventType.TURN_INTERRUPTED for event in child_events)
        assert await store.read_session("parent-a") == ()
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_close_queued_child_skips_late_callback_and_is_bounded(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    entered = asyncio.Event()
    never = asyncio.Event()
    callback_started = asyncio.Event()

    async def blocked_provider(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        entered.set()
        await never.wait()
        return _ProviderResponse(content="never reached")

    async def blocked_notify(_parent: str, _turn: str, _text: str) -> None:
        callback_started.set()
        await never.wait()

    provider = _FakeProvider(blocked_provider)
    capability = SpawnCapability(
        _config(max_concurrent=1, queue_limit=1),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=blocked_notify,
        general_tools_factory=lambda _config: _GeneralTools(),
    )
    try:
        first = await capability.tool_registrations()[0].handler(
            _call(task="running task"), _context("parent-a", "turn-a")
        )
        await _wait_entered(entered)
        second = await capability.tool_registrations()[0].handler(
            _call(task="queued task"), _context("parent-a", "turn-b")
        )
        assert first.status is ToolStatus.SUCCEEDED
        assert second.status is ToolStatus.SUCCEEDED
        assert capability.pending("parent-a") == 2

        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        assert callback_started.is_set() is False
        assert capability.pending() == 0
        by_task = {result.task: result for result in capability.results("parent-a")}
        assert by_task["running task"].status == "interrupted"
        assert by_task["queued task"].status == "cancelled"
        assert by_task["running task"].notify_error is not None
        assert by_task["queued task"].notify_error is not None
        assert await store.read_session("parent-a") == ()
        assert await store.read_session(by_task["queued task"].child_session_id) == ()
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_limits_bound_background_tasks_and_close_keeps_store_open(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        entered.set()
        await release.wait()
        return _ProviderResponse(content="late")

    provider = _FakeProvider(blocked)
    general = _GeneralTools()
    capability = SpawnCapability(
        _config(max_concurrent=1, queue_limit=1),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        general_tools_factory=lambda _config: general,
    )

    first = await capability.tool_registrations()[0].handler(
        _call(call_id="call-1"), _context("parent-a", "turn-a")
    )
    await _wait_entered(entered)
    second = await capability.tool_registrations()[0].handler(
        _call(call_id="call-2"), _context("parent-a", "turn-b")
    )
    third = await capability.tool_registrations()[0].handler(
        _call(call_id="call-3"), _context("parent-a", "turn-c")
    )

    assert first.status is ToolStatus.SUCCEEDED
    assert "No live completion callback" in first.text
    assert second.status is ToolStatus.SUCCEEDED
    assert third.status is ToolStatus.FAILED
    assert third.error is not None and third.error.code == "spawn_limit"
    assert capability.pending("parent-a") == 2

    try:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        assert capability.pending() == 0
        assert general.closed is True
        leftovers = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("subagent:")
        ]
        assert leftovers == []
        # aclose must not close the parent's shared store.
        assert await store.read_session("parent-a") == ()
        closed = await capability.tool_registrations()[0].handler(
            _call(), _context("parent-a", "turn-d")
        )
        assert closed.status is ToolStatus.FAILED
        assert closed.error is not None and closed.error.code == "spawn_closed"
    finally:
        release.set()
        await store.aclose()


async def test_same_tool_call_id_in_two_parent_sessions_does_not_collide(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    child_provider = _FakeProvider(_direct_child_responder("child reply"))
    notifier = _Notifier()
    capability = SpawnCapability(
        _config(),
        provider_resolver=lambda _session: _selection(child_provider),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _GeneralTools(),
    )
    parent_provider = _FakeProvider(_parent_responder("task"))
    host = _parent_host(store, parent_provider, capability)

    try:
        first = await host.submit(TurnRequest("parent-a", "delegate A"))
        second = await host.submit(TurnRequest("parent-b", "delegate B"))
        assert first.status is TurnStatus.COMPLETED
        assert second.status is TurnStatus.COMPLETED

        notified = [await notifier.next(), await notifier.next()]
        assert {item[0] for item in notified} == {"parent-a", "parent-b"}

        results = capability.results()
        assert len(results) == 2
        child_ids = {result.child_session_id for result in results}
        assert len(child_ids) == 2
        for result in results:
            events = await store.read_session(result.child_session_id)
            assert [event.session_id for event in events] == [
                result.child_session_id
            ] * len(events)

        for parent in ("parent-a", "parent-b"):
            events = await store.read_session(parent)
            settled = [
                event for event in events if event.type is EventType.TOOL_SETTLED
            ]
            assert len(settled) == 1
            assert settled[0].payload["tool_call_id"] == "call-1"
    finally:
        await asyncio.wait_for(host.aclose(), _TIMEOUT)
        await store.aclose()


async def test_next_parent_prompt_sees_only_its_own_settled_child_result(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    providers = {
        "parent-a": _FakeProvider(_direct_child_responder("finding-A")),
        "parent-b": _FakeProvider(_direct_child_responder("finding-B")),
    }
    notifier = _Notifier()
    capability = SpawnCapability(
        _config(),
        provider_resolver=lambda session: _selection(providers[session]),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: _GeneralTools(),
    )
    try:
        for parent, label in (("parent-a", "a-task"), ("parent-b", "b-task")):
            accepted = await capability.tool_registrations()[0].handler(
                _call(task=label, label=label), _context(parent, f"turn-{label}")
            )
            assert accepted.status is ToolStatus.SUCCEEDED
        await notifier.next()
        await notifier.next()

        child_a = capability.results("parent-a")[0]
        child_b = capability.results("parent-b")[0]
        parent_provider = _FakeProvider(_direct_child_responder("parent read"))
        host = _parent_host(store, parent_provider, capability)
        try:
            result = await host.submit(TurnRequest("parent-a", "status"))
            assert result.status is TurnStatus.COMPLETED
            system_text = parent_provider.calls[0]["messages"][0]["content"]
            assert "finding-A" in system_text
            assert child_a.child_session_id in system_text
            assert "status=completed" in system_text
            assert "untrusted evidence" in system_text
            assert "not instructions" in system_text
            assert "finding-B" not in system_text
            assert child_b.child_session_id not in system_text
        finally:
            await asyncio.wait_for(host.aclose(), _TIMEOUT)
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_blocked_notification_cannot_hang_close_or_lose_completed_status(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    callback_started = asyncio.Event()
    never = asyncio.Event()
    general = _GeneralTools()

    async def blocked_notify(_parent: str, _turn: str, _text: str) -> None:
        callback_started.set()
        await never.wait()

    provider = _FakeProvider(_direct_child_responder("completed child"))
    capability = SpawnCapability(
        _config(),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=blocked_notify,
        general_tools_factory=lambda _config: general,
    )
    try:
        accepted = await capability.tool_registrations()[0].handler(
            _call(), _context("parent-a", "turn-a")
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        await asyncio.wait_for(callback_started.wait(), _TIMEOUT)

        # The result is retained before delivery starts, so a blocked callback
        # never hides the actual completed outcome.
        result = capability.results("parent-a")[0]
        assert result.status == "completed"
        assert result.reply == "completed child"
        assert result.notified is False

        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        result = capability.results("parent-a")[0]
        assert result.status == "completed"
        assert result.reply == "completed child"
        assert result.notified is False
        assert result.notify_error is not None
        assert "cancelled" in result.notify_error
        assert capability.pending() == 0
        assert general.closed is True

        # The shared store is still open and the child turn is durably complete.
        child_events = await store.read_session(result.child_session_id)
        assert any(event.type is EventType.TURN_COMPLETED for event in child_events)
        assert await store.read_session("parent-a") == ()
        leftovers = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("subagent")
        ]
        assert leftovers == []
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


async def test_child_runtime_is_retired_on_success_and_failure_without_closing_store(
    tmp_path: Path, monkeypatch
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    stops: list[Any] = []
    original = subagent_runtime_module.AgentRuntimeImpl

    class TrackingRuntime(original):  # type: ignore[misc, valid-type]
        async def stop(self) -> None:
            stops.append(self)
            await super().stop()

    monkeypatch.setattr(subagent_runtime_module, "AgentRuntimeImpl", TrackingRuntime)

    async def failing(
        _call_no: int,
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]] | None,
    ) -> _ProviderResponse:
        raise RuntimeError("child failed")

    current_provider = [_FakeProvider(_direct_child_responder("ok"))]
    general = _GeneralTools()
    notifier = _Notifier()
    capability = SpawnCapability(
        _config(),
        provider_resolver=lambda _session: _selection(current_provider[0]),
        store=store,
        notify=notifier,
        general_tools_factory=lambda _config: general,
    )
    try:
        await capability.tool_registrations()[0].handler(
            _call(task="ok"), _context("parent-a", "turn-ok")
        )
        await notifier.next()
        assert len(stops) == 1

        current_provider[0] = _FakeProvider(failing)
        await capability.tool_registrations()[0].handler(
            _call(task="fail"), _context("parent-a", "turn-fail")
        )
        await notifier.next()
        assert len(stops) == 2

        # Retiring a child runtime does not close the shared store, and the
        # shared general-tools object remains open until capability shutdown.
        assert general.closed is False
        for result in capability.results("parent-a"):
            assert await store.read_session(result.child_session_id) is not None
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()


def test_subagent_outcome_report_never_calls_failure_completion() -> None:
    failed = SubagentOutcome(
        session_id="child-1",
        parent_session_id="parent-a",
        parent_turn_id="turn-a",
        turn_id="turn-1",
        label="x",
        task="t",
        status="failed",
        error="boom",
    ).report()
    assert "failed" in failed
    assert "did not complete" in failed
    assert "completed]" not in failed

    cancelled = SubagentOutcome(
        session_id="child-1",
        parent_session_id="parent-a",
        parent_turn_id="turn-a",
        turn_id=None,
        label="x",
        task="t",
        status="cancelled",
    ).report()
    assert "cancelled before it ran" in cancelled
    assert "No model call was made" in cancelled


def test_child_result_is_retained_even_when_delivery_fails(tmp_path: Path) -> None:
    asyncio.run(_delivery_failure_scenario(tmp_path))


async def _delivery_failure_scenario(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    provider = _FakeProvider(_direct_child_responder("delivered anyway"))
    delivered = asyncio.Event()
    calls: list[tuple[str, str, str]] = []

    async def failing_notify(parent: str, turn: str, text: str) -> None:
        calls.append((parent, turn, text))
        delivered.set()
        raise RuntimeError("delivery backend is down")

    capability = SpawnCapability(
        _config(),
        provider_resolver=lambda _session: _selection(provider),
        store=store,
        notify=failing_notify,
        general_tools_factory=lambda _config: _GeneralTools(),
    )
    try:
        accepted = await capability.tool_registrations()[0].handler(
            _call(), _context("parent-a", "turn-a")
        )
        assert accepted.status is ToolStatus.SUCCEEDED
        await asyncio.wait_for(delivered.wait(), _TIMEOUT)
        # aclose waits for the child task, so the retained result is complete.
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        result = capability.results("parent-a")[0]
        assert result.status == "completed"
        assert result.notified is False
        assert result.notify_error is not None
        assert "delivery backend is down" in result.notify_error
        assert "delivered anyway" in result.report_text
    finally:
        await asyncio.wait_for(capability.aclose(), _TIMEOUT)
        await store.aclose()

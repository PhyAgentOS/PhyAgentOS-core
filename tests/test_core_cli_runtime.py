"""Core CLI session ownership: per-turn model selection and resource cleanup."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from PhyAgentOS.agent.capabilities.forge_capability import ForgeCapability
from PhyAgentOS.agent.capabilities.skill_access_capability import SkillAccessCapability
from PhyAgentOS.agent.core import SQLiteSessionStore, TurnRequest, TurnResult, TurnStatus
from PhyAgentOS.agent.core.events import EventType
from PhyAgentOS.agent.core.session import SessionStoreClosedError
from PhyAgentOS.agent.services.skill_documents import SkillDocumentLoader
from PhyAgentOS.agent.services.turn_activation import TurnSkillActivationManager
from PhyAgentOS.cli.command_groups import agent_commands
from PhyAgentOS.cli.command_groups.agent_commands import (
    CoreAgentSession,
    _compose_for_selection,
    _DisplayStore,
    open_core_session,
    run_core_agent,
)
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.base import LLMResponse
from PhyAgentOS.providers.service import RuntimeSelection


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, **kwargs})
        return LLMResponse(content="done", finish_reason="stop")


class RecordingOutput:
    def __init__(self) -> None:
        self.events = []
        self.infos = []
        self.errors = []
        self.results = []

    def event(self, event) -> None:
        self.events.append(event)

    def info(self, text: str) -> None:
        self.infos.append(text)

    def error(self, text: str) -> None:
        self.errors.append(text)

    def result(self, result) -> None:
        self.results.append(result)


class FakeCoordinator:
    def __init__(self) -> None:
        self.activation_manager = None
        self.binding_resolver = None
        self.store = SimpleNamespace(latest_for_session=lambda session: None)
        self.reconcile_calls = 0
        self.reconcile_error: Exception | None = None

    def set_activation_manager(self, manager) -> None:
        self.activation_manager = manager

    def unknown_remote_blockers(self) -> list[dict]:
        return []

    async def reconcile_nonterminal(self):
        self.reconcile_calls += 1
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return None


class FakeSelections:
    def __init__(self, selection: RuntimeSelection) -> None:
        self.selection = selection
        self.commands: list[tuple[str, str]] = []

    def get(self, session_id: str) -> RuntimeSelection:
        return self.selection

    def command(self, session_id: str, text: str) -> str:
        self.commands.append((session_id, text))
        return f"accepted {text}"


def _config(tmp_path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    return config


def _shared_capabilities(tmp_path, coordinator):
    manager = TurnSkillActivationManager(
        SkillDocumentLoader(
            tmp_path / "workspace",
            builtin_skills_dir=tmp_path / "builtin",
            installed_skills_dir=tmp_path / "installed",
        )
    )
    return manager, (
        SkillAccessCapability(manager),
        ForgeCapability(coordinator),
    )


async def test_submit_rebuilds_assembly_for_the_current_selection(tmp_path):
    store = SQLiteSessionStore(":memory:")
    output = RecordingOutput()
    display = _DisplayStore(store, output)
    config = _config(tmp_path)
    coordinator = FakeCoordinator()
    manager, (skill_capability, forge_capability) = _shared_capabilities(
        tmp_path, coordinator
    )
    provider_one, provider_two = RecordingProvider(), RecordingProvider()
    first = RuntimeSelection("fake", "model-one", None, None, provider_one, 128000)
    second = RuntimeSelection("fake", "model-two", "high", None, provider_two, 128000)

    def build(selection):
        return _compose_for_selection(
            selection,
            config=config,
            store=display,
            coordinator=coordinator,
            client=None,
            instructions="test",
            skill_capability=skill_capability,
            forge_capability=forge_capability,
        )

    assemblies = [build(first)]
    seed = assemblies[0]

    def build_and_record(selection):
        assembly = build(selection)
        assemblies.append(assembly)
        return assembly

    selections = FakeSelections(first)
    session = CoreAgentSession(
        seed,
        first,
        build=build_and_record,
        store=display,
        sessions=selections,
        output=output,
    )
    try:
        one = await session.submit(TurnRequest("cli:test", "first"))
        assert one.status is TurnStatus.COMPLETED
        selections.selection = second
        two = await session.submit(TurnRequest("cli:test", "second"))
        assert two.status is TurnStatus.COMPLETED

        events = await store.read_session("cli:test")
        requests = [event for event in events if event.type is EventType.REQUEST_PREPARED]
        assert requests[-2].payload["model"] == "model-one"
        assert requests[-2].payload["reasoning_effort"] is None
        assert requests[-1].payload["model"] == "model-two"
        assert requests[-1].payload["reasoning_effort"] == "high"
        assert [call["model"] for call in provider_one.calls] == ["model-one"]
        assert [call["model"] for call in provider_two.calls] == ["model-two"]
        assert provider_two.calls[0]["reasoning_effort"] == "high"

        # The previous turn's runtime was retired without closing the shared
        # store; a retired assembly can no longer accept turns.
        with pytest.raises(RuntimeError, match="stopped"):
            await assemblies[1].runtime.submit(TurnRequest("cli:test", "again"))
        assert not store._closed
    finally:
        await session.aclose()

    with pytest.raises(SessionStoreClosedError):
        await store.read_session("cli:test")
    assert manager.current("cli:test", "anything") == ()
    assert output.events


async def test_model_command_delegates_to_the_shared_session_runtimes(tmp_path):
    store = SQLiteSessionStore(":memory:")
    output = RecordingOutput()
    display = _DisplayStore(store, output)
    config = _config(tmp_path)
    coordinator = FakeCoordinator()
    manager, (skill_capability, forge_capability) = _shared_capabilities(
        tmp_path, coordinator
    )
    provider = RecordingProvider()
    selection = RuntimeSelection("fake", "model-one", None, None, provider, 128000)

    def build(current):
        return _compose_for_selection(
            current,
            config=config,
            store=display,
            coordinator=coordinator,
            client=None,
            instructions="test",
            skill_capability=skill_capability,
            forge_capability=forge_capability,
        )

    selections = FakeSelections(selection)
    session = CoreAgentSession(
        build(selection),
        selection,
        build=build,
        store=display,
        sessions=selections,
        output=output,
    )
    try:
        assert session.model_command("cli:test", "/status") == "accepted /status"
        assert selections.commands == [("cli:test", "/status")]
    finally:
        await session.aclose()
    assert manager.current("cli:test", "anything") == ()


class _FakeProviderService:
    def __init__(self, config: Config) -> None:
        self.config = config.model_copy(deep=True)
        self.provider = RecordingProvider()

    def resolve(self, name=None, model=None, effort=None) -> RuntimeSelection:
        return RuntimeSelection(
            "fake", "model-one", None, None, self.provider, 128000
        )


class _FakeForgeFactory:
    def __init__(self) -> None:
        self.cleanup: list[str] = []
        self.coordinator: FakeCoordinator | None = None

    def __call__(self, config, provider, *, write_legacy_lessons=None, resources=None):
        cleanup = self.cleanup
        coordinator = FakeCoordinator()
        self.coordinator = coordinator

        class Client:
            async def close(self):
                cleanup.append("client")

        client = Client()
        verifier = SimpleNamespace(stop=lambda: cleanup.append("verifier"))
        if resources is not None:
            resources.push_async_callback(client.close)
            resources.push_async_callback(asyncio.to_thread, verifier.stop)
        return client, None, coordinator, lambda name: True


async def test_open_core_session_creates_store_parent_and_closes_resources(
    tmp_path, monkeypatch
):
    factory = _FakeForgeFactory()
    created: list[SQLiteSessionStore] = []

    class RecordingStore(SQLiteSessionStore):
        def __init__(self, path, **kwargs):
            super().__init__(path, **kwargs)
            created.append(self)

    monkeypatch.setattr(agent_commands, "ProviderService", _FakeProviderService)
    monkeypatch.setattr(agent_commands, "build_forge_components", factory)
    monkeypatch.setattr(agent_commands, "SQLiteSessionStore", RecordingStore)
    config = _config(tmp_path)
    output = RecordingOutput()

    async with open_core_session(config, output=output):
        assert (tmp_path / "workspace" / "sessions" / "core.db").is_file()

    assert len(created) == 1
    assert created[0]._closed
    assert factory.cleanup == ["client", "verifier"] or factory.cleanup == [
        "verifier",
        "client",
    ]


async def test_open_core_session_recovers_forge_once_before_the_first_turn(
    tmp_path, monkeypatch
):
    factory = _FakeForgeFactory()
    monkeypatch.setattr(agent_commands, "ProviderService", _FakeProviderService)
    monkeypatch.setattr(agent_commands, "build_forge_components", factory)
    config = _config(tmp_path)
    output = RecordingOutput()

    async with open_core_session(config, output=output) as session:
        coordinator = factory.coordinator
        assert coordinator is not None
        # Startup recovery ran while opening, before any turn was accepted.
        assert coordinator.reconcile_calls == 1
        first = await session.submit(TurnRequest("cli:test", "one"))
        second = await session.submit(TurnRequest("cli:test", "two"))
        assert first.status is TurnStatus.COMPLETED
        assert second.status is TurnStatus.COMPLETED
        # Later turns do not rerun recovery.
        assert coordinator.reconcile_calls == 1


async def test_open_core_session_reports_recovery_failure_and_still_cleans_up(
    tmp_path, monkeypatch
):
    class FailingRecoveryFactory(_FakeForgeFactory):
        def __call__(self, config, provider, *, write_legacy_lessons=None, resources=None):
            result = super().__call__(
                config,
                provider,
                write_legacy_lessons=write_legacy_lessons,
                resources=resources,
            )
            assert self.coordinator is not None
            self.coordinator.reconcile_error = RuntimeError("gateway unreachable")
            return result

    factory = FailingRecoveryFactory()
    monkeypatch.setattr(agent_commands, "ProviderService", _FakeProviderService)
    monkeypatch.setattr(agent_commands, "build_forge_components", factory)
    config = _config(tmp_path)
    output = RecordingOutput()

    async with open_core_session(config, output=output) as session:
        assert factory.coordinator is not None
        assert factory.coordinator.reconcile_calls == 1
        assert any("startup recovery" in text for text in output.errors)
        result = await session.submit(TurnRequest("cli:test", "one"))
        assert result.status is TurnStatus.COMPLETED

    assert factory.cleanup == ["client", "verifier"] or factory.cleanup == [
        "verifier",
        "client",
    ]


async def test_open_core_session_failure_closes_forge_components_before_store(
    tmp_path, monkeypatch
):
    factory = _FakeForgeFactory()

    class FailingStore:
        def __init__(self, path, **kwargs):
            raise RuntimeError("store path unavailable")

    monkeypatch.setattr(agent_commands, "ProviderService", _FakeProviderService)
    monkeypatch.setattr(agent_commands, "build_forge_components", factory)
    monkeypatch.setattr(agent_commands, "SQLiteSessionStore", FailingStore)
    config = _config(tmp_path)
    output = RecordingOutput()

    with pytest.raises(RuntimeError, match="store path unavailable"):
        async with open_core_session(config, output=output):
            pass
    assert factory.cleanup == ["client", "verifier"] or factory.cleanup == [
        "verifier",
        "client",
    ]


async def test_open_core_session_closes_composed_host_when_session_creation_fails(
    tmp_path, monkeypatch
):
    factory = _FakeForgeFactory()
    created: list[SQLiteSessionStore] = []

    class RecordingStore(SQLiteSessionStore):
        def __init__(self, path, **kwargs):
            super().__init__(path, **kwargs)
            created.append(self)

    def fail_session(*args, **kwargs):
        raise RuntimeError("session construction failed")

    monkeypatch.setattr(agent_commands, "ProviderService", _FakeProviderService)
    monkeypatch.setattr(agent_commands, "build_forge_components", factory)
    monkeypatch.setattr(agent_commands, "SQLiteSessionStore", RecordingStore)
    monkeypatch.setattr(agent_commands, "CoreAgentSession", fail_session)
    config = _config(tmp_path)
    output = RecordingOutput()

    with pytest.raises(RuntimeError, match="session construction failed"):
        async with open_core_session(config, output=output):
            pass
    assert created[0]._closed
    assert factory.cleanup == ["client", "verifier"] or factory.cleanup == [
        "verifier",
        "client",
    ]


class _FakeRuntime:
    def __init__(self, label: str) -> None:
        self.label = label
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _DoneHost:
    def __init__(self) -> None:
        self.cancel_calls = 0

    async def submit(self, request):
        return TurnResult(
            request.session_id, "turn-done", TurnStatus.COMPLETED, reply="ok"
        )

    async def cancel(self, session_id: str) -> None:
        self.cancel_calls += 1


class _GateHost:
    def __init__(self) -> None:
        self.submitted = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cancel_calls = 0

    async def submit(self, request):
        self.submitted.set()
        await self.cancelled.wait()
        return TurnResult(request.session_id, "turn-gated", TurnStatus.INTERRUPTED)

    async def cancel(self, session_id: str) -> None:
        self.cancel_calls += 1
        self.cancelled.set()


class _FakeCapability:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.closed = False

    async def cancel(self, session_id, context) -> None:
        self.cancelled.append(session_id)

    async def aclose(self) -> None:
        self.closed = True


class _FakeStore:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _fake_assembly(host, runtime, capability):
    return SimpleNamespace(host=host, runtime=runtime, capabilities=(capability,))


async def test_cancel_targets_the_current_assembly_not_a_retired_one():
    """A cancel reaches the assembly registered for the running turn."""
    capability = _FakeCapability()
    store = _FakeStore()
    seed_runtime, seed_host = _FakeRuntime("seed"), _DoneHost()
    seed = _fake_assembly(seed_host, seed_runtime, capability)
    first_runtime, first_host = _FakeRuntime("first"), _DoneHost()
    second_runtime, second_host = _FakeRuntime("second"), _GateHost()

    class Selections:
        selection = RuntimeSelection("fake", "one", None, None, object(), 128000)

        def get(self, session_id):
            return self.selection

    selections = Selections()

    def build(selection):
        if selection.model == "one":
            return _fake_assembly(first_host, first_runtime, capability)
        return _fake_assembly(second_host, second_runtime, capability)

    session = CoreAgentSession(
        seed,
        selections.selection,
        build=build,
        store=store,
        sessions=selections,
        output=RecordingOutput(),
    )
    try:
        first = await session.submit(TurnRequest("cli:test", "first"))
        assert first.status is TurnStatus.COMPLETED
        # One assembly serves one turn, so the first turn's runtime is retired.
        assert first_runtime.stopped

        selections.selection = RuntimeSelection(
            "fake", "two", "high", None, object(), 128000
        )
        second_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "second"))
        )
        await asyncio.wait_for(second_host.submitted.wait(), 2)

        # The cancel targets the turn that is registered now; it is neither the
        # seed that never ran nor the retired first assembly.
        cancel = await asyncio.wait_for(session.cancel("cli:test"), 2)
        second = await asyncio.wait_for(second_task, 2)

        assert cancel is None
        assert second.status is TurnStatus.INTERRUPTED
        assert second_host.cancel_calls == 1
        assert first_host.cancel_calls == 0
        assert seed_host.cancel_calls == 0
        assert second_runtime.stopped
    finally:
        await session.aclose()

    assert store.closed
    assert capability.closed
    assert capability.cancelled == ["cli:test"]


async def test_a_queued_submit_is_not_cancelled_by_the_running_turn():
    """The cancel settles the running turn only; the queued turn starts clean."""
    capability = _FakeCapability()
    store = _FakeStore()
    seed_runtime, seed_host = _FakeRuntime("seed"), _DoneHost()
    seed = _fake_assembly(seed_host, seed_runtime, capability)
    first_runtime, first_host = _FakeRuntime("first"), _GateHost()
    second_runtime, second_host = _FakeRuntime("second"), _GateHost()

    class Selections:
        selection = RuntimeSelection("fake", "one", None, None, object(), 128000)

        def get(self, session_id):
            return self.selection

    selections = Selections()

    def build(selection):
        if selection.model == "one":
            return _fake_assembly(first_host, first_runtime, capability)
        return _fake_assembly(second_host, second_runtime, capability)

    session = CoreAgentSession(
        seed,
        selections.selection,
        build=build,
        store=store,
        sessions=selections,
        output=RecordingOutput(),
    )
    try:
        first_task = asyncio.create_task(session.submit(TurnRequest("cli:test", "first")))
        await asyncio.wait_for(first_host.submitted.wait(), 2)

        selections.selection = RuntimeSelection(
            "fake", "two", "high", None, object(), 128000
        )
        second_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "second"))
        )
        await asyncio.sleep(0)
        assert not second_host.submitted.is_set()  # queued behind the first turn

        # The cancel reaches the running turn and returns without waiting for
        # the queued submit or for the next turn.
        assert await asyncio.wait_for(session.cancel("cli:test"), 2) is None
        first = await asyncio.wait_for(first_task, 2)
        assert first.status is TurnStatus.INTERRUPTED
        assert first_host.cancel_calls == 1
        assert second_host.cancel_calls == 0

        # The queued submit becomes current only after the first turn settled,
        # with its own runtime that never saw the earlier cancel.
        await asyncio.wait_for(second_host.submitted.wait(), 2)
        assert second_host.cancel_calls == 0
        assert await asyncio.wait_for(session.cancel("cli:test"), 2) is None
        second = await asyncio.wait_for(second_task, 2)
        assert second.status is TurnStatus.INTERRUPTED
        assert second_host.cancel_calls == 1
        assert first_host.cancel_calls == 1
        assert seed_host.cancel_calls == 0
    finally:
        await session.aclose()

    assert store.closed
    assert capability.closed
    assert capability.cancelled == ["cli:test"]


async def test_a_failed_turn_does_not_strand_the_session_queue():
    """A build failure reaches its caller; the next submit still runs."""
    capability = _FakeCapability()
    store = _FakeStore()
    seed_runtime, seed_host = _FakeRuntime("seed"), _DoneHost()
    seed = _fake_assembly(seed_host, seed_runtime, capability)
    runtime, host = _FakeRuntime("turn"), _DoneHost()

    class Selections:
        selection = RuntimeSelection("fake", "broken", None, None, object(), 128000)

        def get(self, session_id):
            return self.selection

    selections = Selections()

    def build(selection):
        if selection.model == "broken":
            raise RuntimeError("composition failed")
        return _fake_assembly(host, runtime, capability)

    session = CoreAgentSession(
        seed,
        selections.selection,
        build=build,
        store=store,
        sessions=selections,
        output=RecordingOutput(),
    )
    try:
        with pytest.raises(RuntimeError, match="composition failed"):
            await session.submit(TurnRequest("cli:test", "first"))
        assert seed_host.cancel_calls == 0
        # The failed submit left no current assembly, so a cancel still reaches
        # the seed instead of a half-built turn.
        assert await session.cancel("cli:test") is None
        assert seed_host.cancel_calls == 1

        selections.selection = RuntimeSelection(
            "fake", "ok", None, None, object(), 128000
        )
        result = await asyncio.wait_for(
            session.submit(TurnRequest("cli:test", "second")), 2
        )
        assert result.status is TurnStatus.COMPLETED
        assert host.cancel_calls == 0
        assert runtime.stopped
    finally:
        await session.aclose()

    assert store.closed


class _GatedProvider:
    """Record the generation arguments, then block until the test releases it.

    ``stubborn`` swallows the first local cancellation so a shutdown can be
    held open while the test checks that a queued submit already left.
    """

    def __init__(self, reply: str = "done", *, stubborn: bool = False) -> None:
        self.reply, self.stubborn = reply, stubborn
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        self.calls.append(
            {
                "model": kwargs.get("model"),
                "reasoning_effort": kwargs.get("reasoning_effort"),
            }
        )
        self.started.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            if not self.stubborn:
                raise
            await self.gate.wait()
        return LLMResponse(content=self.reply, finish_reason="stop")


def _open_real_session(tmp_path, selections, *, builds=None):
    """Build a CoreAgentSession over a real store and per-turn assemblies."""
    store = SQLiteSessionStore(":memory:")
    output = RecordingOutput()
    display = _DisplayStore(store, output)
    config = _config(tmp_path)
    coordinator = FakeCoordinator()
    manager, (skill_capability, forge_capability) = _shared_capabilities(
        tmp_path, coordinator
    )

    def build(selection):
        if builds is not None:
            builds.append(selection.model)
        return _compose_for_selection(
            selection,
            config=config,
            store=display,
            coordinator=coordinator,
            client=None,
            instructions="test",
            skill_capability=skill_capability,
            forge_capability=forge_capability,
        )

    selected = selections.get("cli:test")
    session = CoreAgentSession(
        build(selected),
        selected,
        build=build,
        store=display,
        sessions=selections,
        output=output,
    )
    return store, output, session, manager


async def test_same_session_submits_run_in_registration_order(tmp_path):
    first_provider = _GatedProvider("first reply")
    second_provider = _GatedProvider("second reply")
    first = RuntimeSelection("fake", "model-one", None, None, first_provider, 128000)
    second = RuntimeSelection("fake", "model-two", "high", None, second_provider, 128000)
    selections = FakeSelections(first)
    builds: list[str] = []
    store, _, session, manager = _open_real_session(tmp_path, selections, builds=builds)
    try:
        assert builds == ["model-one"]  # the seed only; no turn has started
        first_task = asyncio.create_task(session.submit(TurnRequest("cli:test", "first")))
        await asyncio.wait_for(first_provider.started.wait(), 2)

        selections.selection = second
        second_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "second"))
        )
        for _ in range(20):
            await asyncio.sleep(0)
        # The queued submit built nothing and reached no provider, and the
        # running turn is still executing.
        assert builds == ["model-one", "model-one"]
        assert second_provider.calls == []
        assert not first_task.done()

        first_provider.gate.set()
        one = await asyncio.wait_for(first_task, 2)
        await asyncio.wait_for(second_provider.started.wait(), 2)
        second_provider.gate.set()
        two = await asyncio.wait_for(second_task, 2)

        assert one.status is TurnStatus.COMPLETED
        assert two.status is TurnStatus.COMPLETED
        assert first_provider.calls == [{"model": "model-one", "reasoning_effort": None}]
        assert second_provider.calls == [
            {"model": "model-two", "reasoning_effort": "high"}
        ]

        events = await store.read_session("cli:test")
        prepared = [
            (event.payload["model"], event.payload["reasoning_effort"])
            for event in events
            if event.type is EventType.REQUEST_PREPARED
        ]
        assert prepared == [("model-one", None), ("model-two", "high")]
        assert not store._closed
    finally:
        await session.aclose()

    assert manager.current("cli:test", "anything") == ()
    with pytest.raises(SessionStoreClosedError):
        await store.read_session("cli:test")


async def test_cancel_of_the_running_turn_leaves_the_queued_turn_alone(tmp_path):
    blocking = _GatedProvider("never released")
    finished = RecordingProvider()
    first = RuntimeSelection("fake", "model-one", None, None, blocking, 128000)
    second = RuntimeSelection("fake", "model-two", "high", None, finished, 128000)
    selections = FakeSelections(first)
    builds: list[str] = []
    store, _, session, _ = _open_real_session(tmp_path, selections, builds=builds)
    try:
        first_task = asyncio.create_task(session.submit(TurnRequest("cli:test", "first")))
        await asyncio.wait_for(blocking.started.wait(), 2)

        selections.selection = second
        second_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "second"))
        )
        await asyncio.sleep(0)
        cancel_task = asyncio.create_task(session.cancel("cli:test"))

        one = await asyncio.wait_for(first_task, 2)
        await asyncio.wait_for(cancel_task, 2)
        two = await asyncio.wait_for(second_task, 2)

        assert one.status is TurnStatus.INTERRUPTED
        assert two.status is TurnStatus.COMPLETED
        assert builds == ["model-one", "model-one", "model-two"]
        assert [call["model"] for call in finished.calls] == ["model-two"]
        assert finished.calls[0]["reasoning_effort"] == "high"

        events = await store.read_session("cli:test")
        terminal = {
            event.turn_id: event.type
            for event in events
            if event.type in (EventType.TURN_COMPLETED, EventType.TURN_INTERRUPTED)
        }
        assert sorted(terminal.values()) == [
            EventType.TURN_COMPLETED,
            EventType.TURN_INTERRUPTED,
        ]
        assert terminal[two.turn_id] is EventType.TURN_COMPLETED
        assert terminal[one.turn_id] is EventType.TURN_INTERRUPTED
        assert not store._closed
    finally:
        await session.aclose()


async def test_aclose_aborts_a_queued_submit_before_it_starts_its_turn(tmp_path):
    stubborn = _GatedProvider("late reply", stubborn=True)
    queued = RecordingProvider()
    first = RuntimeSelection("fake", "model-one", None, None, stubborn, 128000)
    second = RuntimeSelection("fake", "model-two", "high", None, queued, 128000)
    selections = FakeSelections(first)
    builds: list[str] = []
    store, output, session, _ = _open_real_session(tmp_path, selections, builds=builds)
    try:
        first_task = asyncio.create_task(session.submit(TurnRequest("cli:test", "first")))
        await asyncio.wait_for(stubborn.started.wait(), 2)

        selections.selection = second
        second_task = asyncio.create_task(
            session.submit(TurnRequest("cli:test", "second"))
        )
        await asyncio.sleep(0)
        close_task = asyncio.create_task(session.aclose())

        # A submit that was still queued leaves without building a turn and
        # without touching the store, even though the running turn is held open.
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(second_task, 2)
        assert not close_task.done()
        assert builds == ["model-one", "model-one"]
        assert queued.calls == []

        stubborn.gate.set()
        await asyncio.wait_for(close_task, 2)
        one = await asyncio.wait_for(first_task, 2)
        assert one.status is TurnStatus.INTERRUPTED
        assert store._closed
    finally:
        stubborn.gate.set()
        await session.aclose()

    with pytest.raises(SessionStoreClosedError):
        await store.read_session("cli:test")


class _BrokenDisplayOutput(RecordingOutput):
    """A terminal that fails after the store already persisted the event."""

    def event(self, event) -> None:
        raise RuntimeError("terminal encoding failed: token=secret-value")


async def test_a_display_failure_does_not_change_persisted_turn_semantics(tmp_path):
    store = SQLiteSessionStore(":memory:")
    output = _BrokenDisplayOutput()
    display = _DisplayStore(store, output)
    config = _config(tmp_path)
    coordinator = FakeCoordinator()
    _, (skill_capability, forge_capability) = _shared_capabilities(tmp_path, coordinator)
    provider = RecordingProvider()
    selection = RuntimeSelection("fake", "model-one", None, None, provider, 128000)

    def build(current):
        return _compose_for_selection(
            current,
            config=config,
            store=display,
            coordinator=coordinator,
            client=None,
            instructions="test",
            skill_capability=skill_capability,
            forge_capability=forge_capability,
        )

    session = CoreAgentSession(
        build(selection),
        selection,
        build=build,
        store=display,
        sessions=FakeSelections(selection),
        output=output,
    )
    try:
        result = await session.submit(TurnRequest("cli:test", "hello"))
        assert result.status is TurnStatus.COMPLETED
        events = await store.read_session("cli:test")
        assert [event.type for event in events] == [
            EventType.INPUT_ACCEPTED,
            EventType.TURN_STARTED,
            EventType.REQUEST_PREPARED,
            EventType.MODEL_ATTEMPT_STARTED,
            EventType.MODEL_ATTEMPT_COMPLETED,
            EventType.TURN_COMPLETED,
        ]
        # The display failed once per event, but it is reported once, without
        # the failed renderer's message (which may carry untrusted text).
        assert len(output.errors) == 1
        assert "persisted event" in output.errors[0]
        assert "secret-value" not in output.errors[0]
        assert "terminal encoding failed" not in output.errors[0]
        assert not store._closed
    finally:
        await session.aclose()


async def test_run_core_agent_wires_one_argument_model_command(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    class FakeSession:
        class Sessions:
            def get(self, session_id):
                return SimpleNamespace(name="fake", model="fake-model", effort=None)

        sessions = Sessions()

        async def submit(self, request):
            raise AssertionError("interactive run must not submit a message")

        async def cancel(self, session_id):
            seen["cancelled"] = session_id

        def model_command(self, session_id, text):
            seen["command"] = (session_id, text)
            return "model list"

    fake_session = FakeSession()

    @asynccontextmanager
    async def fake_open(config, *, provider_name=None, model=None, effort=None, output, additional_capabilities=(), **_kwargs):
        yield fake_session

    class FakeInteractive:
        def request_stop(self):
            pass

        def __init__(self, session, *, session_id, output, read_input, model_command, **kwargs):
            seen["session"] = session
            seen["session_id"] = session_id
            seen["model_command"] = model_command

        async def run(self):
            assert await seen["model_command"]("/model") == "model list"

    class FakeTerminal:
        def bind_stop(self, handler):
            self._stop_handler = handler

        menu_capable = False

        def __init__(self, display=None):
            seen["display"] = display

        def initialize(self):
            seen["initialized"] = True

        async def read(self):
            raise EOFError

        def restore(self):
            seen["restored"] = True

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )
    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.interactive_session.InteractiveAgent", FakeInteractive
    )
    monkeypatch.setattr("PhyAgentOS.cli.interaction.terminal_input.TerminalInput", FakeTerminal)

    output_console = Console(file=StringIO(), width=160, color_system=None)
    code = await run_core_agent(
        message=None,
        session_id="cli:test",
        workspace=None,
        config_path=None,
        markdown=True,
        logs=False,
        provider_name=None,
        model=None,
        effort=None,
        console=output_console,
    )
    assert code == 0
    assert seen["session"] is fake_session
    assert seen["command"] == ("cli:test", "/model")
    assert seen["initialized"] is True
    assert seen["restored"] is True


async def test_run_core_agent_pauses_reader_for_the_model_menu(tmp_path, monkeypatch):
    seen: dict[str, object] = {"order": []}

    class Sessions:
        def get(self, session_id):
            return SimpleNamespace(name="fake", model="fake-model", effort="high")

    class FakeSession:
        sessions = Sessions()

        async def submit(self, request):
            raise AssertionError("a menu must not open a turn")

        async def cancel(self, session_id):
            seen["cancelled"] = session_id

        def model_command(self, session_id, text):
            seen["text_command"] = (session_id, text)
            return "text path"

    @asynccontextmanager
    async def fake_open(config, *, provider_name=None, model=None, effort=None, output, additional_capabilities=(), **_kwargs):
        yield FakeSession()

    async def fake_picker(text, sessions, key):
        seen["order"].append("picker")
        seen["picker"] = (text, sessions, key)
        return "menu selected"

    class FakeInteractive:
        def request_stop(self):
            pass

        def __init__(self, session, *, session_id, output, read_input, model_command, **kwargs):
            seen["model_command"] = model_command

        async def run(self):
            assert await seen["model_command"]("/model") == "menu selected"
            assert await seen["model_command"]("/model gpt-4o") == "text path"
            assert await seen["model_command"]("/effort") == "menu selected"

    class FakeTerminal:
        def bind_stop(self, handler):
            self._stop_handler = handler

        menu_capable = True

        def __init__(self, display=None):
            pass

        async def pause(self):
            seen["order"].append("pause")

        def resume(self):
            seen["order"].append("resume")

        def initialize(self):
            pass

        async def read(self):
            raise EOFError

        def restore(self):
            pass

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )
    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.interactive_session.InteractiveAgent", FakeInteractive
    )
    monkeypatch.setattr("PhyAgentOS.cli.interaction.terminal_input.TerminalInput", FakeTerminal)
    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.model_picker._interactive_model_command", fake_picker
    )

    code = await run_core_agent(
        message=None,
        session_id="cli:test",
        workspace=None,
        config_path=None,
        markdown=True,
        logs=False,
        provider_name=None,
        model=None,
        effort=None,
        console=Console(file=StringIO(), width=160, color_system=None),
    )
    assert code == 0
    assert seen["order"] == ["pause", "picker", "resume", "pause", "picker", "resume"]
    assert seen["text_command"] == ("cli:test", "/model gpt-4o")


async def test_run_core_agent_single_message_echoes_request_once_and_closes_turn(
    tmp_path, monkeypatch
):
    class Sessions:
        def get(self, session_id):
            return SimpleNamespace(name="fake", model="fake-model", effort=None)

    class FakeSession:
        sessions = Sessions()

        async def submit(self, request):
            return TurnResult(
                request.session_id, "turn-1", TurnStatus.COMPLETED, reply="done"
            )

        async def cancel(self, session_id):
            raise AssertionError("no cancel expected")

    @asynccontextmanager
    async def fake_open(config, *, provider_name=None, model=None, effort=None, output, additional_capabilities=(), **_kwargs):
        yield FakeSession()

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )

    buffer = StringIO()
    code = await run_core_agent(
        message="hello",
        session_id="cli:test",
        workspace=None,
        config_path=None,
        markdown=True,
        logs=False,
        provider_name=None,
        model=None,
        effort=None,
        console=Console(file=buffer, width=160, color_system=None),
    )
    text = buffer.getvalue()
    assert code == 0
    assert "Preparing (Forge recovery)" in text
    assert "Preparing" in text and "done" in text
    # The prompt header names the stable session id; the request is echoed once
    # above the OS divider and closed by the End Turn divider.
    assert "session cli:test · fake/fake-model" in text
    assert "│ hello" in text and "You:" not in text and text.count("hello") == 1
    assert "PhyAgentOS · Turn 1" in text and "End Turn 1" in text
    assert "🍞 PhyAgentOS:" not in text
    assert "\x1b" not in text and "\r" not in text


class _MutableSessions:
    def __init__(self, name="fake", model="fake-model", effort=None):
        self.current = SimpleNamespace(name=name, model=model, effort=effort)

    def get(self, session_id):
        return self.current

    def update(self, **values):
        fields = {
            "name": self.current.name,
            "model": self.current.model,
            "effort": self.current.effort,
        }
        fields.update(values)
        self.current = SimpleNamespace(**fields)


def _label_from(output) -> str:
    return "".join(text for _, text in output.display.prompt_message())


async def test_run_core_agent_republishes_the_actual_label_after_text_commands(
    tmp_path, monkeypatch
):
    seen: dict[str, object] = {}

    class FakeSession:
        def __init__(self):
            self.sessions = _MutableSessions()

        async def submit(self, request):
            raise AssertionError("commands must not open a turn")

        async def cancel(self, session_id):
            pass

        def model_command(self, session_id, text):
            command, _, argument = text.partition(" ")
            if text == "/model fail":
                raise RuntimeError("selection rejected; state unchanged")
            if command == "/provider":
                self.sessions.update(name=argument or "fake")
            elif command == "/model":
                if argument in {"", "list"}:
                    self.sessions.update(model="picked-model")
                elif argument == "reset":
                    self.sessions.update(model="default-model")
                else:
                    self.sessions.update(model=argument)
            elif command == "/effort":
                self.sessions.update(effort=argument or None)
            return f"applied {text}"

    fake_session = FakeSession()

    @asynccontextmanager
    async def fake_open(config, *, provider_name=None, model=None, effort=None, output, additional_capabilities=(), **_kwargs):
        seen["output"] = output
        yield fake_session

    class FakeInteractive:
        def request_stop(self):
            pass

        def __init__(self, session, *, session_id, output, read_input, model_command, **kwargs):
            seen["model_command"] = model_command

        async def run(self):
            output = seen["output"]
            assert "fake/fake-model" in _label_from(output)
            await seen["model_command"]("/provider other")
            assert "other/fake-model" in _label_from(output)
            await seen["model_command"]("/model gpt-4o")
            assert "other/gpt-4o" in _label_from(output)
            await seen["model_command"]("/effort high")
            assert "effort high" in _label_from(output)
            await seen["model_command"]("/model reset")
            label = _label_from(output)
            assert "other/default-model" in label and "effort high" in label
            with pytest.raises(RuntimeError):
                await seen["model_command"]("/model fail")
            assert "other/default-model" in _label_from(output)

    class FakeTerminal:
        def bind_stop(self, handler):
            self._stop_handler = handler

        menu_capable = False

        def __init__(self, display=None):
            pass

        def initialize(self):
            pass

        async def read(self):
            raise EOFError

        def restore(self):
            pass

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )
    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.interactive_session.InteractiveAgent", FakeInteractive
    )
    monkeypatch.setattr("PhyAgentOS.cli.interaction.terminal_input.TerminalInput", FakeTerminal)

    code = await run_core_agent(
        message=None,
        session_id="cli:test",
        workspace=None,
        config_path=None,
        markdown=True,
        logs=False,
        provider_name=None,
        model=None,
        effort=None,
        console=Console(file=StringIO(), width=160, color_system=None),
    )
    assert code == 0


async def test_run_core_agent_menu_refresh_shows_the_actual_selection(tmp_path, monkeypatch):
    seen: dict[str, object] = {"order": []}
    fake_session = SimpleNamespace(sessions=_MutableSessions())
    fake_session.submit = lambda request: None
    fake_session.cancel = lambda session_id: None
    fake_session.model_command = lambda session_id, text: "text path"

    @asynccontextmanager
    async def fake_open(config, *, provider_name=None, model=None, effort=None, output, additional_capabilities=(), **_kwargs):
        seen["output"] = output
        yield fake_session

    async def fake_picker(text, sessions, key):
        if seen["menu_mode"] == "success":
            sessions.update(model="menu-model")
            return "Updated this session: model=menu-model."
        return "Model selection cancelled; current model unchanged."

    class FakeInteractive:
        def request_stop(self):
            pass

        def __init__(self, session, *, session_id, output, read_input, model_command, **kwargs):
            seen["model_command"] = model_command

        async def run(self):
            output = seen["output"]
            assert "fake/fake-model" in _label_from(output)
            seen["menu_mode"] = "success"
            await seen["model_command"]("/model")
            assert "fake/menu-model" in _label_from(output)
            seen["menu_mode"] = "cancel"
            await seen["model_command"]("/model")
            # Cancel changes nothing; the label shows the real selection.
            assert "fake/menu-model" in _label_from(output)

    class FakeTerminal:
        def bind_stop(self, handler):
            self._stop_handler = handler

        menu_capable = True

        def __init__(self, display=None):
            pass

        async def pause(self):
            seen["order"].append("pause")

        def resume(self):
            seen["order"].append("resume")

        def initialize(self):
            pass

        async def read(self):
            raise EOFError

        def restore(self):
            pass

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )
    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.interactive_session.InteractiveAgent", FakeInteractive
    )
    monkeypatch.setattr("PhyAgentOS.cli.interaction.terminal_input.TerminalInput", FakeTerminal)
    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.model_picker._interactive_model_command", fake_picker
    )

    code = await run_core_agent(
        message=None,
        session_id="cli:test",
        workspace=None,
        config_path=None,
        markdown=True,
        logs=False,
        provider_name=None,
        model=None,
        effort=None,
        console=Console(file=StringIO(), width=160, color_system=None),
    )
    assert code == 0
    assert seen["order"] == ["pause", "resume", "pause", "resume"]


async def test_run_core_agent_single_message_passes_request_text_and_finishes_after_result(
    tmp_path, monkeypatch
):
    calls: list[tuple] = []

    class FakeOutput:
        display = None
        ledger = None

        def startup(self, detail="Forge recovery"):
            pass

        def ready(self):
            pass

        def configure_stats(self, **kwargs):
            pass

        def set_session_label(self, label):
            pass

        def refresh_status(self, session_id=None):
            pass

        def info(self, text):
            calls.append(("info", text))

        def error(self, text):
            calls.append(("error", text))

        def turn_started(self, session_id=None, request_text=None):
            calls.append(("started", session_id, request_text))

        def result(self, result):
            calls.append(("result", result.turn_id))

        def turn_finished(self):
            calls.append(("finished",))

        def cancel_requested(self):
            calls.append(("cancel",))

    class FakeSession:
        sessions = _MutableSessions()

        async def submit(self, request):
            return TurnResult(
                request.session_id, "turn-1", TurnStatus.COMPLETED, reply="done"
            )

        async def cancel(self, session_id):
            raise AssertionError("no cancel expected")

    @asynccontextmanager
    async def fake_open(config, *, provider_name=None, model=None, effort=None, output, additional_capabilities=(), **_kwargs):
        calls.append(("output", output))
        yield FakeSession()

    monkeypatch.setattr(agent_commands, "open_core_session", fake_open)
    monkeypatch.setattr(
        agent_commands, "load_agent_config", lambda config, workspace: _config(tmp_path)
    )
    monkeypatch.setattr(
        "PhyAgentOS.cli.interaction.agent_output.AgentOutput",
        lambda console, markdown=True: FakeOutput(),
    )

    code = await run_core_agent(
        message="hello",
        session_id="cli:test",
        workspace=None,
        config_path=None,
        markdown=True,
        logs=False,
        provider_name=None,
        model=None,
        effort=None,
        console=Console(file=StringIO(), width=160, color_system=None),
    )

    assert code == 0
    assert ("started", "cli:test", "hello") in calls
    assert ("result", "turn-1") in calls
    assert ("finished",) in calls
    assert calls.index(("result", "turn-1")) < calls.index(("finished",))
    # The session header uses the stable id, not the request-derived title.
    assert any(
        kind == "info" and "session cli:test" in text and "hello" not in text
        for kind, text in (item[:2] for item in calls if item[0] == "info")
    )

"""Core reads workspace rules at turn start, not once at host startup.

These tests go through ``open_core_session`` with a fake provider and a real
SQLite store, so they cover the wiring rather than ``_instructions`` alone:
a same-session later turn, a queued turn, a new session, and compaction all
see the current ``AGENTS.md`` without duplicating an older system message.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from PhyAgentOS.agent.core import TurnRequest, TurnStatus
from PhyAgentOS.agent.core.prompt import is_runtime_context_message
from PhyAgentOS.cli.command_groups import agent_commands
from PhyAgentOS.cli.command_groups.agent_commands import open_core_session
from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.base import LLMResponse
from PhyAgentOS.providers.service import RuntimeSelection


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": [dict(message) for message in messages], "tools": tools, **kwargs}
        )
        return LLMResponse(content="done", finish_reason="stop")


class GatedProvider(RecordingProvider):
    """Hold the first model call so a second submit stays queued."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": [dict(message) for message in messages], "tools": tools, **kwargs}
        )
        self.started.set()
        await self.gate.wait()
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

    def set_activation_manager(self, manager) -> None:
        self.activation_manager = manager

    def unknown_remote_blockers(self) -> list[dict]:
        return []

    async def reconcile_nonterminal(self):
        return None


class _FakeForgeFactory:
    def __call__(self, config, provider, *, write_legacy_lessons=None, resources=None):
        class Client:
            async def close(self):
                return None

        client = Client()
        verifier = SimpleNamespace(stop=lambda: None)
        if resources is not None:
            resources.push_async_callback(client.close)
            resources.push_async_callback(asyncio.to_thread, verifier.stop)
        return client, None, FakeCoordinator(), lambda name: True


class _FakeProviderService:
    def __init__(self, config: Config, provider=None) -> None:
        self.config = config.model_copy(deep=True)
        self.provider = provider if provider is not None else RecordingProvider()

    def resolve(self, name=None, model=None, effort=None) -> RuntimeSelection:
        return RuntimeSelection(
            "fake", "model-one", None, None, self.provider, 128000
        )


def _config(tmp_path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    return config


def _write_agents(tmp_path, text: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(text, encoding="utf-8")


def _systems(call) -> list[str]:
    return [
        message["content"]
        for message in call["messages"]
        if message.get("role") == "system" and isinstance(message.get("content"), str)
    ]


def _users(call) -> list[str]:
    return [
        message["content"]
        for message in call["messages"]
        if message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and not is_runtime_context_message(message)
    ]


async def test_turn_start_reads_current_workspace_rules(tmp_path, monkeypatch):
    _write_agents(tmp_path, "AGENTS-V1")
    service = _FakeProviderService(_config(tmp_path))
    monkeypatch.setattr(agent_commands, "ProviderService", lambda config: service)
    monkeypatch.setattr(agent_commands, "build_forge_components", _FakeForgeFactory())

    async with open_core_session(_config(tmp_path), output=RecordingOutput()) as session:
        first = await session.submit(TurnRequest("cli:one", "first"))
        assert first.status is TurnStatus.COMPLETED
        first_systems = _systems(service.provider.calls[-1])
        assert len(first_systems) == 1
        assert "AGENTS-V1" in first_systems[0]

        _write_agents(tmp_path, "AGENTS-V2")
        second = await session.submit(TurnRequest("cli:one", "second"))
        assert second.status is TurnStatus.COMPLETED
        second_call = service.provider.calls[-1]
        second_systems = _systems(second_call)
        # A new turn replaces the system text instead of adding a second one.
        assert len(second_systems) == 1
        assert "AGENTS-V2" in second_systems[0]
        assert "AGENTS-V1" not in "".join(
            message.get("content") or ""
            for message in second_call["messages"]
            if isinstance(message.get("content"), str)
        )

        # A new session reads the same current workspace text and does not
        # copy the previous session's conversation.
        fresh = await session.submit(TurnRequest("cli:two", "fresh"))
        assert fresh.status is TurnStatus.COMPLETED
        fresh_call = service.provider.calls[-1]
        fresh_systems = _systems(fresh_call)
        assert len(fresh_systems) == 1
        assert "AGENTS-V2" in fresh_systems[0]
        assert "AGENTS-V1" not in fresh_systems[0]
        assert _users(fresh_call) == ["fresh"]


async def test_queued_turn_reads_rules_when_it_starts(tmp_path, monkeypatch):
    _write_agents(tmp_path, "AGENTS-V1")
    provider = GatedProvider()
    service = _FakeProviderService(_config(tmp_path), provider)
    monkeypatch.setattr(agent_commands, "ProviderService", lambda config: service)
    monkeypatch.setattr(agent_commands, "build_forge_components", _FakeForgeFactory())

    async with open_core_session(_config(tmp_path), output=RecordingOutput()) as session:
        running = asyncio.create_task(session.submit(TurnRequest("cli:q", "one")))
        await provider.started.wait()
        queued = asyncio.create_task(session.submit(TurnRequest("cli:q", "two")))
        for _ in range(1000):
            if session._pending >= 2:
                break
            await asyncio.sleep(0)
        assert session._pending >= 2

        # The first turn already assembled its prompt; only the queued turn
        # starts after this edit.
        _write_agents(tmp_path, "AGENTS-V2")
        provider.gate.set()
        results = await asyncio.gather(running, queued)

    assert [result.status for result in results] == [
        TurnStatus.COMPLETED,
        TurnStatus.COMPLETED,
    ]
    assert "AGENTS-V1" in _systems(provider.calls[0])[0]
    assert "AGENTS-V2" in _systems(provider.calls[1])[0]
    assert "AGENTS-V1" not in _systems(provider.calls[1])[0]


async def test_compaction_uses_current_workspace_rules(tmp_path, monkeypatch):
    _write_agents(tmp_path, "AGENTS-V1")
    service = _FakeProviderService(_config(tmp_path))
    monkeypatch.setattr(agent_commands, "ProviderService", lambda config: service)
    monkeypatch.setattr(agent_commands, "build_forge_components", _FakeForgeFactory())

    captured: dict = {}

    async def fake_compact_session(store, provider, session_id, **kwargs):
        kwargs["request_token_estimator"]([])
        return None

    def fake_estimate(provider, model, messages, tools):
        captured["messages"] = list(messages)
        return 0, "test"

    monkeypatch.setattr(agent_commands, "compact_session", fake_compact_session)
    monkeypatch.setattr(agent_commands, "estimate_prompt_tokens_chain", fake_estimate)

    async with open_core_session(_config(tmp_path), output=RecordingOutput()) as session:
        assert (await session.submit(TurnRequest("cli:c", "one"))).status is TurnStatus.COMPLETED
        _write_agents(tmp_path, "AGENTS-V2")
        await session.compact("cli:c")

    systems = [
        message["content"]
        for message in captured["messages"]
        if message.get("role") == "system"
    ]
    assert len(systems) == 1
    assert "AGENTS-V2" in systems[0]
    assert "AGENTS-V1" not in systems[0]

"""Explicit turn identity and stale-activation regressions for the Core path."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from PhyAgentOS.agent.capabilities.skill_access_capability import SkillAccessCapability
from PhyAgentOS.agent.composition import compose_agent
from PhyAgentOS.agent.core import (
    Budget,
    CancellationToken,
    Capability,
    ExecutionContext,
    SQLiteSessionStore,
    ToolCall,
    ToolStatus,
    TurnRequest,
    TurnStatus,
)
from PhyAgentOS.agent.services.skill_documents import SkillDocumentLoader
from PhyAgentOS.agent.services.turn_activation import TurnSkillActivationManager
from PhyAgentOS.forge.task import AgentTaskRecord, PlanRevision
from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest


def _manager(tmp_path):
    for name in ("primary", "support", "another"):
        directory = tmp_path / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test workflow\n---\n# {name}\n"
        )
    documents = SkillDocumentLoader(
        tmp_path, builtin_skills_dir=tmp_path / "builtin",
        installed_skills_dir=tmp_path / "installed",
    )
    return TurnSkillActivationManager(documents)


def _context(session="cli:test", turn="turn-1"):
    return ExecutionContext(session, turn, "step", CancellationToken(), Budget(10))


async def test_activation_is_idempotent_only_in_same_turn(tmp_path):
    manager = _manager(tmp_path)
    first, content = await manager.activate(
        session_key="cli:test", turn_id="one", name="primary"
    )
    same, _ = await manager.activate(session_key="cli:test", turn_id="one", name="primary")
    assert first is same
    assert "# primary" in content
    second, _ = await manager.activate(session_key="cli:test", turn_id="two", name="primary")
    assert second.activation_id != first.activation_id
    with pytest.raises(ValueError, match="call activate_skill"):
        manager.require_activation(session_key="cli:test", activation_id=first.activation_id)
    assert manager.require_activation(
        session_key="cli:test", activation_id=second.activation_id, role="primary"
    ) == second


async def test_activation_does_not_cross_session_or_restart(tmp_path):
    manager = _manager(tmp_path)
    first, _ = await manager.activate(session_key="A", turn_id="one", name="primary")
    await manager.activate(session_key="B", turn_id="one", name="primary")
    with pytest.raises(ValueError, match="current session turn"):
        manager.require_activation(session_key="B", activation_id=first.activation_id)
    restarted = TurnSkillActivationManager(manager.documents)
    restarted.begin_turn("A", "one")
    with pytest.raises(ValueError, match="current session turn"):
        restarted.require_activation(session_key="A", activation_id=first.activation_id)


async def test_one_primary_and_supporting_role_are_enforced(tmp_path):
    manager = _manager(tmp_path)
    await manager.activate(session_key="A", turn_id="one", name="primary")
    with pytest.raises(ValueError, match="primary Skill"):
        await manager.activate(session_key="A", turn_id="one", name="another")
    support, _ = await manager.activate(
        session_key="A", turn_id="one", name="support", role="supporting"
    )
    with pytest.raises(ValueError, match="role 'primary'"):
        manager.require_activation(session_key="A", activation_id=support.activation_id, role="primary")
    with pytest.raises(ValueError, match="already activated"):
        await manager.activate(session_key="A", turn_id="one", name="support", role="primary")
    with pytest.raises(ValueError, match="unavailable"):
        await manager.activate(session_key="A", turn_id="one", name="../outside")


async def test_prompt_resets_authority_before_any_tool_in_new_turn(tmp_path):
    manager = _manager(tmp_path)
    capability = SkillAccessCapability(manager)
    assert isinstance(capability, Capability)
    one = _context()
    capability.prompt_context(one)
    registration, = capability.tool_registrations()
    outcome = await registration.handler(ToolCall("call", "activate_skill", {"name": "primary"}), one)
    assert outcome.status is ToolStatus.SUCCEEDED
    activation_id = json.loads(outcome.text)["activation"]["activation_id"]
    assert activation_id in capability.prompt_context(one)
    prompt = capability.prompt_context(_context(turn="two"))
    assert activation_id not in prompt
    assert "Current turn activations: []" in prompt
    with pytest.raises(ValueError, match="current session turn"):
        manager.require_activation(session_key=one.session_id, activation_id=activation_id)
    await capability.aclose()
    assert manager.current(one.session_id, "two") == ()


async def test_document_change_cannot_reuse_old_document_identity(tmp_path):
    manager = _manager(tmp_path)
    await manager.activate(session_key="A", turn_id="one", name="primary")
    (tmp_path / "skills" / "primary" / "SKILL.md").write_text("changed workflow")
    with pytest.raises(ValueError, match="document changed"):
        await manager.activate(session_key="A", turn_id="one", name="primary")


async def test_delayed_binding_preview_cannot_publish_into_a_new_turn(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def preview(name):
        entered.set()
        await release.wait()
        return SimpleNamespace(candidate_id="candidate")

    manager.binding_resolver = SimpleNamespace(
        catalog=SimpleNamespace(get=lambda name: (_ for _ in ()).throw(LookupError())),
        preview=preview,
    )
    path = tmp_path / "skills" / "primary" / "skill.yaml"
    path.write_text("test")
    monkeypatch.setattr("PhyAgentOS.agent.services.turn_activation.load_manifest", lambda path: SimpleNamespace(version="1"))
    activation = asyncio.create_task(manager.activate(session_key="A", turn_id="one", name="primary"))
    await entered.wait()
    manager.begin_turn("A", "two")
    release.set()
    with pytest.raises(ValueError, match="turn changed"):
        await activation
    assert manager.current("A", "two") == ()


async def test_document_change_during_preview_is_not_published(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def preview(name):
        entered.set()
        await release.wait()
        return SimpleNamespace(candidate_id="candidate")

    manager.binding_resolver = SimpleNamespace(
        catalog=SimpleNamespace(get=lambda name: (_ for _ in ()).throw(LookupError())),
        preview=preview,
    )
    (tmp_path / "skills" / "primary" / "skill.yaml").write_text("test")
    monkeypatch.setattr(
        "PhyAgentOS.agent.services.turn_activation.load_manifest",
        lambda path: SimpleNamespace(version="1"),
    )
    activation = asyncio.create_task(
        manager.activate(session_key="A", turn_id="one", name="primary")
    )
    await entered.wait()
    (tmp_path / "skills" / "primary" / "SKILL.md").write_text(
        "changed while the preview was pending"
    )
    release.set()
    with pytest.raises(ValueError, match="changed during activation"):
        await activation
    assert manager.current("A", "one") == ()


async def test_real_core_can_recover_from_historical_activation_id(tmp_path):
    manager = _manager(tmp_path)
    store = SQLiteSessionStore(":memory:")
    seen = []
    created = []

    def create_task(**kwargs):
        manager.require_activation(
            session_key=kwargs["origin_session_key"],
            activation_id=kwargs["activation_id"], role="primary",
        )
        created.append(kwargs)
        return AgentTaskRecord(
            task_id="task_good",
            task_description=kwargs["task_description"],
            verification=kwargs["verification"],
            revisions=[
                PlanRevision(revision_id="revision_good", number=1, reason="initial plan")
            ],
            active_revision_id="revision_good",
            origin_session_key=kwargs["origin_session_key"],
        )

    coordinator = SimpleNamespace(
        store=SimpleNamespace(latest_for_session=lambda session: None),
        create_task=create_task,
        unknown_remote_blockers=lambda: [],
    )

    class Provider:
        calls = 0

        async def chat_with_retry(self, messages, **kwargs):
            self.calls += 1
            seen.append(messages)
            if self.calls == 1:
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id=f"activate-{self.calls}", name="activate_skill", arguments={"name": "primary"}
                )], finish_reason="tool_calls")
            if self.calls == 2:
                return LLMResponse(content="first done")
            if self.calls == 3:
                old = next(m for m in messages if m.get("name") == "activate_skill")
                old_id = json.loads(old["content"])["activation"]["activation_id"]
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id="stale", name="forge_task_create", arguments={
                        "activation_id": old_id, "task_description": "second task",
                        "verification": {"mode": "off"},
                    }
                )], finish_reason="tool_calls")
            if self.calls == 4:
                # The stale activation id is not authorization. With stage
                # gating, forge_task_create is not even offered before a fresh
                # primary activation, so the rejection is the policy one; the
                # turn must still activate first and then create.
                assert "forge_task_create" in messages[-1]["content"]
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id="new", name="activate_skill", arguments={"name": "primary"}
                )], finish_reason="tool_calls")
            if self.calls == 5:
                fresh = json.loads(messages[-1]["content"])["activation"]["activation_id"]
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id="valid", name="forge_task_create", arguments={
                        "activation_id": fresh, "task_description": "second task",
                        "verification": {"mode": "off"},
                    }
                )], finish_reason="tool_calls")
            return LLMResponse(content="second done")

    provider = Provider()
    assembly = compose_agent(
        provider=provider, store=store, coordinator=coordinator,
        system_instructions="test", additional_capabilities=(SkillAccessCapability(manager),),
    )
    try:
        assert (await assembly.host.submit(TurnRequest("cli:test", "first"))).status is TurnStatus.COMPLETED
        result = await assembly.host.submit(TurnRequest("cli:test", "second"))
        assert result.status is TurnStatus.COMPLETED
        assert len(created) == 1
        assert "Current turn activations: []" in seen[2][0]["content"]
    finally:
        await assembly.host.aclose()

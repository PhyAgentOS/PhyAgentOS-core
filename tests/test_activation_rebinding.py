"""Regression tests: re-activating a Skill after its runtime restarts.

``ForgeSkillBindingResolver.preview()`` derives ``candidate_id`` from a seed
that includes the runtime instance id, so a restarted runtime yields a new
candidate. Within one turn, ``SkillActivationManager`` used to return the
first activation unchanged, so the fresh binding was discarded and task
creation kept targeting the dead runtime — while the platform's own
remediation ("re-activate the Skill") was a no-op.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))

from PhyAgentOS.agent.experience.activation import SkillActivationManager  # noqa: E402
from PhyAgentOS.agent.experience.store import ExperienceStore  # noqa: E402

SKILL_TEXT = """---
name: demo-workflow
description: Use for repeatable demo workflows.
always: false
---

# Demo Workflow
"""

SKILL_YAML = """\
manifest_version: 2
name: demo-workflow
version: "1.0.0"
description: Demo workflow skill.
skill_document: SKILL.md
gateway_url: http://127.0.0.1:9000
required_tools:
  - demo_tool
profiles:
  default:
    dataflow: dataflow.yml
"""


class _StubCatalog:
    def get(self, name):
        return None


class _StubBindingResolver:
    """Minimal stand-in for ForgeSkillBindingResolver.

    Tests flip ``candidate_id`` to simulate a runtime restart, mirroring
    how the real resolver's candidate changes with the runtime instance.
    """

    def __init__(self) -> None:
        self.catalog = _StubCatalog()
        self.candidate_id = "candidate_runtime_1"

    async def preview(self, skill_name: str):
        return SimpleNamespace(candidate_id=self.candidate_id)


def _manager(tmp_path: Path, resolver: _StubBindingResolver) -> SkillActivationManager:
    skill_dir = tmp_path / "skills" / "demo-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(SKILL_YAML, encoding="utf-8")
    manager = SkillActivationManager(
        workspace=tmp_path,
        store=ExperienceStore(tmp_path),
        binding_resolver=resolver,
    )
    manager.begin_turn("cli:test", "run the demo")
    return manager


def _activate(manager: SkillActivationManager):
    return asyncio.run(
        manager.activate(
            session_key="cli:test", name="demo-workflow", role="primary"
        )
    )


def test_reactivation_after_runtime_restart_rebinds(tmp_path: Path) -> None:
    resolver = _StubBindingResolver()
    manager = _manager(tmp_path, resolver)

    first, _, _ = _activate(manager)
    assert first.binding_candidate_id == "candidate_runtime_1"

    # The Skill runtime restarts: preview() now computes a new candidate.
    resolver.candidate_id = "candidate_runtime_2"
    second, _, _ = _activate(manager)

    assert second.binding_candidate_id == "candidate_runtime_2"
    assert second.activation_id != first.activation_id
    activations = manager.snapshot("cli:test")["skill_activations"]
    assert len(activations) == 1
    assert activations[0]["binding_candidate_id"] == "candidate_runtime_2"


def test_reactivation_with_same_binding_reuses_activation(tmp_path: Path) -> None:
    resolver = _StubBindingResolver()
    manager = _manager(tmp_path, resolver)

    first, _, _ = _activate(manager)
    second, _, _ = _activate(manager)

    assert second.activation_id == first.activation_id
    assert len(manager.snapshot("cli:test")["skill_activations"]) == 1


def test_reactivation_with_different_role_still_rejected(tmp_path: Path) -> None:
    resolver = _StubBindingResolver()
    manager = _manager(tmp_path, resolver)

    _activate(manager)
    resolver.candidate_id = "candidate_runtime_2"
    try:
        asyncio.run(
            manager.activate(
                session_key="cli:test", name="demo-workflow", role="supporting"
            )
        )
    except ValueError as exc:
        assert "already activated as" in str(exc)
    else:
        raise AssertionError("role conflict must still be rejected after a rebind")

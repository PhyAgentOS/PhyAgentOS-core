from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from PhyAgentOS.agent.context import ContextBuilder  # noqa: E402
from PhyAgentOS.agent.skills import SkillsLoader  # noqa: E402


def test_active_runtime_skill_is_injected_in_full(tmp_path: Path) -> None:
    installed = tmp_path / "installed" / "move-arm-by-ee"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text(
        """---
name: move-arm-by-ee
description: Move an arm by its end effector.
metadata: {"PhyAgentOS":{"requires":{"runtime":["move-arm-by-ee"]}}}
---

# Runtime-only instructions

Call the Query before the Action.
""",
        encoding="utf-8",
    )
    builder = ContextBuilder(tmp_path)
    builder.skills = SkillsLoader(
        tmp_path,
        builtin_skills_dir=tmp_path / "builtin",
        installed_skills_dir=tmp_path / "installed",
        runtime_availability_provider=lambda name: name == "move-arm-by-ee",
    )

    prompt = builder.build_system_prompt()

    assert "# Active Skills" in prompt
    assert "### Skill: move-arm-by-ee" in prompt
    assert "Call the Query before the Action." in prompt


def test_inactive_runtime_skill_is_summary_only(tmp_path: Path) -> None:
    installed = tmp_path / "installed" / "move-arm-by-ee"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text(
        """---
name: move-arm-by-ee
description: Move an arm by its end effector.
metadata: {"PhyAgentOS":{"requires":{"runtime":["move-arm-by-ee"]}}}
---

SECRET_RUNTIME_INSTRUCTIONS
""",
        encoding="utf-8",
    )
    builder = ContextBuilder(tmp_path)
    builder.skills = SkillsLoader(
        tmp_path,
        builtin_skills_dir=tmp_path / "builtin",
        installed_skills_dir=tmp_path / "installed",
    )

    prompt = builder.build_system_prompt()

    assert 'available="false"' in prompt
    assert "runtime: move-arm-by-ee" in prompt
    assert "SECRET_RUNTIME_INSTRUCTIONS" not in prompt

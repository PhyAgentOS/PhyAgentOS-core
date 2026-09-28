from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).parents[1]))

from PhyAgentOS.agent.skills import SkillsLoader  # noqa: E402

BUILTIN_SKILLS = Path(__file__).parents[1] / "PhyAgentOS" / "skills"


def _installed_forge_skill(root: Path, name: str = "example-forge") -> None:
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Synthetic Forge workflow.\n"
        f'metadata: {{"PhyAgentOS":{{"always":false,"requires":{{"runtime":["{name}"]}}}}}}\n'
        "---\n\n# Synthetic Forge workflow\n",
        encoding="utf-8",
    )


def test_installed_forge_skill_requires_active_runtime(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    _installed_forge_skill(installed)
    loader = SkillsLoader(
        tmp_path,
        builtin_skills_dir=BUILTIN_SKILLS,
        installed_skills_dir=installed,
    )

    metadata = loader.get_skill_metadata("example-forge")
    assert metadata is not None
    assert metadata["name"] == "example-forge"
    assert metadata["description"]

    available_names = {skill["name"] for skill in loader.list_skills()}
    all_names = {skill["name"] for skill in loader.list_skills(filter_unavailable=False)}
    assert "example-forge" not in available_names
    assert "example-forge" in all_names

    summary = ElementTree.fromstring(loader.build_skills_summary())
    skill = next(node for node in summary.findall("skill") if node.findtext("name") == "example-forge")
    assert skill.attrib["available"] == "false"
    assert skill.findtext("requires") == "runtime: example-forge"


def test_installed_forge_skill_is_active_when_runtime_is_ready(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    _installed_forge_skill(installed)
    loader = SkillsLoader(
        tmp_path,
        builtin_skills_dir=BUILTIN_SKILLS,
        installed_skills_dir=installed,
        runtime_availability_provider=lambda name: name == "example-forge",
    )

    assert "example-forge" in {skill["name"] for skill in loader.list_skills()}
    assert loader.get_active_skills() == ["example-forge"]


def test_source_distribution_has_no_concrete_forge_skill_bundle() -> None:
    assert not list(BUILTIN_SKILLS.glob("*/skill.yaml"))
    assert not [path for path in BUILTIN_SKILLS.glob("*/profiles/**/*") if path.is_file()]

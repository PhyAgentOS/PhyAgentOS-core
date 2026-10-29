"""Independent skill document imports and legacy module compatibility."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from PhyAgentOS.agent.services import skill_documents
from PhyAgentOS.agent.services.skill_documents import SkillDocumentLoader

REPO_ROOT = Path(__file__).parents[1]


def test_skill_documents_import_does_not_load_agent_legacy() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from PhyAgentOS.agent.services.skill_documents import SkillDocumentLoader\n"
            "assert SkillDocumentLoader.__module__ == 'PhyAgentOS.agent.services.skill_documents'\n"
            "assert not any(name == 'PhyAgentOS.agent_legacy' or "
            "name.startswith('PhyAgentOS.agent_legacy.') for name in sys.modules)\n",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_legacy_skills_exports_same_module_class_and_builtin_directory() -> None:
    from PhyAgentOS.agent_legacy import skills
    from PhyAgentOS.agent_legacy.skills import BUILTIN_SKILLS_DIR, SkillsLoader

    assert skills is skill_documents
    assert SkillsLoader is SkillDocumentLoader
    assert skill_documents.SkillsLoader is SkillDocumentLoader
    assert BUILTIN_SKILLS_DIR == REPO_ROOT / "PhyAgentOS" / "skills"


def test_skill_document_loader_uses_packaged_builtin_directory(tmp_path: Path) -> None:
    loader = SkillDocumentLoader(tmp_path, installed_skills_dir=tmp_path / "installed")

    assert loader.builtin_skills == REPO_ROOT / "PhyAgentOS" / "skills"
    assert loader.load_skill("memory") == (
        loader.builtin_skills / "memory" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert loader.resolve_skill("memory", require_available=False)["source"] == "builtin"


def test_legacy_module_patches_control_loader_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PhyAgentOS.agent_legacy.skills import SkillsLoader

    config_path = tmp_path / "config" / "config.json"
    builtin = tmp_path / "builtin"
    monkeypatch.setattr(
        "PhyAgentOS.agent_legacy.skills.get_config_path", lambda: config_path
    )
    monkeypatch.setattr("PhyAgentOS.agent_legacy.skills.BUILTIN_SKILLS_DIR", builtin)

    for loader_type in (SkillsLoader, SkillDocumentLoader):
        loader = loader_type(tmp_path)
        assert loader.installed_skills == config_path.parent / "skills"
        assert loader.builtin_skills == builtin


def test_new_loader_preserves_workspace_installed_builtin_precedence(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    installed = tmp_path / "installed"
    for root, content in (
        (builtin, "builtin"),
        (installed, "installed"),
        (tmp_path / "skills", "workspace"),
    ):
        skill = root / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(content, encoding="utf-8")
    loader = SkillDocumentLoader(
        tmp_path, builtin_skills_dir=builtin, installed_skills_dir=installed
    )

    assert loader.load_skill("example") == "workspace"
    assert loader.resolve_skill("example")["source"] == "workspace"
    (tmp_path / "skills" / "example" / "SKILL.md").unlink()
    assert loader.load_skill("example") == "installed"
    assert loader.resolve_skill("example")["source"] == "installed"
    (installed / "example" / "SKILL.md").unlink()
    assert loader.load_skill("example") == "builtin"
    assert loader.resolve_skill("example")["source"] == "builtin"

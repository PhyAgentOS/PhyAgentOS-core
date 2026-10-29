"""Boundary checks for the split between the new core and legacy agent code."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import PhyAgentOS

PACKAGE_DIR = Path(PhyAgentOS.__file__).parent
REPO_ROOT = PACKAGE_DIR.parent


def _imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def _module_parts(path: Path) -> list[str]:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _resolved_imports(path: Path) -> set[str]:
    """Return absolute module names, resolving ``from .`` and ``from ..``."""
    package = _module_parts(path)
    base = package if path.name == "__init__.py" else package[:-1]
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                prefix = base[: len(base) - node.level + 1]
                resolved = ".".join([*prefix, module] if module else prefix)
            else:
                resolved = module
            names.add(resolved)
            names.update(
                f"{resolved}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
    return names


def test_legacy_modules_import_from_agent_legacy_and_old_paths_have_no_shims() -> None:
    legacy_loop = importlib.import_module("PhyAgentOS.agent_legacy.loop")
    legacy_tools = importlib.import_module("PhyAgentOS.agent_legacy.tools.forge_task")
    legacy_experience = importlib.import_module(
        "PhyAgentOS.agent_legacy.experience.activation"
    )

    assert legacy_loop.AgentLoop is not None
    assert legacy_tools.ForgeTaskCreateTool is not None
    assert legacy_experience.SkillActivationManager is not None

    import PhyAgentOS.agent as agent_package

    assert not hasattr(agent_package, "AgentLoop")
    assert importlib.util.find_spec("PhyAgentOS.agent.loop") is None
    assert importlib.util.find_spec("PhyAgentOS.agent.tools") is None


def test_all_non_generated_source_imports_use_agent_legacy_not_old_agent_paths() -> None:
    allowed = (
        "PhyAgentOS.agent.core",
        "PhyAgentOS.agent.capabilities",
    )
    offenders: dict[str, list[str]] = {}
    for path in PACKAGE_DIR.rglob("*.py"):
        imports = _imported_modules(path.read_text(encoding="utf-8"))
        bad = sorted(
            name
            for name in imports
            if name == "PhyAgentOS.agent" or (
                name.startswith("PhyAgentOS.agent.")
                and not name.startswith(allowed)
            )
        )
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = bad
    assert offenders == {}


def test_forge_reverse_import_points_at_agent_legacy() -> None:
    service = (PACKAGE_DIR / "forge" / "interaction" / "service.py").read_text(
        encoding="utf-8"
    )
    imports = _imported_modules(service)

    assert "PhyAgentOS.agent_legacy.interaction.supervisor" in imports
    assert "PhyAgentOS.agent.interaction.supervisor" not in imports


def test_forge_package_does_not_import_new_agent_runtime_modules() -> None:
    offenders: dict[str, list[str]] = {}
    for path in (PACKAGE_DIR / "forge").rglob("*.py"):
        imports = _imported_modules(path.read_text(encoding="utf-8"))
        bad = sorted(
            name
            for name in imports
            if name.startswith("PhyAgentOS.agent.")
            and not name.startswith(("PhyAgentOS.agent.core", "PhyAgentOS.agent_legacy"))
        )
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = bad
    # The historical reverse import above is the only allowed Agent import.
    assert offenders == {}


def test_agent_package_imports_never_resolve_to_agent_legacy() -> None:
    """Both absolute and relative imports are resolved before checking."""
    offenders: dict[str, list[str]] = {}
    for path in (PACKAGE_DIR / "agent").rglob("*.py"):
        bad = sorted(
            name
            for name in _resolved_imports(path)
            if name == "PhyAgentOS.agent_legacy"
            or name.startswith("PhyAgentOS.agent_legacy.")
        )
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = bad
    assert offenders == {}


def test_importing_new_composition_does_not_load_agent_legacy() -> None:
    code = "\n".join(
        [
            "import sys",
            "import PhyAgentOS.agent.composition",
            "import PhyAgentOS.agent.capabilities.forge_capability",
            "import PhyAgentOS.agent.capabilities.forge_task_tools",
            "import PhyAgentOS.agent.capabilities.forge_tool_api",
            "loaded = [",
            "    name for name in sys.modules",
            "    if name == 'PhyAgentOS.agent_legacy'",
            "    or name.startswith('PhyAgentOS.agent_legacy.')",
            "]",
            "assert loaded == [], loaded",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

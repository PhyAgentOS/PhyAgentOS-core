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


# The new agent package is the only place allowed to use ``PhyAgentOS.agent``.
# Each module is listed explicitly so a new top-level agent module cannot be
# mistaken for the old loop/tools/experience paths that moved to agent_legacy.
_NEW_AGENT_MODULES = frozenset(
    {
        "PhyAgentOS.agent",
        "PhyAgentOS.agent.capabilities",
        "PhyAgentOS.agent.composition",
        "PhyAgentOS.agent.core",
        "PhyAgentOS.agent.bootstrap",
        "PhyAgentOS.agent.services",
    }
)


def _is_new_agent_module(name: str) -> bool:
    return any(
        name == module or name.startswith(f"{module}.")
        for module in _NEW_AGENT_MODULES
    )


def test_all_non_generated_source_imports_use_agent_legacy_not_old_agent_paths() -> None:
    offenders: dict[str, list[str]] = {}
    for path in PACKAGE_DIR.rglob("*.py"):
        imports = _imported_modules(path.read_text(encoding="utf-8"))
        bad = sorted(
            name
            for name in imports
            if (name == "PhyAgentOS.agent" or name.startswith("PhyAgentOS.agent."))
            and not _is_new_agent_module(name)
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


def test_core_cli_modules_import_without_agent_legacy() -> None:
    """The Core CLI entry must not pull the legacy runtime into the process.

    This covers the whole entry module now that Core is the default: building
    the Typer app, including its one registration point, does not import
    ``PhyAgentOS.agent_legacy`` or ``PhyAgentOS.cli.legacy``. Those are loaded
    only when ``--runtime legacy`` is actually selected.
    """
    code = "\n".join(
        [
            "import sys",
            "import PhyAgentOS.cli.commands",
            "import PhyAgentOS.cli.command_groups.agent_commands",
            "import PhyAgentOS.cli.configuration",
            "import PhyAgentOS.cli.interaction.agent_output",
            "import PhyAgentOS.cli.interaction.interactive_session",
            "import PhyAgentOS.cli.interaction.terminal_input",
            "loaded = [",
            "    name for name in sys.modules",
            "    if name == 'PhyAgentOS.agent_legacy'",
            "    or name.startswith('PhyAgentOS.agent_legacy.')",
            "    or name == 'PhyAgentOS.cli.legacy'",
            "    or name.startswith('PhyAgentOS.cli.legacy.')",
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

"""Workspace template baseline: discovery, hashing and persistence.

The baseline records which package version and content hash each workspace
template was copied from. It is written only by ``sync_workspace_templates``
and ``paos workspace upgrade --apply``; check-only code only reads it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Iterable

from PhyAgentOS import __version__

BASELINE_SCHEMA = 1
BASELINE_RELATIVE_PATH = Path(".paos") / "template-baseline.json"

# Data files that belong to the user. Existing copies are never replaced.
PROTECTED_TEMPLATE_PATHS = frozenset({"memory/MEMORY.md", "memory/HISTORY.md", "USER.md"})


@dataclass(frozen=True)
class TemplateTarget:
    """One bundled template and its destination relative to the workspace."""

    relative_path: str
    content: bytes
    sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def iter_template_targets() -> list[TemplateTarget]:
    """Return the package templates that sync/upgrade manages.

    The package is the source of truth. This function never touches the
    workspace and never depends on the working tree or git.
    """
    try:
        templates = pkg_files("PhyAgentOS") / "templates"
    except Exception:
        return []
    if not templates.is_dir():
        return []

    targets: list[TemplateTarget] = []

    def add(item, relative: str) -> None:
        if not item.is_file():
            return
        content = item.read_bytes()
        targets.append(
            TemplateTarget(
                relative_path=relative,
                content=content,
                sha256=sha256_bytes(content),
            )
        )

    for item in sorted(templates.iterdir(), key=lambda entry: entry.name):
        if item.name.startswith(".") or not item.name.endswith(".md"):
            continue
        add(item, item.name)

    add(templates / "memory" / "MEMORY.md", "memory/MEMORY.md")

    configs = templates / "configs"
    if configs.is_dir():
        for item in sorted(configs.iterdir(), key=lambda entry: entry.name):
            if item.name.endswith(".json") and not item.name.startswith("."):
                add(item, f"configs/{item.name}")

    return targets


def template_set_sha256(targets: Iterable[TemplateTarget]) -> str:
    """Stable digest of the bundled template set."""
    digest = hashlib.sha256()
    for target in sorted(targets, key=lambda item: item.relative_path):
        digest.update(target.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(target.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def baseline_path(workspace: Path) -> Path:
    return workspace / BASELINE_RELATIVE_PATH


def is_within_workspace(workspace: Path, path: Path) -> bool:
    """Return False when ``path`` resolves outside ``workspace`` via symlinks.

    The workspace itself may be a symlink: both sides are resolved, so a normal
    workspace symlink stays inside while a nested symlink escape is caught.
    """
    try:
        root = workspace.resolve(strict=False)
        path.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def read_baseline(workspace: Path) -> dict:
    """Read the baseline without creating or modifying anything.

    A missing, unreadable or foreign-schema file is reported as ``exists`` /
    ``valid`` flags rather than treated as a crash.
    """
    path = baseline_path(workspace)
    result = {
        "path": str(path),
        "exists": path.is_file(),
        "valid": False,
        "package_version": None,
        "template_set_sha256": None,
        "templates": {},
        "error": None,
    }
    if not result["exists"]:
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    if not isinstance(data, dict) or data.get("schema") != BASELINE_SCHEMA:
        result["error"] = "unsupported baseline schema"
        return result

    templates = data.get("templates")
    if not isinstance(templates, dict):
        result["error"] = "baseline templates must be an object"
        return result

    clean: dict[str, dict[str, str]] = {}
    for relative, entry in templates.items():
        if not isinstance(relative, str) or not isinstance(entry, dict):
            continue
        digest = entry.get("sha256")
        version = entry.get("version")
        if isinstance(digest, str) and _is_sha256(digest):
            clean[relative] = {
                "sha256": digest.lower(),
                "version": version if isinstance(version, str) else "",
            }

    result["valid"] = True
    result["package_version"] = (
        data.get("package_version") if isinstance(data.get("package_version"), str) else None
    )
    result["template_set_sha256"] = (
        data.get("template_set_sha256")
        if isinstance(data.get("template_set_sha256"), str)
        else None
    )
    result["templates"] = clean
    return result


def record_template_baseline(
    workspace: Path, paths: Iterable[str] | None = None, *, force: bool = False
) -> bool:
    """Record current package templates whose workspace file still matches.

    Old releases left no baseline. A file that equals the current package
    content can be recorded safely; a custom or old file is left unrecorded so
    later diagnosis can classify it from known release hashes instead of
    claiming a false origin.

    ``force`` writes the package target version even when no workspace file
    matches (used by an explicit upgrade run). Returns ``True`` when the
    baseline file changed.
    """
    workspace = Path(workspace)
    if not workspace.is_dir():
        return False

    targets = iter_template_targets()
    if not targets:
        return False

    wanted = set(paths) if paths is not None else None
    current = read_baseline(workspace)
    entries = dict(current["templates"]) if current["valid"] else {}

    for target in targets:
        relative = target.relative_path
        if wanted is not None and relative not in wanted:
            continue
        path = workspace / relative
        if path.is_symlink() or not is_within_workspace(workspace, path):
            continue
        if not path.is_file():
            continue
        try:
            digest = sha256_file(path)
        except OSError:
            continue
        if digest != target.sha256:
            continue
        entry = {"sha256": target.sha256, "version": __version__}
        if entries.get(relative) != entry:
            entries[relative] = entry

    if not entries and not force:
        return False

    target_path = baseline_path(workspace)
    if not is_within_workspace(workspace, target_path):
        return False

    desired = {
        "schema": BASELINE_SCHEMA,
        "package_version": __version__,
        "template_set_sha256": template_set_sha256(targets),
        "templates": dict(sorted(entries.items())),
    }
    if current["valid"] and _same_baseline_content(current, desired):
        return False

    desired["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    atomic_write(
        target_path,
        (json.dumps(desired, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return True


def _same_baseline_content(current: dict, desired: dict) -> bool:
    return (
        current["package_version"] == desired["package_version"]
        and current["template_set_sha256"] == desired["template_set_sha256"]
        and current["templates"] == desired["templates"]
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644
    if path.exists():
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            pass
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

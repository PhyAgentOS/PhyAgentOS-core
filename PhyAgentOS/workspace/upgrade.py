"""Read-only workspace diagnosis and explicit template upgrade."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from typing import Any, Iterator

from PhyAgentOS import __version__
from PhyAgentOS.config.loader import get_config_path, load_config
from PhyAgentOS.workspace.baseline import (
    BASELINE_SCHEMA,
    PROTECTED_TEMPLATE_PATHS,
    TemplateTarget,
    atomic_write,
    is_within_workspace,
    iter_template_targets,
    read_baseline,
    record_template_baseline,
    sha256_bytes,
    sha256_file,
    template_set_sha256,
)
from PhyAgentOS.workspace.known_versions import KNOWN_TEMPLATE_HASHES


class WorkspaceUpgradeError(ValueError):
    """A configuration or workspace path cannot be inspected safely."""


@dataclass(frozen=True)
class WorkspaceContext:
    """Resolved paths for one instance. Constructing it never creates files."""

    workspace: Path
    config_path: Path
    config_exists: bool
    config_source: str
    workspace_source: str
    config: Any

    @property
    def installed_skills_dir(self) -> Path:
        # Matches SkillDocumentLoader: installed bundles live beside config.json.
        return self.config_path.parent / "skills"


def resolve_context(config: str | None, workspace: str | None) -> WorkspaceContext:
    """Resolve the explicit/default config and its workspace without mkdir."""
    if config:
        config_path = Path(config).expanduser()
        config_source = "explicit"
        if not config_path.is_file():
            raise WorkspaceUpgradeError(f"Config file not found: {config_path}")
    else:
        config_path = get_config_path().expanduser()
        config_source = "default"

    config_exists = config_path.is_file()
    if config_exists:
        try:
            config_obj = load_config(config_path, strict=True)
        except Exception as exc:
            raise WorkspaceUpgradeError(
                _safe_config_error_message(config_path, exc)
            ) from None
    else:
        config_obj = _default_config()

    if workspace:
        workspace_path = Path(workspace).expanduser()
        workspace_source = "explicit"
    else:
        workspace_path = Path(config_obj.workspace_path).expanduser()
        workspace_source = "config"

    if workspace_path.exists() and not workspace_path.is_dir():
        raise WorkspaceUpgradeError(f"Workspace is not a directory: {workspace_path}")

    return WorkspaceContext(
        workspace=workspace_path,
        config_path=config_path,
        config_exists=config_exists,
        config_source=config_source,
        workspace_source=workspace_source,
        config=config_obj,
    )


def _safe_config_error_message(config_path: Path, error: Exception) -> str:
    """Describe a config failure without echoing values (secrets included)."""
    message = f"Failed to load config {config_path} ({type(error).__name__})"
    fields: list[str] = []
    errors = getattr(error, "errors", None)
    if callable(errors):
        try:
            for item in errors():
                if not isinstance(item, dict):
                    continue
                loc = item.get("loc")
                if isinstance(loc, (list, tuple)) and loc:
                    fields.append(".".join(str(part) for part in loc))
        except Exception:
            fields = []
    if fields:
        message += f"; fields: {', '.join(sorted(set(fields)))}"
    return message


def check_workspace(
    context: WorkspaceContext,
    *,
    gateway_url: str | None = None,
    skill_profile: str | None = None,
) -> dict:
    """Diagnose one workspace. Never writes, creates, or deletes anything."""
    return _build_report(
        context,
        mode="check",
        apply=False,
        gateway_url=gateway_url,
        skill_profile=skill_profile,
    )


def apply_workspace(
    context: WorkspaceContext,
    *,
    gateway_url: str | None = None,
    skill_profile: str | None = None,
) -> dict:
    """Upgrade recognised originals, create missing files, preserve custom files."""
    if not context.workspace.is_dir():
        context.workspace.mkdir(parents=True, exist_ok=True)
    with _workspace_lock(context.workspace):
        return _build_report(
            context,
            mode="apply",
            apply=True,
            gateway_url=gateway_url,
            skill_profile=skill_profile,
        )


def _build_report(
    context: WorkspaceContext,
    *,
    mode: str,
    apply: bool,
    gateway_url: str | None,
    skill_profile: str | None,
) -> dict:
    targets = iter_template_targets()
    baseline_before = read_baseline(context.workspace)
    entries = [_classify(context.workspace, target, baseline_before) for target in targets]

    backup_root: Path | None = None
    backup_manifest: list[dict] = []
    if apply:
        if any(entry["status"] in {"outdated", "conflict"} for entry in entries):
            _backup_root(context.workspace)
        for entry, target in zip(entries, targets, strict=True):
            status = entry["status"]
            path = context.workspace / entry["path"]
            if status == "missing":
                if path.exists() or path.is_symlink():
                    entry.update(_classify(context.workspace, target, baseline_before))
                    if entry["status"] == "current":
                        entry["action"] = "none"
                    elif entry["status"] == "blocked":
                        entry["action"] = "blocked"
                    else:
                        entry["action"] = "kept"
                    entry["reason"] = "file appeared before upgrade; left untouched"
                    continue
                atomic_write(path, target.content)
                entry["action"] = "created"

            if status == "outdated" and entry.get("action") is None:
                original = _read_regular(path)
                if original is None:
                    _mark_blocked(entry, "file became unreadable or non-regular before upgrade")
                elif sha256_bytes(original) != entry["workspace_sha256"]:
                    fresh = _classify(context.workspace, target, baseline_before)
                    if fresh["status"] == "outdated":
                        fresh["status"] = "conflict"
                        fresh["reason"] = "content changed before upgrade; original kept"
                    entry.update(fresh)
                else:
                    backup_root = _backup_original(
                        context, entry, original, backup_manifest, backup_root
                    )
                    atomic_write(path, target.content)
                    entry["action"] = "updated"

            if entry["status"] == "outdated" and entry.get("action") is None:
                continue
            if entry["status"] == "conflict" and entry.get("action") is None:
                current = _read_regular(path)
                if current is None:
                    _mark_blocked(entry, "file became unreadable or non-regular before upgrade")
                    continue
                digest = sha256_bytes(current)
                if digest != entry["workspace_sha256"]:
                    entry["workspace_sha256"] = digest
                    entry["reason"] = "content changed before upgrade; original kept"
                candidate, wrote = _write_candidate(path, target.content, target.sha256)
                entry["candidate"] = _relative(context.workspace, candidate)
                entry["action"] = "candidate" if wrote else "kept"
                if wrote:
                    # The custom file itself is never changed. A snapshot is
                    # still useful before the user performs the manual merge.
                    backup_root = _backup_original(
                        context,
                        entry,
                        current,
                        backup_manifest,
                        backup_root,
                        kind="custom-conflict",
                    )
            elif entry["status"] in {"protected", "symlink"}:
                entry["action"] = "preserved"
            elif entry["status"] == "blocked":
                entry["action"] = "blocked"
            elif entry["status"] == "current":
                entry["action"] = "none"
            elif entry["status"] == "missing" and entry.get("action") is None:
                entry["action"] = "kept"

        if backup_root is not None and backup_manifest:
            manifest = {
                "schema": BASELINE_SCHEMA,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "package_version": __version__,
                "workspace": str(context.workspace),
                "files": backup_manifest,
            }
            atomic_write(
                backup_root / "manifest.json",
                (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )

    baseline_written = (
        record_template_baseline(context.workspace, force=True) if apply else False
    )
    baseline = read_baseline(context.workspace) if apply else baseline_before
    counts = _status_counts(entries)
    actions = _action_counts(entries)
    skills = _skill_report(context)
    deprecated = _deprecated_report(context)
    gateway = _gateway_report(context, gateway_url, skill_profile) if gateway_url else None

    template_issues = (
        counts["missing"] + counts["outdated"] + counts["conflict"] + counts["blocked"]
    )
    deprecated_issues = len(deprecated)
    gateway_issues = (
        len(gateway.get("mismatches", []))
        if gateway is not None and gateway.get("ok")
        else 0
    )
    issues = template_issues + deprecated_issues + gateway_issues
    remaining_template = (
        _remaining_template_issues(entries) if apply else template_issues
    )
    remaining_issues = remaining_template + deprecated_issues + gateway_issues
    exit_code = 0 if remaining_issues == 0 else 1

    gateway_error_count = 1 if gateway is not None and not gateway.get("ok") else 0
    gateway_unresolved = (
        len(gateway.get("warnings", []))
        if gateway is not None and gateway.get("ok")
        else 0
    )
    skill_override_diffs = sum(
        1 for skill in skills if skill["overrides"] and skill["same"] is False
    )
    warnings = {
        "skill_override_diffs": skill_override_diffs,
        "gateway_errors": gateway_error_count,
        "gateway_unresolved": gateway_unresolved,
        "protected": counts["protected"],
        "symlink": counts["symlink"],
    }
    warnings["total"] = sum(warnings.values())

    report = {
        "mode": mode,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "package_version": __version__,
        "target_version": __version__,
        "workspace_version": baseline["package_version"],
        "previous_workspace_version": baseline_before["package_version"],
        "template_upgrade_complete": remaining_template == 0,
        "complete": exit_code == 0,
        "target_template_set_sha256": template_set_sha256(targets),
        "workspace": {
            "path": str(context.workspace),
            "source": context.workspace_source,
            "exists": context.workspace.is_dir(),
        },
        "config": {
            "path": str(context.config_path),
            "source": context.config_source,
            "exists": context.config_exists,
            "installed_skills": str(context.installed_skills_dir),
        },
        "baseline": {
            "path": baseline["path"],
            "exists": baseline["exists"],
            "valid": baseline["valid"],
            "package_version": baseline["package_version"],
            "template_set_sha256": baseline["template_set_sha256"],
            "error": baseline["error"],
            "recorded_templates": len(baseline["templates"]),
            "previous_package_version": baseline_before["package_version"],
            "previous_exists": baseline_before["exists"],
            "previous_recorded_templates": len(baseline_before["templates"]),
            "updated": baseline_written,
        },
        "templates": entries,
        "summary": {
            **counts,
            "issues": issues,
            "template_issues": template_issues,
            "deprecated_issues": deprecated_issues,
            "gateway_issues": gateway_issues,
            "remaining_template_issues": remaining_template,
            "remaining_issues": remaining_issues,
            "warnings": warnings,
            "actions": actions,
        },
        "skills": skills,
        "deprecated": deprecated,
        "gateway": gateway,
        "backup_dir": str(backup_root) if backup_root is not None else None,
        "restore_hint": (
            f"cp -a {backup_root}/<path> {context.workspace}/<path>"
            if backup_root is not None
            else None
        ),
        "baseline_written": baseline_written,
    }
    return report


def _classify(workspace: Path, target: TemplateTarget, baseline: dict) -> dict:
    path = workspace / target.relative_path
    baseline_entry = baseline["templates"].get(target.relative_path)
    baseline_sha = baseline_entry["sha256"] if isinstance(baseline_entry, dict) else None
    baseline_version = baseline_entry.get("version") if isinstance(baseline_entry, dict) else None
    known = KNOWN_TEMPLATE_HASHES.get(target.relative_path, {})

    entry = {
        "path": target.relative_path,
        "absolute_path": str(path),
        "status": "missing",
        "target_version": __version__,
        "workspace_version": None,
        "workspace_sha256": None,
        "target_sha256": target.sha256,
        "baseline_sha256": baseline_sha,
        "baseline_version": baseline_version or None,
        "detected_version": None,
        "reason": "",
        "action": None,
        "backup": None,
        "candidate": None,
    }

    if path.is_symlink():
        entry["status"] = "symlink"
        entry["reason"] = "symlink; left untouched"
        return entry
    if not is_within_workspace(workspace, path):
        entry["status"] = "symlink"
        entry["reason"] = "path resolves outside the workspace; left untouched"
        return entry
    if not path.exists():
        entry["status"] = "missing"
        entry["reason"] = "not present in workspace"
        return entry
    if not path.is_file():
        entry["status"] = "blocked"
        entry["reason"] = "path exists but is not a regular file; not upgradable"
        return entry

    try:
        workspace_sha = sha256_file(path)
    except OSError as exc:
        entry["status"] = "blocked"
        entry["reason"] = f"cannot read file; not upgradable ({exc})"
        return entry
    entry["workspace_sha256"] = workspace_sha

    if workspace_sha == target.sha256:
        entry["status"] = "current"
        entry["workspace_version"] = __version__
        entry["reason"] = "matches current packaged template"
        return entry

    if target.relative_path in PROTECTED_TEMPLATE_PATHS:
        entry["status"] = "protected"
        entry["workspace_version"] = baseline_version
        entry["reason"] = "user data; never replaced by a template upgrade"
        return entry

    if baseline_sha == workspace_sha:
        entry["status"] = "outdated"
        entry["workspace_version"] = baseline_version or "baseline"
        entry["detected_version"] = baseline_version or "baseline"
        entry["reason"] = f"unmodified template recorded in baseline ({baseline_version or 'baseline'})"
        return entry

    if baseline_sha:
        entry["status"] = "conflict"
        entry["workspace_version"] = baseline_version
        entry["reason"] = "content changed after baseline; user content preserved"
        return entry

    detected = known.get(workspace_sha)
    if detected:
        entry["status"] = "outdated"
        entry["workspace_version"] = detected
        entry["detected_version"] = detected
        entry["reason"] = f"matches unmodified template from {detected}"
        return entry

    entry["status"] = "conflict"
    entry["reason"] = "no baseline and content does not match a known release; user content preserved"
    return entry


def _status_counts(entries: list[dict]) -> dict[str, int]:
    statuses = (
        "current",
        "missing",
        "outdated",
        "conflict",
        "blocked",
        "protected",
        "symlink",
    )
    counts = {status: 0 for status in statuses}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts


def _action_counts(entries: list[dict]) -> dict[str, int]:
    actions = {
        "created": 0,
        "updated": 0,
        "candidate": 0,
        "blocked": 0,
        "kept": 0,
        "preserved": 0,
    }
    for entry in entries:
        action = entry.get("action")
        if action in actions:
            actions[action] += 1
    return actions


def _remaining_template_issues(entries: list[dict]) -> int:
    remaining = 0
    for entry in entries:
        status = entry["status"]
        action = entry.get("action")
        if status == "current" or status in {"protected", "symlink"}:
            continue
        if status == "missing" and action == "created":
            continue
        if status == "outdated" and action == "updated":
            continue
        remaining += 1
    return remaining


def _read_regular(path: Path) -> bytes | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _mark_blocked(entry: dict, reason: str) -> None:
    entry["status"] = "blocked"
    entry["reason"] = reason
    entry["action"] = "blocked"


def _backup_original(
    context: WorkspaceContext,
    entry: dict,
    original: bytes,
    manifest: list[dict],
    backup_root: Path | None,
    *,
    kind: str | None = None,
) -> Path:
    root = backup_root if backup_root is not None else _new_backup_dir(context.workspace)
    backup_path = root / entry["path"]
    atomic_write(backup_path, original)
    record = {
        "path": entry["path"],
        "backup": _relative(context.workspace, backup_path),
        "sha256": sha256_bytes(original),
        "size": len(original),
    }
    if kind:
        record["kind"] = kind
    manifest.append(record)
    entry["backup"] = _relative(context.workspace, backup_path)
    return root


@contextmanager
def _workspace_lock(workspace: Path) -> Iterator[None]:
    """Serialize one workspace's apply without blocking check."""
    lock_path = workspace / ".paos" / "upgrade.lock"
    if not is_within_workspace(workspace, lock_path):
        raise WorkspaceUpgradeError(
            "Upgrade lock resolves outside the workspace; refusing to modify templates"
        )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(handle, 0, os.SEEK_SET)
            try:
                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkspaceUpgradeError(
                    "Another workspace upgrade is running; retry shortly"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkspaceUpgradeError(
                    "Another workspace upgrade is running; retry shortly"
                ) from exc
        if os.fstat(handle).st_size == 0:
            os.write(handle, b"\0")
            os.fsync(handle)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def _backup_root(workspace: Path) -> Path:
    root = workspace / ".paos" / "backups" / "templates"
    if not is_within_workspace(workspace, root):
        raise WorkspaceUpgradeError(
            "Backup directory resolves outside the workspace; refusing to modify templates"
        )
    return root


def _new_backup_dir(workspace: Path) -> Path:
    root = _backup_root(workspace)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = root / stamp
    suffix = 1
    while candidate.exists():
        candidate = root / f"{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _write_candidate(path: Path, content: bytes, target_sha: str) -> tuple[Path, bool]:
    """Write a target-version candidate beside a custom file, never over it."""
    candidate = path.with_name(path.name + ".new")
    if not candidate.exists() and not candidate.is_symlink():
        atomic_write(candidate, content)
        return candidate, True
    if _read_regular(candidate) == content:
        return candidate, False

    stamped = path.with_name(f"{path.name}.new.{target_sha[:8]}")
    suffix = 2
    while stamped.exists() or stamped.is_symlink():
        if _read_regular(stamped) == content:
            return stamped, False
        stamped = path.with_name(f"{path.name}.new.{target_sha[:8]}.{suffix}")
        suffix += 1
    atomic_write(stamped, content)
    return stamped, True


def _skill_report(context: WorkspaceContext) -> list[dict]:
    workspace_skills = context.workspace / "skills"
    installed_skills = context.installed_skills_dir
    builtin_skills = Path(__file__).resolve().parents[1] / "skills"
    if not workspace_skills.is_dir():
        return []

    report: list[dict] = []
    for skill_dir in sorted(workspace_skills.iterdir(), key=lambda item: item.name):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        workspace_skill = skill_dir / "SKILL.md"
        if not workspace_skill.is_file():
            continue

        installed_skill = installed_skills / skill_dir.name / "SKILL.md"
        builtin_skill = builtin_skills / skill_dir.name / "SKILL.md"
        if installed_skill.is_file():
            source = "installed"
            other = installed_skill
        elif builtin_skill.is_file():
            source = "builtin"
            other = builtin_skill
        else:
            report.append(
                {
                    "name": skill_dir.name,
                    "source": "workspace-only",
                    "overrides": None,
                    "same": None,
                    "workspace_sha256": _safe_sha(workspace_skill),
                    "other_sha256": None,
                    "diff_lines": None,
                    "diff": None,
                    "diff_truncated": False,
                }
            )
            continue

        workspace_text = _read_text(workspace_skill)
        other_text = _read_text(other)
        same = workspace_text == other_text
        diff = None
        diff_lines = None
        truncated = False
        if not same and workspace_text is not None and other_text is not None:
            lines = list(
                unified_diff(
                    other_text.splitlines(),
                    workspace_text.splitlines(),
                    fromfile=f"{source}:{other}",
                    tofile=f"workspace:{workspace_skill}",
                    lineterm="",
                )
            )
            diff_lines = len(lines)
            diff = "\n".join(lines[:160])
            truncated = len(lines) > 160

        report.append(
            {
                "name": skill_dir.name,
                "source": source,
                "overrides": source,
                "same": same,
                "workspace_sha256": _safe_sha(workspace_skill),
                "other_sha256": _safe_sha(other),
                "diff_lines": diff_lines,
                "diff": diff,
                "diff_truncated": truncated,
            }
        )
    return report


_DEPRECATED_TOOLS: dict[str, str] = {
    "forge_execute_task": "removed Session tool; use forge_tool_* / forge_task_*",
    "forge_get_session": "removed Session tool; use forge_tool_session_status",
    "forge_cancel_session": "removed Session tool; use forge_tool_stop_session",
    "forge_get_context": "removed Session tool; use forge_tool_context",
    "forge_reset": "removed Session tool; use the Gateway Tool API",
    "verify_forge_session": "removed Session tool; use forge_task_finalize",
    "create_replanned_forge_session": "removed Session tool; use forge_task_begin_revision",
}
_DEPRECATED_TOOL_RE = re.compile(r"\b(" + "|".join(map(re.escape, _DEPRECATED_TOOLS)) + r")\b")
_DEPRECATED_RULES: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (
        re.compile(r"\bmemoryWindow\b"),
        "memoryWindow",
        "config",
        "use maxTokens and contextWindowTokens",
    ),
    (
        re.compile(r"\btools\.exec\.restrictToWorkspace\b"),
        "tools.exec.restrictToWorkspace",
        "config",
        "use tools.restrictToWorkspace",
    ),
    (
        re.compile(r"/agent/sessions\b"),
        "/agent/sessions",
        "rule",
        "retired Gateway Session path; use the /tools Tool API",
    ),
)
_NEGATIVE_CONTEXT = (
    "retired",
    "removed",
    "deprecated",
    "do not",
    "don't",
    "never",
    "must not",
    "no longer",
    "not use",
    "avoid",
)


def _deprecated_report(context: WorkspaceContext) -> list[dict]:
    findings: list[dict] = []
    for path in _workspace_document_paths(context.workspace):
        text = _read_text(path)
        if text is None:
            continue
        relative = _relative(context.workspace, path)
        for line_number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            if any(marker in lowered for marker in _NEGATIVE_CONTEXT):
                continue
            for match in _DEPRECATED_TOOL_RE.finditer(line):
                token = match.group(1)
                findings.append(
                    {
                        "path": relative,
                        "line": line_number,
                        "kind": "tool",
                        "token": token,
                        "replacement": _DEPRECATED_TOOLS[token],
                        "snippet": line.strip()[:160],
                    }
                )
            for pattern, token, kind, replacement in _DEPRECATED_RULES:
                if pattern.search(line):
                    findings.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "kind": kind,
                            "token": token,
                            "replacement": replacement,
                            "snippet": line.strip()[:160],
                        }
                    )

    if context.config_exists:
        text = _read_text(context.config_path)
        if text is not None:
            try:
                raw_config = json.loads(text)
            except ValueError:
                raw_config = None

            if isinstance(raw_config, dict):
                defaults = raw_config.get("agents", {})
                defaults = defaults.get("defaults", {}) if isinstance(defaults, dict) else {}
                if isinstance(defaults, dict) and "memoryWindow" in defaults:
                    findings.append(
                        _config_finding(
                            context.config_path,
                            text,
                            "memoryWindow",
                            "memoryWindow",
                            "use maxTokens and contextWindowTokens",
                        )
                    )

                tools = raw_config.get("tools", {})
                exec_cfg = tools.get("exec", {}) if isinstance(tools, dict) else {}
                if isinstance(exec_cfg, dict) and "restrictToWorkspace" in exec_cfg:
                    findings.append(
                        _config_finding(
                            context.config_path,
                            text,
                            "restrictToWorkspace",
                            "tools.exec.restrictToWorkspace",
                            "use tools.restrictToWorkspace",
                        )
                    )

                if "runtime" in raw_config:
                    findings.append(
                        _config_finding(
                            context.config_path,
                            text,
                            "runtime",
                            "runtime",
                            "legacy runtime config is unsupported; remove it",
                        )
                    )

    findings.sort(key=lambda item: (item["path"], item["line"], item["token"]))
    return findings


def _config_finding(
    path: Path,
    text: str,
    key: str,
    token: str,
    replacement: str,
) -> dict:
    pattern = re.compile(r'"' + re.escape(key) + r'"\s*:')
    line_number = next(
        (number for number, line in enumerate(text.splitlines(), start=1) if pattern.search(line)),
        1,
    )
    return {
        "path": str(path),
        "line": line_number,
        "kind": "config",
        "token": token,
        "replacement": replacement,
        # Never echo a config value; only the key name leaves this function.
        "snippet": token,
    }


def _workspace_document_paths(workspace: Path) -> list[Path]:
    if not workspace.is_dir():
        return []
    paths = set(workspace.glob("*.md"))
    memory = workspace / "memory"
    if memory.is_dir():
        paths.update(memory.glob("*.md"))

    skills = workspace / "skills"
    if skills.is_dir():
        count = 0
        for root, dirnames, filenames in os.walk(skills, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
            for filename in sorted(filenames):
                if not filename.endswith(".md"):
                    continue
                paths.add(Path(root) / filename)
                count += 1
                if count >= 200:
                    break
            if count >= 200:
                break
    return sorted(paths)


def _gateway_matches(manifest, url: str) -> bool:
    return manifest.gateway_url.rstrip("/") == url.rstrip("/")


def _gateway_report(
    context: WorkspaceContext, url: str, explicit_profile: str | None = None
) -> dict:
    result = {
        "url": url,
        "ok": False,
        "status_code": None,
        "tool_ids": [],
        "skills": [],
        "mismatches": [],
        "warnings": [],
        "ignored_skills": [],
        "error": None,
    }
    try:
        import httpx

        endpoint = url.rstrip("/") + "/tools"
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            response = client.get(endpoint)
        result["status_code"] = response.status_code
        try:
            payload = response.json()
        except ValueError:
            result["error"] = "non-JSON response"
            return result
        if response.status_code != 200 or not isinstance(payload, dict):
            result["error"] = f"unexpected HTTP {response.status_code}"
            return result
        if payload.get("ok") is not True:
            result["error"] = "Gateway response is not ok"
            return result
        data = payload.get("data")
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, list):
            result["error"] = "Gateway response has no tools list"
            return result

        tool_ids: set[str] = set()
        for item in tools:
            if isinstance(item, str):
                tool_ids.add(item)
            elif isinstance(item, dict):
                for key in ("tool_id", "id", "name"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        tool_ids.add(value)
                        break
        result["tool_ids"] = sorted(tool_ids)
        result["ok"] = True

        try:
            from PhyAgentOS.skill_runtime.catalog import SkillCatalog

            manifests = SkillCatalog(root=context.installed_skills_dir).list()
        except Exception:
            manifests = []

        for manifest in manifests:
            if not _gateway_matches(manifest, url):
                result["ignored_skills"].append(
                    {
                        "skill": manifest.name,
                        "version": manifest.version,
                        "gateway_url": manifest.gateway_url,
                    }
                )
                continue
            state_profile, state_warning = _state_profile(context, manifest.name)
            skill_result = _gateway_skill_result(
                manifest,
                url,
                tool_ids,
                state_profile=state_profile,
                explicit_profile=explicit_profile,
            )
            if state_warning:
                result["warnings"].append(
                    {
                        "skill": manifest.name,
                        "profile": state_profile,
                        "reason": state_warning,
                    }
                )
            result["skills"].append(skill_result)
            result["mismatches"].extend(skill_result["mismatches"])
            result["warnings"].extend(skill_result["warnings"])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _gateway_skill_result(
    manifest,
    url: str,
    tool_ids: set[str],
    *,
    state_profile: str | None,
    explicit_profile: str | None,
) -> dict:
    """Compare only the selected profile; never use Skill-wide as a fallback."""
    profile_names = sorted(manifest.profiles)
    selected = explicit_profile if explicit_profile is not None else state_profile
    selected_profile = selected if selected in manifest.profiles else None

    result = {
        "skill": manifest.name,
        "version": manifest.version,
        "gateway_url": manifest.gateway_url,
        "state_profile": state_profile,
        "selected_profile": selected_profile,
        "selection": "none",
        "profiles": [],
        "mismatches": [],
        "warnings": [],
    }

    if selected is None:
        result["selection"] = "none"
        result["warnings"].append(
            {
                "skill": manifest.name,
                "profile": None,
                "reason": "no active runtime state; pass --profile to select one",
            }
        )
    elif selected_profile is None:
        result["selection"] = "unknown-profile"
        result["warnings"].append(
            {
                "skill": manifest.name,
                "profile": selected,
                "reason": (
                    f"profile {selected!r} is not declared by the Skill; "
                    f"available: {', '.join(profile_names)}"
                ),
            }
        )
    else:
        result["selection"] = "explicit" if explicit_profile else "runtime-state"

    for profile_name in profile_names:
        required = list(manifest.tools_for_profile(profile_name))
        missing = [tool for tool in required if tool not in tool_ids]
        is_selected = profile_name == selected_profile
        status = (
            "not-selected"
            if not is_selected
            else ("ok" if not missing else "mismatch")
        )
        result["profiles"].append(
            {
                "profile": profile_name,
                "selected": is_selected,
                "required_tools": required,
                "missing": missing,
                "status": status,
            }
        )
        if is_selected and missing:
            result["mismatches"].append(
                {
                    "skill": manifest.name,
                    "profile": profile_name,
                    "missing": missing,
                }
            )
    return result


def _state_profile(context: WorkspaceContext, skill_name: str) -> tuple[str | None, str | None]:
    """Read the active profile from existing runtime state; never write it."""
    state_root = context.config_path.parent / "run" / "skills"
    state_path = state_root / f"{skill_name}.json"
    if not state_path.is_file():
        return None, None
    try:
        from PhyAgentOS.skill_runtime.state import RuntimeStateStore

        state = RuntimeStateStore(root=state_root).load(skill_name)
    except Exception as exc:
        return None, f"cannot read runtime state: {type(exc).__name__}: {exc}"
    return (state.profile if state is not None else None), None


def _default_config():
    from PhyAgentOS.config.schema import Config

    return Config()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _safe_sha(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)

"""Workspace template diagnosis/upgrade behavior in temporary fixtures."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from PhyAgentOS import __version__
from PhyAgentOS.config.loader import save_config
from PhyAgentOS.config.schema import Config
from PhyAgentOS.skill_runtime.manifest import RuntimeProfile, SkillManifest
from PhyAgentOS.utils.helpers import sync_workspace_templates
from PhyAgentOS.workspace import (
    WorkspaceUpgradeError,
    apply_workspace,
    check_workspace,
    iter_template_targets,
    read_baseline,
    resolve_context,
)
from PhyAgentOS.workspace import upgrade as upgrade_module
from PhyAgentOS.workspace.baseline import baseline_path, sha256_bytes
from PhyAgentOS.workspace.known_versions import (
    KNOWN_TEMPLATE_HASHES,
    KNOWN_TEMPLATE_PROVENANCE,
)


def make_config(tmp_path: Path, workspace: Path, name: str = "config.json") -> Path:
    config = Config()
    config.agents.defaults.workspace = str(workspace)
    path = tmp_path / name
    save_config(config, path)
    return path


def populate(workspace: Path, names: set[str] | None = None) -> None:
    for target in iter_template_targets():
        if names is not None and target.relative_path not in names:
            continue
        path = workspace / target.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(target.content)


def target_for(relative: str):
    return next(item for item in iter_template_targets() if item.relative_path == relative)


def write_baseline(
    workspace: Path,
    entries: dict[str, dict[str, str]],
    *,
    package_version: str = "v0.2.0",
    template_set_sha256: str = "0" * 64,
) -> Path:
    path = baseline_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "package_version": package_version,
                "template_set_sha256": template_set_sha256,
                "templates": entries,
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def snapshot(root: Path) -> dict:
    dirs = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    links = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_symlink())
    return {"dirs": dirs, "files": files, "links": links}


def entry_for(report: dict, relative: str) -> dict:
    return next(item for item in report["templates"] if item["path"] == relative)


def context(tmp_path: Path, workspace: Path):
    return resolve_context(str(make_config(tmp_path, workspace)), None)


def test_sync_records_baseline_for_created_templates(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    added = sync_workspace_templates(workspace, silent=True)

    assert "AGENTS.md" in added
    baseline = read_baseline(workspace)
    assert baseline["valid"] is True
    assert baseline["templates"]["AGENTS.md"]["sha256"] == target_for("AGENTS.md").sha256
    assert baseline["templates"]["AGENTS.md"]["version"] == __version__


def test_check_never_writes_and_reports_all_states(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    (workspace / "SOUL.md").unlink()
    (workspace / "AGENTS.md").write_text("my own rules\n", encoding="utf-8")
    old = "old skills text\n"
    (workspace / "SKILLS.md").write_text(old, encoding="utf-8")
    write_baseline(workspace, {"SKILLS.md": {"sha256": sha256_bytes(old.encode()), "version": "v0.2.0"}})

    before = snapshot(workspace)
    report = check_workspace(context(tmp_path, workspace))
    after = snapshot(workspace)

    assert before == after
    assert report["exit_code"] == 1
    assert entry_for(report, "SOUL.md")["status"] == "missing"
    assert entry_for(report, "AGENTS.md")["status"] == "conflict"
    assert entry_for(report, "SKILLS.md")["status"] == "outdated"
    assert entry_for(report, "SKILLS.md")["workspace_version"] == "v0.2.0"
    assert report["baseline"]["valid"] is True
    assert not (workspace / ".paos" / "backups").exists()


def test_apply_creates_missing_templates_and_is_idempotent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = context(tmp_path, workspace)

    first = apply_workspace(ctx)
    assert first["exit_code"] == 0
    assert (workspace / "AGENTS.md").read_bytes() == target_for("AGENTS.md").content
    assert (workspace / "memory" / "MEMORY.md").read_bytes() == target_for("memory/MEMORY.md").content
    baseline = read_baseline(workspace)
    assert baseline["valid"] is True
    assert baseline["templates"]["AGENTS.md"]["sha256"] == target_for("AGENTS.md").sha256
    assert baseline["package_version"] == __version__

    after_first = snapshot(workspace)
    second = apply_workspace(ctx)
    assert second["exit_code"] == 0
    assert second["summary"]["actions"] == {
        "created": 0,
        "updated": 0,
        "candidate": 0,
        "blocked": 0,
        "kept": 0,
        "preserved": 0,
    }
    assert second["baseline_written"] is False
    assert snapshot(workspace) == after_first


def test_baseline_original_is_backed_up_upgraded_and_restorable(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    old = "old SKILLS from v0.2.0\n"
    (workspace / "SKILLS.md").write_text(old, encoding="utf-8")
    write_baseline(workspace, {"SKILLS.md": {"sha256": sha256_bytes(old.encode()), "version": "v0.2.0"}})

    report = apply_workspace(context(tmp_path, workspace))
    assert report["exit_code"] == 0
    entry = entry_for(report, "SKILLS.md")
    assert entry["status"] == "outdated"
    assert entry["action"] == "updated"
    assert (workspace / "SKILLS.md").read_bytes() == target_for("SKILLS.md").content

    backup_file = workspace / entry["backup"]
    assert backup_file.read_bytes() == old.encode()
    manifest = json.loads((Path(report["backup_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["path"] == "SKILLS.md"
    assert manifest["files"][0]["sha256"] == sha256_bytes(old.encode())

    # The backup is a complete copy, so a manual rollback restores the old file.
    (workspace / "SKILLS.md").write_bytes(backup_file.read_bytes())
    assert (workspace / "SKILLS.md").read_text(encoding="utf-8") == old

    baseline = read_baseline(workspace)
    assert baseline["templates"]["SKILLS.md"]["sha256"] == target_for("SKILLS.md").sha256
    assert baseline["templates"]["SKILLS.md"]["version"] == __version__
    assert report["previous_workspace_version"] == "v0.2.0"
    assert report["workspace_version"] == __version__
    assert report["target_version"] == __version__
    assert report["template_upgrade_complete"] is True
    assert report["complete"] is True
    assert report["baseline"]["previous_package_version"] == "v0.2.0"
    assert report["baseline"]["updated"] is True


def test_no_baseline_old_original_is_recognised_from_known_hash(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    populate(workspace)
    old = "old AGENTS shipped with an old release\n"
    old_hash = sha256_bytes(old.encode())
    (workspace / "AGENTS.md").write_text(old, encoding="utf-8")
    monkeypatch.setitem(KNOWN_TEMPLATE_HASHES["AGENTS.md"], old_hash, "v0.1.6")

    report = apply_workspace(context(tmp_path, workspace))
    entry = entry_for(report, "AGENTS.md")
    assert entry["status"] == "outdated"
    assert entry["detected_version"] == "v0.1.6"
    assert entry["action"] == "updated"
    assert (workspace / "AGENTS.md").read_bytes() == target_for("AGENTS.md").content
    assert (workspace / entry["backup"]).read_text(encoding="utf-8") == old
    assert not (workspace / "AGENTS.md.new").exists()


def test_real_known_hash_list_contains_released_old_template():
    # v0.2.0 SKILLS.md, taken from the release tag, not from the live package.
    assert (
        KNOWN_TEMPLATE_HASHES["SKILLS.md"][
            "1368bc2f5727cf8c9549a27603bd5331ed65ab431da23a6d7f10e29d7ab23313"
        ]
        == "v0.2.0"
    )


def test_custom_file_is_never_overwritten_and_gets_candidate(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    custom = "my custom agent rules\n"
    (workspace / "AGENTS.md").write_text(custom, encoding="utf-8")

    report = apply_workspace(context(tmp_path, workspace))
    assert report["exit_code"] == 1
    entry = entry_for(report, "AGENTS.md")
    assert entry["status"] == "conflict"
    assert entry["action"] == "candidate"
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == custom
    assert (workspace / entry["candidate"]).read_bytes() == target_for("AGENTS.md").content
    assert (workspace / entry["backup"]).read_text(encoding="utf-8") == custom
    assert report["summary"]["actions"]["candidate"] == 1
    assert report["backup_dir"] is not None
    assert report["template_upgrade_complete"] is False
    assert report["complete"] is False

    after_first = snapshot(workspace)
    second = apply_workspace(context(tmp_path, workspace))
    assert second["exit_code"] == 1
    assert second["summary"]["actions"]["candidate"] == 0
    assert snapshot(workspace) == after_first


def test_user_data_templates_are_not_reset(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    memory = workspace / "memory" / "MEMORY.md"
    user = workspace / "USER.md"
    memory.write_text("remembered facts\n", encoding="utf-8")
    user.write_text("name: someone\n", encoding="utf-8")

    report = apply_workspace(context(tmp_path, workspace))
    assert entry_for(report, "memory/MEMORY.md")["status"] == "protected"
    assert entry_for(report, "USER.md")["status"] == "protected"
    assert memory.read_text(encoding="utf-8") == "remembered facts\n"
    assert user.read_text(encoding="utf-8") == "name: someone\n"


def test_custom_workspace_and_config_are_reported_without_fallback(tmp_path):
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    config_a = make_config(tmp_path, workspace_a, "a.json")
    config_b = make_config(tmp_path, workspace_b, "b.json")

    explicit = resolve_context(str(config_b), str(workspace_a))
    report = check_workspace(explicit)
    assert report["config"]["path"] == str(config_b)
    assert report["config"]["source"] == "explicit"
    assert report["workspace"]["path"] == str(workspace_a)
    assert report["workspace"]["source"] == "explicit"
    assert report["config"]["installed_skills"] == str(config_b.parent / "skills")

    from_config = resolve_context(str(config_a), None)
    assert from_config.workspace == workspace_a
    assert check_workspace(from_config)["workspace"]["source"] == "config"


def test_workspace_skill_override_is_reported_with_diff(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    config_path = make_config(tmp_path, workspace)
    installed = config_path.parent / "skills" / "pick"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("installed version\n", encoding="utf-8")
    workspace_skill = workspace / "skills" / "pick"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text("workspace version\n", encoding="utf-8")

    report = check_workspace(resolve_context(str(config_path), None))
    skill = report["skills"][0]
    assert skill["name"] == "pick"
    assert skill["overrides"] == "installed"
    assert skill["same"] is False
    assert "workspace version" in skill["diff"]
    assert skill["workspace_sha256"] != skill["other_sha256"]
    assert report["exit_code"] == 0
    assert report["summary"]["issues"] == 0
    assert report["summary"]["warnings"]["skill_override_diffs"] == 1
    assert report["summary"]["warnings"]["total"] >= 1
    # Reporting must not modify or remove either Skill copy.
    assert (installed / "SKILL.md").read_text(encoding="utf-8") == "installed version\n"
    assert (workspace_skill / "SKILL.md").read_text(encoding="utf-8") == "workspace version\n"


def test_deprecated_tool_and_rule_references_are_reported(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    config_path = make_config(tmp_path, workspace)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["agents"]["defaults"]["memoryWindow"] = 10
    data["tools"]["exec"]["restrictToWorkspace"] = True
    config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        "Call forge_execute_task now.\n"
        "Use /agent/sessions for execution.\n"
        "The retired /agent/sessions path is not used.\n",
        encoding="utf-8",
    )

    report = check_workspace(resolve_context(str(config_path), None))
    tokens = {(item["kind"], item["token"]) for item in report["deprecated"]}
    assert ("tool", "forge_execute_task") in tokens
    assert ("rule", "/agent/sessions") in tokens
    assert ("config", "memoryWindow") in tokens
    assert ("config", "tools.exec.restrictToWorkspace") in tokens
    session_hits = [item for item in report["deprecated"] if item["token"] == "/agent/sessions"]
    assert len(session_hits) == 1  # the "retired ..." context line is skipped


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink not supported")
def test_file_symlink_is_left_untouched(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    outside = tmp_path / "outside.md"
    outside.write_text("outside content\n", encoding="utf-8")
    (workspace / "AGENTS.md").unlink()
    (workspace / "AGENTS.md").symlink_to(outside)

    report = apply_workspace(context(tmp_path, workspace))
    entry = entry_for(report, "AGENTS.md")
    assert entry["status"] == "symlink"
    assert entry["action"] == "preserved"
    assert (workspace / "AGENTS.md").is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside content\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink not supported")
def test_nested_symlink_directory_cannot_redirect_writes(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "MEMORY.md").write_text("outside memory\n", encoding="utf-8")
    (workspace / "memory").rename(workspace / "memory-real")
    (workspace / "memory").symlink_to(outside, target_is_directory=True)

    report = apply_workspace(context(tmp_path, workspace))
    entry = entry_for(report, "memory/MEMORY.md")
    assert entry["status"] == "symlink"
    assert entry["action"] == "preserved"
    assert (outside / "MEMORY.md").read_text(encoding="utf-8") == "outside memory\n"
    assert not (outside / "MEMORY.md.new").exists()


def test_deprecated_references_are_issues_and_apply_cannot_claim_success(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    (workspace / "NOTES.md").write_text("Run forge_execute_task here.\n", encoding="utf-8")
    ctx = context(tmp_path, workspace)

    check = check_workspace(ctx)
    assert check["exit_code"] == 1
    assert check["summary"]["template_issues"] == 0
    assert check["summary"]["deprecated_issues"] == 1
    assert check["summary"]["issues"] == 1

    applied = apply_workspace(ctx)
    assert applied["exit_code"] == 1
    assert applied["summary"]["remaining_template_issues"] == 0
    assert applied["summary"]["deprecated_issues"] == 1
    assert applied["summary"]["remaining_issues"] == 1
    assert applied["template_upgrade_complete"] is True
    assert applied["complete"] is False


def test_gateway_mismatch_is_issue_but_transport_error_is_warning(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    populate(workspace)
    ctx = context(tmp_path, workspace)

    def mismatch(context, url, profile=None):
        return {
            "url": url,
            "ok": True,
            "status_code": 200,
            "tool_ids": ["shared"],
            "skills": [],
            "mismatches": [{"skill": "pick", "profile": "alpha", "missing": ["a1"]}],
            "warnings": [],
            "ignored_skills": [],
            "error": None,
        }

    monkeypatch.setattr(upgrade_module, "_gateway_report", mismatch)
    report = check_workspace(ctx, gateway_url="http://gateway.invalid")
    assert report["exit_code"] == 1
    assert report["summary"]["gateway_issues"] == 1
    assert report["summary"]["remaining_issues"] == 1
    assert report["summary"]["warnings"]["gateway_errors"] == 0

    def failed(context, url, profile=None):
        return {
            "url": url,
            "ok": False,
            "status_code": None,
            "tool_ids": [],
            "skills": [],
            "mismatches": [],
            "warnings": [],
            "ignored_skills": [],
            "error": "connection refused",
        }

    monkeypatch.setattr(upgrade_module, "_gateway_report", failed)
    failed_report = check_workspace(ctx, gateway_url="http://gateway.invalid")
    assert failed_report["exit_code"] == 0
    assert failed_report["summary"]["issues"] == 0
    assert failed_report["summary"]["warnings"]["gateway_errors"] == 1


def _gateway_manifest() -> SkillManifest:
    return SkillManifest(
        name="pick",
        version="1.0.0",
        description="test",
        skill_document=Path("SKILL.md"),
        gateway_url="http://gateway:8080",
        required_tools=("shared",),
        profiles={
            "alpha": RuntimeProfile(
                dataflow=Path("alpha.yaml"), required_tools=("a1",)
            ),
            "beta": RuntimeProfile(dataflow=Path("beta.yaml"), required_tools=("b1",)),
        },
    )


def test_gateway_profile_selection_uses_profile_tools_not_skill_wide():
    manifest = _gateway_manifest()
    result = upgrade_module._gateway_skill_result(
        manifest,
        "http://gateway:8080",
        {"a1"},
        state_profile="alpha",
        explicit_profile=None,
    )
    assert result["selection"] == "runtime-state"
    assert result["selected_profile"] == "alpha"
    profiles = {item["profile"]: item for item in result["profiles"]}
    assert profiles["alpha"]["required_tools"] == ["a1"]
    assert profiles["alpha"]["selected"] is True
    assert profiles["alpha"]["status"] == "ok"
    assert profiles["beta"]["selected"] is False
    assert profiles["beta"]["status"] == "not-selected"
    assert result["mismatches"] == []


def test_gateway_explicit_profile_mismatch_and_unknown_profile_are_separate():
    manifest = _gateway_manifest()
    mismatch = upgrade_module._gateway_skill_result(
        manifest,
        "http://gateway:8080",
        {"a1"},
        state_profile="alpha",
        explicit_profile="beta",
    )
    assert mismatch["selection"] == "explicit"
    assert mismatch["selected_profile"] == "beta"
    assert mismatch["mismatches"] == [
        {"skill": "pick", "profile": "beta", "missing": ["b1"]}
    ]

    unknown = upgrade_module._gateway_skill_result(
        manifest,
        "http://gateway:8080",
        {"a1"},
        state_profile="missing",
        explicit_profile=None,
    )
    assert unknown["selection"] == "unknown-profile"
    assert unknown["selected_profile"] is None
    assert all(not profile["selected"] for profile in unknown["profiles"])
    assert unknown["mismatches"] == []
    assert unknown["warnings"]


def test_gateway_url_matching_and_no_profile_does_not_use_skill_wide():
    manifest = _gateway_manifest()
    assert upgrade_module._gateway_matches(manifest, "http://gateway:8080/")
    assert not upgrade_module._gateway_matches(manifest, "http://other:8080")

    unselected = upgrade_module._gateway_skill_result(
        manifest,
        "http://gateway:8080",
        {"shared"},
        state_profile=None,
        explicit_profile=None,
    )
    assert unselected["selection"] == "none"
    assert unselected["selected_profile"] is None
    assert all(not profile["selected"] for profile in unselected["profiles"])
    assert unselected["mismatches"] == []  # Skill-wide tools are not a current allowlist
    assert unselected["warnings"]


def test_directory_target_is_blocked_not_a_candidate(tmp_path):
    workspace = tmp_path / "workspace"
    populate(workspace)
    (workspace / "AGENTS.md").unlink()
    (workspace / "AGENTS.md").mkdir()
    ctx = context(tmp_path, workspace)

    before = snapshot(workspace)
    checked = check_workspace(ctx)
    assert snapshot(workspace) == before
    assert checked["exit_code"] == 1
    assert entry_for(checked, "AGENTS.md")["status"] == "blocked"
    assert checked["summary"]["blocked"] == 1

    applied = apply_workspace(ctx)
    assert applied["exit_code"] == 1
    entry = entry_for(applied, "AGENTS.md")
    assert entry["status"] == "blocked"
    assert entry["action"] == "blocked"
    assert entry["candidate"] is None
    assert (workspace / "AGENTS.md").is_dir()
    assert not (workspace / "AGENTS.md.new").exists()


def test_concurrent_upgrade_lock_is_reported(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    workspace = tmp_path / "workspace"
    populate(workspace)
    ctx = context(tmp_path, workspace)
    lock_path = workspace / ".paos" / "upgrade.lock"
    lock_path.parent.mkdir(parents=True)
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WorkspaceUpgradeError):
            apply_workspace(ctx)
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def test_outdated_file_changed_before_write_becomes_candidate(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    populate(workspace)
    old = "old original\n"
    (workspace / "AGENTS.md").write_text(old, encoding="utf-8")
    write_baseline(workspace, {"AGENTS.md": {"sha256": sha256_bytes(old.encode()), "version": "v0.2.0"}})
    ctx = context(tmp_path, workspace)

    real_read_regular = upgrade_module._read_regular

    def edited(path):
        if path == workspace / "AGENTS.md":
            return b"edited after check\n"
        return real_read_regular(path)

    monkeypatch.setattr(upgrade_module, "_read_regular", edited)
    report = apply_workspace(ctx)
    entry = entry_for(report, "AGENTS.md")
    assert entry["status"] == "conflict"
    assert entry["action"] == "candidate"
    assert report["exit_code"] == 1
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == old
    assert (workspace / entry["candidate"]).read_bytes() == target_for("AGENTS.md").content


def test_runtime_state_profile_read_is_side_effect_free(tmp_path):
    from PhyAgentOS.skill_runtime.state import RuntimeState

    workspace = tmp_path / "workspace"
    populate(workspace)
    config_path = make_config(tmp_path, workspace)
    state_root = config_path.parent / "run" / "skills"
    state_root.mkdir(parents=True)
    state = RuntimeState(
        skill_name="pick",
        profile="alpha",
        status="running",
        flow_name="flow_pick",
        gateway_url="http://gateway:8080",
    )
    state_path = state_root / "pick.json"
    state_path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    before = state_path.read_bytes()
    before_files = sorted(path.name for path in state_root.iterdir())

    profile, warning = upgrade_module._state_profile(
        resolve_context(str(config_path), None), "pick"
    )
    assert profile == "alpha"
    assert warning is None
    assert state_path.read_bytes() == before
    assert sorted(path.name for path in state_root.iterdir()) == before_files


def test_config_validation_error_does_not_echo_field_value(tmp_path):
    secret = "SECRET_MARKER_123"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"agents": {"defaults": {"maxTokens": secret}}}),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceUpgradeError) as raised:
        resolve_context(str(config), None)
    message = str(raised.value)
    assert secret not in message
    assert "Failed to load config" in message
    assert "ValidationError" in message
    assert "maxTokens" in message


def test_real_old_release_template_is_recognised_and_upgraded(tmp_path):
    digest = "1368bc2f5727cf8c9549a27603bd5331ed65ab431da23a6d7f10e29d7ab23313"
    tag, commit = KNOWN_TEMPLATE_PROVENANCE[("SKILLS.md", digest)]
    shown = subprocess.run(
        ["git", "show", f"{commit}:PhyAgentOS/templates/SKILLS.md"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        check=False,
    )
    if shown.returncode != 0:
        pytest.skip(f"git object for {tag} is unavailable in this checkout")
    assert sha256_bytes(shown.stdout) == digest

    workspace = tmp_path / "workspace"
    populate(workspace)
    (workspace / "SKILLS.md").write_bytes(shown.stdout)

    report = apply_workspace(context(tmp_path, workspace))
    entry = entry_for(report, "SKILLS.md")
    assert entry["status"] == "outdated"
    assert entry["detected_version"] == "v0.2.0"
    assert entry["action"] == "updated"
    assert (workspace / "SKILLS.md").read_bytes() == target_for("SKILLS.md").content
    assert (workspace / entry["backup"]).read_bytes() == shown.stdout


def test_known_hashes_match_real_git_release_objects():
    repo_root = Path(__file__).parents[1]
    expected = {
        (path, digest)
        for path, hashes in KNOWN_TEMPLATE_HASHES.items()
        for digest in hashes
    }
    assert set(KNOWN_TEMPLATE_PROVENANCE) == expected

    for (path, digest), (tag, commit) in KNOWN_TEMPLATE_PROVENANCE.items():
        shown = subprocess.run(
            ["git", "show", f"{commit}:PhyAgentOS/templates/{path}"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if shown.returncode != 0:
            pytest.skip(f"git object for {tag} is unavailable in this checkout")
        assert sha256_bytes(shown.stdout) == digest
        tag_commit = subprocess.run(
            ["git", "rev-list", "-n", "1", tag],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if tag_commit.returncode == 0:
            assert tag_commit.stdout.strip() == commit

"""CLI registration, JSON output and exit codes for ``paos workspace``."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from PhyAgentOS.cli.commands import app
from PhyAgentOS.config.loader import save_config
from PhyAgentOS.config.schema import Config
from PhyAgentOS.workspace import iter_template_targets


def make_config(tmp_path: Path, workspace: Path) -> Path:
    config = Config()
    config.agents.defaults.workspace = str(workspace)
    path = tmp_path / "config.json"
    save_config(config, path)
    return path


def populate(workspace: Path) -> None:
    for target in iter_template_targets():
        path = workspace / target.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(target.content)


def test_workspace_group_is_registered() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "workspace" in result.stdout

    help_result = CliRunner().invoke(app, ["workspace", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "check" in help_result.stdout
    assert "upgrade" in help_result.stdout
    check_help = CliRunner().invoke(app, ["workspace", "check", "--help"])
    assert check_help.exit_code == 0, check_help.output
    assert "--profile" in check_help.stdout


def test_check_reports_missing_with_exit_1_and_json(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = make_config(tmp_path, workspace)

    result = CliRunner().invoke(
        app, ["workspace", "check", "-c", str(config), "--json"]
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["mode"] == "check"
    assert report["ok"] is False
    assert report["workspace"]["path"] == str(workspace)
    assert report["summary"]["missing"] > 0
    # check is read-only even through the CLI.
    assert not (workspace / ".paos").exists()


def test_upgrade_preview_writes_nothing_then_apply_exits_0(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = make_config(tmp_path, workspace)

    preview = CliRunner().invoke(
        app, ["workspace", "upgrade", "-c", str(config), "--json"]
    )
    assert preview.exit_code == 1, preview.output
    assert not (workspace / "AGENTS.md").exists()

    applied = CliRunner().invoke(
        app, ["workspace", "upgrade", "-c", str(config), "--apply", "--json"]
    )
    assert applied.exit_code == 0, applied.output
    assert (workspace / "AGENTS.md").exists()
    assert (workspace / ".paos" / "template-baseline.json").exists()

    clean = CliRunner().invoke(
        app, ["workspace", "check", "-c", str(config), "--json"]
    )
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.stdout)["ok"] is True


def test_clean_workspace_check_exits_0(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    populate(workspace)
    config = make_config(tmp_path, workspace)

    result = CliRunner().invoke(
        app, ["workspace", "check", "-c", str(config), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["summary"]["issues"] == 0


def test_conflict_apply_keeps_original_and_exits_1(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    populate(workspace)
    (workspace / "AGENTS.md").write_text("custom\n", encoding="utf-8")
    config = make_config(tmp_path, workspace)

    result = CliRunner().invoke(
        app, ["workspace", "upgrade", "-c", str(config), "--apply", "--json"]
    )
    assert result.exit_code == 1, result.output
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "custom\n"
    report = json.loads(result.stdout)
    entry = next(item for item in report["templates"] if item["path"] == "AGENTS.md")
    assert entry["status"] == "conflict"
    assert entry["action"] == "candidate"


def test_missing_explicit_config_exits_2(tmp_path) -> None:
    result = CliRunner().invoke(
        app, ["workspace", "check", "-c", str(tmp_path / "missing.json"), "--json"]
    )
    assert result.exit_code == 2, result.output
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["exit_code"] == 2
    assert "Config file not found" in report["error"]


def test_invalid_config_json_keeps_json_output_parseable(tmp_path) -> None:
    config = tmp_path / "config.json"
    config.write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(app, ["workspace", "check", "-c", str(config), "--json"])
    assert result.exit_code == 2, result.output
    report = json.loads(result.stdout)
    assert report["exit_code"] == 2
    assert "Failed to load config" in report["error"]


def test_config_validation_error_does_not_leak_secret_value(tmp_path) -> None:
    secret = "SECRET_MARKER_456"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"agents": {"defaults": {"maxTokens": secret}}}),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["workspace", "check", "-c", str(config), "--json"]
    )
    assert result.exit_code == 2, result.output
    assert secret not in result.output
    report = json.loads(result.stdout)
    assert report["exit_code"] == 2
    assert "Failed to load config" in report["error"]
    assert "ValidationError" in report["error"]


def test_apply_with_deprecated_reference_still_exits_1(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    populate(workspace)
    (workspace / "NOTES.md").write_text("Use forge_execute_task.\n", encoding="utf-8")
    config = make_config(tmp_path, workspace)

    result = CliRunner().invoke(
        app, ["workspace", "upgrade", "-c", str(config), "--apply", "--json"]
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["summary"]["deprecated_issues"] == 1
    assert report["summary"]["remaining_template_issues"] == 0
    assert report["summary"]["remaining_issues"] == 1

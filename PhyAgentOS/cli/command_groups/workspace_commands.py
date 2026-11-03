"""Workspace template diagnosis and upgrade commands."""

from __future__ import annotations

import json

import typer
from rich.console import Console

from PhyAgentOS.workspace import (
    WorkspaceUpgradeError,
    apply_workspace,
    check_workspace,
    resolve_context,
)

console = Console()
workspace_app = typer.Typer(help="Check or upgrade workspace template files")


@workspace_app.command("check")
def workspace_check(
    workspace: str | None = typer.Option(
        None, "--workspace", "-w", help="Workspace directory (default: from config)"
    ),
    config: str | None = typer.Option(
        None, "--config", "-c", help="Path to config.json (default: ~/.PhyAgentOS/config.json)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
    gateway: str | None = typer.Option(
        None, "--gateway", help="Optional Gateway base URL for a read-only GET /tools check"
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Skill profile to compare against Gateway tools"
    ),
) -> None:
    """Diagnose templates, overridden Skills and deprecated names. Writes nothing."""
    _run(
        "check",
        workspace=workspace,
        config=config,
        as_json=as_json,
        gateway=gateway,
        profile=profile,
        apply=False,
    )


@workspace_app.command("upgrade")
def workspace_upgrade(
    workspace: str | None = typer.Option(
        None, "--workspace", "-w", help="Workspace directory (default: from config)"
    ),
    config: str | None = typer.Option(
        None, "--config", "-c", help="Path to config.json (default: ~/.PhyAgentOS/config.json)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
    gateway: str | None = typer.Option(
        None, "--gateway", help="Optional Gateway base URL for a read-only GET /tools check"
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Skill profile to compare against Gateway tools"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write changes (default is a read-only preview)"
    ),
) -> None:
    """Preview or apply workspace template upgrades.

    Missing files are created. Files matching the recorded baseline or a known
    release hash are backed up and replaced. Custom files are never overwritten;
    a ``.new`` candidate is written beside them instead.
    """
    _run(
        "upgrade",
        workspace=workspace,
        config=config,
        as_json=as_json,
        gateway=gateway,
        profile=profile,
        apply=apply,
    )


def _run(
    mode: str,
    *,
    workspace: str | None,
    config: str | None,
    as_json: bool,
    gateway: str | None,
    profile: str | None,
    apply: bool,
) -> None:
    try:
        context = resolve_context(config, workspace)
        report = (
            apply_workspace(context, gateway_url=gateway, skill_profile=profile)
            if apply
            else check_workspace(context, gateway_url=gateway, skill_profile=profile)
        )
    except WorkspaceUpgradeError as exc:
        _error(mode, str(exc), as_json)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _error(mode, f"{type(exc).__name__}: {exc}", as_json)

    if as_json:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _render(report)
    raise typer.Exit(report["exit_code"])


def _error(mode: str, message: str, as_json: bool) -> None:
    if as_json:
        typer.echo(
            json.dumps(
                {"mode": mode, "ok": False, "exit_code": 2, "error": message},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(2)


def _render(report: dict) -> None:
    workspace = report["workspace"]
    config = report["config"]
    baseline = report["baseline"]
    summary = report["summary"]

    console.print(f"[bold]Workspace {report['mode']}[/bold]")
    console.print(
        f"  Workspace: {workspace['path']} "
        f"[dim]({workspace['source']}, {'present' if workspace['exists'] else 'missing'})[/dim]"
    )
    console.print(
        f"  Config:    {config['path']} "
        f"[dim]({config['source']}, {'found' if config['exists'] else 'not found'})[/dim]"
    )
    console.print(f"  Installed Skills: {config['installed_skills']}")
    console.print(f"  Package target: {report['package_version']}")
    if baseline["exists"] and baseline["valid"]:
        previous = baseline["previous_package_version"]
        previous_note = (
            f", previous {previous}"
            if previous and previous != baseline["package_version"]
            else ""
        )
        console.print(
            f"  Baseline: {baseline['path']} "
            f"[dim](version {baseline['package_version'] or 'unknown'}, "
            f"{baseline['recorded_templates']} files{previous_note})[/dim]"
        )
    elif baseline["exists"]:
        console.print(f"  Baseline: {baseline['path']} [red](invalid: {baseline['error']})[/red]")
    else:
        console.print(f"  Baseline: none [dim]({baseline['path']})[/dim]")

    console.print(
        f"  Templates: {summary['current']} current, {summary['missing']} missing, "
        f"{summary['outdated']} outdated, {summary['conflict']} conflict, "
        f"{summary['blocked']} blocked, {summary['protected']} protected, "
        f"{summary['symlink']} symlink"
    )
    if summary["warnings"]["total"]:
        warnings = summary["warnings"]
        console.print(
            f"  Warnings: {warnings['skill_override_diffs']} Skill override diff, "
            f"{warnings['gateway_errors']} Gateway error, "
            f"{warnings['gateway_unresolved']} unselected profile, "
            f"{warnings['protected']} protected, {warnings['symlink']} symlink"
        )
    if summary["remaining_issues"]:
        console.print(
            f"  Remaining issues: {summary['remaining_issues']} "
            f"[dim]({summary['remaining_template_issues']} template, "
            f"{summary['deprecated_issues']} deprecated, "
            f"{summary['gateway_issues']} Gateway)[/dim]"
        )

    for entry in report["templates"]:
        if entry["status"] == "current":
            continue
        line = f"  {entry['status'].upper():10} {entry['path']}"
        if entry["status"] == "outdated":
            line += f" ({entry['workspace_version']} -> {report['package_version']})"
        elif entry["status"] == "conflict":
            if entry["candidate"]:
                line += f" [dim](kept; candidate {entry['candidate']})[/dim]"
            else:
                line += f" [dim]({entry['reason']})[/dim]"
        elif entry["status"] in {"protected", "symlink", "blocked"}:
            line += f" [dim]({entry['reason']})[/dim]"
        console.print(line)

    if report["mode"] == "apply":
        actions = summary["actions"]
        if actions["created"] or actions["updated"] or actions["candidate"] or actions["blocked"]:
            console.print(
                f"  Applied: {actions['created']} created, {actions['updated']} updated, "
                f"{actions['candidate']} candidate(s), {actions['blocked']} blocked"
            )
        if report["backup_dir"]:
            console.print(f"  Backup:  {report['backup_dir']}")
            console.print(f"  Restore: {report['restore_hint']}")
        if report["baseline_written"]:
            console.print("  Baseline updated")
        else:
            console.print("  Baseline unchanged")

    if report["skills"]:
        console.print("[bold]Skill overrides[/bold]")
        for skill in report["skills"]:
            if skill["overrides"]:
                detail = "contents same" if skill["same"] else f"contents differ, {skill['diff_lines']} diff lines"
                console.print(
                    f"  {skill['name']}: workspace overrides {skill['source']} [dim]({detail})[/dim]"
                )
                if skill["diff"] and not skill["same"]:
                    for diff_line in skill["diff"].splitlines()[:8]:
                        console.print(f"    [dim]{diff_line}[/dim]")
                    if skill["diff_truncated"]:
                        console.print("    [dim]...[/dim]")
            else:
                console.print(f"  {skill['name']}: workspace only")

    if report["deprecated"]:
        console.print("[bold]Deprecated references[/bold]")
        for finding in report["deprecated"]:
            console.print(
                f"  {finding['path']}:{finding['line']} "
                f"{finding['kind']} {finding['token']} -> {finding['replacement']}"
            )

    gateway = report["gateway"]
    if gateway is not None:
        if gateway["ok"]:
            console.print(f"[bold]Gateway[/bold] {gateway['url']}: {len(gateway['tool_ids'])} tools")
            for skill in gateway["skills"]:
                selected = skill["selected_profile"] or "none"
                console.print(
                    f"  {skill['skill']} [dim](selection {skill['selection']}, "
                    f"profile {selected})[/dim]"
                )
                for profile in skill["profiles"]:
                    marker = "selected" if profile["selected"] else "not selected"
                    detail = "ok" if not profile["missing"] else f"missing {', '.join(profile['missing'])}"
                    console.print(
                        f"    {profile['profile']}: {marker}, {detail}"
                    )
            for missing in gateway["mismatches"]:
                console.print(
                    f"  [red]{missing['skill']}[/red] profile {missing['profile']} "
                    f"missing required tools: {', '.join(missing['missing'])}"
                )
            for warning in gateway["warnings"]:
                console.print(
                    f"  [yellow]{warning['skill']}[/yellow] {warning['reason']}"
                )
            if gateway["ignored_skills"]:
                console.print(
                    f"  [dim]Ignored {len(gateway['ignored_skills'])} Skill(s) "
                    "whose manifest points at another Gateway URL.[/dim]"
                )
        else:
            console.print(f"[yellow]Gateway check failed:[/yellow] {gateway['error']}")

    conflicts = [entry for entry in report["templates"] if entry["status"] == "conflict"]
    if conflicts and report["mode"] == "apply":
        console.print("[yellow]Manual merge needed (original files were not changed):[/yellow]")
        for entry in conflicts:
            if not entry["candidate"]:
                continue
            original = report["workspace"]["path"].rstrip("/") + "/" + entry["path"]
            candidate = report["workspace"]["path"].rstrip("/") + "/" + entry["candidate"]
            console.print(f"  diff -u {original} {candidate}")
            console.print(f"  then merge only the changes you want into {entry['path']}")
    elif report["mode"] == "check" and report["exit_code"] != 0:
        console.print("[dim]Next: paos workspace upgrade --apply[/dim]")

    if report["mode"] == "apply" and report["exit_code"] != 0:
        console.print(
            "[red]Upgrade completed with remaining issues.[/red] "
            "Templates may be current, but deprecated references or Gateway/profile "
            "mismatches still need manual action."
        )


def register_workspace_commands(app) -> None:
    app.add_typer(workspace_app, name="workspace")

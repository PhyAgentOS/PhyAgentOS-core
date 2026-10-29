"""Skill Runtime management commands."""

from contextlib import contextmanager
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

console = Console()

skill_app = typer.Typer(help="Manage installed Skill runtimes")


@contextmanager
def _download_progress():
    tasks: dict[str, int] = {}
    with Progress(
        TextColumn("[cyan]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:

        def report(event, artifact, downloaded: int, total: int | None) -> None:
            label = artifact.name or artifact.version or "artifact"
            task_id = tasks.get(artifact.url)
            if event == "cached":
                progress.console.print(
                    f"[green]✓[/green] Using cached [cyan]{label}[/cyan] "
                    f"({downloaded} bytes)"
                )
                return
            if task_id is None:
                progress.console.print(
                    f"[bold]Downloading[/bold] [cyan]{label}[/cyan]\n"
                    f"[dim]{artifact.url}[/dim]"
                )
                task_id = progress.add_task(label, total=total, completed=downloaded)
                tasks[artifact.url] = task_id
            else:
                progress.update(task_id, total=total)
            if event in {"advance", "complete"}:
                progress.update(task_id, completed=downloaded)

        yield report


def _skill_runtime_error(error: Exception) -> None:
    console.print(f"[red]Error: {error}[/red]")
    raise typer.Exit(1)


@skill_app.command("search")
def skill_search(
    query: str = typer.Argument("", help="Skill name or search text"),
    index: str | None = typer.Option(
        None,
        "--index",
        help="Schema-v3 static package index path or URL",
    ),
):
    """Search Skills in a static index or the configured Resource Registry."""
    from PhyAgentOS.skill_runtime.registry import RegistryClient, StaticPackageIndex

    try:
        source = StaticPackageIndex(index) if index else RegistryClient()
        with source:
            items = source.search_skills(query)
    except Exception as error:
        _skill_runtime_error(error)
        return
    table = Table(title="Registry Skills")
    table.add_column("Skill", style="cyan")
    table.add_column("Version")
    table.add_column("Description")
    for item in items:
        table.add_row(
            str(item.get("name", "")),
            str(item.get("version", "")),
            str(item.get("description", "")),
        )
    console.print(table)


def _install_skill_bundle(
    archive: Path,
    *,
    expected_sha256: str,
    expected_version: str | None = None,
    index: str | None = None,
) -> None:
    import tempfile

    from PhyAgentOS.skill_runtime.catalog import SkillCatalog, SkillNotFoundError
    from PhyAgentOS.skill_runtime.installer import NodeInstaller, SkillInstaller
    from PhyAgentOS.skill_runtime.registry import (
        DownloadCache,
        RegistryClient,
        StaticPackageIndex,
    )
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    with _download_progress() as report:
        cache = DownloadCache(progress=report)
        try:
            with tempfile.TemporaryDirectory(prefix="paos-skill-preview-") as directory:
                preview_root = Path(directory)
                preview = SkillInstaller(
                    preview_root / "skills",
                    state_store=RuntimeStateStore(preview_root / "run"),
                ).install(
                    archive,
                    expected_sha256=expected_sha256,
                )
                if expected_version is not None and preview.version != expected_version:
                    raise RuntimeError(
                        f"Registry returned Skill {preview.name!r} version "
                        f"{preview.version!r}; requested version was {expected_version!r}"
                    )
                node_installer = NodeInstaller()
                missing_nodes = [
                    (node_id, lock)
                    for node_id, lock in sorted(preview.artifacts.nodes.items())
                    if not node_installer.satisfies(lock)
                ]
                if missing_nodes and (
                    index is not None or preview.artifacts.resolver == "registry"
                ):
                    source = StaticPackageIndex(index) if index else RegistryClient()
                    with source:
                        for node_id, lock in missing_nodes:
                            node_artifact = source.node(
                                lock.artifact_id,
                                expected_sha256=lock.sha256,
                            )
                            if node_artifact.sha256 != lock.sha256:
                                raise RuntimeError(
                                    f"Registry digest for Node {node_id!r} does not match "
                                    "Skill lock"
                                )
                            node_archive = cache.download(node_artifact)
                            installed = node_installer.install(node_archive, lock)
                            if not node_installer.satisfies(lock):
                                raise RuntimeError(
                                    f"Node executable {installed!s} does not satisfy Skill lock"
                                )
                    missing_nodes = []
                try:
                    local = SkillCatalog().get(preview.name)
                except SkillNotFoundError:
                    local = None
                if local == preview:
                    if missing_nodes:
                        names = ", ".join(node_id for node_id, _ in missing_nodes)
                        console.print(
                            f"[yellow]Skill [cyan]{preview.name}[/cyan] is installed; "
                            f"install locked Node(s) locally before start: {names}[/yellow]"
                        )
                    else:
                        console.print(
                            f"[green]✓[/green] Skill [cyan]{preview.name}[/cyan] "
                            f"{preview.version} is already ready"
                        )
                    return
                manifest = SkillInstaller().install(
                    archive,
                    expected_sha256=expected_sha256,
                )
        finally:
            cache.close()
    console.print(
        f"[green]✓[/green] Installed Skill [cyan]{manifest.name}[/cyan] {manifest.version}"
    )
    if missing_nodes:
        names = ", ".join(node_id for node_id, _ in missing_nodes)
        console.print(
            f"[yellow]Install locked Node(s) locally before start: {names}[/yellow]"
        )


def _confirm_skill_install(name: str, source: str, size: int) -> bool:
    size_mib = size / (1024 * 1024)
    console.print(
        f"[bold]Skill:[/bold] [cyan]{name}[/cyan]\n"
        f"[bold]Source:[/bold] {source}\n"
        f"[bold]Skill Bundle:[/bold] {size_mib:.1f} MiB ({size} bytes)\n"
        "[yellow]Additional locked Forge Node archives may be downloaded after the "
        "Skill Bundle is inspected.[/yellow]"
    )
    return typer.confirm("Continue with installation and downloads?", default=False)


def _install_skill_from_registry(
    name: str,
    *,
    version: str | None,
    index: str | None,
    ask_confirmation: bool,
) -> None:
    from PhyAgentOS.skill_runtime.registry import (
        DownloadCache,
        RegistryClient,
        StaticPackageIndex,
    )

    source = StaticPackageIndex(index) if index else RegistryClient()
    with source:
        artifact = source.skill(name, version)
    assert artifact.sha256 is not None and artifact.size is not None
    if ask_confirmation and not _confirm_skill_install(name, artifact.url, artifact.size):
        console.print("[yellow]Installation cancelled.[/yellow]")
        return
    with _download_progress() as report:
        cache = DownloadCache(progress=report)
        try:
            archive = cache.download(artifact)
        finally:
            cache.close()
    _install_skill_bundle(
        archive,
        expected_sha256=artifact.sha256,
        expected_version=version,
        index=index,
    )


def _resolve_local_bundle_path(name: str) -> Path:
    path = Path(name).expanduser()
    if not path.is_file():
        raise RuntimeError(f"Skill bundle file not found: {name!r}")
    if not str(path).endswith((".tar.gz", ".tgz")):
        raise RuntimeError("local Skill bundle must be a .tar.gz or .tgz archive")
    return path


def _install_skill_from_local_bundle(
    path: Path,
    *,
    index: str | None,
    ask_confirmation: bool,
) -> None:
    from PhyAgentOS.skill_runtime.archive import sha256_file

    size = path.stat().st_size
    if ask_confirmation and not _confirm_skill_install(path.name, str(path.resolve()), size):
        console.print("[yellow]Installation cancelled.[/yellow]")
        return
    _install_skill_bundle(
        path,
        expected_sha256=sha256_file(path),
        index=index,
    )


def _resolve_skill_install_source(name: str) -> str | Path:
    path = Path(name).expanduser()
    if path.exists() or name.endswith((".tar.gz", ".tgz")) or "/" in name or "\\" in name:
        return _resolve_local_bundle_path(name)
    return name


@skill_app.command("install")
def skill_install(
    name: str = typer.Argument(..., help="Registry Skill name or local .tar.gz bundle"),
    # Hidden until the Registry serves more than one version per Skill.
    version: str | None = typer.Option(
        None, "--version", "-v", help="Exact version", hidden=True
    ),
    index: str | None = typer.Option(
        None,
        "--index",
        help="Schema-v3 static package index path or URL",
    ),
    local: bool = typer.Option(False, "--local", help="Treat NAME as a local bundle path"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the install confirmation"),
):
    """Install a verified Skill from the Registry, static index, or local bundle."""
    try:
        source = _resolve_local_bundle_path(name) if local else _resolve_skill_install_source(name)
        if isinstance(source, Path):
            _install_skill_from_local_bundle(
                source, index=index, ask_confirmation=not yes
            )
        else:
            _install_skill_from_registry(
                source,
                version=version,
                index=index,
                ask_confirmation=not yes,
            )
    except Exception as error:
        _skill_runtime_error(error)


@skill_app.command("update")
def skill_update(
    name: str = typer.Argument(..., help="Installed Skill name"),
    # Hidden until the Registry serves more than one version per Skill.
    version: str | None = typer.Option(
        None, "--version", "-v", help="Target version", hidden=True
    ),
    index: str | None = typer.Option(
        None,
        "--index",
        help="Schema-v3 static package index path or URL",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the update confirmation"),
):
    """Update an installed, stopped Skill while retaining a backup."""
    try:
        from PhyAgentOS.skill_runtime.catalog import SkillCatalog

        SkillCatalog().get(name)
        _install_skill_from_registry(
            name,
            version=version,
            index=index,
            ask_confirmation=not yes,
        )
    except Exception as error:
        _skill_runtime_error(error)


@skill_app.command("remove")
def skill_remove(name: str = typer.Argument(..., help="Installed Skill name")):
    """Remove a Skill unless it is running or owns invocations, Sessions, or tasks."""
    from PhyAgentOS.skill_runtime.installer import SkillInstaller

    try:
        SkillInstaller().remove(name)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(f"[green]✓[/green] Removed Skill [cyan]{name}[/cyan]")


@skill_app.command("list")
def skill_list():
    """List locally installed Skill bundles."""
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    catalog = SkillCatalog()
    states = RuntimeStateStore()
    table = Table(title="Installed Skills")
    table.add_column("Skill", style="cyan")
    table.add_column("Version")
    table.add_column("Profiles")
    table.add_column("Runtime")
    try:
        manifests = catalog.list()
    except Exception as error:
        _skill_runtime_error(error)
        return
    for manifest in manifests:
        state = states.load(manifest.name)
        table.add_row(
            manifest.name,
            manifest.version,
            ", ".join(sorted(manifest.profiles)),
            state.status if state is not None else "not started",
        )
    for name, error in catalog.errors().items():
        table.add_row(name, "-", "-", f"[red]invalid: {error}[/red]")
    console.print(table)


@skill_app.command("inspect")
def skill_inspect(skill_name: str = typer.Argument(..., help="Installed Skill name")):
    """Inspect a Skill manifest and its last runtime state."""
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    try:
        manifest = SkillCatalog().get(skill_name)
        state = RuntimeStateStore().load(skill_name)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(f"[bold cyan]{manifest.name}[/bold cyan] {manifest.version}")
    console.print(manifest.description)
    console.print(f"Document: {manifest.skill_document.as_posix()}")
    console.print(f"Gateway: {manifest.gateway_url}")
    console.print(
        f"Tools: {', '.join(manifest.required_tools)} (Skill-wide; profiles may override)"
    )
    profile_table = Table(title="Runtime Profiles")
    profile_table.add_column("Profile")
    profile_table.add_column("Dataflow")
    profile_table.add_column("Binaries")
    profile_table.add_column("Assets")
    profile_table.add_column("Tools")
    for name, profile in sorted(manifest.profiles.items()):
        profile_table.add_row(
            name,
            profile.dataflow.as_posix(),
            str(len(profile.required_binaries)),
            str(len(profile.required_assets)),
            ", ".join(manifest.tools_for_profile(name)),
        )
    console.print(profile_table)
    console.print(f"Runtime: {state.status if state is not None else 'not started'}")


@skill_app.command("start")
def skill_start(
    skill_name: str = typer.Argument(..., help="Installed Skill name"),
    profile: str = typer.Option(..., "--profile", "-p", help="Runtime profile"),
):
    """Start an installed Skill's named Dora dataflow."""
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    try:
        state = RuntimeManager().start(skill_name, profile)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(
        f"[green]✓[/green] Skill [cyan]{state.skill_name}[/cyan] is running "
        f"(profile={state.profile}, flow={state.flow_name})"
    )


@skill_app.command("status")
def skill_status(skill_name: str = typer.Argument(..., help="Installed Skill name")):
    """Reconcile persisted state with Dora and Gateway health."""
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    try:
        report = RuntimeManager().status(skill_name)
    except Exception as error:
        _skill_runtime_error(error)
        return
    if report.state is None:
        console.print(f"Skill [cyan]{skill_name}[/cyan]: not started")
        return
    state = report.state
    console.print(f"Skill: {state.skill_name}")
    console.print(f"State: {state.status}")
    console.print(f"Profile: {state.profile}")
    console.print(f"Dora flow: {state.flow_name} ({'running' if report.flow_running else 'down'})")
    console.print(f"Gateway GET /tools: {'ready' if report.gateway_ready else 'unavailable'}")
    for tool_id, ready in report.tool_contexts.items():
        console.print(f"Tool context {tool_id}: {'ready' if ready else 'not ready'}")
    if state.last_error:
        console.print(f"[red]Last error: {state.last_error}[/red]")


@skill_app.command("switch")
def skill_switch(
    skill_name: str = typer.Argument(..., help="Installed target Skill name"),
    profile: str = typer.Option(..., "--profile", "-p", help="Target Runtime profile"),
):
    """Safely switch the active Runtime when no AgentTask is non-terminal."""
    from PhyAgentOS.config.loader import load_config
    from PhyAgentOS.forge.task import AgentTaskStore
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.integration import (
        ActiveRuntimeRegistry,
        SkillRuntimeController,
        discover_active_runtime,
    )
    from PhyAgentOS.skill_runtime.manager import RuntimeManager
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    try:
        catalog = SkillCatalog()
        states = RuntimeStateStore()
        manager = RuntimeManager(catalog=catalog, state_store=states)
        active = discover_active_runtime(
            catalog=catalog,
            state_store=states,
            manager=manager,
        )
        registry = ActiveRuntimeRegistry(active)
        controller = SkillRuntimeController(
            registry,
            manager=manager,
            catalog=catalog,
            state_store=states,
            task_store=AgentTaskStore(load_config().workspace_path),
        )
        selected = controller.switch(skill_name, profile)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(
        f"[green]✓[/green] Active Forge Runtime is now "
        f"[cyan]{selected.skill_name}[/cyan] "
        f"(profile={selected.profile}, instance={selected.runtime_instance_id})"
    )


@skill_app.command("logs")
def skill_logs(
    skill_name: str = typer.Argument(..., help="Installed Skill name"),
    lines: int = typer.Option(200, "--lines", "-n", min=1, help="Lifecycle log lines"),
):
    """Show recent Skill runtime lifecycle logs."""
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    try:
        content = RuntimeManager().read_logs(skill_name, lines=lines)
    except Exception as error:
        _skill_runtime_error(error)
        return
    if content:
        console.print(content, end="")
    else:
        console.print("[dim]No lifecycle logs recorded.[/dim]")


@skill_app.command("stop")
def skill_stop(
    skill_name: str = typer.Argument(..., help="Installed Skill name"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Force-stop despite active invocations, Sessions, or task bindings",
    ),
):
    """Stop a managed Skill dataflow without shutting down shared Dora services."""
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    try:
        state = RuntimeManager().stop(skill_name, force=force)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(
        f"[green]✓[/green] Skill [cyan]{state.skill_name}[/cyan] is {state.status}"
    )


def register_skill_commands(app) -> None:
    app.add_typer(skill_app, name="skill")

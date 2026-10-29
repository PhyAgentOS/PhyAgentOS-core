"""Forge Node installation and verification commands."""

from pathlib import Path

import typer
from rich.console import Console

from .skill_commands import _download_progress, _skill_runtime_error

console = Console()

forge_node_app = typer.Typer(help="Install and verify independently versioned Forge nodes")


@forge_node_app.command("install")
def forge_node_install(
    skill_name: str = typer.Argument(..., help="Installed Skill containing the Node lock"),
    node_id: str = typer.Argument(..., help="Node ID from the Skill lock"),
    archive: Path | None = typer.Option(
        None,
        "--archive",
        help="Independently obtained local Node .tar.gz instead of a Registry download",
    ),
):
    """Download the exact node archive pinned by a Skill lock."""
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.installer import NodeInstaller
    from PhyAgentOS.skill_runtime.registry import DownloadCache, RegistryClient

    with _download_progress() as report:
        cache = DownloadCache(progress=report)
        try:
            manifest = SkillCatalog().get(skill_name)
            lock = manifest.artifacts.nodes.get(node_id)
            if lock is None:
                raise RuntimeError(f"Skill {skill_name!r} does not lock Node {node_id!r}")
            if archive is None:
                with RegistryClient() as registry:
                    artifact = registry.node(
                        lock.artifact_id,
                        expected_sha256=lock.sha256,
                    )
                if artifact.sha256 != lock.sha256:
                    raise RuntimeError("Registry Node sha256 does not match the Skill lock")
                node_archive = cache.download(artifact)
            else:
                node_archive = archive.expanduser().resolve()
                if not node_archive.is_file() or node_archive.is_symlink():
                    raise RuntimeError("local Node archive is not a regular file")
            installed = NodeInstaller().install(node_archive, lock)
        except Exception as error:
            _skill_runtime_error(error)
            return
        finally:
            cache.close()
    console.print(
        f"[green]✓[/green] Installed Forge node "
        f"[cyan]{lock.node_id}[/cyan] {lock.version} ({lock.artifact_id}) at {installed}"
    )


@forge_node_app.command("verify")
def forge_node_verify(
    skill_name: str = typer.Argument(..., help="Installed Skill containing the Node lock"),
    node_id: str = typer.Argument(..., help="Node ID from the Skill lock"),
):
    """Verify an installed executable and receipt against its Skill lock."""
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.installer import NodeInstaller

    try:
        manifest = SkillCatalog().get(skill_name)
        lock = manifest.artifacts.nodes.get(node_id)
        if lock is None:
            raise RuntimeError(f"Skill {skill_name!r} does not lock Node {node_id!r}")
        NodeInstaller().load(lock, strict=True)
    except Exception as error:
        _skill_runtime_error(error)
        return
    console.print(
        f"[green]✓[/green] Forge node [cyan]{lock.node_id}[/cyan] "
        f"{lock.artifact_id} SHA-256 verified"
    )


def register_forge_node_commands(app) -> None:
    app.add_typer(forge_node_app, name="forge-node")

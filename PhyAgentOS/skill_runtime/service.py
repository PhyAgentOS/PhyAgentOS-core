"""Shared Skill install/update/remove orchestration.

Used by both the CLI (``paos skill install/update/remove``) and the local
settings UI. The module only mutates the instance's own data directories;
running-skill guards and SHA-256 verification stay inside ``SkillInstaller``
and ``NodeInstaller``.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import yaml

from PhyAgentOS.skill_runtime.catalog import SkillCatalog, SkillNotFoundError
from PhyAgentOS.skill_runtime.installer import NodeInstaller, SkillInstaller
from PhyAgentOS.skill_runtime.manifest import SkillManifest
from PhyAgentOS.skill_runtime.registry import (
    DownloadCache,
    DownloadProgressCallback,
    RegistryClient,
    RegistryError,
    StaticPackageIndex,
)
from PhyAgentOS.skill_runtime.state import RuntimeStateStore

ConfirmCallback = Callable[[str, str, int], bool]


@dataclass(frozen=True)
class SkillServicePaths:
    """Instance data directories used by install/remove operations."""

    bundles: Path
    state: Path
    forge_runtime: Path
    cache: Path


@dataclass(frozen=True)
class InstallOutcome:
    """Result of one install/update attempt."""

    status: Literal["installed", "already_ready", "cancelled"]
    manifest: SkillManifest | None = None
    missing_nodes: tuple[str, ...] = field(default_factory=tuple)


def install_skill_from_registry(
    name: str,
    *,
    version: str | None = None,
    index: str | None = None,
    paths: SkillServicePaths,
    confirm: ConfirmCallback | None = None,
    progress: DownloadProgressCallback | None = None,
    registry: RegistryClient | None = None,
    download_client: Any = None,
) -> InstallOutcome:
    """Install or update one Skill from the Registry or a static index."""
    source = registry if registry is not None else (
        StaticPackageIndex(index) if index else RegistryClient()
    )
    with source:
        artifact = source.skill(name, version)
    if artifact.sha256 is None or artifact.size is None:
        raise RegistryError("registry artifact metadata is missing sha256 or size")
    if confirm is not None and not confirm(name, artifact.url, artifact.size):
        return InstallOutcome(status="cancelled")
    cache = DownloadCache(root=paths.cache, progress=progress, client=download_client)
    try:
        archive = cache.download(artifact)
    finally:
        cache.close()

    def source_factory() -> Any:
        if registry is not None:
            return RegistryClient(base_url=registry.base_url, client=download_client)
        return StaticPackageIndex(index) if index else RegistryClient()

    return install_bundle(
        archive,
        expected_sha256=artifact.sha256,
        expected_version=version,
        index=index,
        paths=paths,
        progress=progress,
        download_client=download_client,
        source_factory=source_factory,
    )


def install_bundle(
    archive: Path,
    *,
    expected_sha256: str | None = None,
    expected_version: str | None = None,
    index: str | None = None,
    paths: SkillServicePaths,
    progress: DownloadProgressCallback | None = None,
    download_client: Any = None,
    source_factory: Callable[[], Any] | None = None,
) -> InstallOutcome:
    """Install a Skill bundle archive, fetching any missing locked Nodes."""
    with tempfile.TemporaryDirectory(prefix="paos-skill-install-") as directory:
        preview_root = Path(directory)
        preview = SkillInstaller(
            preview_root / "skills",
            state_store=RuntimeStateStore(preview_root / "run"),
        ).install(archive, expected_sha256=expected_sha256)
        if expected_version is not None and preview.version != expected_version:
            raise RuntimeError(
                f"Registry returned Skill {preview.name!r} version "
                f"{preview.version!r}; requested version was {expected_version!r}"
            )
        node_installer = NodeInstaller(
            paths.forge_runtime, state_store=RuntimeStateStore(paths.state)
        )
        missing = [
            (node_id, lock)
            for node_id, lock in sorted(preview.artifacts.nodes.items())
            if not node_installer.satisfies(lock)
        ]
        if missing and (index is not None or preview.artifacts.resolver == "registry"):
            source = (source_factory or _default_source_factory(index))()
            cache = DownloadCache(root=paths.cache, progress=progress, client=download_client)
            try:
                with source:
                    for node_id, lock in missing:
                        node_artifact = source.node(
                            lock.artifact_id,
                            expected_sha256=lock.sha256,
                        )
                        if node_artifact.sha256 != lock.sha256:
                            raise RegistryError(
                                f"Registry digest for Node {node_id!r} does not match "
                                "Skill lock"
                            )
                        node_archive = cache.download(node_artifact)
                        installed = node_installer.install(node_archive, lock)
                        if not node_installer.satisfies(lock):
                            raise RuntimeError(
                                f"Node executable {installed!s} does not satisfy Skill lock"
                            )
                missing = []
            finally:
                cache.close()
        try:
            local = SkillCatalog(paths.bundles).get(preview.name)
        except SkillNotFoundError:
            local = None
        if local == preview:
            return InstallOutcome(
                status="already_ready",
                manifest=preview,
                missing_nodes=tuple(node_id for node_id, _ in missing),
            )
        manifest = SkillInstaller(
            paths.bundles, state_store=RuntimeStateStore(paths.state)
        ).install(archive, expected_sha256=expected_sha256)
        return InstallOutcome(
            status="installed",
            manifest=manifest,
            missing_nodes=tuple(node_id for node_id, _ in missing),
        )


def remove_skill(name: str, *, paths: SkillServicePaths) -> None:
    """Remove an installed, stopped Skill."""
    SkillInstaller(
        paths.bundles, state_store=RuntimeStateStore(paths.state)
    ).remove(name)


def _default_source_factory(index: str | None) -> Callable[[], Any]:
    def factory() -> Any:
        return StaticPackageIndex(index) if index else RegistryClient()

    return factory


def installed_version(bundles_root: Path, name: str) -> str | None:
    """Best-effort installed version lookup for pre-checks."""
    try:
        return SkillCatalog(bundles_root).get(name).version
    except Exception:
        return None


def read_manifest_version(bundle_root: Path) -> str | None:
    """Read ``version`` from a raw skill.yaml without full validation."""
    manifest = bundle_root / "skill.yaml"
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if isinstance(data, dict) and isinstance(data.get("version"), str):
        return data["version"]
    return None

"""Transactional installers for Skill bundles and Forge Runtime artifact sets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from PhyAgentOS.config.paths import get_forge_runtime_root, get_skill_bundle_root
from PhyAgentOS.skill_runtime.archive import (
    ArchiveError,
    ArchiveLimits,
    ArchiveValidator,
    sha256_file,
)
from PhyAgentOS.skill_runtime.locking import SkillOperationBusyError, SkillOperationLock
from PhyAgentOS.skill_runtime.manifest import NodeLock, SkillManifest, load_manifest
from PhyAgentOS.skill_runtime.runtime_manifest import normalize_arch, normalize_platform
from PhyAgentOS.skill_runtime.state import RuntimeStateStore


class InstallerError(RuntimeError):
    """Raised when an installation cannot be safely committed."""


# Forge nodes carry their own runtime tree (for example PyInstaller onedir
# bundles), so directory archives need room beyond the Skill bundle defaults.
_NODE_ARCHIVE_LIMITS = ArchiveLimits(
    max_files=100_000,
    max_file_size=2 * 1024 * 1024 * 1024,
    max_total_size=32 * 1024 * 1024 * 1024,
)


def _payload_root(extracted: Path, required: str) -> Path:
    if (extracted / required).is_file():
        return extracted
    raise InstallerError(f"archive root must contain {required}")


def _entrypoint_path(lock: NodeLock, install_root: Path) -> Path:
    """Resolve the executable inside an installed node version directory."""
    if lock.artifact_type == "directory_tar_gz":
        return install_root / lock.entrypoint / lock.entrypoint
    return install_root / lock.entrypoint


def _active_skills(store: RuntimeStateStore) -> list[str]:
    if not store.root.is_dir():
        return []
    active = []
    for path in store.root.glob("*.json"):
        try:
            state = store.load(path.stem)
        except Exception:
            active.append(path.stem)
            continue
        if state is not None and (
            state.status in {"starting", "running", "stopping"}
            or state.active_invocations
            or state.active_sessions
            or state.active_task_bindings
        ):
            active.append(state.skill_name)
    return sorted(active)


class SkillInstaller:
    """Install or remove a Skill without exposing a partially validated bundle."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        validator: ArchiveValidator | None = None,
        state_store: RuntimeStateStore | None = None,
    ) -> None:
        self.root = (root or get_skill_bundle_root()).expanduser()
        self.validator = validator or ArchiveValidator()
        self.state_store = state_store or RuntimeStateStore()

    def install(
        self,
        archive: Path,
        *,
        expected_sha256: str | None = None,
        verify_archive_manifest: bool = True,
    ) -> SkillManifest:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".skill-install-", dir=self.root))
        extracted = temporary / "extracted"
        target: Path | None = None
        backup: Path | None = None
        committed = False
        try:
            self.validator.extract(
                archive,
                extracted,
                expected_sha256=expected_sha256,
                verify_manifest=verify_archive_manifest,
            )
            payload = _payload_root(extracted, "skill.yaml")
            if not (payload / "SKILL.md").is_file():
                raise InstallerError("Skill archive root must contain SKILL.md")
            # ``load_manifest`` requires the directory name to equal the Skill name.
            try:
                data = yaml.safe_load((payload / "skill.yaml").read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise InstallerError("cannot parse Skill manifest") from exc
            if not isinstance(data, dict) or not isinstance(data.get("name"), str):
                raise InstallerError("Skill manifest does not contain a valid name")
            normalized = temporary / data["name"]
            if normalized.exists():
                raise InstallerError("Skill archive has an ambiguous root")
            os.replace(payload, normalized)
            manifest = load_manifest(normalized / "skill.yaml")
            if manifest.skill_document != Path("SKILL.md"):
                raise InstallerError("installed Skill skill_document must be SKILL.md")
            try:
                with SkillOperationLock(self.state_store.root, manifest.name):
                    target = self.root / manifest.name
                    if target.exists():
                        if manifest.name in _active_skills(self.state_store):
                            raise InstallerError(f"Skill {manifest.name!r} is currently running")
                        try:
                            old_version = load_manifest(target / "skill.yaml").version
                        except Exception:
                            try:
                                legacy = yaml.safe_load(
                                    (target / "skill.yaml").read_text(encoding="utf-8")
                                )
                                old_version = str(legacy.get("version", "legacy"))
                            except Exception:
                                old_version = "legacy"
                        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                        backup = (
                            self.root
                            / ".backups"
                            / manifest.name
                            / f"{old_version}-{stamp}"
                        )
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(target, backup)
                    try:
                        os.replace(normalized, target)
                        installed = load_manifest(target / "skill.yaml")
                    except Exception:
                        if target is not None and target.exists():
                            failed = temporary / ".failed-install"
                            os.replace(target, failed)
                        if backup is not None and target is not None:
                            os.replace(backup, target)
                        raise
                    committed = True
                    return installed
            except SkillOperationBusyError as exc:
                raise InstallerError(str(exc)) from exc
        except InstallerError:
            raise
        except Exception as exc:
            raise InstallerError(f"Skill installation failed: {exc}") from exc
        finally:
            if not committed and backup is not None and target is not None and not target.exists():
                os.replace(backup, target)
            shutil.rmtree(temporary, ignore_errors=True)

    def remove(self, name: str) -> None:
        try:
            with SkillOperationLock(self.state_store.root, name):
                target = self.root / name
                if not target.is_dir():
                    raise InstallerError(f"Skill {name!r} is not installed")
                if name in _active_skills(self.state_store):
                    raise InstallerError(f"Skill {name!r} is currently running")
                temporary = self.root / f".remove-{name}-{os.getpid()}"
                os.replace(target, temporary)
                try:
                    shutil.rmtree(temporary)
                except Exception:
                    if not target.exists():
                        os.replace(temporary, target)
                    raise
        except (SkillOperationBusyError, ValueError) as exc:
            raise InstallerError(str(exc)) from exc


class NodeInstaller:
    """Install SHA-256-pinned node ``tar.gz`` assets.

    ``executable_tar_gz`` archives hold a single root-level executable;
    ``directory_tar_gz`` archives hold one root directory named after the
    entrypoint that contains the executable and its runtime tree.
    """

    receipt_name = ".paos-node.json"
    artifact_types = ("executable_tar_gz", "directory_tar_gz")

    def __init__(
        self,
        root: Path | None = None,
        *,
        state_store: RuntimeStateStore | None = None,
    ) -> None:
        runtime_root = (root or get_forge_runtime_root()).expanduser()
        self.root = runtime_root / "nodes"
        self.state_store = state_store or RuntimeStateStore()

    def install(self, archive: Path, lock: NodeLock) -> Path:
        active = _active_skills(self.state_store)
        if active:
            raise InstallerError(
                f"cannot install Forge nodes while Skills are running: {', '.join(active)}"
            )
        self._verify_lock_host(lock)
        if not archive.is_file() or archive.is_symlink():
            raise InstallerError("downloaded Forge node archive is not a regular file")
        if sha256_file(archive) != lock.sha256:
            raise InstallerError("downloaded Forge node archive sha256 does not match Skill lock")

        versions = self.root / lock.node_id / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        target = versions / lock.artifact_id
        if target.exists():
            if self.satisfies(lock):
                return _entrypoint_path(lock, target)
            raise InstallerError("installed node artifact ID has different contents")

        temporary = Path(tempfile.mkdtemp(prefix=".node-install-", dir=versions))
        try:
            if lock.artifact_type == "directory_tar_gz":
                self._extract_directory(archive, temporary, lock)
            else:
                staged = temporary / lock.entrypoint
                self._extract_executable(archive, staged, lock)
            staged = _entrypoint_path(lock, temporary)
            staged.chmod(0o755)
            receipt = {
                "schema_version": 1,
                "node_id": lock.node_id,
                "artifact_id": lock.artifact_id,
                "artifact_type": lock.artifact_type,
                "entrypoint": lock.entrypoint,
                "archive_sha256": lock.sha256,
                "binary_sha256": sha256_file(staged),
            }
            (temporary / self.receipt_name).write_text(
                json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, target)
            return _entrypoint_path(lock, target)
        except InstallerError:
            raise
        except Exception as exc:
            raise InstallerError(f"Forge node installation failed: {exc}") from exc
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def load(self, lock: NodeLock) -> Path:
        self._verify_lock_host(lock)
        install_root = self.root / lock.node_id / "versions" / lock.artifact_id
        path = _entrypoint_path(lock, install_root)
        receipt_path = install_root / self.receipt_name
        if not path.is_file() or path.is_symlink():
            raise InstallerError("installed Forge node executable is missing")
        if path.stat().st_mode & 0o111 == 0:
            raise InstallerError("installed Forge node is not executable")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallerError("installed Forge node receipt is missing or invalid") from exc
        expected = {
            "schema_version": 1,
            "node_id": lock.node_id,
            "artifact_id": lock.artifact_id,
            "artifact_type": lock.artifact_type,
            "entrypoint": lock.entrypoint,
            "archive_sha256": lock.sha256,
        }
        if not isinstance(receipt, dict) or any(
            receipt.get(name) != value for name, value in expected.items()
        ):
            raise InstallerError("installed Forge node receipt does not match Skill lock")
        binary_sha256 = receipt.get("binary_sha256")
        if (
            not isinstance(binary_sha256, str)
            or len(binary_sha256) != 64
            or sha256_file(path) != binary_sha256
        ):
            raise InstallerError("installed Forge node executable sha256 does not match receipt")
        return path

    def satisfies(self, lock: NodeLock) -> bool:
        try:
            self.load(lock)
        except InstallerError:
            return False
        return True

    @staticmethod
    def _verify_lock_host(lock: NodeLock) -> None:
        if lock.artifact_type not in NodeInstaller.artifact_types:
            raise InstallerError(f"unsupported Forge node artifact type: {lock.artifact_type}")
        if lock.platform != normalize_platform() or lock.arch != normalize_arch():
            raise InstallerError(
                f"node platform/arch {lock.platform}/{lock.arch} does not match host "
                f"{normalize_platform()}/{normalize_arch()}"
            )

    @staticmethod
    def _extract_executable(archive: Path, output: Path, lock: NodeLock) -> None:
        try:
            with tarfile.open(archive, mode="r:gz") as bundle:
                files = []
                for member in bundle.getmembers():
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise InstallerError("Forge node archive may contain only one regular file")
                    files.append(member)
                if len(files) != 1:
                    raise InstallerError(
                        "Forge node archive must contain exactly one executable file"
                    )
                member = files[0]
                normalized_name = member.name.removeprefix("./")
                if "/" in normalized_name or "\\" in normalized_name:
                    raise InstallerError("Forge node executable must be at the archive root")
                if normalized_name != lock.entrypoint:
                    raise InstallerError(
                        "Forge node archive filename does not match Skill entrypoint"
                    )
                source = bundle.extractfile(member)
                if source is None:
                    raise InstallerError("cannot read Forge node executable from archive")
                with source, output.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
        except InstallerError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise InstallerError(f"invalid Forge node tar.gz archive: {exc}") from exc

    @staticmethod
    def _extract_directory(archive: Path, staging: Path, lock: NodeLock) -> None:
        """Extract a ``directory_tar_gz`` tree into ``staging``.

        The archive must contain exactly one root directory named after the
        lock entrypoint; the executable lives at
        ``<entrypoint>/<entrypoint>`` inside that tree.
        """
        payload = staging / ".payload"
        validator = ArchiveValidator(_NODE_ARCHIVE_LIMITS, allow_internal_links=True)
        try:
            validator.extract(archive, payload, verify_manifest=False)
        except ArchiveError as exc:
            raise InstallerError(f"invalid Forge node tar.gz archive: {exc}") from exc
        root = payload / lock.entrypoint
        try:
            entries = [entry.name for entry in payload.iterdir()]
            if entries != [lock.entrypoint]:
                raise InstallerError(
                    "Forge node directory archive must contain exactly one root "
                    f"directory named after the entrypoint: {', '.join(sorted(entries))}"
                )
            if not root.is_dir() or root.is_symlink():
                raise InstallerError(
                    "Forge node directory archive root must be a real directory"
                )
            executable = root / lock.entrypoint
            if executable.is_symlink() or not executable.is_file():
                raise InstallerError(
                    f"Forge node directory archive must contain executable "
                    f"'{lock.entrypoint}/{lock.entrypoint}'"
                )
            os.replace(root, staging / lock.entrypoint)
        finally:
            shutil.rmtree(payload, ignore_errors=True)


def _inject_node_environment(
    rendered: str,
    profile_environment: dict[str, str],
    runtime_environment: dict[str, str],
) -> str:
    """Bind the launching runtime's environment to every Dora node.

    Dora nodes inherit the environment of the daemon that spawns them, so a
    pre-existing shared daemon can supply a stale HOME or
    PAOS_SKILL_VERSION. Node-declared values win over the profile-level
    environment, and runtime-derived identity wins over both.
    """
    try:
        document = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise InstallerError(f"rendered Skill dataflow is not valid YAML: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
        raise InstallerError("rendered Skill dataflow must define a nodes list")
    profile_values = {str(key): str(value) for key, value in profile_environment.items()}
    runtime_values = {str(key): str(value) for key, value in runtime_environment.items()}
    for node in document["nodes"]:
        if not isinstance(node, dict):
            raise InstallerError("rendered Skill dataflow node must be a mapping")
        declared = node.get("env") or {}
        if not isinstance(declared, dict):
            raise InstallerError("rendered Skill dataflow node env must be a mapping")
        declared_values = {str(key): str(value) for key, value in declared.items()}
        node["env"] = {**profile_values, **declared_values, **runtime_values}
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


class SkillEnvironmentBuilder:
    """Create immutable per-Skill executable views from exact node locks."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        state_store: RuntimeStateStore | None = None,
    ) -> None:
        self.runtime_root = (root or get_forge_runtime_root()).expanduser()
        self.environments = self.runtime_root / "environments"
        self.nodes = NodeInstaller(self.runtime_root, state_store=state_store)

    def prepare(self, skill: SkillManifest, profile_name: str) -> Path:
        profile = skill.profiles.get(profile_name)
        if profile is None:
            raise InstallerError(f"unknown Skill profile: {profile_name}")
        providers: dict[str, tuple[NodeLock, Path]] = {}
        for _, lock in sorted(skill.artifacts.nodes.items()):
            binary = self.nodes.load(lock)
            if lock.entrypoint in providers:
                raise InstallerError(f"duplicate Forge node entrypoint: {lock.entrypoint}")
            providers[lock.entrypoint] = (lock, binary)
        required = {path.as_posix() for path in profile.required_binaries}
        missing = sorted(required - providers.keys())
        if missing:
            raise InstallerError(
                f"Skill profile requires unavailable binaries: {', '.join(missing)}"
            )

        lock_value = {
            "manifest_version": 1,
            "skill": skill.name,
            "skill_version": skill.version,
            "profile": profile_name,
            "nodes": {
                lock.node_id: {
                    "artifact_id": lock.artifact_id,
                    "artifact_type": lock.artifact_type,
                    "entrypoint": lock.entrypoint,
                    "sha256": lock.sha256,
                }
                for lock, _ in providers.values()
            },
            "entrypoints": sorted(required),
            "dataflow": profile.dataflow.as_posix(),
            "environment": {
                str(key): str(value) for key, value in profile.environment.items()
            },
        }
        profile_files: dict[str, str] = {}
        profile_parent = skill.bundle_root / profile.dataflow.parent
        for source in sorted(profile_parent.iterdir()):
            if source.is_file():
                profile_files[source.name] = sha256_file(source)
        lock_value["profile_files"] = profile_files
        encoded = json.dumps(
            lock_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        lock_digest = hashlib.sha256(encoded).hexdigest()
        profile_root = self.environments / skill.name / profile_name
        target = profile_root / lock_digest
        rendered_dataflow = target / "launch" / profile.dataflow
        if target.exists() and not rendered_dataflow.is_file():
            shutil.rmtree(target)
        if not target.exists():
            profile_root.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".environment-", dir=profile_root))
            try:
                bin_dir = temporary / "bin"
                bin_dir.mkdir()
                for name in sorted(required):
                    _, source = providers[name]
                    os.symlink(os.path.relpath(source, bin_dir), bin_dir / name)
                (temporary / "runtime-lock.json").write_bytes(encoded)
                launch_root = temporary / "launch"
                launch_profile = launch_root / profile.dataflow.parent
                launch_profile.mkdir(parents=True)
                source_profile = skill.bundle_root / profile.dataflow.parent
                for source in source_profile.iterdir():
                    if not source.is_file() or source.name == profile.dataflow.name:
                        continue
                    os.symlink(
                        os.path.relpath(source, launch_profile),
                        launch_profile / source.name,
                    )
                assets = skill.bundle_root / "assets"
                if assets.is_dir():
                    os.symlink(os.path.relpath(assets, launch_root), launch_root / "assets")
                source_dataflow = skill.resolve_bundle_path(profile.dataflow)
                rendered = source_dataflow.read_text(encoding="utf-8").replace(
                    "${FORGE_RUNTIME_BIN}", str((target / "bin").resolve())
                ).replace("${PAOS_SKILL_ROOT}", str(skill.bundle_root))
                rendered = rendered.replace("${PAOS_SKILL_NAME}", skill.name).replace(
                    "${PAOS_SKILL_VERSION}", skill.version
                )
                rendered = _inject_node_environment(
                    rendered,
                    profile.environment,
                    {
                        "HOME": str(Path.home()),
                        "PAOS_SKILL_NAME": skill.name,
                        "PAOS_SKILL_VERSION": skill.version,
                        "PAOS_SKILL_ROOT": str(skill.bundle_root),
                        "FORGE_RUNTIME_BIN": str((target / "bin").resolve()),
                    },
                )
                (launch_profile / profile.dataflow.name).write_text(
                    rendered, encoding="utf-8"
                )
                os.replace(temporary, target)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        self._replace_symlink(profile_root / "current", target)
        return target / "bin"

    @staticmethod
    def _replace_symlink(link: Path, target: Path) -> None:
        temporary = link.parent / f".{link.name}.{os.getpid()}.tmp"
        temporary.unlink(missing_ok=True)
        os.symlink(os.path.relpath(target, link.parent), temporary)
        os.replace(temporary, link)

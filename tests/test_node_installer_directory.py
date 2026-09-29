from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest
import yaml

from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.skill_runtime.installer import (
    InstallerError,
    NodeInstaller,
    SkillEnvironmentBuilder,
)
from PhyAgentOS.skill_runtime.manifest import ManifestError, NodeLock, load_manifest
from PhyAgentOS.skill_runtime.runtime_manifest import normalize_arch, normalize_platform
from PhyAgentOS.skill_runtime.state import RuntimeStateStore


def _file_member(archive: tarfile.TarFile, name: str, data: bytes, *, mode: int = 0o755) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    archive.addfile(info, io.BytesIO(data))


def _dir_member(archive: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    archive.addfile(info)


def _link_member(
    archive: tarfile.TarFile, name: str, target: str, *, hard: bool = False
) -> None:
    info = tarfile.TarInfo(name)
    if hard:
        info.type = tarfile.LNKTYPE
        info.linkname = target
    else:
        info.type = tarfile.SYMTYPE
        info.linkname = target
    info.mode = 0o777
    archive.addfile(info)


def _directory_archive(path: Path) -> str:
    """A minimal well-formed onedir archive: lerobot_infer/lerobot_infer + lib tree."""
    with tarfile.open(path, "w:gz") as archive:
        _dir_member(archive, "lerobot_infer")
        _dir_member(archive, "lerobot_infer/_internal")
        _dir_member(archive, "lerobot_infer/_internal/libs")
        _file_member(archive, "lerobot_infer/lerobot_infer", b"#!/bin/sh\nexit 0\n")
        _file_member(
            archive, "lerobot_infer/_internal/libs/libdemo.so", b"lib-bytes", mode=0o644
        )
        _link_member(archive, "lerobot_infer/_internal/libdemo.so", "libs/libdemo.so")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock(sha256: str, *, artifact_type: str = "directory_tar_gz") -> NodeLock:
    return NodeLock.from_dict(
        "lerobot_inference",
        {
            "artifact_id": "lerobot_inference-1.0.5-linux-x86_64",
            "version": "1.0.5",
            "platform": normalize_platform(),
            "arch": normalize_arch(),
            "artifact_type": artifact_type,
            "entrypoint": "lerobot_infer",
            "sha256": sha256,
        },
    )


def _installer(tmp_path: Path) -> NodeInstaller:
    return NodeInstaller(
        tmp_path / "runtime", state_store=RuntimeStateStore(tmp_path / "states")
    )


def _version_dir(installer: NodeInstaller, lock: NodeLock) -> Path:
    return installer.root / lock.node_id / "versions" / lock.artifact_id


# ---------------------------------------------------------------------------
# executable_tar_gz regression
# ---------------------------------------------------------------------------


def test_executable_archive_still_installs_at_version_root(tmp_path: Path) -> None:
    archive_path = tmp_path / "node.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        _file_member(archive, "lerobot_infer", b"single-binary")
    lock = _lock(
        hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        artifact_type="executable_tar_gz",
    )
    installer = _installer(tmp_path)

    installed = installer.install(archive_path, lock)

    assert installed == _version_dir(installer, lock) / "lerobot_infer"
    assert installed.read_bytes() == b"single-binary"
    assert installer.load(lock) == installed
    assert installer.satisfies(lock)
    assert (_version_dir(installer, lock) / ".paos-node.json").is_file()


# ---------------------------------------------------------------------------
# directory_tar_gz happy path
# ---------------------------------------------------------------------------


def test_directory_archive_installs_tree_and_returns_nested_entrypoint(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "node.tar.gz"
    lock = _lock(_directory_archive(archive_path))
    installer = _installer(tmp_path)

    installed = installer.install(archive_path, lock)

    entrypoint = _version_dir(installer, lock) / "lerobot_infer" / "lerobot_infer"
    assert installed == entrypoint
    assert entrypoint.is_file()
    assert os.access(entrypoint, os.X_OK)
    link = _version_dir(installer, lock) / "lerobot_infer/_internal/libdemo.so"
    assert link.is_symlink()
    assert link.resolve().name == "libdemo.so"
    assert (_version_dir(installer, lock) / ".paos-node.json").is_file()
    assert not (_version_dir(installer, lock) / ".payload").exists()
    assert installer.satisfies(lock)
    assert installer.load(lock) == entrypoint


def test_directory_install_is_idempotent(tmp_path: Path) -> None:
    archive_path = tmp_path / "node.tar.gz"
    lock = _lock(_directory_archive(archive_path))
    installer = _installer(tmp_path)

    first = installer.install(archive_path, lock)
    second = installer.install(archive_path, lock)

    assert first == second


def test_directory_receipt_pins_nested_binary_sha256(tmp_path: Path) -> None:
    archive_path = tmp_path / "node.tar.gz"
    lock = _lock(_directory_archive(archive_path))
    installer = _installer(tmp_path)

    installer.install(archive_path, lock)

    receipt_text = (_version_dir(installer, lock) / ".paos-node.json").read_text(
        encoding="utf-8"
    )
    receipt = yaml.safe_load(receipt_text)
    assert receipt["binary_sha256"] == hashlib.sha256(b"#!/bin/sh\nexit 0\n").hexdigest()
    # Tampering with the executable must fail the load-time verification.
    entrypoint = _version_dir(installer, lock) / "lerobot_infer" / "lerobot_infer"
    entrypoint.write_bytes(b"tampered")
    assert not installer.satisfies(lock)


# ---------------------------------------------------------------------------
# directory_tar_gz rejections
# ---------------------------------------------------------------------------


def _rejects(tmp_path: Path, lock: NodeLock, *, match: str, build) -> None:
    archive_path = tmp_path / "node.tar.gz"
    build(archive_path)
    broken = _lock("0" * 64)
    object.__setattr__(broken, "sha256", hashlib.sha256(archive_path.read_bytes()).hexdigest())
    # Rebuild the lock with the real digest of the crafted archive.
    real = _lock(hashlib.sha256(archive_path.read_bytes()).hexdigest())
    object.__setattr__(real, "artifact_type", lock.artifact_type)
    object.__setattr__(real, "entrypoint", lock.entrypoint)
    with pytest.raises(InstallerError, match=match):
        _installer(tmp_path).install(archive_path, real)


def test_directory_archive_rejects_wrong_root_name(tmp_path: Path) -> None:
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "wrong_name")
            _file_member(archive, "wrong_name/lerobot_infer", b"#!/bin/sh\nexit 0\n")

    _rejects(tmp_path, _lock("0" * 64), match="one root", build=build)


def test_directory_archive_rejects_multiple_root_entries(tmp_path: Path) -> None:
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "lerobot_infer")
            _file_member(archive, "lerobot_infer/lerobot_infer", b"#!/bin/sh\nexit 0\n")
            _file_member(archive, "extra", b"stray", mode=0o644)

    _rejects(tmp_path, _lock("0" * 64), match="one root", build=build)


def test_directory_archive_rejects_missing_nested_entrypoint(tmp_path: Path) -> None:
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "lerobot_infer")
            _file_member(
                archive, "lerobot_infer/_internal/data", b"data", mode=0o644
            )

    _rejects(tmp_path, _lock("0" * 64), match="must contain executable", build=build)


def test_directory_archive_rejects_symlinked_entrypoint(tmp_path: Path) -> None:
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "lerobot_infer")
            _file_member(
                archive, "lerobot_infer/_internal/real", b"#!/bin/sh\nexit 0\n"
            )
            _link_member(archive, "lerobot_infer/lerobot_infer", "_internal/real")

    _rejects(tmp_path, _lock("0" * 64), match="must contain executable", build=build)


def test_directory_archive_rejects_escaping_symlink(tmp_path: Path) -> None:
    # Three levels up from a depth-2 member resolves above the archive root;
    # two levels (../../outside) only reach the root and are caught later by
    # the dangling-target check instead.
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "lerobot_infer")
            _file_member(archive, "lerobot_infer/lerobot_infer", b"#!/bin/sh\nexit 0\n")
            _link_member(archive, "lerobot_infer/_internal/escape", "../../../outside")

    _rejects(tmp_path, _lock("0" * 64), match="inside the archive", build=build)


def test_directory_archive_accepts_dotdot_symlink_staying_inside(tmp_path: Path) -> None:
    # `libs/x.so -> ../libx.so` is a common layout in real runtime trees and
    # resolves inside the archive; containment is judged on the resolved
    # target, not on the mere presence of "..".
    archive_path = tmp_path / "node.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        _dir_member(archive, "lerobot_infer")
        _dir_member(archive, "lerobot_infer/_internal")
        _dir_member(archive, "lerobot_infer/_internal/libs")
        _file_member(archive, "lerobot_infer/lerobot_infer", b"#!/bin/sh\nexit 0\n")
        _file_member(archive, "lerobot_infer/_internal/libx.so", b"lib-bytes", mode=0o644)
        _link_member(archive, "lerobot_infer/_internal/libs/libx.so", "../libx.so")
    lock = _lock(hashlib.sha256(archive_path.read_bytes()).hexdigest())
    installer = _installer(tmp_path)

    installed = installer.install(archive_path, lock)

    link = _version_dir(installer, lock) / "lerobot_infer/_internal/libs/libx.so"
    assert link.is_symlink()
    assert link.resolve().name == "libx.so"
    assert installed.is_file()


def test_directory_archive_rejects_absolute_symlink(tmp_path: Path) -> None:
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "lerobot_infer")
            _file_member(archive, "lerobot_infer/lerobot_infer", b"#!/bin/sh\nexit 0\n")
            _link_member(archive, "lerobot_infer/_internal/abs", "/etc/passwd")

    _rejects(tmp_path, _lock("0" * 64), match="relative", build=build)


def test_directory_archive_rejects_dangling_symlink(tmp_path: Path) -> None:
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "lerobot_infer")
            _file_member(archive, "lerobot_infer/lerobot_infer", b"#!/bin/sh\nexit 0\n")
            _link_member(archive, "lerobot_infer/_internal/dangling", "missing.so")

    _rejects(tmp_path, _lock("0" * 64), match="dangling or cyclic", build=build)


def test_directory_archive_rejects_cyclic_symlinks(tmp_path: Path) -> None:
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "lerobot_infer")
            _file_member(archive, "lerobot_infer/lerobot_infer", b"#!/bin/sh\nexit 0\n")
            _link_member(archive, "lerobot_infer/_internal/a", "b")
            _link_member(archive, "lerobot_infer/_internal/b", "a")

    _rejects(tmp_path, _lock("0" * 64), match="dangling or cyclic", build=build)


def test_directory_archive_rejects_hard_links(tmp_path: Path) -> None:
    def build(path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            _dir_member(archive, "lerobot_infer")
            _file_member(archive, "lerobot_infer/lerobot_infer", b"#!/bin/sh\nexit 0\n")
            _file_member(
                archive, "lerobot_infer/_internal/real", b"real", mode=0o644
            )
            _link_member(archive, "lerobot_infer/_internal/hard", "_internal/real", hard=True)

    _rejects(tmp_path, _lock("0" * 64), match="hard links are forbidden", build=build)


def test_directory_archive_rejects_sha256_mismatch(tmp_path: Path) -> None:
    archive_path = tmp_path / "node.tar.gz"
    _directory_archive(archive_path)
    lock = _lock("f" * 64)

    with pytest.raises(InstallerError, match="sha256 does not match"):
        _installer(tmp_path).install(archive_path, lock)


# ---------------------------------------------------------------------------
# manifest parsing
# ---------------------------------------------------------------------------


def test_manifest_accepts_directory_tar_gz_locks() -> None:
    lock = _lock("a" * 64)

    assert lock.artifact_type == "directory_tar_gz"


def test_manifest_still_rejects_unknown_artifact_types() -> None:
    with pytest.raises(ManifestError, match="reserved for future installers"):
        NodeLock.from_dict(
            "gateway",
            {
                "artifact_id": "gateway-one",
                "version": "1.0.0",
                "platform": normalize_platform(),
                "arch": normalize_arch(),
                "artifact_type": "archive",
                "entrypoint": "gateway",
                "sha256": "0" * 64,
            },
        )


# ---------------------------------------------------------------------------
# SkillEnvironmentBuilder integration
# ---------------------------------------------------------------------------


def _installed_skill_with_directory_node(tmp_path: Path) -> tuple[Path, NodeLock]:
    archive_path = tmp_path / "node.tar.gz"
    lock = _lock(_directory_archive(archive_path))
    _installer(tmp_path).install(archive_path, lock)

    bundle = tmp_path / "skills" / "demo"
    (bundle / "profiles" / "local").mkdir(parents=True)
    (bundle / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    manifest = {
        "manifest_version": 2,
        "name": "demo",
        "version": "1.0.0",
        "description": "Directory node integration",
        "skill_document": "SKILL.md",
        "gateway_url": "http://127.0.0.1:19002",
        "required_tools": ["demo.run"],
        "profiles": {
            "local": {
                "dataflow": "profiles/local/dataflow.yaml",
                "required_binaries": ["lerobot_infer"],
            }
        },
        "artifacts": {
            "resolver": "local",
            "nodes": {"lerobot_inference": yaml.safe_load(yaml.safe_dump(
                {
                    "artifact_id": lock.artifact_id,
                    "version": lock.version,
                    "platform": lock.platform,
                    "arch": lock.arch,
                    "artifact_type": lock.artifact_type,
                    "entrypoint": lock.entrypoint,
                    "sha256": lock.sha256,
                }
            ))},
        },
    }
    (bundle / "skill.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (bundle / "profiles" / "local" / "dataflow.yaml").write_text(
        "nodes: []\n", encoding="utf-8"
    )
    return bundle, lock


def test_environment_builder_links_directory_entrypoint_into_bin(tmp_path: Path) -> None:
    bundle, lock = _installed_skill_with_directory_node(tmp_path)
    skill = load_manifest(bundle / "skill.yaml")
    runtime_root = tmp_path / "runtime"
    builder = SkillEnvironmentBuilder(
        runtime_root, state_store=RuntimeStateStore(tmp_path / "states")
    )

    binary_root = builder.prepare(skill, "local")

    link = binary_root / "lerobot_infer"
    assert link.is_symlink()
    assert link.resolve() == (
        runtime_root
        / "nodes"
        / lock.node_id
        / "versions"
        / lock.artifact_id
        / "lerobot_infer"
        / "lerobot_infer"
    )
    assert os.access(link, os.X_OK)


def test_catalog_loads_skill_with_directory_lock(tmp_path: Path) -> None:
    bundle, _lock = _installed_skill_with_directory_node(tmp_path)

    skill = SkillCatalog(tmp_path / "skills").get("demo")

    assert skill.artifacts.nodes["lerobot_inference"].artifact_type == "directory_tar_gz"

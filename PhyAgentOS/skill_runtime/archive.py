"""Validation and safe extraction for downloaded ``tar.gz`` artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import tarfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class ArchiveError(ValueError):
    """Raised when an archive is malformed, unsafe, or fails integrity checks."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_files: int = 10_000
    max_file_size: int = 512 * 1024 * 1024
    max_total_size: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: float = 200.0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in name
        or "\\" in name
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise ArchiveError(f"unsafe archive path: {name!r}")
    normalized = PurePosixPath(*(part for part in path.parts if part not in {"", "."}))
    if not normalized.parts:
        raise ArchiveError(f"unsafe archive path: {name!r}")
    return normalized


def _parse_file_manifest(value: Any) -> dict[str, tuple[str, int | None]]:
    if not isinstance(value, dict):
        raise ArchiveError("archive manifest must be a JSON object")
    raw_files = value.get("files")
    result: dict[str, tuple[str, int | None]] = {}
    if isinstance(raw_files, dict):
        iterable = [
            {"path": path, **({"sha256": item} if isinstance(item, str) else item)}
            for path, item in raw_files.items()
            if isinstance(item, (str, dict))
        ]
    elif isinstance(raw_files, list):
        iterable = raw_files
    else:
        raise ArchiveError("archive manifest files must be a list or mapping")
    for item in iterable:
        if not isinstance(item, dict):
            raise ArchiveError("archive manifest file entries must be objects")
        path = _safe_path(item.get("path") if isinstance(item.get("path"), str) else "").as_posix()
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
        ):
            raise ArchiveError(f"invalid sha256 for {path}")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise ArchiveError(f"invalid size for {path}")
        if path in result:
            raise ArchiveError(f"duplicate archive manifest path: {path}")
        result[path] = (digest.lower(), size)
    return result


class ArchiveValidator:
    """Reject unsafe tar members and verify an embedded per-file manifest."""

    manifest_names = ("archive-manifest.json", ".paos-manifest.json")

    def __init__(
        self,
        limits: ArchiveLimits | None = None,
        *,
        allow_internal_links: bool = False,
    ) -> None:
        self.limits = limits or ArchiveLimits()
        self.allow_internal_links = allow_internal_links

    def extract(
        self,
        archive: Path,
        destination: Path,
        *,
        expected_sha256: str | None = None,
        verify_manifest: bool = True,
    ) -> Path:
        if expected_sha256 and sha256_file(archive) != expected_sha256.lower():
            raise ArchiveError("archive sha256 mismatch")
        compressed_size = archive.stat().st_size
        if compressed_size <= 0:
            raise ArchiveError("archive is empty")
        destination.mkdir(parents=True, exist_ok=False)
        try:
            with tarfile.open(archive, mode="r:gz") as tar:
                members = tar.getmembers()
                if len(members) > self.limits.max_files:
                    raise ArchiveError("archive exceeds member count limit")
                files: dict[str, tarfile.TarInfo] = {}
                links: dict[str, str] = {}
                seen: set[str] = set()
                collision_keys: dict[str, str] = {}
                total_size = 0
                for member in members:
                    path = _safe_path(member.name).as_posix()
                    if path in seen:
                        raise ArchiveError(f"duplicate archive path: {path}")
                    seen.add(path)
                    collision_key = unicodedata.normalize("NFC", path).casefold()
                    previous = collision_keys.get(collision_key)
                    if previous is not None:
                        raise ArchiveError(
                            f"archive paths collide after normalization: {previous}, {path}"
                        )
                    collision_keys[collision_key] = path
                    if member.islnk():
                        raise ArchiveError(f"hard links are forbidden in archives: {path}")
                    if member.issym():
                        if not self.allow_internal_links:
                            raise ArchiveError(f"links are forbidden in archives: {path}")
                        target = member.linkname
                        if (
                            not target
                            or target.startswith("/")
                            or "\x00" in target
                            or "\\" in target
                            or PurePosixPath(target).is_absolute()
                        ):
                            raise ArchiveError(
                                f"symbolic link must be relative: {path} -> {target!r}"
                            )
                        # Containment judged on the lexically resolved target, not on
                        # the mere presence of "..": `lib/x.so -> ../libx.so` resolves
                        # inside the archive and is a common layout in real runtime
                        # trees, while `lib/x.so -> ../../etc/passwd` must be refused.
                        # posixpath.normpath, not os.path: member names are POSIX
                        # regardless of the host running the check.
                        resolved = posixpath.normpath(
                            str(PurePosixPath(path).parent / target)
                        )
                        if resolved.startswith("/") or ".." in PurePosixPath(resolved).parts:
                            raise ArchiveError(
                                f"symbolic link must stay inside the archive: {path} -> {target!r}"
                            )
                        links[path] = target
                        continue
                    if not (member.isfile() or member.isdir()):
                        raise ArchiveError(f"special archive member is forbidden: {path}")
                    if member.isfile():
                        if member.size > self.limits.max_file_size:
                            raise ArchiveError(f"archive member exceeds size limit: {path}")
                        total_size += member.size
                        files[path] = member
                if total_size > self.limits.max_total_size:
                    raise ArchiveError("archive exceeds total extracted size limit")
                if total_size / compressed_size > self.limits.max_compression_ratio:
                    raise ArchiveError("archive exceeds compression ratio limit")

                manifest_path = next((name for name in self.manifest_names if name in files), None)
                if manifest_path is None and verify_manifest:
                    raise ArchiveError("archive does not contain an embedded file manifest")
                expected_files: dict[str, tuple[str, int | None]] | None = None
                if verify_manifest:
                    assert manifest_path is not None
                    manifest_handle = tar.extractfile(files[manifest_path])
                    if manifest_handle is None:
                        raise ArchiveError("cannot read embedded archive manifest")
                    try:
                        expected_files = _parse_file_manifest(json.load(manifest_handle))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ArchiveError("embedded archive manifest is not valid JSON") from exc

                    payload_files = set(files) - set(self.manifest_names)
                    if payload_files != set(expected_files):
                        missing = sorted(set(expected_files) - payload_files)
                        extra = sorted(payload_files - set(expected_files))
                        raise ArchiveError(
                            f"archive manifest file set mismatch; missing={missing}, extra={extra}"
                        )

                for member in members:
                    path = _safe_path(member.name)
                    if verify_manifest and path.as_posix() in self.manifest_names:
                        continue
                    if member.issym():
                        continue
                    target = destination.joinpath(*path.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if member.islnk():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        raise ArchiveError(f"cannot read archive member: {path.as_posix()}")
                    digest = hashlib.sha256()
                    written = 0
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    with os.fdopen(os.open(target, flags, 0o600), "wb") as output:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            written += len(chunk)
                            if written > member.size or written > self.limits.max_file_size:
                                raise ArchiveError(
                                    f"archive member expanded beyond declared size: {path.as_posix()}"
                                )
                            output.write(chunk)
                            digest.update(chunk)
                    if written != member.size or (
                        expected_files is not None
                        and expected_files[path.as_posix()][1] is not None
                        and written != expected_files[path.as_posix()][1]
                    ):
                        raise ArchiveError(f"file size mismatch: {path.as_posix()}")
                    if (
                        expected_files is not None
                        and digest.hexdigest() != expected_files[path.as_posix()][0]
                    ):
                        raise ArchiveError(f"file sha256 mismatch: {path.as_posix()}")
                    safe_mode = member.mode & 0o755
                    os.chmod(target, safe_mode or 0o600, follow_symlinks=False)
                for path, target_name in sorted(links.items()):
                    link_path = destination.joinpath(*PurePosixPath(path).parts)
                    link_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.symlink(target_name, link_path)
                    except FileExistsError as exc:
                        raise ArchiveError(
                            f"symbolic link collides with an extracted member: {path}"
                        ) from exc
                for path in sorted(links):
                    if not os.path.exists(destination.joinpath(*PurePosixPath(path).parts)):
                        raise ArchiveError(
                            f"symbolic link is dangling or cyclic: {path} -> {links[path]!r}"
                        )
                resolved_root = os.path.realpath(destination)
                for path in sorted(links):
                    link_path = destination.joinpath(*PurePosixPath(path).parts)
                    if not os.path.realpath(link_path).startswith(resolved_root + os.sep):
                        raise ArchiveError(
                            f"symbolic link resolves outside the archive: {path}"
                        )
            return destination
        except Exception:
            import shutil

            shutil.rmtree(destination, ignore_errors=True)
            raise

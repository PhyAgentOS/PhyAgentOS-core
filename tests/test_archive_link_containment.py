"""Internal links must never redirect extraction writes outside the payload."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from PhyAgentOS.skill_runtime.archive import ArchiveError, ArchiveValidator


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("link_name,parent_name", [("a", "a"), ("A", "a"), ("é", "e\u0301")])
def test_symlink_ancestor_is_rejected_before_external_writes(
    tmp_path: Path, reverse: bool, link_name: str, parent_name: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    archive_path = tmp_path / "payload.tar.gz"
    links = [
        (link_name, "."),
        (f"{parent_name}/b", "../outside"),
        (f"{parent_name}/b/created/link", "target"),
    ]
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, target in reversed(links) if reverse else links:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            archive.addfile(member)

    destination = tmp_path / "extract"
    with pytest.raises(ArchiveError, match="symbolic link parent"):
        ArchiveValidator(allow_internal_links=True).extract(
            archive_path, destination, verify_manifest=False,
        )

    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"unchanged"
    assert not destination.exists()


def test_internal_directory_link_and_link_chain_are_allowed(tmp_path: Path) -> None:
    archive_path = tmp_path / "payload.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        directory = tarfile.TarInfo("real")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name, target in [("alias", "real"), ("chain", "alias")]:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            archive.addfile(member)

    destination = tmp_path / "extract"
    ArchiveValidator(allow_internal_links=True).extract(
        archive_path, destination, verify_manifest=False,
    )
    assert (destination / "chain").resolve() == destination / "real"

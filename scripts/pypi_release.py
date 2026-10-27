"""Verify GitHub release distributions before publishing the same bytes to PyPI.

Uses only the standard library; never imports or executes the release package.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path

PROJECT = "PhyAgentOS-ai"
REPOSITORY = "PhyAgentOS/PhyAgentOS-core"


def version_for(tag):
    if not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag):
        raise ValueError("Only stable vMAJOR.MINOR.PATCH tags are supported")
    return tag[1:]


def filenames(version):
    return (
        f"phyagentos_ai-{version}-py3-none-any.whl",
        f"phyagentos_ai-{version}.tar.gz",
    )


def canonical_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_metadata(data, version):
    metadata = BytesParser().parsebytes(data)
    if canonical_name(metadata.get("Name", "")) != canonical_name(PROJECT):
        raise ValueError("Distribution project name does not match PhyAgentOS-ai")
    if metadata.get("Version") != version:
        raise ValueError("Distribution version does not match release tag")


def verify_assets(directory, tag):
    version = version_for(tag)
    expected = set(filenames(version))
    checksums = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](\S+)", line)
        if not match or match[2] not in expected or match[2] in checksums:
            raise ValueError("SHA256SUMS must list each expected distribution exactly once")
        checksums[match[2]] = match[1]
    if set(checksums) != expected:
        raise ValueError("SHA256SUMS is missing a distribution")
    for filename, digest in checksums.items():
        path = directory / filename
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"SHA256 mismatch: {filename}")
        if filename.endswith(".whl"):
            with zipfile.ZipFile(path) as archive:
                members = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
                if len(members) != 1:
                    raise ValueError("Wheel must contain exactly one METADATA file")
                validate_metadata(archive.read(members[0]), version)
        else:
            with tarfile.open(path, "r:gz") as archive:
                member = archive.getmember(f"phyagentos_ai-{version}/PKG-INFO")
                if not member.isfile():
                    raise ValueError("Source distribution PKG-INFO must be a regular file")
                validate_metadata(archive.extractfile(member).read(), version)
    return checksums


def release_assets(release, tag):
    version = version_for(tag)
    if release["tag_name"] != tag or release["draft"] or release["prerelease"]:
        raise ValueError("Only a published stable GitHub release can be uploaded")
    if not release.get("published_at"):
        raise ValueError("GitHub release has not been published")
    expected = {*filenames(version), "SHA256SUMS"}
    assets = [a for a in release["assets"] if a["name"] in expected]
    if len(assets) != 3 or {a["name"] for a in assets} != expected:
        raise ValueError("Release must include wheel, source distribution and SHA256SUMS")
    return assets


def download_assets(tag, directory):
    release = json.loads(subprocess.check_output([
        "gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}",
    ]))
    assets = release_assets(release, tag)
    directory.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        path = directory / asset["name"]
        with path.open("wb") as output:
            subprocess.run([
                "gh", "api", f"repos/{REPOSITORY}/releases/assets/{asset['id']}",
                "-H", "Accept: application/octet-stream",
            ], stdout=output, check=True)
        if path.stat().st_size != asset["size"]:
            raise ValueError(f"Release asset size mismatch: {path.name}")
        digest = asset.get("digest")
        if digest and digest != f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}":
            raise ValueError(f"GitHub asset digest mismatch: {path.name}")


def pypi_files(version):
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{PROJECT}/{version}/json", timeout=30,
        ) as response:
            return json.load(response)["urls"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise


def missing_files(checksums, published):
    seen = set()
    for item in published:
        name = item["filename"]
        if name not in checksums or name in seen:
            raise ValueError(f"Unexpected or duplicate file on PyPI: {name}")
        if item.get("yanked"):
            raise ValueError(f"PyPI file is yanked: {name}")
        if item["digests"]["sha256"] != checksums[name]:
            raise ValueError(f"PyPI already has different bytes for {name}; use a new version")
        seen.add(name)
    return sorted(set(checksums) - seen)


def stage_missing(directory, upload_dir, checksums, published):
    missing = missing_files(checksums, published)
    upload_dir.mkdir(parents=True, exist_ok=False)
    for filename in missing:
        shutil.copyfile(directory / filename, upload_dir / filename)
    return missing


def verify_pypi(checksums, version, attempts=12, delay=10):
    for attempt in range(attempts):
        try:
            missing = missing_files(checksums, pypi_files(version))
            if not missing:
                print("PyPI filenames and SHA256 hashes match the GitHub release")
                return
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise
        except urllib.error.URLError:
            pass
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise ValueError("PyPI did not expose all expected distributions before the timeout")


def verify_install_report(report_path, checksums, version):
    installs = json.loads(report_path.read_text())["install"]
    matches = [i for i in installs if canonical_name(i["metadata"]["name"]) == canonical_name(PROJECT)]
    if len(matches) != 1 or matches[0]["metadata"]["version"] != version:
        raise ValueError("pip did not install the requested project and version")
    download = matches[0]["download_info"]
    # pip must fetch the release wheel from the official PyPI file service.
    url = urllib.parse.urlparse(download["url"])
    wheel = filenames(version)[0]
    if url.scheme != "https" or url.hostname != "files.pythonhosted.org" or not url.path.endswith("/" + wheel):
        raise ValueError("pip did not download the expected wheel from official PyPI")
    if download["archive_info"]["hashes"]["sha256"] != checksums[wheel]:
        raise ValueError("Installed wheel SHA256 differs from the GitHub release")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "verify", "installed"])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--assets", type=Path, default=Path("release-assets"))
    parser.add_argument("--upload-dir", type=Path, default=Path("dist"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    version = version_for(args.tag)
    if args.mode == "prepare":
        download_assets(args.tag, args.assets)
    checksums = verify_assets(args.assets, args.tag)
    if args.mode == "prepare":
        missing = stage_missing(args.assets, args.upload_dir, checksums, pypi_files(version))
        print(f"Verified {args.tag}: {len(missing)} distribution(s) need uploading")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write(f"version={version}\nmissing={len(missing)}\n")
    elif args.mode == "verify":
        verify_pypi(checksums, version)
    else:
        if args.report is None:
            parser.error("--report is required for installed mode")
        verify_install_report(args.report, checksums, version)
        print("Installed wheel came from official PyPI and matches the release SHA256")


if __name__ == "__main__":
    main()

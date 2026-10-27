"""Release checks must fail closed and support retries without changing bytes."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import pypi_release as release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.tag = "v1.2.3"
        self.version = "1.2.3"
        self.wheel, self.sdist = release.filenames(self.version)
        self.make_assets()

    def make_assets(self, name="PhyAgentOS-ai", version="1.2.3"):
        metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n".encode()
        with zipfile.ZipFile(self.directory / self.wheel, "w") as archive:
            archive.writestr("phyagentos_ai-1.2.3.dist-info/METADATA", metadata)
        with tarfile.open(self.directory / self.sdist, "w:gz") as archive:
            member = tarfile.TarInfo("phyagentos_ai-1.2.3/PKG-INFO")
            member.size = len(metadata)
            archive.addfile(member, io.BytesIO(metadata))
        self.checksums = {
            name: hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
            for name in (self.wheel, self.sdist)
        }
        (self.directory / "SHA256SUMS").write_text("".join(
            f"{digest} *{name}\n" for name, digest in self.checksums.items()
        ))

    def published(self):
        return [{"filename": name, "digests": {"sha256": digest}, "yanked": False}
                for name, digest in self.checksums.items()]

    def test_verified_assets(self):
        self.assertEqual(release.verify_assets(self.directory, self.tag), self.checksums)

    def test_invalid_tags(self):
        for tag in ["1.2.3", "v1.2.3rc1", "v01.2.3", "v1.2.3\n", "v1.2.3; echo bad"]:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.version_for(tag)

    def test_corrupt_artifact(self):
        (self.directory / self.wheel).write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            release.verify_assets(self.directory, self.tag)

    def test_missing_artifact(self):
        (self.directory / self.sdist).unlink()
        with self.assertRaises(FileNotFoundError):
            release.verify_assets(self.directory, self.tag)

    def test_manifest_rejects_traversal_duplicates_and_missing_entries(self):
        path = self.directory / "SHA256SUMS"
        original = path.read_text()
        for text in [original.replace(self.wheel, "../" + self.wheel),
                     original + original, original.splitlines()[0] + "\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                path.write_text(text)
                release.verify_assets(self.directory, self.tag)

    def test_wrong_package_name_or_version(self):
        for name, version in [("unrelated-project", self.version), (release.PROJECT, "9.9.9")]:
            with self.subTest(name=name, version=version), self.assertRaises(ValueError):
                self.make_assets(name, version)
                release.verify_assets(self.directory, self.tag)

    def test_unpublished_or_incomplete_release(self):
        good = {"tag_name": self.tag, "draft": False, "prerelease": False,
                "published_at": "2026-10-27T00:00:00Z",
                "assets": [{"name": n} for n in [self.wheel, self.sdist, "SHA256SUMS"]]}
        self.assertEqual(len(release.release_assets(good, self.tag)), 3)
        for change in [{"draft": True}, {"prerelease": True}, {"published_at": None},
                       {"tag_name": "v9.9.9"}, {"assets": good["assets"][:-1]}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                release.release_assets(good | change, self.tag)

    def test_new_version_stages_only_package_files(self):
        target = self.directory / "upload"
        missing = release.stage_missing(self.directory, target, self.checksums, [])
        self.assertEqual(set(missing), set(self.checksums))
        self.assertEqual({p.name for p in target.iterdir()}, set(self.checksums))
        for name in missing:
            self.assertEqual((target / name).read_bytes(), (self.directory / name).read_bytes())

    def test_same_version_is_noop(self):
        self.assertEqual(release.missing_files(self.checksums, self.published()), [])

    def test_partial_upload_stages_only_missing_file(self):
        target = self.directory / "upload"
        self.assertEqual(release.stage_missing(
            self.directory, target, self.checksums, self.published()[:1],
        ), [self.sdist])
        self.assertEqual([p.name for p in target.iterdir()], [self.sdist])

    def test_conflicting_hash_blocks_upload(self):
        published = self.published()
        published[0]["digests"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "different bytes"):
            release.stage_missing(self.directory, self.directory / "upload", self.checksums, published)
        self.assertFalse((self.directory / "upload").exists())

    def test_yanked_unexpected_and_duplicate_files_fail(self):
        published = self.published()
        for files in [[published[0] | {"yanked": True}],
                      [published[0] | {"filename": "other.whl"}], published + published]:
            with self.subTest(files=files), self.assertRaises(ValueError):
                release.missing_files(self.checksums, files)

    def test_only_pypi_404_means_not_published(self):
        for status in [404, 403, 503]:
            error = urllib.error.HTTPError("https://pypi.org", status, "error", {}, None)
            with patch.object(release.urllib.request, "urlopen", side_effect=error):
                if status == 404:
                    self.assertEqual(release.pypi_files(self.version), [])
                else:
                    with self.assertRaises(urllib.error.HTTPError):
                        release.pypi_files(self.version)

    def test_post_upload_waits_for_registry(self):
        with patch.object(release, "pypi_files", side_effect=[[], self.published()]) as query:
            release.verify_pypi(self.checksums, self.version, attempts=2, delay=0)
            self.assertEqual(query.call_count, 2)

    def test_post_upload_never_accepts_conflicts(self):
        published = self.published()
        published[0]["digests"]["sha256"] = "0" * 64
        with patch.object(release, "pypi_files", return_value=published) as query:
            with self.assertRaises(ValueError):
                release.verify_pypi(self.checksums, self.version, attempts=2, delay=0)
            self.assertEqual(query.call_count, 1)

    def test_install_report_origin_and_hash(self):
        info = {"url": f"https://files.pythonhosted.org/packages/{self.wheel}",
                "archive_info": {"hashes": {"sha256": self.checksums[self.wheel]}}}
        item = {"metadata": {"name": release.PROJECT, "version": self.version},
                "download_info": info}
        report = self.directory / "report.json"
        report.write_text(json.dumps({"install": [item]}))
        release.verify_install_report(report, self.checksums, self.version)
        for change in [{"url": f"https://example.com/{self.wheel}"},
                       {"archive_info": {"hashes": {"sha256": "0" * 64}}}]:
            report.write_text(json.dumps({"install": [item | {"download_info": info | change}]}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                release.verify_install_report(report, self.checksums, self.version)


if __name__ == "__main__":
    unittest.main()

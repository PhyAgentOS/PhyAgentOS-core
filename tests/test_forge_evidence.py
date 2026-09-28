from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PhyAgentOS.forge.evidence import ForgeEvidenceWriter
from PhyAgentOS.forge.observation import CapturedImage, ObservationSnapshot, utc_now


class ForgeEvidenceWriterTests(unittest.TestCase):
    @staticmethod
    def _image(source_id: str, data: bytes, sequence: int = 1) -> CapturedImage:
        now = utc_now()
        return CapturedImage(
            source_id=source_id,
            sequence=sequence,
            captured_at=1.0,
            received_at=now,
            media_type="image/png",
            data=data,
        )

    @staticmethod
    def _manifest(workspace: Path, reference: str) -> dict:
        return json.loads((workspace / reference).read_text(encoding="utf-8"))

    def test_sanitized_source_collision_writes_distinct_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            writer = ForgeEvidenceWriter(workspace, "session", "command")
            images = {
                "cam/a": self._image("cam/a", b"slash"),
                "cam_a": self._image("cam_a", b"underscore"),
            }

            reference = writer.write_snapshot(
                "before", ObservationSnapshot(utc_now(), images)
            )
            entries = {
                entry["source_id"]: entry
                for entry in self._manifest(workspace, reference)["entries"]
            }

            self.assertNotEqual(entries["cam/a"]["uri"], entries["cam_a"]["uri"])
            for source_id, expected in (("cam/a", b"slash"), ("cam_a", b"underscore")):
                uri = entries[source_id]["uri"]
                self.assertEqual((workspace / uri).read_bytes(), expected)
                self.assertIn(
                    hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
                    Path(uri).name,
                )

            bundle, _ = writer.write_bundle(
                before_ref=reference,
                after_ref=None,
                terminal_observed_at=utc_now(),
                required_sources=[],
                required_kinds=[],
                errors=[],
            )
            artifacts = {artifact.source_id: artifact for artifact in bundle.artifacts}
            self.assertEqual(artifacts["cam/a"].sha256, hashlib.sha256(b"slash").hexdigest())
            self.assertEqual(
                artifacts["cam_a"].sha256, hashlib.sha256(b"underscore").hexdigest()
            )

    def test_truncated_labels_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            writer = ForgeEvidenceWriter(workspace, "session", "command")
            prefix = "a" * 40
            first = f"{prefix}/one"
            second = f"{prefix}_one"
            reference = writer.write_snapshot(
                "before",
                ObservationSnapshot(
                    utc_now(),
                    {
                        first: self._image(first, b"first"),
                        second: self._image(second, b"second"),
                    },
                ),
            )

            entries = self._manifest(workspace, reference)["entries"]
            self.assertEqual(len({entry["uri"] for entry in entries}), 2)
            self.assertTrue(all(f"before_{prefix}_" in entry["uri"] for entry in entries))

    def test_names_are_deterministic_and_phase_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            writer = ForgeEvidenceWriter(workspace, "session", "command")
            source = "cam/a"
            snapshot = ObservationSnapshot(
                utc_now(), {source: self._image(source, b"frame", sequence=7)}
            )

            first_ref = writer.write_snapshot("before", snapshot)
            first_uri = self._manifest(workspace, first_ref)["entries"][0]["uri"]
            second_ref = writer.write_snapshot("before", snapshot)
            second_uri = self._manifest(workspace, second_ref)["entries"][0]["uri"]
            after_ref = writer.write_snapshot("after", snapshot)
            after_uri = self._manifest(workspace, after_ref)["entries"][0]["uri"]

            self.assertEqual(first_uri, second_uri)
            self.assertNotEqual(first_uri, after_uri)
            self.assertTrue((workspace / first_uri).is_file())
            self.assertTrue((workspace / after_uri).is_file())

    def test_duplicate_target_preflight_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            writer = ForgeEvidenceWriter(workspace, "session", "command")
            snapshot = ObservationSnapshot(
                utc_now(),
                {
                    "one": self._image("one", b"one"),
                    "two": self._image("two", b"two"),
                },
            )

            with patch.object(writer, "_image_filename", return_value="same.png"):
                with self.assertRaisesRegex(ValueError, "duplicate evidence target path"):
                    writer.write_snapshot("before", snapshot)

            self.assertFalse(writer.artifact_dir.exists())

    def test_invalid_extension_and_escaping_path_are_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            writer = ForgeEvidenceWriter(workspace, "session", "command")
            source = "camera"
            snapshot = ObservationSnapshot(
                utc_now(), {source: self._image(source, b"frame")}
            )

            with patch.object(writer, "_suffix_for", return_value="../png"):
                with self.assertRaisesRegex(ValueError, "invalid evidence file extension"):
                    writer.write_snapshot("before", snapshot)
            with patch.object(writer, "_image_filename", return_value="../outside.png"):
                with self.assertRaisesRegex(ValueError, "escapes evidence directory"):
                    writer.write_snapshot("before", snapshot)

            self.assertFalse(writer.artifact_dir.exists())

    def test_legacy_manifest_uri_remains_loadable_and_is_used_by_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            writer = ForgeEvidenceWriter(workspace, "session", "command")
            now = utc_now()
            legacy_path = writer.evidence_dir / "before_cam_a_7.png"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_bytes(b"legacy")
            manifest_path = writer.artifact_dir / "before_snapshot.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": "forge_observation_snapshot_v1",
                        "phase": "before",
                        "captured_at": now.isoformat(),
                        "entries": [
                            {
                                "kind": "rgb_image",
                                "source_id": "cam/a",
                                "sequence": 7,
                                "captured_at": 1.0,
                                "received_at": now.isoformat(),
                                "media_type": "image/png",
                                "uri": str(legacy_path.relative_to(workspace)),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            reference = str(manifest_path.relative_to(workspace))

            snapshot = writer.load_snapshot(reference)
            self.assertEqual(snapshot.images["cam/a"].data, b"legacy")
            bundle, _ = writer.write_bundle(
                before_ref=reference,
                after_ref=None,
                terminal_observed_at=now,
                required_sources=[],
                required_kinds=[],
                errors=[],
            )
            self.assertEqual(bundle.artifacts[0].uri, str(legacy_path.relative_to(workspace)))
            self.assertEqual(bundle.artifacts[0].sha256, hashlib.sha256(b"legacy").hexdigest())


if __name__ == "__main__":
    unittest.main()

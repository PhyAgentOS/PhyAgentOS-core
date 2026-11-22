"""Protect the original Skill and reconnect only the explicitly selected source."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "beginner_builder", ROOT / "example/beginner-simulation/build_skill.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = self.root / "original"
        directory = self.upstream / "profiles/mujoco-piper"
        directory.mkdir(parents=True)
        self.flow = {
            "nodes": [
                {"id": f"control{i}", "path": f"${{FORGE_RUNTIME_BIN}}/control{i}"}
                for i in range(8)
            ]
            + [
                {
                    "id": "image_viewer",
                    "path": "${FORGE_RUNTIME_BIN}/image_viewer",
                    "inputs": {"image": "mujoco/image/angle"},
                }
            ]
        }
        self.flow["nodes"][0]["inputs"] = {
            "tool_in": "gateway/tool_out",
            "tick": "dora/timer/millis/50",
        }
        (directory / "dataflow.yaml").write_text(yaml.safe_dump(self.flow))
        (directory / "gateway.yaml").write_text("port: 19002\n")
        self.manifest = {
            "name": "robot-control-sim",
            "version": "0.1.2",
            "artifacts": {"nodes": json.loads(builder.LOCKS.read_text())},
            "profiles": {
                "mujoco-piper": {
                    "dataflow": "profiles/mujoco-piper/dataflow.yaml",
                    "required_binaries": ["gateway", "image_viewer"],
                }
            },
        }
        (self.upstream / "skill.yaml").write_text(yaml.safe_dump(self.manifest))
        (self.upstream / "SKILL.md").write_text("Original instructions")

    def build(self, output=None, source=None):
        return builder.build(
            self.upstream,
            output or self.root / "result",
            Path(sys.executable),
            self.root / "video.mp4",
            source,
        )

    def test_original_controls_locks_and_source_preserved(self):
        before = {
            p.relative_to(self.upstream): p.read_bytes()
            for p in self.upstream.rglob("*")
            if p.is_file()
        }
        custom = self.root / "custom.py"
        custom.write_text('print("custom observer")\n')
        output = self.build(source=custom)
        after = {
            p.relative_to(self.upstream): p.read_bytes()
            for p in self.upstream.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        manifest = yaml.safe_load((output / "skill.yaml").read_text())
        flow = yaml.safe_load((output / "profiles/mujoco-piper-record/dataflow.yaml").read_text())
        self.assertEqual(manifest["artifacts"], self.manifest["artifacts"])
        self.assertEqual(
            manifest["profiles"]["mujoco-piper"], self.manifest["profiles"]["mujoco-piper"]
        )
        viewer_flow = yaml.safe_load((output / "profiles/mujoco-piper/dataflow.yaml").read_text())
        self.assertEqual(flow["nodes"][:8], viewer_flow["nodes"][:8])
        self.assertEqual(
            flow["nodes"][0]["inputs"],
            {
                "tool_in": {"source": "gateway/tool_out", "queue_size": 1000},
                "tick": "dora/timer/millis/50",
            },
        )
        self.assertEqual(flow["nodes"][1:8], self.flow["nodes"][1:8])
        self.assertEqual(flow["nodes"][-1]["inputs"], {"image": "mujoco/image/angle"})
        self.assertEqual(
            (output / "profiles/mujoco-piper-record/recorder.py").read_bytes(), custom.read_bytes()
        )
        self.assertTrue(
            (output / "profiles/mujoco-piper-record/run_recorder.sh").stat().st_mode & 0o111
        )
        with self.assertRaisesRegex(ValueError, "new output"):
            self.build()

    def test_alias_into_original_and_changed_locks_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.upstream, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.build(alias / "accidental-output")
        self.assertFalse((self.upstream / "accidental-output").exists())
        self.manifest["artifacts"]["nodes"]["gateway"]["sha256"] = "0" * 64
        (self.upstream / "skill.yaml").write_text(yaml.safe_dump(self.manifest))
        with self.assertRaisesRegex(ValueError, "locks"):
            self.build()

    def test_invalid_custom_source_creates_no_output(self):
        source = self.root / "broken.py"
        source.write_text("def (")
        with self.assertRaises(SyntaxError):
            self.build(source=source)
        self.assertFalse((self.root / "result").exists())


if __name__ == "__main__":
    unittest.main()

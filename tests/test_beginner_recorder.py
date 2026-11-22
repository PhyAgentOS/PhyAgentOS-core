"""Focused tests for the beginner tutorial's image recording boundary."""

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PATH = (
    Path(__file__).resolve().parents[1]
    / "example/beginner-simulation/nodes/video_recorder/recorder.py"
)
spec = importlib.util.spec_from_file_location("tutorial_recorder", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ImageBoundaryTests(unittest.TestCase):
    def test_padding_does_not_become_pixels(self):
        row = {
            "width": 1,
            "height": 2,
            "step": 4,
            "encoding": "rgb8",
            "data": b"\xff\x00\x00X\x00\xff\x00Y",
        }
        self.assertEqual(module.decode_image(row), (1, 2, b"\xff\x00\x00\x00\xff\x00"))

    def test_bgr_rgba_and_mono_conversion(self):
        for encoding, payload, expected in [
            ("bgr8", b"\x01\x02\x03", b"\x03\x02\x01"),
            ("rgba8", b"\x01\x02\x03\x00", b"\x01\x02\x03"),
            ("mono8", b"\x7f", b"\x7f\x7f\x7f"),
        ]:
            with self.subTest(encoding=encoding):
                self.assertEqual(
                    module.decode_image(
                        {
                            "width": 1,
                            "height": 1,
                            "step": len(payload),
                            "encoding": encoding,
                            "data": payload,
                        }
                    )[2],
                    expected,
                )

    def test_rejects_malformed_dimensions_stride_and_payload(self):
        row = {"width": 2, "height": 2, "step": 6, "encoding": "rgb8", "data": bytes(12)}
        for change in [
            {"width": True},
            {"width": 0},
            {"height": 4097},
            {"step": 5},
            {"step": 2**30},
            {"data": bytes(11)},
            {"encoding": "depth32f"},
            {"encoding": []},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.decode_image(dict(row, **change))

    def test_no_frames_is_failure_and_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "result.mp4"
            recorder = module.Recorder(output)
            try:
                with self.assertRaisesRegex(ValueError, "No image frames"):
                    recorder.finish()
            finally:
                recorder.close()
            self.assertFalse(output.exists())
            output.write_bytes(b"previous recording")
            with self.assertRaisesRegex(ValueError, "never replaced"):
                module.Recorder(output)
            self.assertEqual(output.read_bytes(), b"previous recording")

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg/ffprobe required for encoder integration",
    )
    def test_real_encoder_frame_count_dimensions_and_duration(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "result.mp4"
            recorder = module.Recorder(output, fps=10)
            row = {
                "width": 17,
                "height": 19,
                "step": 51,
                "encoding": "rgb8",
                "data": b"\xff\x00\x00" * 17 * 19,
            }
            try:
                for _ in range(6):
                    recorder.add(row)
                result = recorder.finish()
            finally:
                recorder.close()
            self.assertEqual(result["frames"], 6)
            probe = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-count_frames",
                        "-show_streams",
                        "-of",
                        "json",
                        str(output),
                    ]
                )
            )["streams"][0]
            self.assertEqual((probe["codec_name"], probe["pix_fmt"]), ("h264", "yuv420p"))
            self.assertEqual((probe["width"], probe["height"]), (18, 20))
            self.assertEqual(int(probe["nb_read_frames"]), 6)
            self.assertAlmostEqual(float(probe["duration"]), 0.6, places=2)
            self.assertEqual(list(Path(root).glob(".recording-*")), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required for abort integration")
    def test_invalid_later_frame_removes_partial_recording(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "result.mp4"
            recorder = module.Recorder(output)
            try:
                recorder.add(
                    {"width": 2, "height": 2, "step": 6, "encoding": "rgb8", "data": bytes(12)}
                )
                with self.assertRaisesRegex(ValueError, "dimensions changed"):
                    recorder.add(
                        {"width": 4, "height": 2, "step": 12, "encoding": "rgb8", "data": bytes(24)}
                    )
            finally:
                recorder.close()
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(root).glob(".recording-*")), [])


if __name__ == "__main__":
    unittest.main()

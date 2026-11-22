"""Tutorial-only Dora 0.4.1 consumer for the locked simulator's Forge Image.

This node writes constant-frame-rate MP4. It does not control the robot, publish a
ToolEndpoint, infer physical success, or change any registered Node artifact.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024


def decode_image(row: dict) -> tuple[int, int, bytes]:
    """Convert one Forge Image row to packed RGB, respecting per-row padding."""
    width, height, step = (row.get(k) for k in ("width", "height", "step"))
    if any(type(n) is not int for n in (width, height, step)):
        raise ValueError("Image width, height and step must be integers")
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        raise ValueError("Image dimensions must be in 1..4096")
    encoding = row.get("encoding")
    if not isinstance(encoding, str):
        raise ValueError("Image encoding must be a string")
    channels = {"rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4, "mono8": 1}.get(encoding)
    if channels is None:
        raise ValueError(f"Unsupported Image encoding: {encoding!r}")
    payload = row.get("data")
    if not isinstance(payload, bytes) or not width * channels <= step or step * height > MAX_BYTES:
        raise ValueError("Invalid Image stride or payload type/size")
    if len(payload) != step * height:
        raise ValueError("Image payload length does not match step * height")
    packed = b"".join(payload[y * step : y * step + width * channels] for y in range(height))
    if encoding == "rgb8":
        return width, height, packed
    rgb = bytearray(width * height * 3)
    if encoding == "mono8":
        rgb[0::3] = rgb[1::3] = rgb[2::3] = packed
    else:
        rgb[0::3] = packed[(2 if encoding.startswith("bgr") else 0) :: channels]
        rgb[1::3] = packed[1::channels]
        rgb[2::3] = packed[(0 if encoding.startswith("bgr") else 2) :: channels]
    return width, height, bytes(rgb)


def transform_rgb(width: int, height: int, rgb: bytes) -> bytes:
    """Edit this function in your local clone, then reconnect that Node."""
    return rgb


class Recorder:
    def __init__(self, output: Path, fps: int = 30):
        if type(fps) is not int or not 1 <= fps <= 60:
            raise ValueError("VIDEO_FPS must be an integer in 1..60")
        if not output.expanduser().is_absolute():
            raise ValueError("VIDEO_OUTPUT must be an absolute path")
        self.output = output.expanduser().absolute()
        if self.output.suffix.lower() != ".mp4" or self.output.exists():
            raise ValueError(
                "Choose a new .mp4 output path; existing recordings are never replaced"
            )
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.fps, self.frames = fps, 0
        self.size = None
        self.process = None
        self.temporary = None
        self.errors = tempfile.TemporaryFile()

    def add(self, row: dict) -> None:
        width, height, rgb = decode_image(row)
        rgb = transform_rgb(width, height, rgb)
        if not isinstance(rgb, bytes) or len(rgb) != width * height * 3:
            raise ValueError("Custom transform must return packed RGB bytes at the original size")
        if self.size and self.size != (width, height):
            raise ValueError("Image dimensions changed during recording")
        if self.process is None:
            self.size = width, height
            fd, name = tempfile.mkstemp(prefix=".recording-", suffix=".mp4", dir=self.output.parent)
            os.close(fd)
            self.temporary = Path(name)
            self.process = subprocess.Popen(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "rawvideo",
                    "-pixel_format",
                    "rgb24",
                    "-video_size",
                    f"{width}x{height}",
                    "-framerate",
                    str(self.fps),
                    "-i",
                    "pipe:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(self.temporary),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self.errors,
            )
        self.process.stdin.write(rgb)
        self.frames += 1

    def finish(self) -> dict:
        if self.process is None or not self.frames:
            raise ValueError("No image frames received; no successful video was produced")
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Video finalization exceeded 30 seconds") from exc
        if code:
            self.errors.seek(0)
            raise RuntimeError("ffmpeg failed: " + self.errors.read(4000).decode(errors="replace"))
        # Same-directory hard link is atomic and cannot overwrite a competing output.
        os.link(self.temporary, self.output)
        self.temporary.unlink()
        self.temporary = None
        return {
            "path": str(self.output),
            "frames": self.frames,
            "fps": self.fps,
            "duration_seconds": self.frames / self.fps,
            "source_size": list(self.size),
            "timing": "constant frame rate; not a physical-success verdict",
        }

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process is not None and self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        if self.temporary is not None:
            self.temporary.unlink(missing_ok=True)
        self.errors.close()


def main() -> None:
    import pyarrow as pa
    from dora import Node

    max_frames = int(os.environ.get("VIDEO_MAX_FRAMES", "900"))
    if not 1 <= max_frames <= 18000:
        raise ValueError("VIDEO_MAX_FRAMES must be in 1..18000")

    def interrupted(signum, frame):
        raise InterruptedError("Recording interrupted before finalization")

    # A terminated Node must clean its encoder/partial file, not publish success.
    signal.signal(signal.SIGTERM, interrupted)
    recorder = Recorder(Path(os.environ["VIDEO_OUTPUT"]), int(os.environ.get("VIDEO_FPS", "30")))
    try:
        for event in Node():
            if event["type"] == "STOP":
                break
            if event["type"] == "ERROR":
                raise RuntimeError("Dora reported an input error")
            if event["type"] != "INPUT" or event["id"] != "image":
                continue
            value = event["value"]
            if not isinstance(value, pa.StructArray) or len(value) != 1:
                raise ValueError("Expected a single-row Forge Image StructArray")
            recorder.add(value[0].as_py())
            if recorder.frames >= max_frames:
                break
        print(json.dumps(recorder.finish()), flush=True)
    finally:
        recorder.close()


if __name__ == "__main__":
    main()

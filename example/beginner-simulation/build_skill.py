"""Create an isolated local Skill with viewer and recording profiles.

No registered artifacts are changed or published. Copy an editable recorder
source with --recorder-source to demonstrate changing and reconnecting a Node.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
LOCKS = ROOT.parent / "robot-control-sim-piper-x/node-locks.json"


def build(
    upstream: Path,
    output: Path,
    python: Path,
    video: Path,
    recorder_source: Path | None = None,
    fps: int = 30,
    max_frames: int = 900,
) -> Path:
    upstream, output = upstream.resolve(), output.resolve()
    python, video = python.expanduser(), video.expanduser()
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(
            "--python must name an existing absolute Python executable (keep the venv path)"
        )
    if not video.is_absolute() or video.suffix.lower() != ".mp4" or video.exists():
        raise ValueError("--video must be a new absolute .mp4 path")
    if not 1 <= fps <= 60 or not 1 <= max_frames <= 18000:
        raise ValueError("fps must be 1..60 and max-frames 1..18000")
    if output.exists() or output == upstream or upstream in output.parents:
        raise ValueError("Use a new output directory outside the original Skill")
    manifest = yaml.safe_load((upstream / "skill.yaml").read_text())
    if (manifest.get("name"), str(manifest.get("version"))) != ("robot-control-sim", "0.1.2"):
        raise ValueError("Expected robot-control-sim 0.1.2")
    if manifest.get("artifacts", {}).get("nodes") != json.loads(LOCKS.read_text()):
        raise ValueError("The nine Node locks do not match the tutorial baseline")
    profile = manifest["profiles"]["mujoco-piper"]
    if profile["dataflow"] != "profiles/mujoco-piper/dataflow.yaml":
        raise ValueError("Unexpected upstream profile layout")
    flow = yaml.safe_load((upstream / profile["dataflow"]).read_text())
    viewers = [n for n in flow["nodes"] if n["id"] == "image_viewer"]
    if len(viewers) != 1 or viewers[0]["path"] != "${FORGE_RUNTIME_BIN}/image_viewer":
        raise ValueError("Expected exactly one locked image_viewer Node")
    # The published profile's short-form Tool inputs use Dora's small default
    # queues. Explicit buffering mitigates known control-frame drops (#13 in
    # adapter-forge-gateway); it does not fix that Node's result-lookup race.
    control_ports = {
        "tool_in",
        "tool_response",
        "tool_request",
        "sim_tool_in",
        "action_tool_in",
        "gripper_tool_in",
    }
    for node in flow["nodes"]:
        for port, value in node.get("inputs", {}).items():
            if port in control_ports:
                node["inputs"][port] = {
                    "source": value if isinstance(value, str) else value["source"],
                    "queue_size": 1000,
                }
    source = (recorder_source or ROOT / "nodes/video_recorder/recorder.py").resolve()
    if not source.is_file() or source.suffix != ".py":
        raise ValueError("--recorder-source must be a local Python source file")
    # Validate before creating the output; preserve the input bundle byte for byte.
    compile(source.read_text(), str(source), "exec")
    shutil.copytree(upstream, output, ignore=shutil.ignore_patterns("archive-manifest.json"))
    # Apply the same transport configuration to both local comparison profiles;
    # the installed upstream bundle and all Node artifacts remain untouched.
    (output / profile["dataflow"]).write_text(yaml.safe_dump(flow, sort_keys=False))
    recording_dir = output / "profiles/mujoco-piper-record"
    shutil.copytree(output / "profiles/mujoco-piper", recording_dir)
    shutil.copyfile(source, recording_dir / "recorder.py")
    launcher = recording_dir / "run_recorder.sh"
    launcher.write_text(
        '#!/bin/sh\nset -eu\nexec "$RECORDER_PYTHON" "$(dirname -- "$0")/recorder.py"\n'
    )
    launcher.chmod(0o755)
    # Both local profiles share these eight control Nodes and Tool queue sizes.
    index = flow["nodes"].index(viewers[0])
    flow["nodes"][index] = {
        "id": "video_recorder",
        "path": "./run_recorder.sh",
        "inputs": {"image": "mujoco/image/angle"},
    }
    (recording_dir / "dataflow.yaml").write_text(yaml.safe_dump(flow, sort_keys=False))
    recording = copy.deepcopy(profile)
    recording["dataflow"] = "profiles/mujoco-piper-record/dataflow.yaml"
    recording["required_binaries"] = [
        n for n in recording["required_binaries"] if n != "image_viewer"
    ]
    recording["environment"] = {
        **recording.get("environment", {}),
        "RECORDER_PYTHON": str(python),
        "VIDEO_OUTPUT": str(video),
        "VIDEO_FPS": str(fps),
        "VIDEO_MAX_FRAMES": str(max_frames),
    }
    manifest.update(
        name="robot-control-sim-beginner",
        description="Local beginner simulation: compare the original viewer with an editable MP4 recording Node.",
    )
    manifest["profiles"] = {"mujoco-piper": profile, "mujoco-piper-record": recording}
    (output / "skill.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (output / "SKILL.md").write_text("""# robot-control-sim-beginner

Simulation only. Start profile mujoco-piper for the original viewer, or
mujoco-piper-record to record the angle camera to the configured VIDEO_OUTPUT.
Stop the current profile before changing profiles: they share the original
Gateway port. Recording uses a fixed frame rate and ends at VIDEO_MAX_FRAMES;
stop the remaining dataflow after the exercise. A video is not a success verdict.

Call sim.get_robot_info first to discover actual joints, limits and frames.
Use sim.set_joint_positions for absolute joint targets, sim.move_eef for an
absolute end-effector pose (with a unit quaternion), sim.set_gripper_opening
for the gripper, and sim.reset between episodes. Poll actions to a terminal
result; HTTP acceptance does not prove motion success. Only command values
within the discovered bounds. Do not operate real robots or relax safety
limits to conceal a failed motion. Report failed or uncertain actions.

The recorder exposes no ToolEndpoint. Its source is local to this Skill;
rebuild to a new output directory when changing the editable recorder source.
Use a new recording output path each run; existing videos are never overwritten.
""")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("upstream", "output", "python", "video"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--recorder-source", type=Path)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=900)
    args = parser.parse_args()
    print(
        build(
            args.upstream,
            args.output,
            args.python,
            args.video,
            args.recorder_source,
            args.fps,
            args.max_frames,
        )
    )


if __name__ == "__main__":
    main()

# From a chat to an editable simulation pipeline

[English](README.md) | [简体中文](README_zh.md)

Start with the familiar conversational CLI, run a small Piper task in MuJoCo, replace the viewer with an MP4 recorder, then reconnect an edited local copy. No physical robot is required.

**Validation status:** The three native Tool API workflows passed on Linux x86_64. See the [functional report and videos](VALIDATION.md) for results, the initial failure and limits. The model-driven chat walkthrough is not covered by this validation.

## 1. Start with a familiar conversation

Like a coding-agent CLI, `paos agent` accepts goals, shows tool activity and supports follow-up messages. A **Skill** packages workflow instructions, robot tools and a **dataflow** connecting executable **Nodes**. A **profile** selects a concrete configuration.

From the checkout containing this example, use Linux x86_64 and Python 3.11 or 3.12:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
paos onboard
paos provider configure openrouter
paos provider use openrouter --model anthropic/claude-sonnet-4
paos agent
```

Choose your own supported provider/model using the [provider guide](../../docs/en/02-user-manual.md#2-configure-the-model-and-forge); enter credentials in its hidden prompt. Try: “Explain an installed Skill and a running profile. Do not start anything yet.” Exit with Ctrl+D. Chat needs a model connection; the deterministic exercise below does not.

## 2. Build the local tutorial Skill and run its viewer profile

Install ffmpeg with libx264, ffprobe, GLFW/OpenGL, libxkbcommon-x11-0 and libxcb-xkb1 dependencies. Use a working desktop display, or the headless shell below. Run the following from the repository root in the activated venv:

```bash
python -m pip install 'dora-rs==0.4.1' 'dora-rs-cli==0.4.1'
dora --version
ffmpeg -version
paos skill install robot-control-sim --version 0.1.2
paos skill inspect robot-control-sim
BEGINNER_ROOT="$PWD/example/beginner-simulation"
BEGINNER_WORK="$PWD/work/beginner-simulation"
BEGINNER_PYTHON="$VIRTUAL_ENV/bin/python"
mkdir -p "$BEGINNER_WORK"
python "$BEGINNER_ROOT/build_skill.py" \
  --upstream "$HOME/.PhyAgentOS/skills/robot-control-sim" \
  --output "$BEGINNER_WORK/recording-skill" --python "$BEGINNER_PYTHON" \
  --video "$BEGINNER_WORK/simulation.mp4"
python scripts/package_skill.py "$BEGINNER_WORK/recording-skill" --output-dir "$BEGINNER_WORK/dist"
paos skill install "$BEGINNER_WORK/dist/robot-control-sim-beginner-0.1.2.tar.gz" --local
paos skill start robot-control-sim-beginner --profile mujoco-piper
paos skill status robot-control-sim-beginner
python "$BEGINNER_ROOT/exercise.py" --report "$BEGINNER_WORK/viewer-exercise.json"
paos skill stop robot-control-sim-beginner
```

The builder keeps the installed upstream bundle untouched and generates the two local comparison profiles. Tool control inputs in both explicitly use queue_size=1000 to mitigate the published profile’s known message drops ([Gateway #13](https://github.com/PhyAgentOS/adapter-forge-gateway/issues/13)). This finite queue is not a fix for every transport/result-lookup race; failures still fail the exercise. The viewer displays the simulated arm. The exercise discovers joints and limits, resets the scene, moves joint1 by 0.2 radians from the initial pose, then resets again. Expected: `"passed": true`, three successful Actions, joint error ≤0.03 rad. HTTP admission is not success.

For a natural-language version, open `paos agent` while the Skill is running and ask: “In the running Piper simulation, inspect robot limits, reset, move joint1 to 0.2 radians with the other initial joint values, then reset. Poll each Action to its final result; report failure and do not automatically replay an uncertain Action.” Keep the functional report as separate evidence; stop the Skill before switching profiles.

This example locks robot-control-sim 0.1.2 and its nine [Node artifacts](../robot-control-sim-piper-x/node-locks.json), using [Dora 0.4.1](https://github.com/dora-rs/dora/releases/tag/v0.4.1). Do not mix Forge 2.x / Dora 1.x. Registry `--version` validates the current downloaded entry, not a historical lookup. If the current entry changes, use a retained verified official archive: `paos skill install /absolute/path/robot-control-sim-0.1.2.tar.gz --local`.

## 3. Swap the viewer for a recorder

The Agent/Tool API → Gateway → motion/control → MuJoCo chain remains unchanged. Only the observer changes from image_viewer to video_recorder, consuming `mujoco/image/angle`. The builder reads the original Skill without changing it and creates `robot-control-sim-beginner`, retaining the viewer profile and adding `mujoco-piper-record`.

```bash
paos skill start robot-control-sim-beginner --profile mujoco-piper-record
python "$BEGINNER_ROOT/exercise.py" --report "$BEGINNER_WORK/recording-exercise.json"
```

If using an isolated PAOS instance, replace the upstream path with that instance’s installed Skill path. Keep the venv Python path; resolving its symlink to the base interpreter loses its packages.

Wait for `simulation.mp4` to appear. The recorder finalizes after 900 frames (default): a 30-second file at 30 fps. Rendering load can make collection take longer; frame rate is fixed, not measured physical time. Then:

```bash
ffprobe -v error -count_frames -show_entries stream=codec_name,width,height,nb_read_frames,duration \
  -of json "$BEGINNER_WORK/simulation.mp4"
paos skill stop robot-control-sim-beginner
```

Open the MP4 in a media player. Expect H.264 video showing the arm and a separate successful Action report. A video does not prove task success. Use new report/video paths on subsequent runs; files are never silently replaced.

## 4. Edit a local Node copy and reconnect it

```bash
cp -R "$BEGINNER_ROOT/nodes/video_recorder" "$BEGINNER_WORK/video-recorder-custom"
```

Edit `video-recorder-custom/recorder.py`, replacing `transform_rgb` with:

```python
def transform_rgb(width: int, height: int, rgb: bytes) -> bytes:
    result = bytearray(rgb)
    rows = min(24, height)
    result[: width * rows * 3] = b"\xff\x00\x00" * (width * rows)
    return bytes(result)
```

This adds a red strip to every frame without sending robot commands. Rebuild from the explicit source path and install while the previous profile is stopped:

```bash
python "$BEGINNER_ROOT/build_skill.py" \
  --upstream "$HOME/.PhyAgentOS/skills/robot-control-sim" \
  --output "$BEGINNER_WORK/custom-skill" --python "$BEGINNER_PYTHON" \
  --video "$BEGINNER_WORK/custom-simulation.mp4" \
  --recorder-source "$BEGINNER_WORK/video-recorder-custom/recorder.py"
python scripts/package_skill.py "$BEGINNER_WORK/custom-skill" --output-dir "$BEGINNER_WORK/custom-dist"
paos skill install "$BEGINNER_WORK/custom-dist/robot-control-sim-beginner-0.1.2.tar.gz" --local
paos skill start robot-control-sim-beginner --profile mujoco-piper-record
python "$BEGINNER_ROOT/exercise.py" --report "$BEGINNER_WORK/custom-exercise.json"
```

Wait for `custom-simulation.mp4`, inspect it with ffprobe, open it, and run `paos skill stop robot-control-sim-beginner`. The red strip proves the edited source was reconnected; `custom-exercise.json` verifies the unchanged control chain. Videos, reports, editable source and packages are under `BEGINNER_WORK`.

The generated launcher uses the exact venv Python path supplied to the builder. Rebuild if that environment moves. This local copy does not alter or publish a registered Node. Production publication needs its owning repository, a new version/digest and updated Skill locks; see [Track B](../../docs/user_development_guide/track_b/README.md).

## Headless Linux, failures and the recorder contract

On servers, install Xvfb and xauth, open a dedicated display-backed shell, activate the venv and run the walkthrough inside it:

```bash
xvfb-run -a -s '-screen 0 1280x800x24 -nolisten tcp' bash
```

Keep that shell alive until the Skill has stopped. Do not stop another user’s display or shared Dora coordinator. Port 19002 must be free; stop this tutorial’s previous profile rather than killing an unrelated service.

No image frames, an encoder failure, an Action failure or timeout means the run failed. Preserve the report and inspect logs; do not change machines or relax limits to manufacture a pass. Use new paths if outputs already exist. Window failures require checking DISPLAY, GLFW/OpenGL and simulator logs. Version/digest failures require the correct official artifacts, never disabled verification.

The recorder accepts a single-row Forge Image Arrow StructArray: width/height/encoding/step/data. It handles rgb8/bgr8/rgba8/bgra8/mono8 with row padding, rejects depth/compressed input, and exposes no ToolEndpoint. VIDEO_FPS is 1–60 (default 30); VIDEO_MAX_FRAMES is 1–18000 (default 900). H.264/yuv420p output pads odd dimensions. Decoder, real encoder and cleanup tests:

```bash
python -m unittest discover -s tests -p 'test_beginner_*.py' -v
```

# Demo: Add AgileX Piper X to robot-control-sim with MuJoCo

[English](PIPER_X_DEMO.md) | [简体中文](PIPER_X_DEMO_zh.md)

[Bundle and Tool integration reference](README.md) · [TrackB framework](track_b/README.md) · [Three development modes](track_b/SKILL_DEVELOPMENT.md) · [Example code](../../example/robot-control-sim-piper-x/README.md)

> A reusable extension tutorial for MuJoCo simulation, joint targets and end-effector poses.
> Documentation target: Core v1.1.1. Recorded runtime baseline: Core v1.1.0, Dora CLI 0.4.1, original robot-control-sim 0.1.2 Node locks.
> The recorded robot validation below has not been rerun for v1.1.1.
> Companion scripts build/validate a derived Skill without upgrading its locked Nodes.

**Platform availability:** External resource submissions and changes to existing registered repositories are not yet open. They must wait for the approval service to be completed and opened. Current registration/publication is administrator-managed. Local development and validation can proceed; platform publication steps below are conditional on that workflow becoming available. See the [contribution guide](../../CONTRIBUTING.md).

## 1. Goal, baseline and scope

The derived Skill is `robot-control-sim-piper-x`, profile `mujoco-piper-x`, preserving this native chain:

```text
Agent / diagnostic Tool API client
  -> Gateway -> motion_action_policy -> motion_server (joint planning / URDF IK)
  -> joint_trajectory_controller -> mujoco_sim
                                  ^ JointState feedback
```

- `sim.set_joint_positions`: six absolute arm joint angles.
- `sim.move_eef`: absolute end-effector position/orientation; original motion_server solves IK and generates trajectories.
- Existing gripper policy/controller handles opening; sim-control policy provides metadata/reset.
- Preserve all nine artifact/version/SHA256 locks. New content is model, profile, Skill workflow and developer utilities.
- This tutorial does not validate hardware, motor dynamics identification, obstacle avoidance, grasping or whole-workspace reachability.

Use the [official AgileX agx_arm_urdf Piper X model](https://github.com/agilexrobotics/agx_arm_urdf/tree/680fa925cd00db7e2628563d2edc5540963045de/piper_x),
pinned to `680fa925cd00db7e2628563d2edc5540963045de`; do not rename an ordinary Piper model.
The catalog's Forge 2.x / Dora 1.x source is a different baseline and must not be mixed into this deployment.

Development order: pin upstream/locks, prepare model, create a profile, validate FK/servo behavior,
package/install, validate both native Actions, optionally validate AgentTask, stop resources,
then register/publish the derived Skill. The first eight stages produce a locally runnable package;
publication still follows the destination platform's process.

## 2. Prepare the environment and upstream Skill

Use Linux x86_64, Python 3.12, Git, Dora CLI 0.4.1 and a Core v1.1.1 CLI environment; reproducing the original recorded validation requires Core v1.1.0.
Keep Core and model-building environments separate so MuJoCo/numpy do not modify Core dependencies.
See the [Core user manual](../en/02-user-manual.md) for installation, then check:

```bash
paos --help
dora --version
paos skill install robot-control-sim --version 0.1.2
paos skill inspect robot-control-sim
```

In Core v1.1.1, as in the recorded v1.1.0 baseline, Registry `--version` constrains downloaded content rather than querying historical versions.
If the current Registry has advanced, use a retained official immutable URL or local 0.1.2 archive:

```bash
paos skill install /path/to/robot-control-sim-0.1.2.tar.gz --local
```

Do not relabel another version as 0.1.2. Installation validates Bundle/Node digests; resolve missing Nodes
through Registry or separately acquired matching archives. Nine Nodes are required; see the
[source mapping](track_b/REPOSITORIES.md#3-robot-control-sim-node-to-source-mapping).

## 3. Generate the model and independent profile

Run these commands from the repository root. `PIPER_DEMO_UPSTREAM` identifies the installation shown
by `paos skill inspect`. The example uses the default layout; for an isolated Core instance, use its actual Skill path.

```bash
PIPER_DEMO_ROOT="$PWD/example/robot-control-sim-piper-x"
PIPER_DEMO_WORK="$PIPER_DEMO_ROOT/work"
PIPER_DEMO_UPSTREAM="$HOME/.PhyAgentOS/skills/robot-control-sim"
mkdir -p "$PIPER_DEMO_WORK"
python3.12 -m venv "$PIPER_DEMO_ROOT/.venv-model"
"$PIPER_DEMO_ROOT/.venv-model/bin/python" -m pip install -r "$PIPER_DEMO_ROOT/requirements-model.txt"
git clone https://github.com/agilexrobotics/agx_arm_urdf.git "$PIPER_DEMO_WORK/agx_arm_urdf"
git -C "$PIPER_DEMO_WORK/agx_arm_urdf" checkout 680fa925cd00db7e2628563d2edc5540963045de
"$PIPER_DEMO_ROOT/.venv-model/bin/python" "$PIPER_DEMO_ROOT/build_skill.py" \
  --upstream "$PIPER_DEMO_UPSTREAM" \
  --models "$PIPER_DEMO_WORK/agx_arm_urdf" \
  --output "$PIPER_DEMO_WORK/robot-control-sim-piper-x"
```

The generator checks model revision, clean model checkout, upstream Skill identity and all nine locks,
and refuses an existing output directory. Use a new output path when rebuilding; upstream files are preserved.

Without a display server, install Xvfb and append `--headless` to the generator command.
This optional Linux path uses display `:92` and glow/software OpenGL; display and port `19104` must be free.
Desktop runs retain DISPLAY. Changing either requires coordinated changes to generated `skill.yaml`,
`gateway.yaml`, start/stop hooks and diagnostic client arguments.

Generated layout:

```text
robot-control-sim-piper-x/
├── skill.yaml
├── SKILL.md
├── profiles/mujoco-piper-x/*.yaml
├── assets/robots/piper_x/
│   ├── robot.yaml
│   ├── piper_x_with_gripper.urdf
│   ├── mjcf/{scene.xml,piper_x.xml,meshes/}
│   ├── LICENSE.agilex
│   └── PROVENANCE.json
└── start.sh / stop_display.sh       # --headless only
```

## 4. Understand the changes before adding another robot

### 4.1 Model and frames

The generator expands official URDF/gripper xacro, retaining STL geometry, kinematic chain and inertias,
then adds MuJoCo position actuators, damping, armature and gravity compensation.
These are simulation parameters, not calibrated hardware servo parameters.

| Item | Convention |
| --- | --- |
| Arm group | `piper_x_arm` |
| Joint order | `joint1` through `joint6`, positions in rad |
| Base frame / TCP | `base_link` / `tcp` |
| TCP definition | `[0, 0, 0.138]` m in `gripper_base`; URDF fixed link matches MJCF site |
| End-effector target | Position in m; explicit `qx/qy/qz/qw` unit-quaternion fields |
| Initial joint angles | `[0, 0.65, -1.10, 0.25, 0.30, 0.10]` |
| Gripper | Logical total opening `0..0.1` m; opposing sliders coupled by equality, actuator gear=2 |

The model has eight physical joint degrees of freedom but exposes six arm joints and one logical gripper value.
Physical qpos count is not the Tool control dimension.

Only adjacent `base_link/link1` collision is explicitly excluded because imported overlapping shells obstruct joint one.
This model-specific geometry correction does not disable all collisions. Re-evaluate it for another robot rather than copying it blindly.

### 4.2 Configuration changes

| File | Facts that must agree |
| --- | --- |
| `robot.yaml` | Joint names/order/types/limits, initial_qpos, eef_site, gripper identity |
| `simulator.yaml` | Model paths, initial state, logical joints, actuator_order, joint_to_actuator, cameras |
| `motion_server.yaml` | Group, URDF, base/tip, speed/acceleration and tolerances |
| `controller.yaml` | Arm joint order and control tolerances |
| `gripper_controller.yaml` | Logical opening name/range and control parameters |
| `sim_control_policy.yaml` | Descriptor path, reset motion group and gripper invocation |
| `gateway.yaml` | ToolSpec group/frames/joint order/bounds/port/robot_frame_profile |
| `dataflow.yaml` | Original motion chain with robot state, commands and camera ports |
| `skill.yaml` | New Skill/profile, required_assets/tools, gateway_url, original locks |
| `SKILL.md` | Both control modes, preparation, terminal results and verification; no keys or one-off task records |

The readable generator updates these files together. For another robot, establish the model/frames first,
then adapt the profile; do not relax tolerances to conceal frame or joint mapping errors.

### 4.3 Two control modes, one execution chain

Joint `positions[i]` corresponds to `joint_names[i]`, matching motion group, controller and logical simulator joints.
Arm values are radians. Send logical gripper opening through its separate Tool, not physical sliders in a six-axis goal.
End-effector mode specifies `tcp` in `base_link`; original motion_server performs IK.
URDF fixed TCP and the matching MuJoCo site must express the same position/orientation.

Example `sim.move_eef` arguments from the synthetic `eef-a` fixture:

```json
{
  "group_name": "piper_x_arm",
  "reference_frame": "base_link",
  "target_frame": "tcp",
  "target_pose": {
    "x": 0.2870407951459706,
    "y": -0.03170407513342734,
    "z": 0.5422729661601198,
    "qw": 0.7657215005588278,
    "qx": 0.1641599926622322,
    "qy": 0.6218396509287355,
    "qz": -0.006126085528408748
  },
  "velocity_scale": 0.2,
  "acceleration_scale": 0.2,
  "position_tolerance_m": 0.005,
  "orientation_tolerance_rad": 0.03
}
```

Field names define quaternion meaning. MuJoCo arrays use `wxyz`; do not copy them directly as `xyzw`.
Inspect Tool schema/readiness before invoking. Source inspection, raw payloads and successful FK checks
do not replace terminal Action results and measured residuals.

## 5. Validate the model before starting the pipeline

```bash
"$PIPER_DEMO_ROOT/.venv-model/bin/python" "$PIPER_DEMO_ROOT/validate_model.py" \
  --skill "$PIPER_DEMO_WORK/robot-control-sim-piper-x" \
  --output-dir "$PIPER_DEMO_WORK/model-check"
```

The script compares independent URDF matrix FK with MuJoCo sites at 31 poses and checks initial servo stability.
This is model diagnostics, not Tool API/planning-chain acceptance. Position and rotation-matrix element differences
should be below `1e-6`; inspect axes, origins, base/tip and TCP first on failure.

Package/install from the activated **Core environment**, where `python` imports PhyAgentOS:

```bash
python scripts/package_skill.py "$PIPER_DEMO_WORK/robot-control-sim-piper-x" \
  --output-dir "$PIPER_DEMO_WORK/dist"
paos skill install "$PIPER_DEMO_WORK/dist/robot-control-sim-piper-x-0.1.2.tar.gz" --local
paos forge-node verify robot-control-sim-piper-x motion_server
paos forge-node verify robot-control-sim-piper-x mujoco_sim
paos skill inspect robot-control-sim-piper-x
paos skill start robot-control-sim-piper-x --profile mujoco-piper-x
paos skill status robot-control-sim-piper-x
```

Packaging refuses existing output; increment the version for revisions. The generator uses `resolver=registry`
to resolve original Node locks. Initial installation requires all dependencies to resolve; even a populated cache
may query Registry metadata or probe URL size, so cache presence does not establish offline installation.
`paos forge-node install <skill> <node> --archive <archive>` can install a local Node for an already-installed
manifest, but cannot bypass first-install dependency resolution. Both paths must match locks and verify digests.

## 6. Native validation of both control modes

Keep the Core environment active and run the LLM-free diagnostic client:

```bash
python "$PIPER_DEMO_ROOT/accept_pipeline.py" \
  --run-id piper-x-demo-001 \
  --output-dir "$PIPER_DEMO_WORK/acceptance"
```

Use a fresh run-id each time. The native client runs three joint goals, three end-effector goals, gripper and reset.
`motion-fixtures.json` contains reviewable synthetic targets; first confirm scene/profile compatibility.
It queries robot metadata and Tool context, submits one Action at a time and waits for terminal status/results.
Cartesian tests send poses only; they do not solve client-side IK and substitute joint commands.

For initial debugging, run one joint goal and one Cartesian goal separately with new run-ids:

```bash
python "$PIPER_DEMO_ROOT/accept_pipeline.py" --only joint-a \
  --run-id piper-x-joint-001 --output-dir "$PIPER_DEMO_WORK/acceptance"
python "$PIPER_DEMO_ROOT/accept_pipeline.py" --only eef-a \
  --run-id piper-x-eef-001 --output-dir "$PIPER_DEMO_WORK/acceptance"
```

| Check | Success criterion |
| --- | --- |
| Joint target | Correct six-joint order, motion `error_code=SUCCESS`, maximum absolute residual ≤0.03 rad |
| End-effector target | Motion `error_code=SUCCESS`, position error ≤0.005 m, orientation error ≤0.03 rad |
| Gripper | Controller SUCCESS and reached_goal |
| Reset | Scene reset and rehomed |

Raw Gateway `phase=completed` means terminal, while success additionally requires
`data.result.status=succeeded`. Do not wait for nonexistent `phase=succeeded`.
After a timeout, request cancellation and stop the run. Unknown is neither pending nor success and is not automatically resent.

This developer diagnostic does not create AgentTask or an independent model verdict.
To test the complete Agent workflow, configure your model Provider and explicitly select this Skill via
`paos agent`, requesting one fixture joint/EEF goal. The Agent activates the primary Skill, creates/binds
AgentTask, executes, finalizes and checks the persisted verdict. If execution succeeds but verdict is empty,
report both separately; verifier failure must not trigger another motion.

## 7. Troubleshooting order

| Symptom | First checks | Direction |
| --- | --- | --- |
| One joint stalls, others arrive | Joint feedback, actuator mapping, contacts | Distinguish missing commands from contact obstruction; correct only justified adjacent collisions |
| Joint mode works, EEF fails | URDF/MJCF FK, base/tip, quaternion order, reachability | Correct model/frames before IK tuning |
| Window/render startup fails | DISPLAY, Xvfb, renderer, OpenGL | Optional headless profile; check display ownership |
| Immediate result after 202 is unknown | Invocation still dispatching? | Poll status before results; preserve existing unknown diagnostics, do not resend |
| Timeout argument rejected | Gateway schema/limit | Example invoke timeout is 20000 ms, separate from total Action wait |
| Empty verifier output / HTTP 500 | Provider/verifier service errors | Preserve execution facts and empty verdict; investigate verification separately |
| Deserialization fails after replacing a Node | Dora/Forge/message versions and locks | Restore a compatible version set; organization transfer is not protocol-upgrade validation |

These diagnostic patterns contain no local logs, conversations, credentials or deployment-service addresses.

## 8. Stop, reproduce and maintain

```bash
paos skill stop robot-control-sim-piper-x
# --headless builds only: use the actual installation directory
bash "$HOME/.PhyAgentOS/skills/robot-control-sim-piper-x/stop_display.sh"
```

Do not stop unrelated flows/displays. Record your Bundle SHA256, locks, model revision, profile and environment.
Keep runtime validation results local rather than embedding machine-specific records in the public example.

Before submitting another-robot example, validate model FK/limits, both native Actions, clean installation and stop.
Node algorithm changes additionally need unit/integration tests; a handful of tutorial goals do not prove complete control coverage.

## 9. Another robot and deliverables

This generator targets Piper X, not arbitrary URDF import through a universal `--robot` flag.
When reusing the approach, check:

- [ ] Model source/revision/license are verifiable; URDF/MJCF share kinematics and TCP.
- [ ] Joint axes, units, limits, order and logical/physical gripper mapping agree.
- [ ] Initial state/servo behavior is acceptable; collision exclusions have geometric justification.
- [ ] Group, base/tip, Tool schemas, controller and simulator configuration agree.
- [ ] Native joint and EEF targets pass independently; Cartesian IK runs server-side.
- [ ] Timeout/cancel/unknown are diagnosable and failed motion is not blindly repeated.
- [ ] Skill identity/version/assets/locks, startup/stop and isolated installation are checked.

Public deliverables include model conversion instructions, profile, Skill workflow, reviewable test targets,
locks, licenses and build guidance. Exclude credentials, internal deployments and personal conversations;
submit execution evidence separately through the resource platform where required.

The example adds a Skill, not nine duplicate Nodes. Confirm matching dependency versions/digests on the target platform,
then submit the new Skill version, technical validation and required execution evidence. Reuse existing Node records
only after identity checks; rebuilt Nodes need new versions and updated Skill locks.
See [source build and provenance](track_b/SKILL_DEVELOPMENT.md#4-mode-three-develop-nodes-and-assemble-a-skill).

Successful `--local` installation does not establish registration or review/publication. After publication,
use a compatible consumer API to search by name, install into an isolated instance and verify each Node.
Static catalog changes or upload completion are not final acceptance.

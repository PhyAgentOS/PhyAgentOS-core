# TrackB Repository Catalog and Code Organization

[English](REPOSITORIES.md) | [简体中文](REPOSITORIES_zh.md)

[Overview](README.md) · [Repositories](REPOSITORIES.md) · [Protocols](PROTOCOLS.md) · [Runtime](RUNTIME.md) · [Skill development](SKILL_DEVELOPMENT.md)

## 1. Scope

This catalog covers 20 transferred public code repositories: 10 Adapters, 8 Operators, Forge and one benchmark.
It also lists the Forgelab-Robotics `.github` organization configuration; it is not the entire PhyAgentOS organization.
Core task/Skill lifecycle code lives in [this repository](../../../PhyAgentOS), separately from downstream Nodes.

Repository links follow current locations; commit links pin the inspected source snapshot.
Classification does not establish integration in one Skill or validation of every platform, language or hardware capability.

## 2. Categorized catalog

### 2.1 Shared framework

| Category | Current source | Description | Source snapshot |
| --- | --- | --- | --- |
| Framework | [forge](https://github.com/PhyAgentOS/forge) | Common, Msgs, Robot, Policy, Kinematics, Tool; cross-language messages and interfaces | [e59693910bb5](https://github.com/PhyAgentOS/forge/tree/e59693910bb5f9572cf39c5a59475c0747ba76e8) |

### 2.2 Adapters: external boundaries

| Category | Current source | Description | Source snapshot |
| --- | --- | --- | --- |
| Adapter · Sensor | [adapter-camera-orbbec](https://github.com/PhyAgentOS/adapter-camera-orbbec) | Orbbec color/depth/IR/optional point clouds, initialization and publishing | [de3cbc536153](https://github.com/PhyAgentOS/adapter-camera-orbbec/tree/de3cbc5361533d8799f586939266289d7527b203) |
| Adapter · Sensor | [adapter-camera-realsense](https://github.com/PhyAgentOS/adapter-camera-realsense) | RealSense D400 color/depth/IR/optional point-cloud acquisition and publishing | [77627b4d8e39](https://github.com/PhyAgentOS/adapter-camera-realsense/tree/77627b4d8e3977aabf016af6846e9f6857cfcde6) |
| Adapter · Sensor | [adapter-camera-usb](https://github.com/PhyAgentOS/adapter-camera-usb) | Rust/V4L2 USB camera; raw/compressed images and device checks | [bdbe5ebb016d](https://github.com/PhyAgentOS/adapter-camera-usb/tree/bdbe5ebb016da299ff1e0b461260ce1ddd52d234) |
| Adapter · Gateway | [adapter-forge-gateway](https://github.com/PhyAgentOS/adapter-forge-gateway) | HTTP/WebSocket access, Tool routing, Registry and readiness | [5aaa9d16aff1](https://github.com/PhyAgentOS/adapter-forge-gateway/tree/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97) |
| Adapter · Robot | [adapter-franka-fr3](https://github.com/PhyAgentOS/adapter-franka-fr3) | Franka FR3v2 and Franka Hand drivers/models | [efd7a6bd019c](https://github.com/PhyAgentOS/adapter-franka-fr3/tree/efd7a6bd019c7135ae506274ca73ef5879f4a384) |
| Adapter · Robot | [adapter-robot-piper](https://github.com/PhyAgentOS/adapter-robot-piper) | AgileX Piper hardware driver, Dora examples and multi-format robot models | [3ea5156cb369](https://github.com/PhyAgentOS/adapter-robot-piper/tree/3ea5156cb369c302bb2e3ff6f1ebfb6af90a08d8) |
| Adapter · Robot | [adapter-robot-so101](https://github.com/PhyAgentOS/adapter-robot-so101) | SO101 serial/Feetech driver, device utilities and MJCF model | [fb6b59292366](https://github.com/PhyAgentOS/adapter-robot-so101/tree/fb6b59292366059acd10cb2439d155c4c8015cfc) |
| Adapter · Robot | [adapter-robot-xlerobot](https://github.com/PhyAgentOS/adapter-robot-xlerobot) | XLeRobot dual-arm mobile robot integration, configuration and models | [5ceb37fdea4d](https://github.com/PhyAgentOS/adapter-robot-xlerobot/tree/5ceb37fdea4d6c7e454914b1b5e180b4728ef0fa) |
| Adapter · Robot | [adapter-robot-zerith_h1_pro](https://github.com/PhyAgentOS/adapter-robot-zerith_h1_pro) | Zerith H1 Pro joint/base integration, SDK and examples | [e36dc9b0bdfd](https://github.com/PhyAgentOS/adapter-robot-zerith_h1_pro/tree/e36dc9b0bdfd68342d55180485b561c73162cfc3) |
| Adapter · Viewer | [adapter-viewer-image](https://github.com/PhyAgentOS/adapter-viewer-image) | Image viewer consuming Forge image messages | [ea5900a7225c](https://github.com/PhyAgentOS/adapter-viewer-image/tree/ea5900a7225c2bfadd89bbfe23c023ec9fbe1bb6) |

### 2.3 Operators: graph computation

| Category | Current source | Description | Source snapshot |
| --- | --- | --- | --- |
| Operator · Control | [operator-controls](https://github.com/PhyAgentOS/operator-controls) | joint_trajectory_controller, gripper_action_controller and fake plant | [cde73dc2a622](https://github.com/PhyAgentOS/operator-controls/tree/cde73dc2a6222d81b01ffc32f69f12cb12e42760) |
| Operator · Policy | [operator-gripper-action-policy](https://github.com/PhyAgentOS/operator-gripper-action-policy) | Translate Tool opening requests to gripper Actions and execution results | [bb69223497b9](https://github.com/PhyAgentOS/operator-gripper-action-policy/tree/bb69223497b944dd1a0f6fe9ecb36486168a8cc4) |
| Operator · Policy | [operator-kai0](https://github.com/PhyAgentOS/operator-kai0) | Kai0 policy source, inference execution and Node packaging | [94baa639fe6d](https://github.com/PhyAgentOS/operator-kai0/tree/94baa639fe6d36bb5d41d214923108993aa002cc) |
| Operator · Policy | [operator-lerobot-runner](https://github.com/PhyAgentOS/operator-lerobot-runner) | LeRobot policy execution, observations and control | [f6b26f4f89bb](https://github.com/PhyAgentOS/operator-lerobot-runner/tree/f6b26f4f89bbd9831f64e3fc43d36499d6c86a92) |
| Operator · Motion | [operator-motion](https://github.com/PhyAgentOS/operator-motion) | motion_server and motion_action_policy; goals, IK and trajectories | [24c8a430715f](https://github.com/PhyAgentOS/operator-motion/tree/24c8a430715f697eea8431bf169c21b8cbb5eb92) |
| Operator · Policy | [operator-relative-pose-policy](https://github.com/PhyAgentOS/operator-relative-pose-policy) | Relative end-effector pose resolution and motion.relative_pose integration | [4749b696a8b3](https://github.com/PhyAgentOS/operator-relative-pose-policy/tree/4749b696a8b3210e193860d6aeed92382fbad113) |
| Operator · Policy | [operator-sim-control-policy](https://github.com/PhyAgentOS/operator-sim-control-policy) | Robot descriptor queries, simulation reset and deferred re-home | [85679a4be895](https://github.com/PhyAgentOS/operator-sim-control-policy/tree/85679a4be8953e9071de426c7c0973155121d96f) |
| Operator · Simulator | [operator-simulator-mujoco](https://github.com/PhyAgentOS/operator-simulator-mujoco) | MuJoCo stepping, joint commands, images and state output | [0a84a1a81807](https://github.com/PhyAgentOS/operator-simulator-mujoco/tree/0a84a1a81807b467338e696032a38b084cb35747) |

### 2.4 Benchmark and organization configuration

| Category | Current source | Description | Source snapshot |
| --- | --- | --- | --- |
| Organization configuration | [.github](https://github.com/Forgelab-Robotics/.github) | Organization profile/overview; returned to Forgelab-Robotics | [2df5cc313ca9](https://github.com/Forgelab-Robotics/.github/tree/2df5cc313ca9193d6ceb75b87a85e77072d9d1aa) |
| Benchmark | [benchmark-libero](https://github.com/PhyAgentOS/benchmark-libero) | LIBERO simulation/evaluation and Node packaging; classified by responsibility without adding another Node category | [e01cd66167b8](https://github.com/PhyAgentOS/benchmark-libero/tree/e01cd66167b85522afe427c5b85da8af30c00c97) |

### 2.5 Six families inside forge

| Family | Main directories | Capabilities |
| --- | --- | --- |
| Common | `packages/common`, `crates/forge_common`, `cpp/forge_common` | Logging, tracing, utilities and optional publish/receive observability |
| Msgs | `interfaces/forge_msgs`, `packages/msgs`, `crates/forge_msgs`, `cpp/forge_msgs` | Authoritative schemas, message models and Arrow serialization |
| Robot | `packages/robot`, `cpp/forge_robot` | Driver protocols, joint-limit helpers and optional Dora runners |
| Policy | `packages/policy` | Policy plugin protocols, observation cache and Operator loop |
| Kinematics | `packages/kinematics` | URDF FK, Jacobian and IK helpers |
| Tool | `interfaces/forge_tool`, `packages/tool` | Endpoint SPI, Wire models, handlers and carrier bindings |

These are framework families, not six mandatory processes. Check language coverage and releases per family;
Python/Rust/C++ directories do not imply equal implementation of every interface.

## 3. robot-control-sim Node-to-source mapping

| Node in Skill lock | Source repository | Source entrypoint |
| --- | --- | --- |
| `gateway` | [adapter-forge-gateway](https://github.com/PhyAgentOS/adapter-forge-gateway) | [README.md](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/README.md) |
| `sim_control_policy` | [operator-sim-control-policy](https://github.com/PhyAgentOS/operator-sim-control-policy) | [README.md](https://github.com/PhyAgentOS/operator-sim-control-policy/blob/85679a4be8953e9071de426c7c0973155121d96f/README.md) |
| `motion_server / motion_action_policy` | [operator-motion](https://github.com/PhyAgentOS/operator-motion) | [packages/motion_server](https://github.com/PhyAgentOS/operator-motion/tree/24c8a430715f697eea8431bf169c21b8cbb5eb92/packages/motion_server) |
| `joint_trajectory_controller` | [operator-controls](https://github.com/PhyAgentOS/operator-controls) | [packages/joint_trajectory_controller](https://github.com/PhyAgentOS/operator-controls/tree/cde73dc2a6222d81b01ffc32f69f12cb12e42760/packages/joint_trajectory_controller) |
| `gripper_action_controller` | [operator-controls](https://github.com/PhyAgentOS/operator-controls) | [packages/gripper_action_controller](https://github.com/PhyAgentOS/operator-controls/tree/cde73dc2a6222d81b01ffc32f69f12cb12e42760/packages/gripper_action_controller) |
| `gripper_action_policy` | [operator-gripper-action-policy](https://github.com/PhyAgentOS/operator-gripper-action-policy) | [README.md](https://github.com/PhyAgentOS/operator-gripper-action-policy/blob/bb69223497b944dd1a0f6fe9ecb36486168a8cc4/README.md) |
| `mujoco_sim` | [operator-simulator-mujoco](https://github.com/PhyAgentOS/operator-simulator-mujoco) | [src/mujoco_sim](https://github.com/PhyAgentOS/operator-simulator-mujoco/tree/0a84a1a81807b467338e696032a38b084cb35747/src/mujoco_sim) |
| `image_viewer` | [adapter-viewer-image](https://github.com/PhyAgentOS/adapter-viewer-image) | [README.md](https://github.com/PhyAgentOS/adapter-viewer-image/blob/ea5900a7225c2bfadd89bbfe23c023ec9fbe1bb6/README.md) |

This table locates implementations; it does not prove binary build provenance. Original artifact IDs,
versions and SHA256 values come from the installed `skill.yaml`. Rebuilt replacements require their own
published version/digest and validation.

`operator-motion` provides motion server and motion action policy; `operator-controls` provides two controllers.
The example's nine Nodes therefore come from seven repositories. Repository count is not process count.
See [actual runtime relationships](RUNTIME.md#4-actual-robot-control-sim-assembly) for Endpoint routes and Node IDs.

## 4. Maintenance after transfer

- Use PhyAgentOS source URLs and the actual Forgelab-Robotics owner for `.github`.
- Organization transfer preserves repository/source identity and existing release assets. Owner changes do not upgrade package names, artifact IDs, tags or archive digests.
- Check old repository/Release compatibility links; do not occupy transferred repositories' old paths.
- Before publishing again, check CI credentials, package permissions, webhooks and branch protection; transfer alone does not validate build/publish access.
- Register catalog/database resources through the destination platform separately; GitHub transfer does not update a consumer Registry automatically.
- Dora/Forge upgrades require new Node/Skill versions. Check [protocols](PROTOCOLS.md) and dependency locks before cross-repository changes.

See the [development manual](SKILL_DEVELOPMENT.md) for registration vs local installation, immutable artifacts and rollback.

## 5. Node, Endpoint and Tool capability catalog

Counts follow public entrypoints, builds and descriptors, excluding helper functions, CLI aliases and example processes.
Direct Tool support means provider/caller/Gateway implementation; data-plane Nodes may participate indirectly through
existing providers. Optional-mode counts describe enabled provider capabilities, not default registration.

| Repository | Production Nodes | Direct Tool role | Endpoints / operations |
| --- | --- | --- | --- |
| forge | 0 (library) | SPI, Wire, runners | No concrete business Endpoint |
| adapter-camera-orbbec | 1 | None | 0 / 0; device enumeration/probe CLIs separate |
| adapter-camera-realsense | 1 | None | 0 / 0; aliases do not add Nodes |
| adapter-camera-usb | 1 | None | 0 / 0; test sink is auxiliary |
| adapter-forge-gateway | 1 | Gateway | Own business Endpoints 0; routes/operations/Tools depend on configuration and registrations |
| adapter-franka-fr3 | 1 | None | 0 / 0 |
| adapter-robot-piper | 1 | None | 0 / 0 |
| adapter-robot-so101 | 1 | None | 0 / 0; device-maintenance CLIs separate |
| adapter-robot-xlerobot | 1 | None | 0 / 0 |
| adapter-robot-zerith_h1_pro | 2 (robot, camera) | None | 0 / 0; compatibility aliases do not add Nodes |
| adapter-viewer-image | 1 | None | 0 / 0 |
| operator-controls | 2, plus fake plant | None | 0 / 0; Dora Action messages are not ToolEndpoint |
| operator-gripper-action-policy | 1 | Provider | 1 / 1: gripper.controller / set_opening (Action) |
| operator-kai0 | 1 | Optional provider | 1 / 1: policy.runner / serve (Session); mode=session_endpoint |
| operator-lerobot-runner | 1 | Optional provider | 1 / 1: policy.runner / serve (Session); mode=session_endpoint |
| operator-motion | 2 | Policy Node is provider | 1 / 2: motion.server / move_joints, move_pose (Action) |
| operator-relative-pose-policy | 1 | Provider | 1 / 1: motion.relative_pose / resolve (Query) |
| operator-sim-control-policy | 1 | Provider + caller | 1 / 2: sim.control / get_robot_info (Query), reset (Action) |
| operator-simulator-mujoco | 1 | None | 0 / 0; commands arrive via sim_command |
| benchmark-libero | 1 | Optional provider | 1 / 2: benchmark.controller / describe (Query), run (Session); serve=true |

Seven provider repositories plus Gateway directly implement Tool integration. Kai0 and LeRobot share `policy.runner`:
they are deployment alternatives for one route, not automatically separate Endpoints in one Gateway.
Each Endpoint ID has one current instance in the Gateway Directory.

Current LIBERO serve source composes the Endpoint and benchmark loop in one Node. Its older README described
an independent `bench_endpoint`, but the directory is absent from this snapshot, so it does not supply a second Node.
See [main.py](https://github.com/PhyAgentOS/benchmark-libero/blob/e01cd66167b85522afe427c5b85da8af30c00c97/main.py)
and [descriptor](https://github.com/PhyAgentOS/benchmark-libero/blob/e01cd66167b85522afe427c5b85da8af30c00c97/bench_tool/descriptor.py).

These providers do not directly serve HTTP Tool APIs; Gateway owns the caller-facing Tool/Invocation routes.
The pinned Gateway source has 10 such HTTP route templates, including Session stop, excluding other
runtime/record/playback/WS APIs. See its [HTTP controller](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/src/forge_gateway/controllers/tool_controller.py).
Check historical Skill binary capabilities using their locked versions.

Required README fields/template: [development manual](SKILL_DEVELOPMENT.md#required-node-repository-readme-content).

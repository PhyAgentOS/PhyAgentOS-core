# TrackB Protocols and Important Node Types

[English](PROTOCOLS.md) | [简体中文](PROTOCOLS_zh.md)

[Overview](README.md) · [Repositories](REPOSITORIES.md) · [Protocols](PROTOCOLS.md) · [Runtime](RUNTIME.md) · [Skill development](SKILL_DEVELOPMENT.md)

This page summarizes interface entrypoints and constraints; pinned source schemas/interfaces define the authoritative fields.
See the [overview](README.md) and [catalog](REPOSITORIES.md) for categories/code organization,
and [Runtime relationships](RUNTIME.md) for deployment mappings. These contracts describe the Forge source snapshot;
older Skill binaries require their own compatible versions and must not be mixed indiscriminately.

## 1. Data plane: Forge Msgs

Standard messages use single-row Apache Arrow `RecordBatch` values. Put semantics in actual columns,
not Arrow schema metadata that Dora/IPC may discard. There is no universal ROS Header: timing comes
from Dora event context, while frames/fixed calibration come from configuration or Dora metadata.
Publish-time HLC is not sensor acquisition time; preserve explicitly supplied source sampling timestamps.
Action `goal_id` / `goal_status` are explicit event-metadata exceptions and must be recorded alongside payloads.

Definitions: [schema guide](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/SCHEMA.md), [units and timing](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/common.v1.yaml).

## 2. Robot Adapter

Python drivers implement four `RobotDriver` methods:

```python
connect() -> None
disconnect() -> None
get_state() -> JointState
set_command(command: JointCommand) -> None
```

Optional `LocomotionRobotDriver.set_locomotion_command(command)` adds base control.
`BaseRobotDriver.joint_order` may declare fixed joint order. Drivers own connection, enabling,
limits and device resource cleanup.

| Standard runner port | Message/behavior |
| --- | --- |
| Input `tick` | Read state and publish `state` |
| Input `action` / `action/<source>` | Sparse `JointCommand`; update named joints only, retaining other targets |
| Input `master_state` | Map leader `JointState` to a position command |
| Input `locomotion_command` | Accept `LocomotionCommand` if supported |
| Output `state` | `JointState`; a Node using other port names is governed by its dataflow |

`JointState.name` is nonempty and unique; `position/velocity/effort` are each empty or match its length.
`JointCommand` adds `mode` and `kp/kd`, with `position/velocity/effort/hybrid` modes; missing/null mode
in older payloads means position. Representability does not guarantee driver support.
Revolute joints use rad, rad/s, Nm; prismatic joints use m, m/s, N.
Disjoint sources can update separate joints; overlapping writers still need explicit arbitration.

`LocomotionCommand` is body-frame: `vx` forward, `vy` left (m/s), `wz` counterclockwise (rad/s).
Zero velocity expresses stop; each driver must define watchdogs, limits and unsupported lateral-motion behavior.

Source: [driver interface](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/robot/src/forge_robot/robot_protocol.py), [runner](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/robot/README.md), [control schema](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/control.v1.yaml).

## 3. Sensor / Camera Adapter

The inspected source has no unified `SensorDriver` base corresponding to `RobotDriver`.
Shared boundaries are message schemas; camera repositories implement connection, enumeration,
sampling, reconnection and ports individually. Do not present a proposed template as an existing API.

| Message | Fields and constraints |
| --- | --- |
| `Image` | `height/width/encoding/step/data`; byte stride `step` may include padding; `len(data)=step*height`; multibyte pixels are little-endian |
| `CompressedImage` | `format/data`; JPEG/PNG/WebP must not masquerade as raw Image |
| `PointCloud` | `width/height/is_dense`, `x/y/z: list<float32>` in metres; empty lists represent absent optional attributes; organized clouds retain row/column correspondence |
| `PointCloudBuffer` | Decoded point records preserving dynamic fields, point/row strides and byte order; not raw device packets |
| `Imu` | Angular velocity rad/s, acceleration m/s²; null unavailable orientation; covariance empty or 9 elements; never silently normalize quaternions |
| `AudioChunk` | `sample_rate/channels/sample_format/frame_count/data`; interleaved f32le/s16le frames |

Orbbec and RealSense currently output depth as `Image(32FC1)`, float32 metres with zero invalid.
This is their Adapter convention, not a universal scale inferred from Image encoding.
Producers must document optical frames, alignment, calibration and sampling time.
IMU/audio schemas do not imply that matching hardware Adapters already exist.

Source: [sensor schema](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/sensor.v1.yaml), [Orbbec](https://github.com/PhyAgentOS/adapter-camera-orbbec/blob/de3cbc5361533d8799f586939266289d7527b203/README.md), [RealSense](https://github.com/PhyAgentOS/adapter-camera-realsense/blob/77627b4d8e3977aabf016af6846e9f6857cfcde6/README.md).

## 4. Policy Operator

The standard `PolicyAdapter` plugin requires at least:

```python
is_observation_needed() -> bool
generate_action(observation: dict, alias_for_cameras: list[str] | None = None)
```

The runner handles tick, `proprio_state`, image caches and internal `PolicyCommand` lifecycle messages.
`PolicyCommand` contains `policy_id/command/request_id/inputs_json`; optional `PolicyCommandStatus`
uses `accepted/rejected/running/done/error`. Supported commands depend on the implementation.
These internal messages do not replace Core's user-facing Tool API; Agents invoke declared capabilities through Gateway.

Source: [plugin interface](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/policy/src/forge_policy/policy_protocol.py), [runner](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/policy/src/forge_policy/node_runner.py).

## 5. Motion / Controller Operator

| Action family | Goal | Typical execution chain |
| --- | --- | --- |
| `MoveJoints` | group, joint_names, positions, velocity/acceleration scales | motion policy -> motion_server -> joint trajectory |
| `MovePose` | group, reference_frame, target_frame, target_pose, tolerances | motion policy -> motion_server IK -> joint trajectory |
| `FollowJointTrajectory` | Ordered joints, points, relative times, tolerances | joint_trajectory_controller -> JointCommand |
| `GripperCommand` | One configured gripper-coordinate target | gripper controller -> JointCommand |

Each family has Goal/Feedback/Result. Durations are nonnegative int64 nanoseconds relative to Action/trajectory
start, not wall-clock time. `MovePose.target_pose` expresses target_frame in reference_frame, with position
in metres and quaternion fields `qx/qy/qz/qw`. Gripper coordinates may be linear opening or rotation;
read Endpoint/controller configuration rather than assuming metres.
Dora terminal Action metadata uses `succeeded/aborted/canceled`, distinct from Gateway invocation phases.
Position trajectory controllers do not provide hard-real-time motor servos; hardware owns low-level control.

Source: [motion schema](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/motion.v1.yaml), [Motion server](https://github.com/PhyAgentOS/operator-motion/blob/24c8a430715f697eea8431bf169c21b8cbb5eb92/packages/motion_server/README.md), [controllers](https://github.com/PhyAgentOS/operator-controls/blob/cde73dc2a6222d81b01ffc32f69f12cb12e42760/README.md).

## 6. Simulator Operator

MuJoCo accepts JointCommand on `action` / `action/<source>` and publishes `proprio_state`, `image/*`
and optional `depth/*`. Control ticks may execute multiple physics substeps; control frequency and
physics timestep are configured separately. `joint_to_actuator`, `actuator_order` and logical gripper
state expressions must match the model. `sim_command` / `reset_scene` is a simulator extension used
by sim-control policy, not the general RobotDriver contract. Inspect runtime code as well as the README's I/O list.

Source: [simulator runtime](https://github.com/PhyAgentOS/operator-simulator-mujoco/blob/0a84a1a81807b467338e696032a38b084cb35747/src/mujoco_sim/runtime.py), [scene-control contract](https://github.com/PhyAgentOS/operator-sim-control-policy/blob/85679a4be8953e9071de426c7c0973155121d96f/TOOL_CONTRACTS.md).

## 7. Gateway / ToolEndpoint control plane

Keep three layers distinct: caller-facing HTTP Tool API, Gateway/provider Endpoint Wire and provider-internal SPI.

| Layer | Contract |
| --- | --- |
| Core -> Gateway | `/tools`, `/invocations`; see this repository's Forge Tool API guide |
| Gateway -> provider | `forge.tool.endpoint/v1alpha1`, logical JSON envelopes in Arrow `ToolMessage` |
| Provider SPI | Query: `query`; Action: `start/cancel/status/result`; Session: `start/stop/status/result` |

Wire distinguishes `request_id` (exchange), `invocation_id` (logical invocation), `attempt_id`
(execution attempt) and Endpoint identity. JSON numbers must be finite and integers within ±(2^53−1);
do not place oversized monotonic nanosecond counters in JSON integers.
High-frequency images, joint states and trajectories remain on the data plane.

Check model, handler and deployed-chain capabilities separately:

- The pinned Forge `packages/tool/src/forge_tool/handler.py` dispatches Query/Action/Session; Action uses cancel, Session uses stop. Library support does not mean every Node exposes every semantic.
- The pinned standalone Gateway implementation and `tests/test_tool_session.py` support Query/Action/Session. Session has no Action runtime deadline, but handshakes remain bounded and leases still apply. This robot-control-sim example uses Query/Action only. That commit's older README claiming no Session support does not override code.
- Core Session client contracts alone do not establish compatibility of a particular Gateway/provider pair.
- Architecture/package guides can reflect different development stages; resolve discrepancies through matching code, tests and deployment validation.

HTTP 202 acceptance, Action terminal state and AgentTask verdict are distinct stages;
accepted cancellation does not establish physical stopping.

Implementation: [provider handler](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/tool/src/forge_tool/handler.py),
[Gateway Session implementation](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/src/forge_gateway/services/tool_gateway_service.py),
[Session tests](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/tests/test_tool_session.py).

Source: [Endpoint Wire](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_tool/PROTOCOL.md), [SPI](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/tool/src/forge_tool/endpoint/protocol.py), [ToolMessage](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/tool.v1.yaml); [Core contract](../../forge/README.md).

# TrackB 公共协议与重要节点类型

[English](PROTOCOLS.md) | [简体中文](PROTOCOLS_zh.md)

[整体架构](README_zh.md) · [仓库目录](REPOSITORIES_zh.md) · [公共协议](PROTOCOLS_zh.md) · [Runtime 关系](RUNTIME_zh.md) · [Skill 开发](SKILL_DEVELOPMENT_zh.md)

本页是接口导航与关键约束摘要，权威字段定义以固定源码中的 schema/接口为准。
节点类别与代码目录见 [整体架构](README_zh.md) 和 [仓库目录](REPOSITORIES_zh.md)；
Tool/Endpoint/Gateway 在部署中的对应方式见 [Runtime 关系](RUNTIME_zh.md)。
以下以 Forge 源码快照说明公共契约，旧 Skill 二进制必须使用自己的兼容版本，不能据此直接混装。

## 1. 数据平面：Forge Msgs

节点之间的标准消息使用单行 Apache Arrow `RecordBatch`。语义必须位于真实列中，
不能依赖可能被 Dora/IPC 丢弃的 Arrow schema metadata。
标准消息没有统一的 ROS Header；时间来自 Dora event context，坐标系和固定标定来自配置或 Dora metadata。
Dora 发布时生成的 HLC 不等于传感器采集时间。派生观测应保留明确提供的源采样时间。
动作的 `goal_id` / `goal_status` 是 event metadata 的显式例外，录制时必须一并保存。

权威定义：[Schema 导读](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/SCHEMA.md)、[通用单位与时间约定](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/common.v1.yaml)。

## 2. Robot Adapter

Python 驱动实现 `RobotDriver` 的四个方法：

```python
connect() -> None
disconnect() -> None
get_state() -> JointState
set_command(command: JointCommand) -> None
```

可选 `LocomotionRobotDriver.set_locomotion_command(command)` 提供移动底盘控制。
`BaseRobotDriver.joint_order` 可声明固定关节顺序。连接、使能、限位与设备资源释放由驱动实现。

| 标准 runner 端口 | 消息及行为 |
| --- | --- |
| 输入 `tick` | 读取状态并输出 `state` |
| 输入 `action` / `action/<source>` | 稀疏 `JointCommand`，仅更新 `name` 指定关节；未指定关节保留原目标 |
| 输入 `master_state` | 将 leader 的 `JointState` 映射为位置命令 |
| 输入 `locomotion_command` | 支持移动能力时接收 `LocomotionCommand` |
| 输出 `state` | `JointState`；具体节点若使用不同端口名，以其 dataflow 为准 |

`JointState`：`name` 非空且唯一；`position/velocity/effort` 各为空列表或与 `name` 等长。
`JointCommand` 增加 `mode` 和 `kp/kd`；mode 支持 `position/velocity/effort/hybrid`，
旧载荷缺失或 null mode 按 position 解读。消息能表达某种模式，不表示每个驱动都实现该模式。
旋转关节使用 rad、rad/s、Nm；移动关节使用 m、m/s、N。
不同源可更新互不相交的关节，同一关节的多个写入源仍需显式仲裁。

`LocomotionCommand`：`vx` 向前、`vy` 向左，单位 m/s；`wz` 逆时针，单位 rad/s，均为机体系。
零速度表达停止；实际驱动仍需定义 watchdog、限速及不支持侧移时的行为。

源码：[驱动接口](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/robot/src/forge_robot/robot_protocol.py)、[标准 runner](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/robot/README.md)、[控制消息 schema](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/control.v1.yaml)。

## 3. Sensor / Camera Adapter

本次核查没有发现与 `RobotDriver` 对应的统一 `SensorDriver` 基类；当前共享边界是消息 schema，
设备连接、枚举、采样、重连与端口配置由各相机仓库实现。不要把建议的驱动模板写成现有 API。

| 消息 | 核心字段和约束 |
| --- | --- |
| `Image` | `height/width/encoding/step/data`；`step` 为每行字节数，可含 padding；`len(data)=step*height`；多字节像素使用 little-endian |
| `CompressedImage` | `format/data`，压缩 JPEG/PNG/WebP 不得伪装成 raw Image |
| `PointCloud` | `width/height/is_dense`、`x/y/z: list<float32>`，单位 m；可选属性以空列表表达；组织化点云保留行列对应关系 |
| `PointCloudBuffer` | 保留动态字段、point/row stride 和字节序的已解码点记录；不是设备原始包 |
| `Imu` | 角速度 rad/s、加速度 m/s²；orientation 不可用时 null；协方差为空或 9 个数；不能静默归一化四元数 |
| `AudioChunk` | `sample_rate/channels/sample_format/frame_count/data`；f32le/s16le，按帧交错 |

当前 Orbbec 与 RealSense 节点将深度输出为 `Image(32FC1)`，float32 米，零值无效。
这是这两个 Adapter 的输出约定；不能仅凭通用 Image 的 encoding 推断所有设备的深度比例。
相机光学坐标系、深度对齐方式、标定和采样时刻必须由生产者说明。
Sensor schema 包含 IMU/音频类型，不表示组织中已有对应硬件 Adapter。

源码：[Sensor schema](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/sensor.v1.yaml)、[Orbbec 输出约定](https://github.com/PhyAgentOS/adapter-camera-orbbec/blob/de3cbc5361533d8799f586939266289d7527b203/README.md)、[RealSense 输出约定](https://github.com/PhyAgentOS/adapter-camera-realsense/blob/77627b4d8e3977aabf016af6846e9f6857cfcde6/README.md)。

## 4. Policy Operator

标准算法插件 `PolicyAdapter` 的最小接口为：

```python
is_observation_needed() -> bool
generate_action(observation: dict, alias_for_cameras: list[str] | None = None)
```

runner 处理 tick、`proprio_state`、图像缓存，以及内部 `PolicyCommand` 生命周期消息。
`PolicyCommand` 包含 `policy_id/command/request_id/inputs_json`；可选的 `PolicyCommandStatus`
使用 `accepted/rejected/running/done/error`。具体支持的 command 取决于实现。
这组内部消息不替代 Core 面向用户的 Tool API；Agent 应经 Gateway 调用已声明的能力。

源码：[插件接口](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/policy/src/forge_policy/policy_protocol.py)、[runner](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/policy/src/forge_policy/node_runner.py)。

## 5. Motion / Controller Operator

| 动作族 | 目标 | 典型执行链 |
| --- | --- | --- |
| `MoveJoints` | group、joint_names、positions、速度/加速度比例 | motion policy → motion_server → 关节轨迹 |
| `MovePose` | group、reference_frame、target_frame、target_pose、容差 | motion policy → motion_server 的 IK → 关节轨迹 |
| `FollowJointTrajectory` | 有序关节、轨迹点、相对时间与容差 | joint_trajectory_controller → JointCommand |
| `GripperCommand` | 配置选定的一个夹爪坐标目标 | gripper controller → JointCommand |

每族都有 Goal/Feedback/Result。持续时间为非负 int64 纳秒，相对动作或轨迹起点，不能当墙钟时间。
`MovePose.target_pose` 表达 target_frame 在 reference_frame 下的位置与姿态，位置 m，四元数字段 `qx/qy/qz/qw`。
夹爪坐标可为平移开度或旋转角，必须看 endpoint/controller 配置，不能一律当米。
Dora 动作 metadata 的终态是 `succeeded/aborted/canceled`；不要与 Gateway invocation phase 混用。
位置轨迹 controller 不提供硬实时电机伺服；硬件低层控制仍由设备负责。

源码：[Motion schema](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/motion.v1.yaml)、[Motion server](https://github.com/PhyAgentOS/operator-motion/blob/24c8a430715f697eea8431bf169c21b8cbb5eb92/packages/motion_server/README.md)、[Controllers](https://github.com/PhyAgentOS/operator-controls/blob/cde73dc2a6222d81b01ffc32f69f12cb12e42760/README.md)。

## 6. Simulator Operator

MuJoCo 接收 `action` / `action/<source>` 的 JointCommand，输出 `proprio_state`、`image/*` 和可选 `depth/*`。
控制 tick 内运行若干物理子步；控制频率与物理 timestep 分开配置。
`joint_to_actuator`、`actuator_order`、逻辑夹爪状态表达式必须与模型匹配。
`sim_command` 的 `reset_scene` 是此模拟器的扩展输入，由 sim-control policy 使用；它不是通用 RobotDriver 协议。
当前 README 的 I/O 列表未穷尽该扩展，接入时同时核对 runtime 实现。

源码：[Simulator runtime](https://github.com/PhyAgentOS/operator-simulator-mujoco/blob/0a84a1a81807b467338e696032a38b084cb35747/src/mujoco_sim/runtime.py)、[场景控制契约](https://github.com/PhyAgentOS/operator-sim-control-policy/blob/85679a4be8953e9071de426c7c0973155121d96f/TOOL_CONTRACTS.md)。

## 7. Gateway / ToolEndpoint 控制平面

分清三层：外部调用者的 HTTP Tool API、Gateway 与 provider 之间的 Endpoint Wire、provider 内部的 SPI。
本文不将它们合并成一个协议。

| 层 | 契约 |
| --- | --- |
| Core → Gateway | `/tools`、`/invocations`；见本仓 Forge Tool API 文档 |
| Gateway → provider | `forge.tool.endpoint/v1alpha1`，以 Arrow `ToolMessage` 承载逻辑 JSON envelope |
| provider SPI | Query：`query`；Action：`start/cancel/status/result`；Session：`start/stop/status/result` |

Wire 明确区分 `request_id`（一次交换）、`invocation_id`（逻辑调用）、`attempt_id`（执行尝试）和 endpoint 身份。
JSON 数字必须有限，整数在 ±(2^53−1) 内；不要把超大单调时钟纳秒数直接塞入 JSON 整数。
高频图像、关节状态与轨迹留在数据平面。

协议模型、provider handler 和整条部署链的能力必须分开核对：

- 固定 Forge 快照的 `packages/tool/src/forge_tool/handler.py` 包含 Query、Action、Session dispatch，
  Action 使用 cancel，Session 使用 stop；接口库实现不等于每个节点都暴露这些能力。
- 独立 Gateway 当前固定快照的实现和 `tests/test_tool_session.py` 支持 Query、Action、Session；
  Session 使用 stop，运行期无 Action deadline，但调用握手有界、provider lease 仍有效。本组 robot-control-sim 示例只使用 Query/Action。
  该提交的旧 README 曾写“不实现 Session”，不能据此覆盖实际代码能力。
- Core 存在 Session 客户端契约，也不能据此推断目标 Gateway/provider 组合已支持 Session。
- 不同仓库的架构说明和包指南可能记录不同演进阶段。出现状态描述差异时，以匹配版本的实现、测试和部署验收为依据。

HTTP 202 受理、动作终态和 AgentTask verdict 是不同阶段；取消请求被接受也不证明物理停止。

实现依据：[provider handler](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/tool/src/forge_tool/handler.py)、
[Gateway Session 实现](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/src/forge_gateway/services/tool_gateway_service.py)、
[Session 测试](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/tests/test_tool_session.py)。

源码：[Endpoint Wire](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_tool/PROTOCOL.md)、[SPI](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/packages/tool/src/forge_tool/endpoint/protocol.py)、[ToolMessage](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_msgs/tool.v1.yaml)；[Core 调用契约](../../forge/README_zh.md)。

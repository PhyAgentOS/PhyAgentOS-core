# TrackB 仓库目录与代码组成

[English](REPOSITORIES.md) | [简体中文](REPOSITORIES_zh.md)

[整体架构](README_zh.md) · [仓库目录](REPOSITORIES_zh.md) · [公共协议](PROTOCOLS_zh.md) · [Runtime 关系](RUNTIME_zh.md) · [Skill 开发](SKILL_DEVELOPMENT_zh.md)

## 1. 范围与定位

本页索引本次迁移涉及的 20 个公开代码仓库：10 个 Adapter、8 个 Operator、Forge 基础库和
1 个 benchmark；另列出 Forgelab-Robotics 的组织配置 `.github`。它不是 PhyAgentOS 组织全部项目的清单。
Core 的任务与 Skill 生命周期代码位于 [本仓源码](../../../PhyAgentOS)，与这些节点仓库分层维护。

目录链接指向当前仓库；提交链接固定源码核查快照。职责分类不代表各节点都已在同一 Skill 中集成，
也不表示所有平台、语言或硬件能力均已验收。

## 2. 分类索引

### 2.1 基础框架

| 类别 | 当前仓库源码 | 描述 / 主要内容 | 核查提交 |
| --- | --- | --- | --- |
| 基础库 | [forge](https://github.com/PhyAgentOS/forge) | Common、Msgs、Robot、Policy、Kinematics、Tool；跨语言消息与接口 | [e59693910bb5](https://github.com/PhyAgentOS/forge/tree/e59693910bb5f9572cf39c5a59475c0747ba76e8) |

### 2.2 Adapter：对外边界

| 类别 | 当前仓库源码 | 描述 / 主要内容 | 核查提交 |
| --- | --- | --- | --- |
| Adapter · Sensor | [adapter-camera-orbbec](https://github.com/PhyAgentOS/adapter-camera-orbbec) | Orbbec 彩色/深度/IR/可选点云采集、设备初始化和发布 | [de3cbc536153](https://github.com/PhyAgentOS/adapter-camera-orbbec/tree/de3cbc5361533d8799f586939266289d7527b203) |
| Adapter · Sensor | [adapter-camera-realsense](https://github.com/PhyAgentOS/adapter-camera-realsense) | RealSense D400 彩色/深度/IR/可选点云采集与发布 | [77627b4d8e39](https://github.com/PhyAgentOS/adapter-camera-realsense/tree/77627b4d8e3977aabf016af6846e9f6857cfcde6) |
| Adapter · Sensor | [adapter-camera-usb](https://github.com/PhyAgentOS/adapter-camera-usb) | Rust/V4L2 USB 相机；raw/压缩图像与设备检查 | [bdbe5ebb016d](https://github.com/PhyAgentOS/adapter-camera-usb/tree/bdbe5ebb016da299ff1e0b461260ce1ddd52d234) |
| Adapter · Gateway | [adapter-forge-gateway](https://github.com/PhyAgentOS/adapter-forge-gateway) | HTTP/WebSocket 外部接口、Tool 路由、Registry 和就绪状态 | [5aaa9d16aff1](https://github.com/PhyAgentOS/adapter-forge-gateway/tree/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97) |
| Adapter · Robot | [adapter-franka-fr3](https://github.com/PhyAgentOS/adapter-franka-fr3) | Franka FR3v2 与 Franka Hand 驱动及模型 | [efd7a6bd019c](https://github.com/PhyAgentOS/adapter-franka-fr3/tree/efd7a6bd019c7135ae506274ca73ef5879f4a384) |
| Adapter · Robot | [adapter-robot-piper](https://github.com/PhyAgentOS/adapter-robot-piper) | AgileX Piper 真机驱动、Dora 示例与多格式机器人模型 | [3ea5156cb369](https://github.com/PhyAgentOS/adapter-robot-piper/tree/3ea5156cb369c302bb2e3ff6f1ebfb6af90a08d8) |
| Adapter · Robot | [adapter-robot-so101](https://github.com/PhyAgentOS/adapter-robot-so101) | SO101 串口/Feetech 驱动、设备工具和 MJCF 模型 | [fb6b59292366](https://github.com/PhyAgentOS/adapter-robot-so101/tree/fb6b59292366059acd10cb2439d155c4c8015cfc) |
| Adapter · Robot | [adapter-robot-xlerobot](https://github.com/PhyAgentOS/adapter-robot-xlerobot) | XLeRobot 双臂轮式机器人适配、配置和模型 | [5ceb37fdea4d](https://github.com/PhyAgentOS/adapter-robot-xlerobot/tree/5ceb37fdea4d6c7e454914b1b5e180b4728ef0fa) |
| Adapter · Robot | [adapter-robot-zerith_h1_pro](https://github.com/PhyAgentOS/adapter-robot-zerith_h1_pro) | Zerith H1 Pro 关节/底盘适配、SDK 集成与示例 | [e36dc9b0bdfd](https://github.com/PhyAgentOS/adapter-robot-zerith_h1_pro/tree/e36dc9b0bdfd68342d55180485b561c73162cfc3) |
| Adapter · Viewer | [adapter-viewer-image](https://github.com/PhyAgentOS/adapter-viewer-image) | 图像显示窗口，消费 Forge 图像消息 | [ea5900a7225c](https://github.com/PhyAgentOS/adapter-viewer-image/tree/ea5900a7225c2bfadd89bbfe23c023ec9fbe1bb6) |

### 2.3 Operator：图内计算

| 类别 | 当前仓库源码 | 描述 / 主要内容 | 核查提交 |
| --- | --- | --- | --- |
| Operator · Control | [operator-controls](https://github.com/PhyAgentOS/operator-controls) | joint_trajectory_controller、gripper_action_controller 与 fake plant | [cde73dc2a622](https://github.com/PhyAgentOS/operator-controls/tree/cde73dc2a6222d81b01ffc32f69f12cb12e42760) |
| Operator · Policy | [operator-gripper-action-policy](https://github.com/PhyAgentOS/operator-gripper-action-policy) | 将 Tool 夹爪请求转换为 gripper action 并返回执行结果 | [bb69223497b9](https://github.com/PhyAgentOS/operator-gripper-action-policy/tree/bb69223497b944dd1a0f6fe9ecb36486168a8cc4) |
| Operator · Policy | [operator-kai0](https://github.com/PhyAgentOS/operator-kai0) | kai0 策略源码、推理运行与节点发布 | [94baa639fe6d](https://github.com/PhyAgentOS/operator-kai0/tree/94baa639fe6d36bb5d41d214923108993aa002cc) |
| Operator · Policy | [operator-lerobot-runner](https://github.com/PhyAgentOS/operator-lerobot-runner) | LeRobot 策略运行、观测与控制流程 | [f6b26f4f89bb](https://github.com/PhyAgentOS/operator-lerobot-runner/tree/f6b26f4f89bbd9831f64e3fc43d36499d6c86a92) |
| Operator · Motion | [operator-motion](https://github.com/PhyAgentOS/operator-motion) | motion_server、motion_action_policy；运动目标、IK 和轨迹生成 | [24c8a430715f](https://github.com/PhyAgentOS/operator-motion/tree/24c8a430715f697eea8431bf169c21b8cbb5eb92) |
| Operator · Policy | [operator-relative-pose-policy](https://github.com/PhyAgentOS/operator-relative-pose-policy) | 相对末端位姿解析与 motion.relative_pose 工具接入 | [4749b696a8b3](https://github.com/PhyAgentOS/operator-relative-pose-policy/tree/4749b696a8b3210e193860d6aeed92382fbad113) |
| Operator · Policy | [operator-sim-control-policy](https://github.com/PhyAgentOS/operator-sim-control-policy) | 机器人 descriptor 查询、仿真复位与延迟 re-home | [85679a4be895](https://github.com/PhyAgentOS/operator-sim-control-policy/tree/85679a4be8953e9071de426c7c0973155121d96f) |
| Operator · Simulator | [operator-simulator-mujoco](https://github.com/PhyAgentOS/operator-simulator-mujoco) | MuJoCo 状态推进、关节命令、图像和状态输出 | [0a84a1a81807](https://github.com/PhyAgentOS/operator-simulator-mujoco/tree/0a84a1a81807b467338e696032a38b084cb35747) |

### 2.4 Benchmark 与组织配置

| 类别 | 当前仓库源码 | 描述 / 主要内容 | 核查提交 |
| --- | --- | --- | --- |
| 组织配置 | [.github](https://github.com/Forgelab-Robotics/.github) | 组织 profile 和公共介绍；已转回 Forgelab-Robotics | [2df5cc313ca9](https://github.com/Forgelab-Robotics/.github/tree/2df5cc313ca9193d6ceb75b87a85e77072d9d1aa) |
| Benchmark | [benchmark-libero](https://github.com/PhyAgentOS/benchmark-libero) | LIBERO 仿真/基准运行与节点发布；按实际节点职责归类，不新增第三类节点 | [e01cd66167b8](https://github.com/PhyAgentOS/benchmark-libero/tree/e01cd66167b85522afe427c5b85da8af30c00c97) |

### 2.5 forge 仓库内部的六个家族

| 家族 | 主要目录 | 提供的能力 |
| --- | --- | --- |
| Common | `packages/common`、`crates/forge_common`、`cpp/forge_common` | 日志、tracing、公共工具及可选发布/接收观测 |
| Msgs | `interfaces/forge_msgs`、`packages/msgs`、`crates/forge_msgs`、`cpp/forge_msgs` | 权威 schema、消息模型与 Arrow 序列化 |
| Robot | `packages/robot`、`cpp/forge_robot` | 驱动接口、关节限制辅助、可选标准 Dora runner |
| Policy | `packages/policy` | 策略插件接口、观测缓存与标准 Operator loop |
| Kinematics | `packages/kinematics` | URDF FK、Jacobian 和 IK 辅助 |
| Tool | `interfaces/forge_tool`、`packages/tool` | Endpoint SPI、Wire 模型、handler 与 carrier binding |

六家族是框架能力划分，不是必须启动的六个进程。语言覆盖和版本发布按各家族说明核对，
不能把 Python、Rust、C++ 目录存在等同于所有接口均同等实现。

## 3. robot-control-sim 的 Node 与源码映射

| Skill lock 中的 Node | 源码仓库 | 源码入口 |
| --- | --- | --- |
| `gateway` | [adapter-forge-gateway](https://github.com/PhyAgentOS/adapter-forge-gateway) | [README.md](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/README.md) |
| `sim_control_policy` | [operator-sim-control-policy](https://github.com/PhyAgentOS/operator-sim-control-policy) | [README.md](https://github.com/PhyAgentOS/operator-sim-control-policy/blob/85679a4be8953e9071de426c7c0973155121d96f/README.md) |
| `motion_server / motion_action_policy` | [operator-motion](https://github.com/PhyAgentOS/operator-motion) | [packages/motion_server](https://github.com/PhyAgentOS/operator-motion/tree/24c8a430715f697eea8431bf169c21b8cbb5eb92/packages/motion_server) |
| `joint_trajectory_controller` | [operator-controls](https://github.com/PhyAgentOS/operator-controls) | [packages/joint_trajectory_controller](https://github.com/PhyAgentOS/operator-controls/tree/cde73dc2a6222d81b01ffc32f69f12cb12e42760/packages/joint_trajectory_controller) |
| `gripper_action_controller` | [operator-controls](https://github.com/PhyAgentOS/operator-controls) | [packages/gripper_action_controller](https://github.com/PhyAgentOS/operator-controls/tree/cde73dc2a6222d81b01ffc32f69f12cb12e42760/packages/gripper_action_controller) |
| `gripper_action_policy` | [operator-gripper-action-policy](https://github.com/PhyAgentOS/operator-gripper-action-policy) | [README.md](https://github.com/PhyAgentOS/operator-gripper-action-policy/blob/bb69223497b944dd1a0f6fe9ecb36486168a8cc4/README.md) |
| `mujoco_sim` | [operator-simulator-mujoco](https://github.com/PhyAgentOS/operator-simulator-mujoco) | [src/mujoco_sim](https://github.com/PhyAgentOS/operator-simulator-mujoco/tree/0a84a1a81807b467338e696032a38b084cb35747/src/mujoco_sim) |
| `image_viewer` | [adapter-viewer-image](https://github.com/PhyAgentOS/adapter-viewer-image) | [README.md](https://github.com/PhyAgentOS/adapter-viewer-image/blob/ea5900a7225c2bfadd89bbfe23c023ec9fbe1bb6/README.md) |

该表是实现位置映射，不是二进制构建来源证明。demo 的原始 artifact ID/version/SHA256 从安装后的
`skill.yaml` 继承；若要自行构建替换，必须发布自己的版本及摘要并重新验收。

同一个 `operator-motion` 仓库提供 motion server 和 motion action policy；
`operator-controls` 提供两个 controller。因此，本例的九个 Node 来自七个仓库，仓库数量不等于进程数量。
Gateway 的 Endpoint 路由与 Node ID 对照见 [实际运行关系](RUNTIME_zh.md#4-robot-control-sim-的实际组装)。

## 4. 迁移后维护

- 源码索引使用迁移后的 PhyAgentOS URL；`.github` 使用其实际所属 Forgelab-Robotics URL。
- 组织迁移保留仓库名、源码身份和既有发布资产；包名、artifact ID、tag、归档摘要不因 owner 改变而升级。
- 对旧代码仓库和 Release 地址实际检查兼容入口；不要重新占用已转移代码仓库的旧路径。
- 下一次发布前，维护者核对 CI 凭据、包仓库权限、webhook 和分支保护，不能仅凭 transfer 判断构建发布权限已验证。
- 资源目录或数据库登记要按目标平台的流程单独完成；GitHub 转移不自动更新消费者 Registry。
- Dora/Forge 协议升级使用新的 Node/Skill 版本；跨仓修改前先核对 [公共协议](PROTOCOLS_zh.md) 和依赖锁。

资源登记与本地安装的区别，以及不可变制品与回退方式，见 [Skill 开发手册](SKILL_DEVELOPMENT_zh.md)。

## 5. Node、Endpoint 与 Tool 能力清单

以下按公开源码入口、构建配置和 descriptor 分类，不把仓库中的工具函数、CLI 别名或示例进程计为生产 Node。
这里的 Tool 支持指直接实现 provider/caller/Gateway；数据平面 Node 可以通过已有 provider 间接参与 Tool 执行。
对可选模式，Endpoint 数和 operation 数均指启用 provider 后的能力，不代表默认会注册。

| 仓库 | 生产 Node 数 | 直接 Tool 角色 | Endpoint / operation 数与范围 |
| --- | --- | --- | --- |
| forge | 0（基础库） | SPI、Wire、runner | 无具体业务 Endpoint |
| adapter-camera-orbbec | 1 | 无 | 0 / 0；设备列举/探测 CLI 另计 |
| adapter-camera-realsense | 1 | 无 | 0 / 0；命令别名不重复计数 |
| adapter-camera-usb | 1 | 无 | 0 / 0；test sink 为辅助程序 |
| adapter-forge-gateway | 1 | Gateway | 自有业务 Endpoint 0；路由/operation/Tool 数按配置与当前注册决定 |
| adapter-franka-fr3 | 1 | 无 | 0 / 0 |
| adapter-robot-piper | 1 | 无 | 0 / 0 |
| adapter-robot-so101 | 1 | 无 | 0 / 0；设备维护 CLI 另计 |
| adapter-robot-xlerobot | 1 | 无 | 0 / 0 |
| adapter-robot-zerith_h1_pro | 2（robot、camera） | 无 | 0 / 0；兼容命令别名不重复计数 |
| adapter-viewer-image | 1 | 无 | 0 / 0 |
| operator-controls | 2（另有 fake plant） | 无 | 0 / 0；Dora Action 消息不等于 ToolEndpoint |
| operator-gripper-action-policy | 1 | provider | 1 / 1：gripper.controller / set_opening（Action） |
| operator-kai0 | 1 | 可选 provider | 1 / 1：policy.runner / serve（Session）；mode=session_endpoint |
| operator-lerobot-runner | 1 | 可选 provider | 1 / 1：policy.runner / serve（Session）；mode=session_endpoint |
| operator-motion | 2 | policy Node 为 provider | 1 / 2：motion.server / move_joints、move_pose（Action） |
| operator-relative-pose-policy | 1 | provider | 1 / 1：motion.relative_pose / resolve（Query） |
| operator-sim-control-policy | 1 | provider + caller | 1 / 2：sim.control / get_robot_info（Query）、reset（Action） |
| operator-simulator-mujoco | 1 | 无 | 0 / 0；经 sim_command 接受命令 |
| benchmark-libero | 1 | 可选 provider | 1 / 2：benchmark.controller / describe（Query）、run（Session）；serve=true |

七个 provider 仓库与 Gateway 是八个直接涉及 Tool 的仓库。Kai0 和 LeRobot 使用相同的
`policy.runner` ID，是同一路由的部署候选；不是同一 Gateway 下天然独立的两个 Endpoint。
每个 Endpoint ID 在 Gateway 当前目录中只有一个 current instance。

LIBERO 当前源码的 serve 模式把 Endpoint 和 benchmark loop 放在同一个 Node；
虽然旧 README 曾描述独立 `bench_endpoint`，当前源码树没有该目录，不能据此宣称提供第二个 Node。
入口见 [main.py](https://github.com/PhyAgentOS/benchmark-libero/blob/e01cd66167b85522afe427c5b85da8af30c00c97/main.py)，
操作定义见 [descriptor](https://github.com/PhyAgentOS/benchmark-libero/blob/e01cd66167b85522afe427c5b85da8af30c00c97/bench_tool/descriptor.py)。

这些 provider 不直接监听 HTTP Tool API，外部调用由 Gateway 的 Tool/Invocation 接口提供。
Gateway 当前源码有 10 个此类 HTTP 路由模板（含 Session stop），不含其他 runtime/record/playback/WS 接口；
见 [HTTP controller](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/src/forge_gateway/controllers/tool_controller.py)。
历史 Skill 二进制的能力须按其锁定版本单独确认。

后续仓库 README 的必填内容和模板见 [Skill 开发手册](SKILL_DEVELOPMENT_zh.md#node-仓库-readme-必填内容)。

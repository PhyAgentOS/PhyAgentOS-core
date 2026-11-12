# TrackB Runtime：Skill、Tool、Endpoint 与 Gateway

[English](RUNTIME.md) | [简体中文](RUNTIME_zh.md)

[整体架构](README_zh.md) · [仓库目录](REPOSITORIES_zh.md) · [公共协议](PROTOCOLS_zh.md) · [Runtime 关系](RUNTIME_zh.md) · [Skill 开发](SKILL_DEVELOPMENT_zh.md)

本文先说明逻辑职责，再按公开 `robot-control-sim` 0.1.2 的 `mujoco-piper` profile 解释实际组装。
本指南面向 Core v1.1.1；历史实例使用 Core v1.1.0、Dora CLI 0.4.1 及原始 Node locks；最新 Forge 源码与旧二进制的版本边界见
[总览](README_zh.md#5-理论边界实现版本与源码快照)。

## 1. 先区分三个 Runtime 含义

| 名称 | 所属层 / 实现位置 | 管理什么 |
| --- | --- | --- |
| Skill Runtime | Core 的 `RuntimeManager`、运行环境和 `RuntimeState` | 选 profile、核对制品、启动 Dora flow、等待就绪和停止 |
| Tool Runtime | 通常由 Gateway 承载的逻辑职责 | Tool 目录、实现选择、invocation/attempt、状态与结果 |
| Node / Dora 运行环境 | Dora 和各节点进程 | 端口收发、计算循环、设备/模拟器生命周期 |

Tool Runtime 不要求另起一个叫 runtime 的进程；Skill Runtime 也不是一个控制算法。
Core 任务层的 AgentTask 管理目标、绑定、证据和 verdict，不替代 Gateway/provider 的执行事实。

## 2. 理论架构与对象关系

### 2.1 从 Agent 到执行者的逻辑边界

```mermaid
flowchart LR
    A["外部 Agent / 应用"]
    C["Core：Skill 工作流与 AgentTask"]
    subgraph G["Gateway Adapter"]
        API["调用者 API"]
        CAT["逻辑 ToolSpec 目录"]
        RT["Tool Runtime：invocation 与实现选择"]
        ROUTER["Endpoint Registry 与路由"]
        API --> CAT
        API --> RT
        CAT --> RT
        RT --> ROUTER
    end
    subgraph P["Provider：Operator 或 Adapter 节点"]
        B["Carrier binding / Endpoint handler"]
        E["ToolEndpoint operation"]
        X["算法、设备或下游动作执行"]
        B --> E --> X
    end
    A --> C
    C <-->|"HTTP Tool API"| API
    ROUTER <-->|"低频 Endpoint Wire"| B
    X <-->|"状态、观测、命令、轨迹"| DATA["Dora 数据平面"]
```

理论上，Tool 是对调用者稳定的能力入口，Gateway 的内部绑定把它映射到实现与 Endpoint operation。
Endpoint 是 provider 提供能力的逻辑边界，不等于 HTTP 服务、进程名或源码仓库。
外部调用协议与 provider Wire 是两条边；它们不要求使用相同数据格式或字段。

本图描述职责，不承诺每个已发布 Gateway 版本都完整实现目标架构。
本例实际 API 为 Action 的 `/tools/{tool_id}:invoke` 和 Query 的
`/tools/{endpoint_id}/{operation}:invoke`，仍保留 Endpoint 定向的 Query 路径。
应按匹配版本的 [Core Tool API 契约](../../forge/README_zh.md) 调用，
不把“隐藏 provider 路由”的目标边界误写成旧 API 已具备的全部行为。

架构依据：[Forge Tool architecture](https://github.com/PhyAgentOS/forge/blob/e59693910bb5f9572cf39c5a59475c0747ba76e8/interfaces/forge_tool/ARCHITECTURE.md)。

### 2.2 定义、部署和能力之间的关系

```mermaid
flowchart TB
    S["Skill Bundle"] -->|"包含工作流"| MD["SKILL.md"]
    S -->|"声明部署"| MF["skill.yaml"]
    MF -->|"定义多个"| PR["Profiles"]
    MF -->|"锁定制品身份和摘要"| LOCK["Node artifacts"]
    RUN["一次 Skill Runtime"] -->|"选择一个"| PR
    PR -->|"指定连接图"| FLOW["dataflow.yaml"]
    PR -->|"声明所需能力"| TOOLS["required_tools"]
    FLOW -->|"启动多个"| N["Dora Node 进程"]
    LOCK -.->|"提供可执行文件"| N
    N -->|"部分节点承载"| EP["Endpoint / operations"]
    GW["Gateway ToolSpec 与路由"] -->|"绑定能力"| EP
    TOOLS -->|"按 tool_id 对应"| GW
    MD -->|"指导 Agent 选择和调用"| GW
```

| 对象 | 作用 | 本例位置 |
| --- | --- | --- |
| Skill 工作流 | 告诉 Agent 何时使用能力、先查什么、如何判断完成 | `SKILL.md` |
| Skill Bundle | 把工作流、profile、模型和依赖锁作为版本化部署单元 | `skill.yaml`、`profiles/`、`assets/` |
| Profile | 同一 Bundle 下的一组机器人、连接、参数和环境选择 | `profiles/mujoco-piper/` |
| Tool / ToolSpec | 调用者可见的能力名、语义、schema、frame 和 readiness | `gateway.yaml` 中 `tools.specs` |
| Endpoint operation | provider 实现的 query/start/status/result/cancel 等契约 | Endpoint ID + operation + 匹配版本实现 |
| Provider | 实际承载 Endpoint 的节点 | motion/gripper/sim control policy |
| Gateway | HTTP 边界、ToolSpec、provider 路由与调用状态 | `gateway` 节点 |
| Dataflow | 节点 ID、可执行入口、配置文件及端口连接 | `dataflow.yaml` |

一个 Endpoint 可以有多个 operation，多个 Tool 可以映射到它。并非每个 Node 都需要暴露 Endpoint；
控制器、模拟器和 Viewer 可以只参与数据平面。仓库、二进制 entrypoint、Dora node ID、Tool ID 和 Endpoint ID
分别承担不同身份，不能按同名字符串猜测一一对应关系。

Core 的 `forge_tool_start_action` 等工具是通用 Agent wrapper；`sim.move_eef` 等是该 Skill 暴露的
具体 Tool ID；`SKILL.md` 是工作流文档。这三者也不能混为同一种“工具”。

## 3. 一个 Skill Runtime 如何产生

1. 获取并校验 Skill Bundle 和其锁定的 Node 制品，安装清单与资产。
2. 选择 profile，准备所需 binary 的运行环境，物化 launch dataflow 和配置/资产引用。
3. `RuntimeManager` 检查 Dora、binary、资产、环境及 Gateway 地址，按需运行 Bundle 的 `start.sh`。
4. 启动 Dora flow，使 `${FORGE_RUNTIME_BIN}` 等占位符指向该运行环境。
5. 核对 flow、Gateway 以及该 profile 的 Tool context/readiness，随后记录运行状态和 Gateway 身份。
6. Agent 激活工作流并创建绑定任务；调用时复核 Skill/profile、Runtime 身份、ToolSpec、allowlist 和 readiness。
7. 停止时按该 Runtime 的活动调用与任务绑定规则处理，不把取消已受理当作执行已停止。

`profiles.<name>.required_tools` 若存在，会覆盖 Skill 顶层列表，既用于就绪检查，也影响冻结的任务 Tool allowlist。
因此“同一 Skill 名称”不表示“所有 profile 暴露相同工具”。当前 Core 为每个已安装 Skill 保存当前 RuntimeState；
不要从“可定义多个 profile”推断它支持同一 Skill 的多 profile 并发。切换前先停止当前 Runtime，
并分别解决端口、设备和显示资源占用。

运行与调用身份也分层管理：

| Identity | 含义 |
| --- | --- |
| `runtime_instance_id` | Core 本次 Skill Runtime 实例 |
| `gateway_identity` | 绑定到的 Gateway 服务身份 |
| `endpoint_instance_id` | provider 注册实例，不是静态 Endpoint ID |
| `invocation_id` | Gateway 逻辑调用 |
| `attempt_id` | 对 provider 的一次执行尝试 |

Task、Runtime、Gateway 和 provider 的身份不能互相替代；provider 重启后的同名 Endpoint
不能仅凭字符串相同就被认作原来的执行实例。

实现入口：
[环境物化](../../../PhyAgentOS/skill_runtime/installer.py)、
[RuntimeManager](../../../PhyAgentOS/skill_runtime/manager.py)、
[manifest 与 profile](../../../PhyAgentOS/skill_runtime/manifest.py)、
[RuntimeState](../../../PhyAgentOS/skill_runtime/state.py)、
[任务绑定](../../../PhyAgentOS/forge/binding.py)。

### 运行恢复与进程归属

`paos skill reconcile <name> [--workspace /path/to/workspace]` 通过 Gateway GET
恢复保留的 Action/Session 引用，也检查已结束任务中的 `unknown` 执行记录。
恢复会核对冻结的 Runtime 与 Gateway 身份，补录执行事实而不改变任务原有结论；
只有任务已结束且所有远端执行均确认终态，才释放任务绑定。
Agent 启动复用相同恢复逻辑；普通 `skill stop` 在检查活跃引用前也会重试恢复。
Gateway 不可达、身份变更、缺少任务日志或远端状态未知时保留引用，恢复过程不发送控制 POST。
生命周期锁被占用或 Runtime 状态缺失时同样记为「保留」而不抛错：恢复不得阻断
Agent 启动，也不得阻断非强制的停止。由 interaction 管理的任务仍由其 supervisor 负责恢复。

停止命令返回后还会确认 Dora flow 已退出。缺少 Dora、查询失败和 JSON 损坏都属于
无法观测，不能证明执行已结束。`skill stop --force` 会额外对属于该轮 Runtime 的
Linux 进程执行 TERM/KILL，确认退出后再清理引用，不关闭共享 Dora 服务。
验证失败时保留引用并报错；新一轮 Runtime 不能覆盖尚未释放的引用或已知存活进程，
冷机上 `start` 会先恢复 coordinator，再严格确认旧 flow 已退出，才替换旧状态；
仍无法观测时保留旧状态并报错。每轮新 flow 使用独立名称，回滚仅操作本次启动。
持久化 Agent 恢复与 CLI 共用持锁的对账路径，锁被占用或 Runtime 身份不匹配时，
不会再进入另一条引用写入路径。

新启动的节点通过 dataflow 环境接收 `PAOS_RUNTIME_INSTANCE_ID`，由共享 daemon
创建的节点也会携带此标识。Linux 会发现同用户且携带该精确标识的进程，记录
boot/PID/启动时间，并通过 pidfd 清理，覆盖脱离进程组的子进程并避免 PID 复用误杀。
缺少 pidfd API，或内核返回 `ENOSYS` / `EOPNOTSUPP` 时，会在重新核对启动时间后
回退到 `kill`；权限错误仍明确报错。
节点应向子进程保留标识，启动脚本尽量 `exec` 最终程序。
这是协作式跟踪，不是 cgroup 隔离：在发现前移除标识或无法读取环境的子进程可能无法识别。
升级前的进程没有标识，需要人工检查并停止；其他平台仅验证 Dora flow 退出。
更强的 Linux 隔离需要在 daemon 创建节点的位置建立每个 flow 的 cgroup，不能只包装 CLI。

## 4. robot-control-sim 的实际组装

### 4.1 从哪几个文件读出运行关系

在取得已校验的 `robot-control-sim` 0.1.2 Bundle 后，查看：

```text
robot-control-sim/
├── SKILL.md
├── skill.yaml
├── profiles/mujoco-piper/
│   ├── dataflow.yaml
│   ├── gateway.yaml
│   ├── sim_control_policy.yaml
│   ├── motion_server.yaml
│   ├── controller.yaml
│   ├── gripper_controller.yaml
│   ├── simulator.yaml
│   └── image_viewer.yaml
└── assets/robots/piper/...
```

`skill.yaml` 锁定九个 Node；`mujoco-piper` profile 需要九个 binary，Gateway 地址为
`http://127.0.0.1:19002`。它暴露五个 Tool，而不是清单顶层的两个最小 Tool。
`mujoco-franka` 也暴露这五个 Tool；`mujoco-so101` 不暴露 `sim.set_gripper_opening`，dataflow 使用七个 Node。
九个 artifact locks 不等于每个 profile 都启动九个进程。

Piper X 派生示例另用 `mujoco-piper-x` 和端口 `19104`，不能与本节原始 Piper 的 group/frame 配置混用。
派生过程见 [PIPER_X_DEMO.md](../PIPER_X_DEMO_zh.md)。

### 4.2 五个 Tool、三个 Endpoint、三个 provider

| Tool ID | Semantics | implementation_id | Endpoint / operation | 实际 provider node |
| --- | --- | --- | --- | --- |
| `sim.get_robot_info` | query | `sim.control` | `sim.control` / `get_robot_info` | `sim_control_policy` |
| `sim.set_joint_positions` | action | `motion.integration` | `motion.server` / `move_joints` | `motion_action_policy` |
| `sim.move_eef` | action | `motion.integration` | `motion.server` / `move_pose` | `motion_action_policy` |
| `sim.set_gripper_opening` | action | `piper.gripper` | `gripper.controller` / `set_opening` | `gripper_action_policy` |
| `sim.reset` | action | `sim.control` | `sim.control` / `reset` | `sim_control_policy` |

这里的 `motion.server` 是逻辑 Endpoint ID；Gateway 先将请求交给 `motion_action_policy`，
不是直接把 Endpoint 消息送进名为 `motion_server` 的计算节点。
`implementation_id` 也不是可执行文件名。

`gateway.yaml` 的实际 provider 路由为：

```yaml
tools:
  providers:
    - endpoint_id: sim.control
      input_id: sim_tool_in
      output_id: sim_tool_out
    - endpoint_id: motion.server
      input_id: action_tool_in
      output_id: action_tool_out
    - endpoint_id: gripper.controller
      input_id: gripper_tool_in
      output_id: gripper_tool_out
```

这些 input/output 名称属于 Gateway 视角，具体对端由 dataflow 决定。例如：

```yaml
# dataflow.yaml 节选；不是独立可启动的完整图。
nodes:
  - id: gateway
    inputs:
      action_tool_in: motion_action_policy/tool_out
    outputs: [action_tool_out]
  - id: motion_action_policy
    inputs:
      tool_in: gateway/action_tool_out
    outputs: [tool_out]
```

### 4.3 实际执行与数据反馈图

```mermaid
flowchart TB
    C["Core / 原生诊断客户端"]
    subgraph FLOW["mujoco-piper dataflow：9 个节点"]
        G["gateway"]
        G <-->|"sim_tool_out / sim_tool_in"| S["sim_control_policy"]
        G <-->|"action_tool_out / action_tool_in"| P["motion_action_policy"]
        G <-->|"gripper_tool_out / gripper_tool_in"| GP["gripper_action_policy"]
        S <-->|"tool_request / tool_response：复用其他 Tool"| G
        P -->|"move_joints_goal / move_pose_goal"| M["motion_server"]
        M -->|"trajectory_goal"| J["joint_trajectory_controller"]
        GP -->|"gripper_goal"| GC["gripper_action_controller"]
        J -->|"joint_command 到 action/arm"| SIM["mujoco：entrypoint 为 mujoco_sim"]
        GC -->|"joint_command 到 action/gripper"| SIM
        S -->|"sim_command"| SIM
        SIM -->|"proprio_state"| M
        SIM -->|"proprio_state"| J
        SIM -->|"proprio_state"| GC
        SIM -->|"image/*"| V["image_viewer"]
        J -.->|"trajectory_feedback / result"| M
        M -.->|"move_joints 或 move_pose feedback / result"| P
        GC -.->|"gripper_feedback / result"| GP
    end
    C <-->|"HTTP Tool API"| G
```

图省略 timer 和 cancel 端口，保留主要命令、状态与结果方向。
关节模式和末端模式共用 motion server → trajectory controller → MuJoCo 执行链；区别是入口 operation
以及是否先做 URDF IK。图像直接进入 Viewer，不绕行 HTTP Tool 请求。

`sim.reset` 不是在 Core 里直接写模拟器状态：sim-control policy 既连接 `sim_command`，
也通过 `tool_request/tool_response` 路径复用运动和夹爪 Tool，按配置执行回到初始姿态的流程。
“重置命令发出”和“回到初始姿态成功”不能等同。

### 4.4 一次末端运动的调用过程

```mermaid
sequenceDiagram
    participant C as Core / 客户端
    participant G as Gateway
    participant P as motion_action_policy
    participant M as motion_server
    participant J as joint_trajectory_controller
    participant S as MuJoCo
    C->>G: GET Tool context / readiness
    C->>G: POST /tools/sim.move_eef:invoke
    G-->>C: 202 + invocation_id + attempt_id
    G->>P: motion.server / move_pose
    P->>M: move_pose_goal
    M->>M: 核对 group/frame，求 IK，生成轨迹
    M->>J: trajectory_goal
    loop 轨迹执行与状态反馈
        J->>S: JointCommand
        S-->>J: JointState
        S-->>M: JointState
    end
    J-->>M: trajectory_result
    M-->>P: move_pose_result
    P-->>G: Tool 执行结果
    C->>G: GET invocation status，再读 result
    G-->>C: 终态结果与误差
```

Gateway admission 和 provider dispatch 是异步阶段；HTTP 202 不表示机器人已到位。
原生诊断按 Gateway 的 `phase=completed` 终态读取 result，并检查
`data.result.status=succeeded`、motion `error_code=SUCCESS` 及位置/方向残差。
Core 的 AgentTask 还会在执行后完成独立验证并保存 verdict；执行成功不自动等于任务验证成功。
超时、cancel accepted 或 unknown 不触发盲目重复运动。

原始 Piper 的 motion group 为 `piper_arm`，reference frame 为 `arm_base`，target frame 为 `tcp`。
先查询 `sim.get_robot_info` 并读取 Tool context，不能套用 Piper X 的 `piper_x_arm` / `base_link`。

## 5. 从配置继续追到源码

| 实际职责 | 实现入口 |
| --- | --- |
| Gateway HTTP 和 provider 路由 | [adapter-forge-gateway](https://github.com/PhyAgentOS/adapter-forge-gateway/blob/5aaa9d16aff1c1b689dbb53acbcdef406dcb8d97/README.md) |
| motion Tool provider 与计算节点 | [policy_nodes.py](https://github.com/PhyAgentOS/operator-motion/blob/24c8a430715f697eea8431bf169c21b8cbb5eb92/packages/motion_server/src/forge_motion_server/policy_nodes.py)、[server.py](https://github.com/PhyAgentOS/operator-motion/blob/24c8a430715f697eea8431bf169c21b8cbb5eb92/packages/motion_server/src/forge_motion_server/server.py) |
| 仿真 descriptor / reset 编排 | [sim-control runner](https://github.com/PhyAgentOS/operator-sim-control-policy/blob/85679a4be8953e9071de426c7c0973155121d96f/src/sim_control_policy_node/runner.py) |
| gripper Tool provider | [gripper_action_policy runner](https://github.com/PhyAgentOS/operator-gripper-action-policy/blob/bb69223497b944dd1a0f6fe9ecb36486168a8cc4/src/gripper_action_policy_node/runner.py) |
| controller、模拟器和 Viewer | [Node 源码映射](REPOSITORIES_zh.md#3-robot-control-sim-的-node-与源码映射) |
| Core 的 HTTP client 与 Agent wrapper | [ForgeToolClient](../../../PhyAgentOS/forge/tool_client.py)、[Agent tools](../../../PhyAgentOS/agent/tools/forge_tool_api.py) |

上表用于定位实现职责；固定源码快照与历史二进制的构建对应关系仍须由 Release/构建溯源证明。
修改 Node 后重新发布和更新 Skill locks 的流程见 [Skill 开发手册](SKILL_DEVELOPMENT_zh.md)。

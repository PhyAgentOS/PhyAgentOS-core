# Demo：在 robot-control-sim 中接入 AgileX Piper X（MuJoCo）

[English](PIPER_X_DEMO.md) | [简体中文](PIPER_X_DEMO_zh.md)

[Bundle 与 Tool 接入参考](README_zh.md) · [TrackB 框架说明](track_b/README_zh.md) · [三种开发模式](track_b/SKILL_DEVELOPMENT_zh.md) · [示例代码](../../example/robot-control-sim-piper-x/README_zh.md)

> 本教程将一次实际二次开发过程整理为可复用示例。范围：MuJoCo 仿真、关节目标和末端位姿运动。
> 文档目标版本：Core v1.1.1。历史运行基线：Core v1.1.0、Dora CLI 0.4.1、robot-control-sim 0.1.2 原始 Node locks。
> 下文记录的机器人验证尚未在 v1.1.1 上重新执行。
> 配套脚本生成并验证派生 Skill，不升级锁定的 Node。

**平台开放范围：** 外部贡献提交及已有登记仓库修改暂未开放，需等待审批服务完成并开放。当前登记与发布由管理员维护；可先进行本地开发验证。下文的平台发布步骤以相关流程开放为前提，详见[贡献指南](../../CONTRIBUTING_zh.md)。

## 1. 目标、基线与边界

派生 Skill 名为 `robot-control-sim-piper-x`，profile 为 `mujoco-piper-x`，保留以下原生执行链：

```text
Agent / diagnostic Tool API client
  → Gateway → motion_action_policy → motion_server（关节规划 / URDF IK）
  → joint_trajectory_controller → mujoco_sim
                                  ↑ JointState feedback
```

- `sim.set_joint_positions`：按六个绝对关节角运动。
- `sim.move_eef`：按绝对末端位置与方向运动，由原 motion_server 求 IK 并生成轨迹。
- 原有 gripper policy/controller 提供夹爪开度控制，sim-control policy 提供元数据与复位。
- 九个 Node 的 artifact/version/SHA256 保持原样；新内容是模型、profile、Skill 工作流和开发工具。
- 本教程不验证真机、电机动力学辨识、避障规划、抓取或全工作区可达性。

模型来源为 [AgileX 官方 agx_arm_urdf](https://github.com/agilexrobotics/agx_arm_urdf/tree/680fa925cd00db7e2628563d2edc5540963045de/piper_x)，
固定 commit `680fa925cd00db7e2628563d2edc5540963045de`。使用真正的 Piper X 模型，而非重命名普通 Piper。
源码索引中的最新 Forge 2.x / Dora 1.x 与本例运行基线不同，不得混装。

完整开发顺序为：确认上游版本和锁 → 准备模型 → 生成独立 profile → 验证 FK/伺服 →
打包与本地安装 → 验证两种原生 Action → 按需验证 AgentTask → 停止资源 → 登记并发布派生 Skill。
前八步完成后可得到本地可运行的派生包；最后一步仍须通过所用资源平台的流程。

## 2. 准备环境和上游 Skill

在 Linux x86_64 上准备 Python 3.12、Git、Dora CLI 0.4.1，以及 Core v1.1.1 的 CLI 环境；复现原始验证记录时使用 Core v1.1.0。
本文示例文件随开发手册提供；Core 运行环境与模型构建环境分开，避免 MuJoCo/numpy 依赖改变 Core 环境。
基础安装见 [Core 用户手册](../zh/02-user-manual.md)。先确认：

```bash
paos --help
dora --version
paos skill install robot-control-sim --version 0.1.2
paos skill inspect robot-control-sim
```

Registry 的 `--version` 是下载后约束，不是历史版本查询。如果当前 Registry 已更新，
应使用保存的官方不可变 URL 或本地 0.1.2 归档：

```bash
paos skill install /path/to/robot-control-sim-0.1.2.tar.gz --local
```

不要将其他版本改名为 0.1.2。安装会验证 Bundle/Node 摘要；缺失 Node 需从 Registry 或
独立取得的匹配归档安装。配置依赖九个节点，参见 [Node 源码映射](track_b/REPOSITORIES_zh.md#3-robot-control-sim-的-node-与源码映射)。

## 3. 生成模型和独立 profile

以下命令在本仓根目录执行。`PIPER_DEMO_UPSTREAM` 指向 `paos skill inspect` 对应的安装目录；
示例给出默认安装布局；使用独立 Core 配置实例时，应改成该实例实际的 Skill 安装路径。

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

生成器检查模型 revision、模型工作区无修改、上游 Skill 身份与九个 Node lock；已有输出目录会拒绝覆盖。
重做时用新输出目录。原始上游文件保持不变。

无显示服务器：先安装系统 Xvfb，在生成命令末尾增加 `--headless`。
该选项使用独立 display `:92`、glow/software OpenGL，端口 `19104` 和 display 必须空闲。
它是可选 Linux 示例；桌面环境保留现有 DISPLAY。需要更换端口或 display 时，统一调整生成后的
`skill.yaml`、`gateway.yaml`、启动/停止钩子及验收客户端参数。

生成目录包括：

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
└── start.sh / stop_display.sh       # 仅 --headless
```

## 4. 理解改动，才能接入下一种 robot

### 4.1 模型和坐标

生成器展开官方 URDF/夹爪 xacro，使用官方 STL、运动链和惯量；给 MuJoCo 增加 position actuators、
阻尼、armature 和重力补偿。这些是仿真参数，不是经过标定的真机伺服参数。

| 项目 | 本例约定 |
| --- | --- |
| arm group | `piper_x_arm` |
| 关节顺序 | `joint1` 到 `joint6`，位置单位 rad |
| base frame / TCP | `base_link` / `tcp` |
| TCP 定义 | `gripper_base` 坐标系中 `[0, 0, 0.138]` m；URDF 固定 link 与 MJCF site 一致 |
| 末端目标 | 位置 m；显式字段 `qx/qy/qz/qw`，单位四元数 |
| 初始关节角 | `[0, 0.65, -1.10, 0.25, 0.30, 0.10]` |
| 夹爪 | 逻辑总开度 `0..0.1` m；两个物理 slider 通过 equality 反向耦合，actuator gear=2 |

模型有 8 个物理关节自由度，但对外暴露 6 个臂关节加 1 个逻辑夹爪量。
不能把物理 qpos 数量直接当 Tool 的控制维度。

仅显式排除相邻 `base_link/link1` 的碰撞：本模型导入后的外壳重叠会阻碍第一关节运动。
这是针对模型几何的处理，不是关闭全部碰撞；换机器人时必须重新检查，不能盲目复制该例外。

### 4.2 配置改动表

| 文件 | 需要保持一致的内容 |
| --- | --- |
| `robot.yaml` | joint 名称/顺序/类型/限位、initial_qpos、eef_site、夹爪标识 |
| `simulator.yaml` | 模型路径、初始状态、逻辑关节、actuator_order、joint_to_actuator、相机输出 |
| `motion_server.yaml` | group、URDF、base/tip、速度/加速度与容差 |
| `controller.yaml` | 臂关节顺序和控制容差 |
| `gripper_controller.yaml` | 逻辑开度名称、范围及控制参数 |
| `sim_control_policy.yaml` | descriptor 路径、reset 的 motion group 和夹爪调用 |
| `gateway.yaml` | ToolSpec 的 group、frame、joint order、输入范围、端口与 robot_frame_profile |
| `dataflow.yaml` | 保留原运动链；连接机器人状态、控制命令和相机端口 |
| `skill.yaml` | 新 Skill 名称、profile、required_assets/tools、gateway_url、原 Node locks |
| `SKILL.md` | 描述两种控制方式、准备检查、终态判断和任务验证；不嵌入密钥或一次性任务记录 |

生成器同时更新上述配置，源码是可阅读的修改示例。开发另一种机器人时，应先确定模型与坐标，
再修改 profile；不要用放宽容差来掩盖坐标或关节映射错误。

### 4.3 两种控制模式共用同一执行链

关节模式的 `positions[i]` 对应 `joint_names[i]`，顺序须与 motion group、controller 和模拟器的逻辑关节一致。
六个臂关节使用 rad；夹爪通过单独 Tool 传逻辑开度，不把两个物理 slider 塞入六维臂目标。
末端模式只给出 `base_link` 下 `tcp` 的目标位姿，由原 motion_server 求 IK；
URDF 的 fixed TCP 与 MuJoCo 的同名 site 必须表达同一个位置和方向。

下面是 `sim.move_eef` 的 Tool arguments，来自示例的 `eef-a` 合成目标：

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

字段名明确规定四元数含义；MuJoCo 的四元数数组使用 `wxyz`，不要将数组直接按 `xyzw` 填入。
检查工具 context 中的 schema 和 readiness 后再调用。源码查询、原始载荷或模型 FK 的通过，
都不能代替 Action 终态与实际残差检查。

## 5. 先验证模型，再启动 pipeline

```bash
"$PIPER_DEMO_ROOT/.venv-model/bin/python" "$PIPER_DEMO_ROOT/validate_model.py" \
  --skill "$PIPER_DEMO_WORK/robot-control-sim-piper-x" \
  --output-dir "$PIPER_DEMO_WORK/model-check"
```

该脚本用独立 URDF 矩阵计算和 MuJoCo site 对比 31 个姿态的 FK，检查初始姿态伺服稳定性。
它只是模型诊断，不代表 Tool API/运动规划链验收成功。预期位置差和旋转矩阵元素差均小于 `1e-6`；
失败时先排查关节轴、origin、base/tip 和 TCP。

在已激活的 **Core 环境** 中打包和安装（这里的 `python` 应能导入 PhyAgentOS）：

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

打包命令默认拒绝覆盖同名输出；发布修订时递增版本。示例生成器使用 `resolver=registry`，
以便安装时解析原有 Node locks。首次安装要求全部依赖可解析；使用预置缓存时也仍可能查询 Registry
元数据或探测直链文件大小，不能仅凭缓存存在就宣称可完全离线安装。
`paos forge-node install <skill> <node> --archive <archive>` 可为清单已安装的 Skill 安装本地 Node，
但不能用它绕过首次 Skill 安装时的依赖解析。两种方式都必须匹配原 lock 并保留摘要校验。

## 6. 两种控制方式的原生验收

保持 Core 环境，运行无需 LLM 的诊断客户端：

```bash
python "$PIPER_DEMO_ROOT/accept_pipeline.py" \
  --run-id piper-x-demo-001 \
  --output-dir "$PIPER_DEMO_WORK/acceptance"
```

每次重跑使用新的 run-id。客户端只通过原生 Tool API，依次执行三组关节目标、三组末端目标、夹爪和复位。
`motion-fixtures.json` 是可审阅的合成目标；执行前确认当前 scene/profile 与本例一致。
它先查询 robot metadata 和 Tool context，随后每次只提交一个 Action，等待终态再读取 result。
末端测试仅发送 Cartesian pose，不在客户端求 IK 后改发关节命令。

初次调试也可分别执行一个关节目标和一个末端目标，每次使用新的 run-id：

```bash
python "$PIPER_DEMO_ROOT/accept_pipeline.py" --only joint-a \
  --run-id piper-x-joint-001 --output-dir "$PIPER_DEMO_WORK/acceptance"
python "$PIPER_DEMO_ROOT/accept_pipeline.py" --only eef-a \
  --run-id piper-x-eef-001 --output-dir "$PIPER_DEMO_WORK/acceptance"
```

| 验收项 | 判断依据 |
| --- | --- |
| 关节目标 | 六关节顺序正确、motion `error_code=SUCCESS`、最大绝对残差 ≤0.03 rad |
| 末端目标 | motion `error_code=SUCCESS`，位置误差 ≤0.005 m，方向误差 ≤0.03 rad |
| 夹爪 | controller SUCCESS 且 reached_goal |
| 复位 | scene reset 且 rehomed |

Gateway raw invocation 的 `phase=completed` 表示终结；执行成功还需检查 result 中的
`data.result.status=succeeded`。不能等待不存在的 `phase=succeeded`。
超时请求取消后停止本轮；unknown 不自动重发，也不当作 pending 或成功。

此脚本是开发者执行链诊断，不创建 AgentTask，也不产生独立模型 verdict。
若要测试完整 Agent 工作流，配置自己的模型 Provider 后，通过 `paos agent` 明确选择此 Skill，
要求只完成一个关节或末端目标。Agent 应激活 primary Skill、创建并绑定 AgentTask，
执行终结后 finalize，再检查持久化 verdict。复用 fixtures 的一个目标即可，避免同时改变任务和模型。
执行成功但 verdict 为空时，分别报告两个事实；Verifier 错误不应触发重新运动。

## 7. 常见问题与诊断顺序

| 现象 | 首先检查 | 处理方向 |
| --- | --- | --- |
| 一个关节卡住，其余到位 | 实际关节反馈、actuator 映射、接触对 | 区分控制未下发与接触力阻碍；只修正有依据的相邻碰撞 |
| 关节模式正常，末端模式异常 | URDF/MJCF FK、base/tip、四元数顺序、目标可达性 | 先修正坐标和模型，再检查 IK 配置 |
| 窗口/渲染启动失败 | DISPLAY、Xvfb、renderer 和 OpenGL | 使用可选 headless profile，确认 display 未占用 |
| 202 后立即读 result 得到 unknown | 调用是否仍在 dispatching | 先轮询 status 到终态；已有 unknown 保留诊断，不重发 |
| 接口拒绝 timeout | Gateway 当前 schema/上限 | 本例 invoke timeout 使用 20000 ms；它不同于整个动作等待时间 |
| Verifier 空输出 / HTTP 500 | Provider 与 verifier 服务错误 | 保留执行事实和空 verdict；单独定位验证服务 |
| 更新某个 Node 后反序列化失败 | Dora/Forge/message 版本和锁 | 恢复一致版本集；组织迁移不能替代协议升级验收 |

这些条目来自该类集成中的实际诊断过程，教程不附带本机日志、会话记录、凭据或服务地址。

## 8. 停止、复现与维护

```bash
paos skill stop robot-control-sim-piper-x
# 仅 --headless 构建：在实际安装目录执行
bash "$HOME/.PhyAgentOS/skills/robot-control-sim-piper-x/stop_display.sh"
```

不要停止其他 dataflow 或显示服务。记录自己构建的 Bundle SHA256、Node locks、模型 revision、
profile 和运行环境；保留本地验收结果，不将本机运行记录混入公开示例。

提交新机器人示例前至少完成：模型 FK/限位检查、两种原生 Action、干净环境安装、资源停止。
修改 Node 算法则另外完成其单元测试和跨节点集成测试，不能只用本教程的几个目标证明完整控制能力。

## 9. 接入下一种机械臂与提交交付物

本例生成器专用于 Piper X，不是通过一个 `--robot` 参数导入任意 URDF 的通用工具。
复用其方法接入下一种机器人时，按下面的清单逐项确认：

- [ ] 模型来源、固定 revision 和许可证可核对；URDF/MJCF 共享同一运动链和 TCP 定义。
- [ ] 关节轴、单位、限位、顺序及夹爪逻辑量与物理关节的映射一致。
- [ ] 初始状态可接受，伺服稳定；碰撞排除有具体几何依据。
- [ ] group、base/tip、Tool schema、controller 与 simulator 配置对应。
- [ ] 原生关节目标与原生末端位姿各自通过，Cartesian 测试由服务端求 IK。
- [ ] 超时/取消/unknown 的处理可诊断，失败动作不自动重发。
- [ ] 新 Skill 身份、版本、资产清单、Node locks、启动/停止和独立安装均完成核对。

交付的公开示例应包含模型生成/转换说明、profile、Skill 工作流、可审阅测试目标、依赖锁、
许可证与构建说明。环境凭据、内部部署信息和个人会话记录不进入示例；运行验收材料按资源平台要求单独提交。

本例只新增 Skill，不需要重复创建九个既有 Node。登记前确认这些依赖在目标平台存在且版本/摘要一致，
再按平台要求提交新的 Skill 版本、技术校验和运行证据。已存在的 Node 记录先核对再复用；
若确实重建了 Node，则使用新 Node 版本并更新派生 Skill lock，流程见
[源码构建与制品溯源](track_b/SKILL_DEVELOPMENT_zh.md#4-模式三独立开发-node-并组装自己的-skill)。

本地 `--local` 安装成功不代表平台已登记或审核发布。发布后，才以该平台匹配的消费者 API 验证
按名称检索、安装到独立实例及逐 Node verify；不要把静态目录变更或上传完成当作最终验收。

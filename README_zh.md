<div align="center">
  <img src="docs/imgs/logo_en.png" alt="PhyAgentOS" width="460">
  <h3>面向具身智能体的递归自进化基础设施</h3>
  <p>
    <a href="https://arxiv.org/abs/2607.16636">技术报告</a> ·
    <a href="https://phy-agent-os.net/">官网</a> ·
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-DSH-Plugin">DSH 插件</a> ·
    <a href="docs/README_zh.md">文档</a> ·
    <a href="https://discord.gg/YJztZ4wUM">Discord</a>
  </p>
  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-4264ce" alt="MIT License"></a>
    <img src="https://img.shields.io/badge/Python-3.11%2B-4264ce?logo=python&amp;logoColor=white" alt="Python 3.11 or newer">
    <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/Release-v1.2.0-4264ce" alt="Release v1.2.0"></a>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-core/stargazers"><img src="https://img.shields.io/github/stars/PhyAgentOS/PhyAgentOS-core?style=flat&amp;color=4264ce" alt="GitHub stars"></a>
  </p>
  <p><a href="README.md">English</a> · <a href="README_zh.md">简体中文</a></p>
  <p>
    <a href="#what">What</a> ·
    <a href="#why">Why</a> ·
    <a href="#release">v1.2.0</a> ·
    <a href="#quick-start">安装配置</a> ·
    <a href="docs/quickstart-mujoco_zh.md">仿真 Quickstart</a> ·
    <a href="#quickstart-examples">快速启动示例</a> ·
    <a href="#documentation">文档导航</a>
  </p>
  <p><a href="#release"><b>v1.2.0 · DeepSeek Harness 接入、多策略基准评测与运行恢复</b></a></p>
</div>

---

<a id="what"></a>
## What · PhyAgentOS 是什么？

**PhyAgentOS 是面向具身智能体的递归自进化（RSI）框架。** 它将认知规划、Physical Execution 与任务验证连接为跨 Runtime 的反馈闭环，从经过验证的经验中改进 Skill 与 Lesson，并用于后续任务。

![PhyAgentOS 宏观架构：认知规划、物理执行与递归自进化闭环](docs/imgs/runtime-control-modes.png)

<a id="why"></a>
## Why · 为什么选择 PhyAgentOS？

| 核心能力 | 带来的价值 |
| --- | --- |
| **一套框架，多种控制方式** | 通过已安装 Skill 暴露的能力，接入通用模型、动作模型或混合控制。 |
| **以任务结果为准的验证** | 结合观测和执行事实判断目标是否达成，需要恢复时支持有预算的重新规划。 |
| **让经验改进后续任务** | 从经过验证的经验中积累可复用工作流与作用域 Lesson，并通过受控晋升和版本记录管理改进。 |
| **可复用的物理执行能力** | 将环境相关工具与 Runtime 封装为版本化 Skill，使认知规划与机器人接入保持解耦。 |

<a id="release"></a>
## v1.2.0 更新亮点

- **在 DeepSeek Harness 中运行 Forge Skill。** 直接在 DSH 对话中搜索和安装 Skill，选择配置、启动运行环境，再调用 Gateway 工具读取执行结果。[DSH 插件仓库](https://github.com/PhyAgentOS/PhyAgentOS-DSH-Plugin) · [npm 安装包 0.2.0](https://www.npmjs.com/package/@x-era/dsh-forge/v/0.2.0)
- **选择评测基准和策略。** 公开 Registry 提供 RoboTwin 2.0 的 5 个、LIBERO 的 11 个和 CALVIN 的 2 个策略配置。RoboDojo 的 π0.5 开发包支持瓶子入桶任务，可在回合边界停止，也可在同一策略进程中连续执行多个回合。[评测 Skill 与策略配置](docs/benchmarks.md#benchmark-skills)
- **恢复长会话与 Skill 执行状态。** 会话按块归档并保存进度，`paos skill reconcile <skill>` 可向 Gateway 查询保留的 Action 或 Session 并更新运行绑定。Piper 教程带你控制机械臂、录制 MP4 并修改录像节点。[会话恢复](docs/FAQ_zh.md#session-memory) · [Piper 教程](example/beginner-simulation/README_zh.md)

使用 Node.js 24.9 或更高版本，安装插件适配的 DSH 并启动 Web 对话界面。

```bash
npm install --global @deepseek-ai/dsh@0.2.2-rc.1
dsh plugin --profile web add @x-era/dsh-forge
dsh web
```

[完整更新记录](CHANGELOG.md) · [与 v1.1.1 的版本对比](https://github.com/PhyAgentOS/PhyAgentOS-core/compare/v1.1.1...v1.2.0)

<a id="quick-start"></a>
## 安装与配置

先运行模型驱动的 CLI 对话，再按独立的 [MuJoCo Quickstart](docs/quickstart-mujoco_zh.md) 操控仿真机器人。**准备：** Python 3.11 或 3.12、Git，以及模型服务的 API Key。

### 1. 安装与初始化

```bash
git clone --branch v1.2.0 https://github.com/PhyAgentOS/PhyAgentOS-core.git
cd PhyAgentOS-core
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
paos onboard
```

也可在 Python 虚拟环境中从 PyPI 安装同一版本，运行 `python -m pip install "PhyAgentOS-ai==1.2.0"`，然后执行 `paos onboard`。

Windows PowerShell 请将激活命令替换为 `.venv\Scripts\Activate.ps1`。

### 2. 配置模型

通过隐藏输入配置密钥，再选择默认 Provider：

```bash
paos provider configure openrouter
paos provider use openrouter --model anthropic/claude-sonnet-4
```

Docker/Secret 输入、进程启动覆盖和 `/provider`、`/model`、`/effort`、`/status` 会话命令见
[Provider CLI 指南](docs/zh/02-user-manual.md#2-配置模型与-forge)。切换后的配置从当前会话的下一条请求起生效。

完整配置示例、资源下载地址和排障见 [FAQ](docs/FAQ_zh.md)。

### 3. 运行第一条请求

```bash
paos status
paos agent -m "Hello! Introduce yourself."
paos agent
```

收到模型回复即完成核心 Agent 与模型服务的连通检查；`paos agent` 会打开交互式对话。执行物理任务还需要启动对应的 Skill Runtime。

其他模型服务与部署方式见 [用户手册](docs/zh/02-user-manual.md) · [Docker 部署](docs/user_manual/DOCKER_zh.md).

<a id="robot-skills"></a>
第一次接触具身 Agent？可按[新手仿真教程](example/beginner-simulation/README_zh.md)体验对话 CLI、MP4 录制节点及源码修改后重新接入（[Linux 功能报告](example/beginner-simulation/VALIDATION_zh.md)）。

<a id="simulation-quickstart"></a>
<a id="quickstart-examples"></a>
## 更多示例

| 平台 | 内容 | 指南 |
| --- | --- | --- |
| Piper MuJoCo 仿真 | Dora、Skill 安装、关节与末端控制，以及停止恢复 | [MuJoCo Quickstart](docs/quickstart-mujoco_zh.md) |
| Piper 机械臂 | Skill 安装、CAN 配置、真机启动与工具就绪验证 | [Piper 快速启动](example/quickstart/piper-quickstart.md) |
| LIBERO-10 仿真评测 | Skill/Node 安装、π0.5 权重、模型 API 配置与 50 集测评（π0.5 纯策略 / GPT-6 监督两条路线） | [LIBERO 快速启动](example/quickstart/libero-quickstart.md) |

<a id="documentation"></a>
## 文档导航

| 我想…… | 阅读入口 |
| --- | --- |
| 资源下载、完整配置与会话恢复 | [FAQ](docs/FAQ_zh.md) |
| 安装、配置并运行 PhyAgentOS | [用户手册](docs/zh/02-user-manual.md) |
| 开发 Skill 或接入新环境 | [TrackB 开发指南](docs/user_development_guide/track_b/README_zh.md) |
| 了解 Physical Execution API | [Physical Execution Tool API](docs/forge/README_zh.md) |
| 为 robot-control-sim 接入另一种机械臂 | [Piper X 二次开发 demo](docs/user_development_guide/PIPER_X_DEMO_zh.md) |
| 开发与测试 Core | [Core 开发者手册](docs/zh/03-developer-manual.md) |
| 查看测评结果与评测口径 | [Benchmark 文档](docs/benchmarks.md) |
| 浏览全部文档 | [文档索引](docs/README_zh.md) |

<a id="news"></a>
## News · 最新动态

**2026-11-22 · v1.2.0 发布。** 在 DeepSeek Harness 中运行 Forge Skill，按评测基准选择策略，恢复会话和任务执行状态。[发布说明与安装包](https://github.com/PhyAgentOS/PhyAgentOS-core/releases/tag/v1.2.0)

**2026-10-23 · v1.1.1 发布：** 新增 Opper 网关，完善本地 Skill 开发和 Action 拒绝处理，并补充双语 Track B 与 Piper X 开发指南。[查看发布说明与安装包](https://github.com/PhyAgentOS/PhyAgentOS-core/releases/tag/v1.1.1)。

**2026-10-10 · GOSIM Shenzhen 2026：** PhyAgentOS 入选 Spotlight，并将在开源机器人分论坛做主题分享。[查看活动公告与现场信息](docs/news/gosim-shenzhen-2026_zh.md)。

| 版本 | 日期 | 更新 |
| --- | --- | --- |
| **v1.2.0** | 2026-11-22 | DeepSeek Harness 插件入口、多策略 Benchmark Skill、会话归档与 Skill 运行恢复。 |
| **v1.1.1** | 2026-10-23 | Opper 网关、本地 Skill 开发与 Action 拒绝处理修复、双语 Track B 和 Piper X 指南，以及 MuJoCo Quickstart。 |
| **v1.1.0** | 2026-10-09 | 集中式 Provider 管理、直接 OpenAI Responses 与新网关接入、Skill Runtime 打包和启动改进，以及默认关闭的受治理交互基础。 |
| **v1.0.0** | 2026-08-30 | Initial stable release of PhyAgentOS. |
| **v0.2.3** | 2026-08-27 | Physical Execution Skill 可独立安装和管理，经显式激活冻结到 AgentTask，并通过受治理的 Query、Action、Session Tool API 生命周期执行，支持恢复和按版本限定的经验。 |
| **v0.2.2** | 2026-08-21 | 将 Physical Execution 执行统一到 Query/Action Tool API，并增加 AgentTask 聚合、可校验 Skill Runtime、Resource Registry 接入和 move-arm-by-ee Skill，同时保留 Agent 验证与演化能力。 |

查看[完整更新记录](CHANGELOG.md)。

## 参与贡献与社区

欢迎通过 TrackB 开发 Node、组装 Skill 或接入机器人与仿真环境。贡献指南介绍本地开发流程、源码评审和由维护者负责的资源登记。[贡献指南](CONTRIBUTING_zh.md)

<details>
<summary>开发检查命令</summary>

```bash
python -m pip install -e ".[dev]"
pytest
ruff check PhyAgentOS tests
python -m compileall -q PhyAgentOS tests
```

</details>

[Discord](https://discord.gg/YJztZ4wUM) · [X](https://x.com/phyagentos) · [Bilibili](https://space.bilibili.com/3546880296355920) · [LinkedIn](https://www.linkedin.com/in/phyagent-os-252372401/) · [小红书](https://www.xiaohongshu.com/user/profile/673d83e3000000001c01a183)

## 引用与致谢

如果 PhyAgentOS 对你的研究有帮助，欢迎引用我们的[技术报告](https://arxiv.org/abs/2607.16636)。

```bibtex
@article{liu2026phyagentos,
  title={PhyAgentOS: A Self-Evolving Operating System for Embodied Agents with Decoupled Cognitive Planning and Physical Execution},
  author={Liu, Yang and Chen, Weixing and Song, Xinshuai and Pu, Tao and Mo, Siwen and Bai, Yongjie and Chen, Zihao and Sun, Qianran and Zhong, Liruo and Shen, Ying and others},
  journal={arXiv preprint arXiv:2607.16636},
  year={2026}
}
```

感谢 MuJoCo、ROS、各开放基准的维护者，以及所有 PhyAgentOS 贡献者。

---

<div align="center">
  <p>由 <b>中山大学 HCP 实验室</b>、<b>鹏城实验室</b> 与 <b>拓元智慧</b> 联合开发。</p>
  <img src="docs/imgs/HCP.jpg" alt="HCP Lab" height="64">&nbsp;&nbsp;
  <img src="docs/imgs/Pengcheng.png" alt="Peng Cheng Laboratory" height="64">&nbsp;&nbsp;
  <img src="docs/imgs/logo-xera-mark.png" alt="X-Era Lab" height="64">
  <p><sub>MIT License · Copyright © 2025–2026 PhyAgentOS</sub></p>
</div>

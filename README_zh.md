<div align="center">
  <img src="docs/imgs/logo_en.png" alt="PhyAgentOS" width="460">
  <h3>面向具身智能体的递归自进化基础设施</h3>
  <p>
    <a href="https://arxiv.org/abs/2607.16636">技术报告</a> ·
    <a href="https://phy-agent-os.net/">官网</a> ·
    <a href="docs/README.md">文档</a> ·
    <a href="https://discord.gg/YJztZ4wUM">Discord</a>
  </p>
  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-4264ce" alt="MIT License"></a>
    <img src="https://img.shields.io/badge/Python-3.11%2B-4264ce?logo=python&amp;logoColor=white" alt="Python 3.11 or newer">
    <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/Release-v1.0.0-4264ce" alt="Release v1.0.0"></a>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-core/stargazers"><img src="https://img.shields.io/github/stars/PhyAgentOS/PhyAgentOS-core?style=flat&amp;color=4264ce" alt="GitHub stars"></a>
  </p>
  <p><a href="README.md">English</a> · <a href="README_zh.md">简体中文</a></p>
  <p>
    <a href="#what">What</a> ·
    <a href="#why">Why</a> ·
    <a href="#quick-start">快速开始</a> ·
    <a href="#control-modes">控制方式</a> ·
    <a href="#benchmarks">Benchmark</a> ·
    <a href="#robot-skills">运行机器人技能</a> ·
    <a href="#documentation">文档导航</a>
  </p>
</div>

---

<a id="what"></a>
## What · PhyAgentOS 是什么？

**PhyAgentOS 是面向具身智能体的递归自进化（RSI）框架。** 它连接认知规划、物理执行与经验驱动的技能改进，让智能体通过模型和工具与环境交互、验证任务结果，并将积累的经验用于后续任务。

![PhyAgentOS 宏观架构：认知规划、物理执行与递归自进化闭环](docs/imgs/runtime-control-modes.png)

**RSI 贯穿整个 PhyAgentOS Runtime**：规划、物理执行、观测、验证、反思与经验复用构成统一反馈闭环。**认知 Agent** 负责规划与技能编排，**Physical Execution** 将决策连接到机器人和仿真环境；环境观测与执行结果支撑验证和反思，受控 Skill 更新与作用域 Lesson 再指导后续任务。**技能生态**通过版本化 Bundle 支持显式安装与部署。

图中展示控制关系，不表示模型进程的部署位置；动作模型由对应 Skill Runtime 接入。Runtime-wide RSI 表示跨运行流程的任务级反馈闭环，验证与经验改进由 Agent 侧服务协调；Skill 更新需经过验证和晋升条件，不是每次动作后立即发生，也不涉及模型权重自训练。具体实现见[框架介绍](docs/zh/01-framework-introduction.md)与[经验和自进化指南](docs/zh/05-agent-experience-and-skill-evolution.md)。

<a id="why"></a>
## Why · 为什么选择 PhyAgentOS？

| 核心能力 | 带来的价值 |
| --- | --- |
| **一套框架，多种控制方式** | 通过已安装 Skill 暴露的能力，接入通用模型、动作模型或混合控制。 |
| **以任务结果为准的验证** | 结合观测和执行事实判断目标是否达成，需要恢复时支持有预算的重新规划。 |
| **让经验改进后续任务** | 从经过验证的经验中积累可复用工作流与作用域 Lesson，并通过受控晋升和版本记录管理改进。 |
| **可复用的物理执行能力** | 将环境相关工具与 Runtime 封装为版本化 Skill，使认知规划与机器人接入保持解耦。 |

<a id="quick-start"></a>
## Quickstart · 如何开始使用

先运行模型驱动的 CLI 对话，再按下文接入机器人或仿真 Skill。**准备：** Python 3.11 或 3.12、Git，以及模型服务的 API Key。

### 1. 安装与初始化

```bash
git clone https://github.com/PhyAgentOS/PhyAgentOS-core.git
cd PhyAgentOS-core
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
paos onboard
```

Windows PowerShell 请将激活命令替换为 `.venv\Scripts\Activate.ps1`。

### 2. 配置模型

在 `~/.PhyAgentOS/config.json` 中修改以下字段，保留其他生成的配置。此例使用 OpenRouter：将 `YOUR_API_KEY` 替换为你的密钥，并选择账号可用的模型。

```json
{
  "agents": {
    "defaults": {
      "model": "openrouter/openai/gpt-4o-mini",
      "provider": "openrouter"
    }
  },
  "providers": {
    "openrouter": {"apiKey": "YOUR_API_KEY"}
  }
}
```

### 3. 运行第一条请求

```bash
paos status
paos agent -m "Hello! Introduce yourself."
paos agent
```

收到模型回复即完成核心 Agent 与模型服务的连通检查；`paos agent` 会打开交互式对话。执行物理任务还需要启动对应的 Skill Runtime。

其他模型服务与部署方式见 [用户手册](docs/zh/02-user-manual.md) · [Docker 部署](docs/user_manual/DOCKER.md).

<a id="control-modes"></a>
## 一套执行框架，三种控制方式

![通用模型、混合控制与动作模型通过 PhyAgentOS 执行动作](docs/imgs/control-modes-original.jpg)

图中的 System 1 / System 2 沿用所提供原图的命名，分别指通用模型控制与动作模型控制。

以上描述的是接入方式；具体模型与环境取决于安装的 Physical Execution Skill。Core 不捆绑模型权重、仿真资源或机器人驱动。

<a id="benchmarks"></a>
## Benchmark

以下展示 **LIBERO-Long** 与 **RoboDojo** 上的任务成功率。深色为 PhyAgentOS（PAOS）结果，浅色为公开榜单参考值。

![LIBERO-Long benchmark](docs/imgs/benchmark-libero-long-original.jpg)

![RoboDojo benchmark](docs/imgs/benchmark-robodojo-original.jpg)

| PhyAgentOS 配置 | LIBERO-Long ↑ | RoboDojo ↑ |
| --- | ---: | ---: |
| GPT6 + π0.5 | **100.00%** | 23.30% |
| DeepSeek 4.1 Flash + π0.5 | 97.22% | **26.00%** |
| GLM 5.3 Flash + π0.5 | — | 20.00% |
| DeepSeek 4.1 Flash | 82.00% | — |

> 使用维护者提供的评测原图。完整数值与评测口径见 [Benchmark 说明](docs/benchmarks.md)；“—”表示未提供。

<a id="robot-skills"></a>
## 接入机器人或仿真环境

**Physical Execution Skill** 封装工作流及其运行需求。安装环境对应的 Skill、启动其中一个命名 profile 后，即可让 Agent 使用它。

**1. 为托管 Runtime 安装 Dora**

v1.0.0 的兼容基线为 Dora CLI **0.4.1**（`dora-message` **0.7.0**）。Linux/macOS：

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/dora-rs/dora/releases/download/v0.4.1/dora-cli-installer.sh | sh
dora --version
```

`paos skill start` 需要 Dora，上面的 CLI 对话无需安装。[Windows 与 Cargo 安装说明](docs/zh/02-user-manual.md#托管-skill-profile-所需的-dora-cli)。

**2. 查找并安装 Skill**

从 Registry 下载时，在配置中设置 `resourceRegistry.url`，或将环境变量 `PAOS_RESOURCE_REGISTRY_URL` 指向部署方提供的 Registry，然后运行：

```bash
paos skill search
paos skill install <skill-name> --version <version>
paos skill inspect <skill-name>
```

将 `<skill-name>`、`<version>` 替换为搜索结果。也可以安装独立获取的本地 Bundle：

```bash
paos skill install /path/to/skill-bundle.tar.gz --local
```

**3. 启动 Runtime 并提交任务**

将 `<profile>` 替换为该 Skill 声明的 profile；模型、资源与硬件准备请遵循对应 Skill 的说明。

```bash
paos skill start <skill-name> --profile <profile>
paos skill status <skill-name>
paos agent -m "Inspect the active Skill and Physical Execution capabilities, then report which tasks are available."
```

Runtime 健康后，在 `paos agent` 中描述该 Skill 支持的任务。启动 Agent 不会自动下载或启动 Runtime。

<details>
<summary>常用运行管理命令</summary>

```bash
paos skill list
paos skill logs <skill-name>
paos skill stop <skill-name>
paos gateway
```

`paos gateway` 运行已配置的消息渠道与后台服务。就绪检查与排障见 [运行手册](docs/user_manual/README.md).

</details>

<a id="documentation"></a>
## 文档导航

| 我想…… | 阅读入口 |
| --- | --- |
| 安装、配置并运行 PhyAgentOS | [用户手册](docs/zh/02-user-manual.md) |
| 开发 Skill 或接入新环境 | [集成开发指南](docs/user_development_guide/README.md) |
| 了解 Physical Execution API | [Physical Execution Tool API](docs/forge/README_zh.md) |
| 开发与测试 Core | [开发者手册](docs/zh/03-developer-manual.md) |
| 查看测评数值与来源 | [Benchmark](docs/benchmarks.md) |
| 浏览全部文档 | [文档索引](docs/README.md) |

## Changelog · 最近更新

| 版本 | 日期 | 更新 |
| --- | --- | --- |
| **v1.0.0** | 2026-08-30 | 发布首个稳定版本，升级 Bridge 依赖并修复安全问题。 |
| **v0.2.3** | 2026-08-27 | 支持独立分发的 Physical Execution Skill、不可变任务绑定，以及 Query / Action / Session 生命周期管理。 |
| **v0.2.2** | 2026-08-21 | 统一 Physical Execution Tool API 执行入口，引入 AgentTask、可校验的 Skill Runtime 与 Resource Registry 接入。 |

查看[完整更新记录](CHANGELOG.md)。

## 参与贡献与社区

欢迎报告问题、改进 Skill、接入新环境或完善文档。请先阅读[开发者手册](docs/zh/03-developer-manual.md)，再提交 [Issue](https://github.com/PhyAgentOS/PhyAgentOS-core/issues) 或 Pull Request。

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

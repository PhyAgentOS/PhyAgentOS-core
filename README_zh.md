<div align="center">
  <img src="docs/imgs/logo_en.png" alt="PhyAgentOS" width="560">

  <h3>面向物理智能体的递归自我进化基础设施</h3>

  <p>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-core/stargazers">
      <img src="https://img.shields.io/github/stars/PhyAgentOS/PhyAgentOS-core?style=social" alt="Stars">
    </a>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-core/network/members">
      <img src="https://img.shields.io/github/forks/PhyAgentOS/PhyAgentOS-core?style=social" alt="Forks">
    </a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/Python-≥3.11-3776AB?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/Version-v1.0.0-47A882" alt="Version">
    <img src="https://img.shields.io/badge/License-MIT-3DA639" alt="License">
    <a href="https://arxiv.org/pdf/2607.16636">
      <img src="https://img.shields.io/badge/技术报告-arXiv-b31b1b?logo=arxiv&logoColor=white" alt="技术报告">
    </a>
    <a href="https://phy-agent-os.net/">
      <img src="https://img.shields.io/badge/Website-online-FF6B35" alt="Website">
    </a>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-core">
      <img src="https://img.shields.io/badge/PRs-Welcome-2EA44F" alt="PRs">
    </a>
  <p>
    <a href="https://space.bilibili.com/3546880296355920?spm_id_from=333.1007.0.0">
      <img src="https://img.shields.io/badge/Bilibili-00A1D6?logo=bilibili&logoColor=white" alt="Bilibili">
    </a>
    <a href="https://www.xiaohongshu.com/user/profile/673d83e3000000001c01a183">
      <img src="https://img.shields.io/badge/%E5%B0%8F%E7%BA%A2%E4%B9%A6-FF2442?logo=xiaohongshu&logoColor=white" alt="小红书">
    </a>
    <a href="https://x.com/phyagentos">
      <img src="https://img.shields.io/badge/X-000000?logo=x&logoColor=white" alt="X">
    </a>
    <a href="https://www.linkedin.com/in/phyagent-os-252372401/">
      <img src="https://img.shields.io/badge/LinkedIn-0A66C2?logo=linkedin&logoColor=white" alt="LinkedIn">
    </a>
    <a href="https://discord.gg/YJztZ4wUM">
      <img src="https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white" alt="Discord">
    </a>
  </p>
  </p>
  <p>
    <sub><a href="README.md">English</a> · <a href="README_zh.md">中文</a> · <a href="docs/README.md">文档</a></sub>
  </p>
</div>

---

PhyAgentOS 是一个面向具身任务的 Agent 框架。Agent 规划高层动作，Forge Tool API 返回 Gateway 的执行事实，观测采集器保存动作前后证据，任务级 Verifier 再判断用户目标是否真正达成。

## 📢 更新日志

| 版本 | 日期 | 更新内容 |
|:-----|:-----|:---------|
| ![v1.0.0](https://img.shields.io/badge/v1.0.0-47A882) | 2026-08-30 | Initial stable release of PhyAgentOS. |
| ![v0.2.3](https://img.shields.io/badge/v0.2.3-47A882) | 2026-08-27 | Forge Skill 可独立安装和管理，经显式激活冻结到 AgentTask，并通过受治理的 Query、Action、Session Tool API 生命周期执行，支持恢复和按版本限定的经验。 |
| ![v0.2.2](https://img.shields.io/badge/v0.2.2-47A882) | 2026-08-21 | 将 Forge 执行统一到 Query/Action Tool API，并增加 AgentTask 聚合、可校验 Skill Runtime、Resource Registry 接入和 move-arm-by-ee Skill，同时保留 Agent 验证与演化能力。 |
| ![v0.2.1](https://img.shields.io/badge/v0.2.1-47A882) | 2026-08-14 | 增加经验证的任务经验、显式工作流 Skill 激活、受控 Skill 自进化、聚类式作用域 Lesson，以及用于语义验证的 Skill 作用域建议上下文。 |
| ![v0.2.0](https://img.shields.io/badge/v0.2.0-47A882) | 2026-08-03 | 引入 Forge 执行架构，全面对接 Forge Gateway 1.0.0；新增不可变 Execution/Evidence 公共契约、系统级语义验证、Planner 主导的恢复、崩溃安全 SQLite 编排，并彻底移除旧 Runtime 执行链。 |
| ![v0.1.7](https://img.shields.io/badge/v0.1.7-47A882) | 2026-07-05 | 支持 Policy loop 与 Target-native builtin 两条 Benchmark 路径，并加入 Agent 验证与失败恢复服务。 |
| ![v0.1.6](https://img.shields.io/badge/v0.1.6-47A882) | 2026-06-27 | 增加 BEHAVIOR-1K 支持、`SessionVerifier` 与显式 Session 验证工具。 |
| ![v0.1.5](https://img.shields.io/badge/v0.1.5-47A882) | 2026-06-11 | 清理协议文件与文档，将游戏场景迁移到 `general-game-agent` 分支，主线聚焦仿真与真机工作。 |
| ![v0.1.4](https://img.shields.io/badge/v0.1.4-11648A) | 2026-06-05 | 改进 onboarding、补充通信协议、优化代码规范，并推进 Game Agent 与 Benchmarking。 |
| ![v0.1.3](https://img.shields.io/badge/v0.1.3-11648A) | 2026-05-25 | 建立严格的 `PolicySkillRuntime` / `BuiltinSkillRuntime` 分离，并推进 Game Agent Benchmark。 |
| ![v0.1.2](https://img.shields.io/badge/v0.1.2-11648A) | 2026-05-20 | 引入感知插件系统、Sensor/Perception 配置与可审计的 Environment 写回。 |
| ![v0.1.1](https://img.shields.io/badge/v0.1.1-11648A) | 2026-05-18 | 发布 Session-Centered Runtime MVP 与初始 Dummy Simulation 执行链。 |
| ![v0.1.0](https://img.shields.io/badge/v0.1.0-11648A) | 2026-04-29 | 发布 Hackathon 基线，包括插件化 HAL 与早期 ReKep、SAM3、抓取和 VLN 流程。 |

## 为什么选择 PhyAgentOS？

<table>
<tr><td width="32">🧭</td><td width="190"><b>唯一执行边界</b></td><td>机器人动作统一进入版本化 Forge Gateway 契约；Agent 不直接访问策略、仿真器、Dora 节点或硬件 SDK。</td></tr>
<tr><td>🔎</td><td><b>先证据，后结论</b></td><td>绑定 Action 前后的图像与可选机器人状态经过校验后落盘，保留 source、sequence、时间、大小、摘要和 retention 信息。</td></tr>
<tr><td>🧠</td><td><b>动作无关验证</b></td><td>Verifier 接收 goal、criteria、constraints、执行事实、证据、lineage history 与可选的 Skill 作用域建议，不设计动作专用开关；建议不能替代 criteria 或证据。</td></tr>
<tr><td>🧱</td><td><b>崩溃安全任务聚合</b></td><td>SQLite 事务持久化 AgentTask、PlanRevision、Query record 与 Gateway invocation 引用，不创建第二套物理执行协议。</td></tr>
<tr><td>🔄</td><td><b>Planner 主导恢复</b></td><td>恢复判定在同一任务追加有预算的 PlanRevision；未知物理效果必须先核实，不能盲目重试。</td></tr>
<tr><td>📚</td><td><b>作用域经验</b></td><td>经过验证的 AgentTask 支持可复用工作流 Skill 和聚类 Lesson；无关失败只保留诊断，学习到的指导仅随匹配 Skill 动态加载。</td></tr>
</table>

## 架构

```text
用户 / 消息渠道 / 定时事件
              │
              ▼
      AgentLoop + Planner
              │  绑定 AgentTask 或无任务调用
              ▼
       ForgeToolClient ─────────► AgentTask SQLite + evidence
              │                         │
              │ HTTP Tool API           ▼
              ▼                  ForgeTaskVerifier
 Gateway /tools → ToolInvocation        │
              │                  verdict / PlanRevision
              ▼
 ToolEndpoint → Dora → 机器人/仿真器

终结 AgentTask ───────► Experience Coordinator ──► evolution ledger
                                │                         │
                         Skill candidates          scoped Lessons
                                └──────────► workspace Skills
```

系统始终分离三类事实：

1. **Execution**：Gateway 执行了哪个 Query，或接纳了哪个 ToolInvocation，以及它如何终结。
2. **Evidence**：PAOS 在命令执行前后观察到了什么。
3. **Verdict**：每一项系统级 success criterion 是否满足。

## 核心能力

| 领域 | 当前能力 |
|:-----|:---------|
| Forge 契约 | Query、Action 与 Session 经 `/tools`、`/invocations` 进入同一 Tool API。 |
| 异步编排 | Query 同步返回；Action 与 Session admission 返回 invocation ID，并通过 `/invocations` 核对状态。 |
| 身份校验 | Agent `task_id`、`revision_id`、Query record ID、Gateway `invocation_id` 与 `attempt_id` 始终分离。 |
| 证据 | 通过 `/ws/images`、`/ws/state` 异步采集；使用有界最新帧缓存、媒体校验、SHA-256 和 source sequence 边界。 |
| 验证 | 支持 `off`、`audit`、`enforce`、`recovery`，并生成逐 criteria 的结构化 verdict。 |
| 恢复 | 在同一 AgentTask 上追加有预算和 deadline 的 PlanRevision，不盲目重试未知效果。 |
| 持久化 | SQLite WAL AgentTask 事件日志和工作区证据；现有 evolution 数据保持可读。 |
| 任务经验 | 显式 Skill 激活、去敏 AgentTask episode、异步反思、聚类式作用域 Lesson 与受控 Skill 晋升。 |
| Skill Runtime | manifest v2 Bundle、SHA-256 清单、安全事务安装、命名 Dora profile、持久化健康状态与显式 Registry 解析。 |
| Agent 平台 | CLI、多渠道 Gateway、Provider、工具、Skills、MCP、记忆、Cron、Heartbeat 和知识工作区。 |

## 5 分钟快速开始

### 1. 安装

```bash
git clone https://github.com/PhyAgentOS/PhyAgentOS-core.git
cd PhyAgentOS-core
python -m pip install -e .

# 开发与测试依赖
python -m pip install -e ".[dev]"
```

推荐 Python 3.11 或 3.12。具体 Forge Skill 及其 Runtime 制品独立分发。

通用 Agent 与 `paos skill install` 不需要 Dora；`paos skill start` 启动托管 Forge Skill
profile 时，`PATH` 中必须存在 Dora CLI。PhyAgentOS 1.0.0 以 Dora CLI v0.4.1 及
`dora-message` v0.7.0 作为 Forge Skill 兼容基线。Linux 或 macOS 应安装该精确版本：

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/dora-rs/dora/releases/download/v0.4.1/dora-cli-installer.sh | sh
dora --version
# dora-cli 0.4.1
# dora-message: 0.7.0
```

Windows、Cargo 安装和生命周期检查见[用户手册](docs/zh/02-user-manual.md#托管-skill-profile-所需的-dora-cli)。

### 2. 初始化工作区

```bash
paos onboard
```

该命令创建 `~/.PhyAgentOS/config.json`，并在 `~/.PhyAgentOS/workspace` 初始化默认工作区。

### 3. 配置 Provider 与 Forge

配置保存为 camelCase，同时也接受 snake_case 输入。

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.PhyAgentOS/workspace",
      "model": "openrouter/openai/gpt-4o-mini",
      "provider": "openrouter"
    },
    "verification": {
      "serviceEnabled": true,
      "evidenceRetention": "failed",
      "maxReplansPerEpisode": 2,
      "maxVerifierCallsPerRun": 50
    },
    "evolution": {
      "enabled": true,
      "scope": "verified_forge_lineage",
      "promotionMode": "guarded_auto",
      "minSuccessfulEpisodes": 3,
      "minLessonEpisodes": 3,
      "maxLessonsPerSkill": 8,
      "maxEvolutionCallsPerRun": 20
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "YOUR_API_KEY"
    }
  },
  "forge": {
    "requestTimeoutS": 10,
    "pollIntervalS": 0.5,
    "executionTimeoutS": 300,
    "evidence": {
      "requiredImageSources": ["front"],
      "captureTimeoutS": 5,
      "postCaptureTimeoutS": 5,
      "connectionTimeoutS": 2,
      "maxArtifactBytes": 8388608,
      "associationQuality": "best_effort"
    }
  },
  "resourceRegistry": {
    "url": "https://paos-resource-manager.dev.x-era.com"
  }
}
```

`front` 只是示例。`resourceRegistry.url` 用于选择通用制品仓库；全部制品都从本地 Bundle
或显式静态 index 安装时可留空。PAOS 只连接到已显式启动且健康的 Skill Runtime manifest
所声明的 Gateway URL；启动 Agent 不会隐式启动或下载某个具体 Skill。

Atlas Cloud 可作为 OpenAI 兼容的可选 Provider 使用，不会改变默认配置。请使用 Atlas Cloud
实时模型目录中的模型 ID：

```json
{
  "agents": {
    "defaults": {
      "model": "Qwen/Qwen3-235B-A22B-Instruct-2507",
      "provider": "atlas"
    }
  },
  "providers": {
    "atlas": {
      "apiKey": "YOUR_ATLASCLOUD_API_KEY"
    }
  }
}
```

### 4. 启动 Agent

先启动所需的已安装 Skill Runtime，再选择一种 PAOS 入口：

```bash
# 交互式 CLI
paos agent

# 单条请求；Agent 可以创建 AgentTask 并将 Tool API 调用绑定到任务
paos agent -m "先检查 Forge 能力，再把物体放入目标区域，并根据可见结果验证任务。"

# 长期运行消息渠道、Cron、Heartbeat、Agent 与 Forge Tool API 集成
paos gateway
```

使用 `paos status` 检查本地模型与工作区配置；通过 Agent 调用 `forge_tool_context` 获取实时 ToolSpec、binding、readiness、Endpoint status 和 frame profile。

## 验证模式

| 模式 | 任务契约 | 最终结果 | 恢复 |
|:-----|:---------|:---------|:-----|
| `off` | goal/criteria 可省略 | 跟随 Gateway 执行状态 | 永不恢复 |
| `audit` | 必须提供 goal 和至少一项 criterion | 保持执行派生终态，只记录 verdict/error | 永不恢复 |
| `enforce` | 必须提供 goal 和至少一项 criterion | verdict 决定成功；缺证、非法输出、服务错误和 `inconclusive` 均 fail closed | 不恢复 |
| `recovery` | 必须提供 goal 和至少一项 criterion | 与 enforce 一样 fail closed；`replan_required` 进入恢复 | Planner 追加 PlanRevision |

典型的非 `off` 契约如下：

```json
{
  "mode": "recovery",
  "goal": "红色方块位于托盘内。",
  "success_criteria": [
    "红色方块在图像中完全位于托盘边界内。",
    "没有其他物体被移出工作区。"
  ],
  "constraints": [
    "不要移动蓝色方块。"
  ],
  "evidence_policy": {
    "required_kinds": ["rgb_image"],
    "required_sources": ["front"],
    "minimum_association": "best_effort"
  }
}
```

## Agent 可用的 Forge 工具

| 工具 | 用途 |
|:-----|:-----|
| `forge_task_create/get/begin_revision/finalize/cancel` | 管理 PAOS 任务聚合与用户级验证生命周期。 |
| `forge_tool_context` | 读取实时 ToolSpec、binding、readiness、Endpoint status 和 frame profile。 |
| `forge_tool_query` | 调用同步 Query，可选绑定 AgentTask。 |
| `forge_tool_start_action` | 接受异步 Action 并保留 Gateway invocation identity。 |
| `forge_tool_action_status/result/cancel_action` | 查询或请求取消，不能把 cancel accepted 当作物理停止。 |
| `forge_tool_start_session` | 按绑定策略启动 task-owned、shared 或 runtime-owned Session。 |
| `forge_tool_session_status/result/stop_session` | 核对 Session，并且只允许生命周期所有者停止它。 |

诊断用 context 工具始终可用。任务与变更类工具要求存在一个健康且显式活动的 Skill Runtime，
并使用本轮 primary `activate_skill` 结果冻结出的不可变 Skill binding。

## Forge Skill Runtime

已安装 Skill 通过显式命令管理。Registry 下载必须配置 `resourceRegistry.url`、
`PAOS_RESOURCE_REGISTRY_URL` 或传入静态 index；Bundle manifest、归档清单和锁定 Node 制品
校验完成前不会替换本地版本。

```bash
paos skill search
paos skill install <skill-name> --version <version>
# 也可以安装独立获取的本地 Bundle
paos skill install /path/to/<skill-name>-<version>.tar.gz --local
paos skill inspect <skill-name>
paos skill start <skill-name> --profile <profile>
paos skill status <skill-name>
# 没有非终态 AgentTask 时，切换到另一个已安装 Runtime
paos skill switch <other-skill-name> --profile <profile>
paos skill logs <skill-name>
paos skill stop <skill-name>

# 也可以安装独立获取的本地 Node 归档
paos forge-node install <skill-name> <node-id> --archive /path/to/<node>.tar.gz
paos forge-node verify <skill-name> <node-id>
```

每个 Forge Skill Bundle 声明工作流文档、所需 Tool ID、命名 Runtime profile，以及精确的
平台/架构 Node lock。每个锁定归档具有精确 SHA-256，并且只包含一个指定文件名的根目录
可执行文件；安装时另行记录并校验解包后的 binary hash。
`python scripts/package_skill.py <bundle-dir> --output-dir <directory>` 可生成确定性发布 Bundle。
PhyAgentOS 源码与发布包不内置具体 Forge Skill、Forge node、模型或仿真资源；
部署者只需独立获取实际需要的 Skill 并显式安装。
[集成开发指南](docs/user_development_guide/README.md#5-打包发布与本地闭环)说明 Bundle 布局、
本地验证、不可变发布顺序与 Registry 验收。

## 任务经验与 Skill 自进化

`agents.evolution.enabled=true` 时，Agent 会在首次工具调用前检查已注册 Skill 摘要。`activate_skill(name, role)` 会加载完整工作流及当前任务适用的作用域 Lesson，并记录可审计的任务—Skill 绑定。每个 turn 最多一个 primary Skill，可有多个 supporting Skill；只有 primary 可被自动更新。直接读取 `SKILL.md` 不会建立该绑定。

具有语义判定的 AgentTask 会在后台形成任务级经验：

- 与工作流相关的语义失败先形成规范化 observation；同一失败模式默认需要三个独立 AgentTask 支持，才能合成抽象 Lesson 并投影到 `skills/<name>/references/LESSONS.md`；
- 任务不可满足、Verifier/证据能力不足、外部基础设施问题和不确定归因只记录诊断，不生成 Skill Lesson；
- 语义成功支持 Skill candidate；默认三个独立成功 AgentTask 后，才可晋升经过校验的 workspace Skill revision；
- `inconclusive`、非法 verdict、review-only 和 `verification=off` 不训练 Skill。

已激活 Skill 返回的适用 active Lesson 会随 AgentTask binding 冻结。自动验证、后续 PlanRevision 验证和 review 都使用同一组作用域 Lesson，并且只把它们作为工作流建议。每个 criterion 状态与整体 verdict 都必须依据任务契约、执行事实和合法证据；Lesson 不能证明 criterion，也不能作为 evidence reference。没有激活 Skill 时，不向 Verifier 提供学习型 Lesson。

演化链路 fail-open，不改变 Forge 的提交、执行、证据、验证与恢复流程。Built-in Skill 不会原地修改；晋升结果写为 workspace override，旧版本保存在 `.paos/evolution/`。

## 持久化与工作区

```text
~/.PhyAgentOS/workspace/
├── AGENTS.md / SOUL.md / USER.md / TOOLS.md / SKILLS.md
├── EMBODIED.md / ENVIRONMENT.md / LESSONS.md / TASK.md
├── .paos/agent_tasks/tasks.sqlite3
├── .paos/evolution/experience.sqlite3
├── .paos/evolution/revisions/<skill>/
├── skills/<skill>/SKILL.md
├── skills/<skill>/references/LESSONS.md
└── artifacts/agent_tasks/<task_id>/
    ├── evidence_bundle.json
    ├── before_snapshot.json / after_snapshot.json
    └── evidence/
```

`EMBODIED.md`、`ENVIRONMENT.md` 和 SceneGraph 继续作为知识面存在，但不承担执行队列职责。启用 evolution 后，根目录 `LESSONS.md` 作为旧版/人工材料保留，但不再注入 Agent turn，也不进入 Forge 验证；只有当前任务已激活 Skill 冻结的 active scoped Lesson 可以作为非权威建议随验证请求传入。学习型 Lesson 以经验数据库为事实源。PAOS 不再读取或生成旧 Runtime Markdown queue 文件。

## 项目结构

```text
PhyAgentOS/
├── PhyAgentOS/agent/          # AgentLoop、工具、记忆、经验与 Verifier 集成
├── PhyAgentOS/forge/          # Tool API client、AgentTask 聚合与观测
├── PhyAgentOS/skill_runtime/  # Bundle 校验/安装与显式 Dora 生命周期
├── PhyAgentOS/verification/   # 公共契约、请求构造、Engine、Service
├── PhyAgentOS/channels/       # 消息渠道
├── PhyAgentOS/config/         # 配置 Schema 与加载
├── PhyAgentOS/templates/      # Agent 知识/工作区模板
└── docs/                      # 中英文、运维、接入与 Forge 文档
```

## 文档

| 文档 | 面向 | 内容 |
|:-----|:-----|:-----|
| [Changelog](CHANGELOG.md) | 所有人 | 按 Added、Changed、Security 分类的详细发布记录 |
| [文档索引](docs/README.md) | 所有人 | 双语阅读路径与完整文档地图 |
| [框架介绍](docs/zh/01-framework-introduction.md) | 架构师、用户 | 设计、边界、生命周期和当前能力 |
| [用户手册](docs/zh/02-user-manual.md) | 使用与运维人员 | 安装、配置、任务、Artifact 和排障 |
| [开发者手册](docs/zh/03-developer-manual.md) | 开发者 | 契约、不变量、扩展点和测试 |
| [Forge 配置参考](docs/zh/04-forge-configuration-reference.md) | 部署人员 | Forge、Evidence、Verification 和 Task 精确字段 |
| [Agent 经验与 Skill 自进化](docs/zh/05-agent-experience-and-skill-evolution.md) | 用户、开发者 | Skill 激活、Episode、Lesson 聚类、晋升、持久化与安全门控 |
| [运行手册](docs/user_manual/README.md) | 运维人员 | 启动、监控、重启、取消与故障处理 |
| [集成开发指南](docs/user_development_guide/README.md) | 生态开发者 | 不引入 action-specific verifier 的 Gateway action 接入方式 |
| [Forge Tool API 接入契约](docs/forge/README_zh.md) | Gateway/PAOS 开发者 | Query/Action/Session Tool API、不可变 Skill binding、AgentTask、Runtime、验证与恢复 |

## 开发验证

```bash
python -m pip install -e ".[dev]"
pytest
ruff check PhyAgentOS tests
python -m compileall -q PhyAgentOS tests
```

可选黑盒测试可以通过 `FORGE_GATEWAY_URL` 连接运行中的兼容 Gateway。测试与 PAOS 文档不得修改 Gateway 源码或配置。

## 参与贡献

欢迎提交 PR 和 Issue，我们的开发计划可以在此处查看👉 [开发计划](https://phy-agent-os.net/docs/developer-guide/)。


---

<div align="center">

由 **中山大学 HCP 实验室**、**鹏城实验室** 与 **拓元智慧** 联合开发

<br>

<img src="docs/imgs/HCP.jpg" alt="HCP" height="128">
&nbsp;&nbsp;&nbsp;
<img src="docs/imgs/Pengcheng.png" alt="Pengcheng" height="128">
&nbsp;&nbsp;&nbsp;
<img src="docs/imgs/logo-xera-mark.png" alt="X-Era Lab" height="128">

<br>
<sub>MIT License · Copyright © 2025-2026 PhyAgentOS</sub>

</div>

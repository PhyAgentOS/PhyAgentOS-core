# PhyAgentOS Documentation

版本 / Version: **1.0.0**<br>
实现基线 / Implementation baseline: **Forge Skill binding, Query/Action/Session Tool API, AgentTask recovery, and version-scoped experience, 2026-08-30**

本目录由 PhyAgentOS 开发团队面向用户、运维人员和生态开发者维护。文档只把仓库源码、配置 Schema 与测试实际覆盖的行为称为“当前能力”。`plan/` 中的设计报告是历史背景，不替代这里的运行契约。

The PhyAgentOS team maintains this directory for users, operators, and ecosystem developers. A feature is described as current only when it is supported by repository source, configuration schemas, and tests. Historical reports under `plan/` provide design context but do not replace the operational contract documented here.

## 中文

### 核心手册

1. [框架介绍](zh/01-framework-introduction.md)：项目定位、控制面边界、执行—证据—判定分离、生命周期与实现范围。
2. [用户手册](zh/02-user-manual.md)：安装、Provider/Forge 配置、任务描述、验证模式、Artifact 与排障。
3. [开发者手册](zh/03-developer-manual.md)：AgentTask、Tool API client、Skill Runtime、Evidence、Verifier、Recovery 与测试。
4. [Forge 配置参考](zh/04-forge-configuration-reference.md)：Forge Tool API、Resource Registry、Evidence、Verification、AgentTask 和 Embodiment 字段。
5. [Agent 经验与 Skill 自进化](zh/05-agent-experience-and-skill-evolution.md)：Skill 激活与归因、Episode、Lesson 聚类、Skill 晋升、持久化和安全门控。

### 专题手册

- [Game 接入前通用交互基础](interaction/README.md)：可选协议、真实任务治理、后台 Runner、恢复安全门及假环境验收。

- [运行手册](user_manual/README.md)：上线前检查、启动顺序、状态观测、取消、重启恢复、备份与故障分层。
- [Docker 部署指南](user_manual/DOCKER.md)：基于 Docker 的快速部署，配合一键脚本完成构建、初始化与运行（仅外连网关，无入站端口）。
- [集成开发指南](user_development_guide/README.md)：Tool/Node/Skill 接入、Bundle 打包与不可变发布、本地闭环、证据源、Provider 和 PAOS 扩展边界。
- [通信架构](user_development_guide/COMMUNICATION.md)：Agent 消息、Forge HTTP/WebSocket、system event、SQLite 与 Artifact 边界。
- [Forge Tool API 接入契约](forge/README_zh.md)：Query/Action/Session、不可变 Skill binding、ToolInvocation、AgentTask、证据、验证、恢复和 Skill Runtime 契约。

### 推荐阅读路径

| 目标 | 建议路径 |
|:-----|:---------|
| 先理解项目为何区分执行与任务成功 | [框架介绍](zh/01-framework-introduction.md) → [Forge 接入契约](forge/README_zh.md) |
| 首次部署并跑通 Agent + Forge | [用户手册](zh/02-user-manual.md) → [配置参考](zh/04-forge-configuration-reference.md) |
| 负责长期在线和故障处理 | [运行手册](user_manual/README.md) → [通信架构](user_development_guide/COMMUNICATION.md) |
| 用 Docker 快速部署 | [Docker 部署指南](user_manual/DOCKER.md) |
| 在 Gateway 增加新机器人动作 | [集成开发指南](user_development_guide/README.md) → [开发者手册](zh/03-developer-manual.md) |
| 开发、打包并发布 Forge Skill | [集成开发指南](user_development_guide/README.md#5-打包发布与本地闭环) → [Forge 接入契约](forge/README_zh.md) |
| 修改证据、验证、恢复或持久化 | [开发者手册](zh/03-developer-manual.md) → [Forge 接入契约](forge/README_zh.md) |
| 使用或扩展任务经验、Lesson 与 Skill 自进化 | [Agent 经验与 Skill 自进化](zh/05-agent-experience-and-skill-evolution.md) → [开发者手册](zh/03-developer-manual.md) |

## English

### Core manuals

1. [Framework Introduction](en/01-framework-introduction.md): positioning, control-plane boundaries, execution/evidence/verdict separation, lifecycle, and implemented scope.
2. [User Manual](en/02-user-manual.md): installation, provider and Forge configuration, task description, verification modes, artifacts, and troubleshooting.
3. [Developer Manual](en/03-developer-manual.md): AgentTask, Tool API client, Skill Runtime, evidence, verifier, recovery, and testing.
4. [Forge Configuration Reference](en/04-forge-configuration-reference.md): exact Forge Tool API, Resource Registry, evidence, verification, AgentTask, and embodiment fields.
5. [Agent Experience and Skill Evolution](en/05-agent-experience-and-skill-evolution.md): Skill activation and attribution, episodes, Lesson clustering, Skill promotion, persistence, and guardrails.

### Focused manuals

- [Governed Interaction Foundation](interaction/README.md): opt-in contracts, task governance, background monitoring, recovery gates and fake-world acceptance.

- [Operations Manual](user_manual/README_en.md): preflight checklist, startup order, observation, cancellation, restart recovery, backup, and failure layers.
- [Docker Deployment Guide](user_manual/DOCKER_en.md): Docker-based quick deployment with a one-click script for build, init, and run (outbound-only gateway, no inbound port).
- [Integration Development Guide](user_development_guide/README_en.md): Tool, Node, and Skill integration; Bundle packaging and immutable publication; the local loop; evidence sources; providers; and PAOS extension boundaries.
- [Communication Architecture](user_development_guide/COMMUNICATION_en.md): Agent messages, Forge HTTP/WebSocket, system events, SQLite, and artifact boundaries.
- [Forge Tool API Integration Contract](forge/README.md): Query/Action/Session, immutable Skill binding, ToolInvocation, AgentTask, evidence, verification, recovery, and Skill Runtime contracts.

### Suggested reading paths

| Goal | Suggested path |
|:-----|:---------------|
| Understand why execution and task success differ | [Framework Introduction](en/01-framework-introduction.md) → [Forge Integration Contract](forge/README.md) |
| Deploy Agent + Forge for the first time | [User Manual](en/02-user-manual.md) → [Configuration Reference](en/04-forge-configuration-reference.md) |
| Operate a long-running service | [Operations Manual](user_manual/README_en.md) → [Communication Architecture](user_development_guide/COMMUNICATION_en.md) |
| Deploy quickly with Docker | [Docker Deployment Guide](user_manual/DOCKER_en.md) |
| Add a new robot action in Gateway | [Integration Guide](user_development_guide/README_en.md) → [Developer Manual](en/03-developer-manual.md) |
| Develop, package, and publish a Forge Skill | [Integration Guide](user_development_guide/README_en.md#5-package-publish-and-close-the-local-loop) → [Forge Integration Contract](forge/README.md) |
| Change evidence, verification, recovery, or persistence | [Developer Manual](en/03-developer-manual.md) → [Forge Integration Contract](forge/README.md) |
| Use or extend task experience, Lessons, or Skill evolution | [Agent Experience and Skill Evolution](en/05-agent-experience-and-skill-evolution.md) → [Developer Manual](en/03-developer-manual.md) |

## Terminology

| Term | Meaning |
|:-----|:--------|
| AgentTask | The PAOS aggregate for a user-visible goal, immutable PlanRevisions, bound calls, evidence, and verification. It does not execute the robot. |
| PlanRevision | One append-only planning generation inside a stable AgentTask identity. |
| Query record | The PAOS record of one synchronous Gateway Query bound to an AgentTask. |
| Forge Skill binding | An immutable snapshot of Skill version, Runtime identity, manifest/workflow hashes, and required live ToolSpecs. |
| ToolInvocation | The Gateway-owned identity and lifecycle of one asynchronous Action or Session. |
| Attempt | The Gateway execution attempt identified separately from the ToolInvocation and AgentTask. |
| Tool execution record | A normalized PAOS Query result or Action/Session invocation reference attached to one PlanRevision. |
| Evidence Bundle | Validated, workspace-relative artifact references and capture-quality metadata. |
| Verdict | A structured semantic decision over every success criterion. |
| Task lineage | One AgentTask and all of its PlanRevisions; only one AgentTask may be non-terminal globally. |
| Skill activation | An explicit, per-turn binding between a registered workflow Skill and a task; a direct file read is not an activation. |
| Task episode | One redacted experience record for a completed, semantically verified AgentTask lineage. |
| Failure observation | A normalized, non-answer-specific description of a workflow-related failure pattern. |
| Lesson cluster | Independent AgentTask observations grouped by Skill, workflow, and canonical failure pattern. |
| Scoped Lesson | A validated Lesson with explicit applicability boundaries, dynamically loaded only for a matching activated Skill. |

## Runtime and compatibility boundaries

PAOS supports Forge Query, Action, and Session through `/tools` and `/invocations`. The Agent-side aggregate
is AgentTask, while physical execution remains owned by Gateway ToolInvocation and ToolEndpoint.
Skill Runtime manages manifest-v2 bundles and named Dora profiles; it is distinct from the removed
Markdown queue Runtime. Existing evolution, experience, verification, and Agent workspace data are
read in place. Registry downloads are explicit and digest-verified.

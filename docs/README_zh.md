# PhyAgentOS 文档

[English](README.md) | [简体中文](README_zh.md)

版本：**1.1.1**<br>
实现基线：**Forge Skill binding、Query/Action/Session Tool API、AgentTask recovery、版本作用域经验，2026-10-23**

本目录面向用户、运维和开发者。源码、配置 Schema 与测试定义当前行为；历史设计报告不替代运行契约。
开发从[贡献指南](../CONTRIBUTING_zh.md) → [TrackB 总览](user_development_guide/track_b/README_zh.md) → [三种开发模式](user_development_guide/track_b/SKILL_DEVELOPMENT_zh.md) → [Piper X 示例](user_development_guide/PIPER_X_DEMO_zh.md)进入。
外部平台贡献提交及已有登记仓库修改需等待审批服务完成并开放，当前可先本地开发验证。

## 核心手册

1. [框架介绍](zh/01-framework-introduction.md)：项目定位、控制面边界、执行—证据—判定分离、生命周期与实现范围。
2. [用户手册](zh/02-user-manual.md)：安装、Provider/Forge 配置、任务描述、验证模式、Artifact 与排障。
3. [Core 开发者手册](zh/03-developer-manual.md)：AgentTask、Tool API client、Skill Runtime、Evidence、Verifier、Recovery 与测试。
4. [Forge 配置参考](zh/04-forge-configuration-reference.md)：Forge Tool API、Resource Registry、Evidence、Verification、AgentTask 和 Embodiment 字段。
5. [Agent 经验与 Skill 自进化](zh/05-agent-experience-and-skill-evolution.md)：Skill 激活与归因、Episode、Lesson 聚类、Skill 晋升、持久化和安全门控。

## 专题手册

- [Benchmark 文档](benchmarks.md)：测评结果、耗时、Token 消耗与评测口径。

- [Game 接入前通用交互基础](interaction/README_zh.md)：可选协议、真实任务治理、后台 Runner、恢复安全门及假环境验收。

- [运行手册](user_manual/README_zh.md)：上线前检查、启动顺序、状态观测、取消、重启恢复、备份与故障分层。
- [Docker 部署指南](user_manual/DOCKER_zh.md)：基于 Docker 的快速部署，配合一键脚本完成构建、初始化与运行（仅外连网关，无入站端口）。
- [Bundle 与 Tool 接入参考](user_development_guide/README_zh.md)：ToolSpec 与 manifest v2 示例、启动 hook、Bundle 打包、本地闭环及接口一致性检查。
- [Core 通信架构](user_development_guide/COMMUNICATION_zh.md)：Agent 消息、Forge HTTP/WebSocket、system event、SQLite 与 Artifact 边界。
- [Forge Tool API 接入契约](forge/README_zh.md)：Query/Action/Session、不可变 Skill binding、ToolInvocation、AgentTask、证据、验证、恢复和 Skill Runtime 契约。

- [TrackB 框架说明](user_development_guide/track_b/README_zh.md)：整体架构、仓库目录、公共协议、Runtime 关系和三种 Skill 开发模式。
- [Piper X 二次开发 demo](user_development_guide/PIPER_X_DEMO_zh.md)：复用原 pipeline 接入新机械臂。

## 推荐阅读路径

| 目标 | 建议路径 |
|:-----|:---------|
| 先理解项目为何区分执行与任务成功 | [框架介绍](zh/01-framework-introduction.md) → [Forge 接入契约](forge/README_zh.md) |
| 首次部署并跑通 Agent + Forge | [用户手册](zh/02-user-manual.md) → [配置参考](zh/04-forge-configuration-reference.md) |
| 负责长期在线和故障处理 | [运行手册](user_manual/README_zh.md) → [Core 通信架构](user_development_guide/COMMUNICATION_zh.md) |
| 用 Docker 快速部署 | [Docker 部署指南](user_manual/DOCKER_zh.md) |
| 在 Gateway 增加新机器人动作 | [TrackB 开发手册](user_development_guide/track_b/SKILL_DEVELOPMENT_zh.md) → [节点协议](user_development_guide/track_b/PROTOCOLS_zh.md) |
| 开发与打包 Forge Skill，开放后发布 | [TrackB Skill 开发](user_development_guide/track_b/SKILL_DEVELOPMENT_zh.md) → [Bundle 参考](user_development_guide/README_zh.md#5-打包发布与本地闭环) |
| 修改证据、验证、恢复或持久化 | [Core 开发者手册](zh/03-developer-manual.md) → [Forge 接入契约](forge/README_zh.md) |
| 使用或扩展任务经验、Lesson 与 Skill 自进化 | [Agent 经验与 Skill 自进化](zh/05-agent-experience-and-skill-evolution.md) → [Core 开发者手册](zh/03-developer-manual.md) |

## 文档分工

- Core 开发者手册维护模块、事务、验证和 Runtime 实现。
- Forge Tool API 契约统一维护 HTTP 路由、身份和 invocation/task 语义。
- TrackB 维护节点协议、源码目录、组装关系和三种 Skill 开发流程。
- Bundle 与 Tool 接入参考维护 schema、manifest、启动 hook、打包和接口一致性检查。
- Core 通信架构维护 Agent bus、观测、验证服务和持久化边界。
- 运行与 Docker 手册维护部署和运行步骤；交互基础作为独立受限扩展。

## Runtime 与兼容边界

PAOS 通过 `/tools` 与 `/invocations` 支持 Query、Action 和 Session。AgentTask 负责 Agent 侧聚合，
物理执行由 Gateway ToolInvocation 与 ToolEndpoint 拥有。Skill Runtime 管理 manifest v2 Bundle
及命名 Dora profile，与已移除的 Markdown 队列 Runtime 不同。既有经验、演化、验证及工作区数据
原位读取；Registry 下载显式进行并校验摘要。

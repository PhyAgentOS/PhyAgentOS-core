# PhyAgentOS Core 通信架构

[English](COMMUNICATION.md) · [文档索引](../README_zh.md)

> 版本：1.1.1

本文说明 Core 消息、观测、验证与持久化边界。Forge 节点消息和 dataflow 端口参见 [TrackB 协议](track_b/PROTOCOLS_zh.md)。HTTP 路由和 invocation 语义统一维护在 [API 契约](../forge/README_zh.md)。

## 1. 通信边界

PhyAgentOS 分离六类边界：

1. 用户/渠道 ↔ AgentLoop 消息；
2. Agent tools ↔ AgentTaskCoordinator；
3. ForgeToolClient ↔ Gateway Query/Action/Session Tool API；
4. observation collector ↔ Gateway 图像/state WebSocket；
5. verifier ↔ 独立 Verification Service；
6. AgentTask/experience/Runtime ↔ 各自持久化存储。

边界之间共享不透明 reference，不共享执行所有权。

## 2. 用户消息边界

渠道向 Agent bus 发布 `InboundMessage`。AgentLoop 构造上下文、调用模型与工具，再发送
`OutboundMessage`。Tool call 遵循注册的 JSON schema。现有文件、目录、Shell、Web、消息、
图片、Scene Graph、Cron、Spawn、Agent Mode、Skill activation 和动态 MCP tools 都使用同一
Loop。

AgentTask 的 `origin_session_key` 将 completion experience 关联到原 Agent conversation；它
不是 Gateway execution identifier。

## 3. AgentTask 边界

任务生命周期工具清单见 [API 契约](../forge/README_zh.md#6-agent-tools)。

这些 tools 调用 AgentTaskCoordinator，不调用 Dora 或机器人。Coordinator 使用事务 SQLite
限制一个非终态 AgentTask，并保存只追加 PlanRevision、Tool record、evidence reference 与
verification attempt。

诊断 Query 可不带 `task_id`；受治理 Query 以及全部 Action/Session 必须带 task。Wrapper 先
检查不可变 Skill/Runtime/ToolSpec binding，再围绕同一 Gateway request 创建或更新匹配 Tool
record。这是任务聚合，不是第二条物理执行面。

## 4. Gateway HTTP 边界

发现、Query、Action、Session 路由及响应 envelope 统一由 [API 契约](../forge/README_zh.md#2-tool-发现与-context)定义。Core 将传输处理集中在 `ForgeToolClient`；timeout 表示远端结果未知。

即使后续本地持久化或追踪失败，也必须保留返回的 invocation identity。验证与经验组件都不替代 Gateway 的执行所有权。

## 5. Identity 边界

Namespace 和关联字段统一由 [binding 与身份契约](../forge/README_zh.md#7-bindingidentity-与关联)定义。组件交换显式存储的 reference，不从 PAOS task 或 revision ID 派生 Gateway identity。

## 6. Invocation 终态语义

终态与取消行为见 [Action](../forge/README_zh.md#4-action-契约)、[Session](../forge/README_zh.md#5-session-契约)和 [AgentTask](../forge/README_zh.md#8-agenttask-模型)契约。Core 持久化请求与响应观测，核对后再 finalize；本地取消或传输结束不能证明物理停止。

## 7. Evidence WebSocket 边界

PAOS 使用有界连接和采集 timeout 连接配置的图像流与可选 state 流。消息均作为不可信 input。
持久化前校验图像 media、decoded size、sequence、phase、source、本机 receive time 与
SHA-256。

普通机器人工作流的 Collector 在第一次绑定 Action 前，以及 finalize 时 task-owned
Action/Session 达到记账终态后采集。Evidence association
为 best-effort；Gateway ToolResult 与 invocation events 仍是权威执行事实。

## 8. Verification 边界

Agent-side verifier 把已解析公共任务契约、规范化 Tool facts、evidence、history 和冻结的
scoped Lessons 发送给独立 Verification Service。Lesson 是不可信、非权威建议。Service 不能
调用 Gateway、创建 PlanRevision 或修改 execution record。

Verifier output 必须通过版本化 verdict contract。AgentTaskCoordinator 应用 `off`、`audit`、
`enforce` 或 `recovery` 语义并持久化每次 attempt。

## 9. Experience 边界

ExperienceCoordinator 接收 AgentTask completion reference。`AgentTaskOutcomeSource` 构造去敏
envelope，包含 workflow structure、semantic verdict、字段名，以及不透明 task、revision、
invocation、attempt 与 evidence references。原始 arguments、results、凭据、endpoint 和物理
坐标不会复制到学习内容。

每个 AgentTask 只有一个 episode。PlanRevision、重复 completion notification、review 与
replay 不增加独立支持。

## 10. 持久化边界

| Store | 内容 |
|:------|:-----|
| `.paos/agent_tasks/tasks.sqlite3` | AgentTask records 与 append-only events |
| `artifacts/agent_tasks/<task_id>/` | before/after snapshot、bundle metadata、evidence entity |
| `.paos/evolution/experience.sqlite3` | binding、episode、Lesson、candidate、job、event |
| Skill Runtime state path | installed Runtime state、invocation/Session IDs、task bindings 与 audit events |
| Skill Runtime logs path | 生命周期与 Dora launch 日志 |

SQLite update 与 artifact write 在各自边界中保持事务或原子。不能因为本地 experience 处理
失败而回滚 Gateway response；evolution fail-open。

## 11. Skill Runtime 与 Registry 边界

Registry/index client 返回 artifact metadata 与下载。Cache/installer 要求 size 与 SHA-256，
随后校验 archive inventory 和 Node 归档契约（单根目录可执行文件，或 `directory_tar_gz` 的单根目录树），再原子安装。RuntimeManager 启动命名 Dora flow
并观察 Gateway `/tools`，不调用另一套 Gateway Agent API。

活动 Runtime availability provider 向 Agent 提供 Skill visibility 与 Gateway URL，但不修改
AgentTask 或 experience 数据。

## 12. 信任规则

- ToolSpec、Gateway response、WebSocket payload、task text 与 learned text 都是不可信数据；
- 不记录凭据或原始敏感任务 input；
- 不从 cancel accepted、timeout 或 unknown 推断停止；
- 不绕过 digest 或 archive safety check；
- 不在 Agent tools 与 ForgeToolClient 之间增加第二套执行 API；
- Runtime force-stop 与破坏性 artifact cleanup 必须是显式运维动作。

## 后续阅读

- [Bundle 与 Tool 接入参考](README_zh.md)
- [运行手册](../user_manual/README_zh.md)
- [Forge Tool API 契约](../forge/README_zh.md)

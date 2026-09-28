# Forge Tool API 接入契约

[English](README.md) · [文档索引](../README.md)

> 适用于 PhyAgentOS 1.0.0。

## 1. 执行边界

```text
绑定 AgentTask 的调用 / 诊断 Query
        → ForgeToolClient
        → Gateway /tools → ToolInvocation → ToolEndpoint
        → Dora 与机器人节点
```

PAOS 支持 Query、Action 与 Session。AgentTask 聚合用户目标，但 Gateway 仍是物理执行所有者。
诊断 Query 可以不创建任务；Action 与 task-owned Session 必须使用冻结的 AgentTask binding。
所选 Endpoint operation 执行 `max_concurrency`；PAOS 不增加跨 Tool Resource/Control lease。

## 2. Tool 发现与 context

```text
GET /tools
GET /tools/{tool_id}
GET /tools/{tool_id}/context
```

ToolSpec 声明稳定 identity、implementation/Endpoint binding、operation、`query|action|session`
semantics、严格 input/output schema、readiness 与 robot frame profile。调用前实时读取 context，
调用方不能猜测 frame、unit、readiness 或 binding。

## 3. Query 契约

`forge_tool_query` 读取配置 ToolSpec，确认 `semantics=query`，再调用：

```text
POST /tools/{endpoint_id}/{operation}:invoke
Content-Type: application/json

{
  "arguments": {},
  "caller_id": "paos:<task-or-diagnostic-identity>",
  "timeout_ms": 10000
}
```

成功响应为 HTTP 200 和 `{ "ok": true, "data": { ... } }`。caller ID 由 PAOS 生成。绑定 Query 在 active
PlanRevision 下创建终态 PAOS ToolExecutionRecord；无任务 Query 返回相同 Gateway data，但不
进行任务归因。

## 4. Action 契约

Admission：

```text
POST /tools/{tool_id}:invoke
→ HTTP 202
→ data.invocation_id + data.attempt_id
```

Reconciliation：

```text
GET  /invocations/{invocation_id}
GET  /invocations/{invocation_id}/result
POST /invocations/{invocation_id}/cancel
```

Result HTTP 202 表示 pending。Cancel HTTP 200/202 表示取消请求已处理或接受，不证明停止。
Timeout 表示远端状态未知。显式 `unknown` 终态会以失败关闭 PAOS 记账，但物理效果仍不确定，
不能触发盲目重试。

PAOS 在 admission 前持久化 Action intent 与生成的 caller ID，并保留 Gateway 返回的每个
invocation/attempt identity。Timeout 或传输错误形成 `unknown` record；恢复只能读取已持久化
invocation ID，绝不盲目重复 POST。

## 5. Session 契约

`semantics=session` 的 Tool 通过同一 invoke route 接纳，返回 HTTP 202 与 invocation ID。
Status/result 使用通用 `/invocations/{id}` routes；有所有权的 Session 通过
`POST /invocations/{id}/stop` 停止。

PAOS 将 Session ownership 记为 `task`、`shared` 或 `runtime`。Task 只能停止自己拥有的
Session；shared Session 可跨 task finalize 存续；runtime-owned Session 由 Agent 外部管理。
非终态 task-owned Session 阻止 finalize，Runtime stop 也会计入全部活动 Session。

## 6. Agent tools

| Tool | 契约 |
|:-----|:-----|
| `forge_tool_context` | 读取 ToolSpec 与实时 context。 |
| `forge_tool_query` | 调用同步 Query，可作无任务诊断或绑定 `task_id`。 |
| `forge_tool_start_action` | 为绑定的 `task_id` 接纳异步 Action。 |
| `forge_tool_action_status` | 读取 invocation phase/status。 |
| `forge_tool_action_result` | 读取 pending 或终态 result。 |
| `forge_tool_cancel_action` | 请求取消，不宣称停止。 |
| `forge_tool_start_session` | 以显式 ownership 接纳绑定 Session。 |
| `forge_tool_session_status/result/stop_session` | 核对或停止有所有权的 Session。 |
| `forge_task_create` | 创建唯一活动 AgentTask 与 revision 1。 |
| `forge_task_get` | 读取 task、revisions、Tool records、evidence 与 verdict。 |
| `forge_task_begin_revision` | 在允许 recovery verdict 后追加 revision。 |
| `forge_task_finalize` | 后置采集并执行聚合任务验证。 |
| `forge_task_cancel` | 取消非终态 Action，并停止 task-owned Session。 |

无 Runtime 时仍提供诊断 context tool；受治理 tools 要求恰好一个健康活动 Skill Runtime。
现有通用 Agent tools 与动态 MCP tools 独立保持注册。

## 7. Binding、Identity 与关联

`activate_skill` 读取已安装工作流并预览当前 Runtime。创建 task 时重新校验 candidate，冻结
Skill 名称/版本、manifest 与工作流 hash、profile、Runtime instance、Gateway identity、所需
ToolSpec hash 与 Node artifact ID。每次执行都复核 allowlist、semantics、readiness、ToolSpec hash
和 Runtime identity。

| Identity | 所有者 | 含义 |
|:---------|:-------|:-----|
| `task_id` | PAOS | 稳定任务聚合 |
| `revision_id` | PAOS | 不可变规划世代 |
| `binding_id` | PAOS | 不可变 Skill/Runtime/ToolSpec 快照 |
| `record_id` | PAOS | 绑定 Query、Action 或 Session record |
| `caller_id` | PAOS | admission 前持久化的幂等/关联身份 |
| `invocation_id` | Gateway | 异步 Action 或 Session 生命周期 |
| `attempt_id` | Gateway | 执行 attempt |

关联必须显式保存；这些 ID 不是别名，也不能相互派生。

## 8. AgentTask 模型

全局最多一个非终态 AgentTask；诊断 Query 不占槽位。创建与更新使用 SQLite WAL 和 immediate
transaction。Task 包含只追加 PlanRevision；每个 revision 包含 Tool records、semantic verdict
和 verification attempts。

```text
executing
  ├─ finalize → succeeded | failed
  ├─ recovery verdict → awaiting_replan → begin_revision → executing
  └─ cancel → cancelling → reconcile → finalize → cancelled | failed
```

Tool record 终结后，后续 observation 不改写执行事实。Recovery revision 保持相同 task ID，
并受 replan count 与 deadline 限制。

## 9. Evidence 与 verification

PAOS 在第一次绑定 Action 前和所有绑定 Action 达到记账终态后进行 best-effort 采集。Evidence
artifact 包含 source、phase、sequence、timestamp、media metadata、size、SHA-256 与工作区相对
reference。采集错误会显式记录。Verifier 接收任务上下文前，会检查 bundle 身份、质量、采集时间
窗口、policy 要求以及保留 artifact 字节。

`forge_task_finalize` 聚合全部绑定 Tool facts，并应用任务契约：

- `off`：执行派生结果；
- `audit`：记录 semantic verdict，保留执行派生结果；
- `enforce`：semantic verdict 决定成功并 fail closed；
- `recovery`：enforce 语义加有预算 `replan_required`。

Forge ToolResult 与 events 对执行负责；PAOS verifier 只判断用户任务是否完成。
它的上下文包含冻结的 Skill binding、PlanRevision、ToolExecutionRecord、Gateway 终态结果、
before/after evidence 和作用域建议 Lesson。

## 10. Experience 与 evolution

终态 AgentTask 转换为唯一去敏 episode，引用冻结的 Skill binding 与版本、PlanRevision verdict、
ToolInvocation/attempt fingerprint 和 evidence，但不会把原始 output、凭据、endpoint 或物理参数
写入学习内容。

Lesson 与 failure cluster 按绑定 Skill 版本限定。Evolution fail-open，不改变 Gateway facts、
AgentTask terminal state 或 verification attempt。

## 11. Skill Runtime 与分发

Skill Runtime 安装并管理 manifest v2 Bundle。安装要求安全 contained path、有界解包、SHA-256
文件清单、严格 manifest、staging、原子替换与 rollback。每个 Node lock 固定 artifact ID、版本、
平台、架构、归档类型（`executable_tar_gz`：单个根目录 executable；`directory_tar_gz`：仅含一个
以 entrypoint 命名的根目录，内放可执行文件及其运行时树）、entrypoint 与 SHA-256。静态 index 下载条目包含 size 与 digest；
Registry Node 下载以已验证的 Skill lock 为摘要权威，并在进入 cache 前从 Registry 元数据或直接
下载端点解析精确大小。安装始终显式触发，默认还需确认。

RuntimeManager 要求 `PATH` 中存在 Dora CLI（v0.4.1 与 `dora-message` v0.7.0 是当前 Forge
Skill 兼容基线）。
它物化摘要覆盖 dataflow 路径与 profile 文件内容的环境，并将 `PAOS_SKILL_NAME` 与
`PAOS_SKILL_VERSION` 注入 Dora 进程环境及 dataflow 占位符。

启动时先持久化 `starting`；Bundle 含可选 `start.sh` 时，要求 Bash，并在 Dora 前以
`bash <bundle>/start.sh <name> <version>` 执行且继承终端 stdio。钩子成功后，RuntimeManager
需要时启动本地 Dora 服务，再启动命名 profile，等待 Gateway `/tools` 与 manifest 全部
required Tool context，并持久化 status/log。健康活动 Runtime 提供 Skill availability；其
manifest 是 Gateway URL 的唯一来源。按 Skill 的跨进程锁拒绝重叠生命周期变更。

存在被追踪非终态 invocation、Session 或 task binding 时，正常 stop 会被拒绝。Force stop
记录 audit event，且不改变执行事实。

PhyAgentOS 源码与 Python 分发不包含具体 Forge Skill、Node、模型或仿真资源。它们仅在本地测试
或部署时独立获取。打包工具生成确定性 Skill 归档，安装器在提交前完成验证。

## 12. Conformance

接入需要覆盖 Tool discovery/context、Query response、Action/Session admission、pending/terminal
result、cancel/stop、timeout/unknown、无 POST 重试恢复、endpoint concurrency、不可变 AgentTask
binding/revisions、ownership、evidence、聚合 verification、按版本 experience、Bundle security、
事务安装、Runtime health、安全切换与 availability 传播。

Mock Gateway 测试可完成代码与契约验收；硬件/MuJoCo 验收单独记录确切 artifact digests 与环境。

## 相关文档

- [框架介绍](../zh/01-framework-introduction.md)
- [配置参考](../zh/04-forge-configuration-reference.md)
- [集成开发指南](../user_development_guide/README.md)
- [运行手册](../user_manual/README.md)

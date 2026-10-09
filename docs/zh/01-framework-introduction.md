# PhyAgentOS 框架介绍

[English](../en/01-framework-introduction.md) · [文档索引](../README.md)

> 文档版本：1.0.0 · 实现基线：2026-08-30 源码、配置 Schema 与测试。

## 1. 项目定位

PhyAgentOS 是面向具身任务的 Agent 框架。Agent 理解用户目标、选择 Forge Tool、定义任务级
成功标准，并决定继续还是恢复；Forge Gateway 负责 Tool 执行、ToolEndpoint 选择、Dora
集成以及机器人或仿真器访问。

该边界把认知规划与物理效果分离。通用 Agent tools、verification、任务经验、evolution、
Skill activation 和动态 MCP 工具继续属于 Agent 平台；机器人动作统一使用 Forge
Query/Action/Session Tool API。

## 2. 唯一物理执行面

```text
用户 / 消息渠道 / 定时事件
              │
              ▼
      AgentLoop + Planner
              │  绑定 AgentTask 或诊断 Query
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
```

绑定调用和诊断 Query 经过相同 Gateway endpoint；诊断 Query 不占 AgentTask 槽位，Action 与
task-owned Session 必须使用冻结的 task binding。PAOS 不增加跨 Tool 资源租约，由所选 endpoint
operation 的 `max_concurrency` 决定接纳结果。

## 3. 三类事实

| 事实 | 责任方 | 回答的问题 |
|:-----|:-------|:-----------|
| Execution | Forge Gateway | 哪个 Query 已完成，或哪个 Action/Session invocation 被接纳并如何终结？ |
| Evidence | PAOS observation collector | Task-owned 物理执行前后观察到了什么？ |
| Verdict | PAOS verification | 全部绑定调用是否满足用户目标、criteria 与 constraints？ |

Action/Session admission 不是完成。取消或停止被接受、本地超时或 `unknown` 结果都不能证明物理执行已经
停止，也不能作为盲目重试的依据。

## 4. 身份模型

以下标识有意保持不同：

- `task_id`：一个用户可见目标对应的 AgentTask 聚合；
- `binding_id`：一份不可变 Skill/Runtime/ToolSpec 快照；
- `revision_id`：任务内一次不可变规划世代；
- Query/execution `record_id`：挂在 revision 下的一条 PAOS record；
- `caller_id`：异步 admission 前持久化的 PAOS 生成身份；
- `invocation_id`：Gateway 拥有的异步 Action 或 Session invocation；
- `attempt_id`：Gateway 的一次执行尝试。

这些 ID 不能互相复制或视为别名。Forge 对 Tool 执行事实负责；AgentTask 只保存引用和任务级
解释。

## 5. AgentTask 生命周期

全局最多一个非终态 AgentTask。任务从 revision 1 开始，可包含多个绑定 Query、Action 与 Session。
PlanRevision 只追加，既有 execution record 和 verification attempt 不会被重写。

```text
executing ── finalize success ──► succeeded
    │
    ├── finalize failure ───────► failed
    ├── recovery verdict ───────► awaiting_replan ── begin revision ──► executing
    └── cancel request ─────────► cancelling ── reconcile + finalize ─► cancelled/failed
```

Task tools 负责创建、读取、修订、finalize 与取消聚合；Tool API tools 读取 Tool context、调用
Query/Action/Session、核对异步 status/result，并请求 cancel/stop。

## 6. 验证与恢复

验证模式为 `off`、`audit`、`enforce` 和 `recovery`。

- `off` 根据绑定执行事实派生成功，不调用语义验证；
- `audit` 记录 verdict，但保留执行派生终态语义；
- `enforce` 要求合法任务契约，遇到缺证、非法输出、服务错误或 inconclusive 时 fail closed；
- `recovery` 同样 fail closed，并允许返回 `replan_required`。

恢复在相同 `task_id` 上追加有预算的 PlanRevision。Planner 接收 unmet criteria、保留约束、
guidance、evidence references 和 deadline，再重新选择 Tool。未知 Action 效果只能按已持久化
invocation ID 核实，PAOS 不会重复未知 admission POST。

## 7. Evidence

PAOS 通过 Gateway WebSocket 采集配置的图像源和可选 robot state，使用有界最新帧缓存、媒体与
大小校验、SHA-256、source sequence 边界和工作区相对 artifact 引用。采集是 best-effort；
Forge ToolResult 与事件仍是权威执行事实。

第一次绑定物理执行前只执行一次前置采集；全部 task-owned 执行终结后，finalize 才进行后置采集与
聚合验证。仅含 Query 的任务可以保存 Tool facts，但不会伪造 Action capture window。

## 8. Skill Runtime

Skill Runtime 管理 manifest v2 Bundle 和显式命名 Dora profile。Bundle 使用安全解包、
SHA-256 清单、精确单 entrypoint Node lock、事务替换、持久化状态、生命周期日志以及 Gateway `/tools`
健康检查。

Skill 发现优先级为 workspace override、已安装 Skill、内置 Skill。健康活动 Runtime 会把
Skill availability 传给 `SkillsLoader`、activation、experience 和 evolution；活动 manifest
是 Gateway URL 的唯一来源。

Registry 下载使用 `resourceRegistry.url`、`PAOS_RESOURCE_REGISTRY_URL` 或显式静态 index，
只有显式 CLI 命令和确认后才会发生。PhyAgentOS 分发不包含具体 Forge Skill、Node、模型或
仿真资源。

## 9. Experience 与 evolution

ExperienceCoordinator 记录全部 Agent tool calls，并把显式 Skill activation、AgentTask、
AgentTask 冻结的 binding/版本、PlanRevision verdict、ToolInvocation 引用、verification attempt
与 evidence reference 归入一条去敏 task episode。

语义成功可支持受控 Skill candidate；与工作流相关的语义失败可形成规范化 observation 与
作用域 Lesson cluster。基础设施、证据、Verifier、任务不可满足和不确定失败只保留诊断。
Evolution fail-open，不能改变执行或验证结果。

## 10. 持久化

```text
<workspace>/
├── .paos/agent_tasks/tasks.sqlite3
├── .paos/evolution/experience.sqlite3
├── .paos/evolution/revisions/<skill>/
├── skills/<skill>/SKILL.md
└── artifacts/agent_tasks/<task_id>/
    ├── before_snapshot.json
    ├── after_snapshot.json
    ├── evidence_bundle.json
    └── evidence/
```

Runtime 安装与生命周期状态位于 PAOS 配置的数据路径中，与 AgentTask、evolution 持久化分离。
Runtime 清理不会删除现有 evolution 数据。

## 11. 代码地图

| 领域 | 路径 |
|:-----|:-----|
| Agent Loop 与通用工具 | `PhyAgentOS/agent/` |
| Tool API client、binding 与 AgentTask | `PhyAgentOS/forge/tool_client.py`、`PhyAgentOS/forge/binding.py`、`PhyAgentOS/forge/task.py` |
| Agent Forge tools | `PhyAgentOS/agent/tools/forge_tool_api.py`、`forge_task.py` |
| Skill Runtime | `PhyAgentOS/skill_runtime/` |
| 内置 Agent 工作流 Skills | `PhyAgentOS/skills/` |
| Verification | `PhyAgentOS/verification/`、`PhyAgentOS/agent/session_verifier.py` |
| Experience 与 evolution | `PhyAgentOS/agent/experience/` |

## 12. 当前实现范围

当前 Runtime 通过统一 Tool API 支持 Query、Action 与 Session。跨 Tool 资源租约、Registry
隐式下载和内置具体机器人/仿真制品不在实现契约内。

## 后续阅读

- [用户手册](02-user-manual.md)
- [开发者手册](03-developer-manual.md)
- [Forge Tool API 接入契约](../forge/README_zh.md)
- [Agent 经验与 Skill 自进化](05-agent-experience-and-skill-evolution.md)

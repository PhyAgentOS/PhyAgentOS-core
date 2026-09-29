# Unified Forge Tool API Integration

> Applies to PhyAgentOS 1.0.0. See [Forge Tool API Integration Contract](README.md) for the full
> operational and development contract.

## One physical execution plane

```text
AgentTask-bound call / diagnostic Query
        → ForgeToolClient
        → Gateway /tools → ToolInvocation → ToolEndpoint
        → Dora and robot nodes
```

PAOS supports Query, Action, and Session semantics only through `/tools` and `/invocations`; it
does not call legacy `/agent/sessions` or `/policy/command` routes and does not add a cross-Tool
resource lease. The Gateway routes to the selected Endpoint operation and enforces its
`max_concurrency`.

## Agent tools

- Task lifecycle: `forge_task_create`, `forge_task_get`, `forge_task_begin_revision`,
  `forge_task_finalize`, and `forge_task_cancel`.
- Tool API: `forge_tool_context`, `forge_tool_query`, `forge_tool_start_action`,
  `forge_tool_action_status`, `forge_tool_action_result`, `forge_tool_cancel_action`,
  `forge_tool_start_session`, `forge_tool_session_status`, `forge_tool_session_result`, and
  `forge_tool_stop_session`.

`forge_tool_query` may be diagnostic without a task. Action and task-owned Session calls require a
task whose binding freezes the activated Skill version, Runtime identity, workflow/manifest hashes,
and live ToolSpec hashes. Bound calls are aggregated for PAOS task verification.

Action admission is not completion. `cancel_status=requested|accepted`, a local timeout, or an
`unknown` terminal outcome never proves that the physical effect stopped and must not trigger a
blind retry.

## AgentTask and verification

An AgentTask stores append-only PlanRevisions, Query records, Action/Session invocation references,
evidence, and verification attempts. The following identities are deliberately different:

- PAOS `task_id`;
- PAOS `revision_id`;
- PAOS Query/execution record ID;
- PAOS-generated and pre-admission-persisted `caller_id`;
- Gateway `invocation_id`;
- Gateway `attempt_id`.

Forge owns each Tool execution fact. PAOS captures best-effort evidence before the first bound
Action and after all task-owned executions are terminal, then `forge_task_finalize` applies the existing
`off`, `audit`, `enforce`, or `recovery` task semantics. A recoverable verdict appends a bounded
PlanRevision to the same task; it does not create another execution plane.

## Skill Runtime

`paos skill` discovers and installs manifest-v2 bundles, verifies SHA-256 inventories, and manages
an explicit named Dora profile. Installation uses safe extraction and atomic replacement. Each Node
lock fixes platform, architecture, archive type (`executable_tar_gz` or `directory_tar_gz`), the
entrypoint, and archive SHA-256; the installer also
records the extracted binary hash. Downloads require
either `resourceRegistry.url`, `PAOS_RESOURCE_REGISTRY_URL`, or an explicit static index.

When one healthy Skill Runtime is active, its manifest provides the only Gateway URL and its Skill
becomes available to the Agent loader and activation/evolution pipeline. PhyAgentOS distributions
contain no concrete Forge Skill, Node, model, or simulation asset.

---

# Forge Tool API 统一接入

当前 PAOS 只有一条物理执行链：绑定 AgentTask 的调用与无任务调用都经过
`ForgeToolClient → Gateway /tools → ToolInvocation → ToolEndpoint → Dora/机器人节点`。
PAOS 只通过 `/tools` 与 `/invocations` 支持 Query、Action、Session，不调用旧式
`/agent/sessions`、`/policy/command`，也不新增跨 Tool 资源租约；并发由 Endpoint operation
的 `max_concurrency` 裁决。

AgentTask 负责聚合 PlanRevision、Query record、Action/Session invocation 引用、证据与验证结论，
但不执行机器人。Action 接受、取消接受、timeout 或 `unknown` 都不能解释为物理动作已经
停止，也不能触发盲目重试。恢复验证在同一个 `task_id` 上追加有预算和 deadline 的
PlanRevision。

Action 与 task-owned Session 必须绑定本轮显式激活的 Skill；binding 冻结 Skill 版本、Runtime
identity、manifest/工作流 hash 与 ToolSpec hash。PAOS 在 admission 前持久化自己生成的
caller ID；未知结果只能按已知 invocation ID 查询，不能盲目重复 POST。

Skill Runtime 使用 manifest v2、SHA-256 清单、安全解包、原子安装和持久化状态。活动 Runtime
manifest 是 Gateway URL 的唯一来源。PhyAgentOS 分发不包含具体 Forge Skill、Node、模型或
仿真资源；需要时由部署者显式安装。

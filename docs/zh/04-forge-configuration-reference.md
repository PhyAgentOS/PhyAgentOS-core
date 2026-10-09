# Forge 配置参考

> 适用于 PhyAgentOS 1.0.0 与统一的 Forge Gateway Tool API。

## 1. 配置位置与命名

默认配置为 `~/.PhyAgentOS/config.json`。`paos onboard` 创建或刷新该文件。`paos agent` 与 `paos gateway` 可通过 `--config` 和 `--workspace` 覆盖当前实例路径。

Pydantic 模型接受 camelCase 和 snake_case；`paos onboard` 保存为 camelCase。根级 `runtime` 字段被明确拒绝：

```text
legacy `runtime` configuration is unsupported; remove it and configure `forge`
```

旧 Forge 执行选择字段 `enabled`、`baseUrl`、`apiVersion`（含 snake_case 形式）也会被拒绝。
Runtime 选择与 Gateway URL 来自已安装 Skill manifest 和显式启动的 profile。

## 2. `forge`

| JSON 字段 | 类型 | 默认值 | 约束与含义 |
|:----------|:-----|:-------|:-----------|
| `requestTimeoutS` | number | `10.0` | HTTP 请求 timeout，必须大于 0。 |
| `pollIntervalS` | number | `0.5` | Action/Session status/result 建议对账间隔，范围 `[0.1, 5.0]` 秒。 |
| `executionTimeoutS` | number | `300.0` | Agent 侧任务 deadline 指引；timeout 不能证明远端执行已停止。 |
| `evidence` | object | 见下表 | AgentTask 的 best-effort 前后证据采集设置。 |

PAOS 只通过 `/tools` 与 `/invocations` 调用 Query、Action 和 Session，不调用旧式
`/agent/sessions` 或 `/policy/command`。并发由所选 Endpoint operation 的 `max_concurrency` 裁决。

## 2.1 `resourceRegistry`

| JSON 字段 | 类型 | 默认值 | 约束与含义 |
|:----------|:-----|:-------|:-----------|
| `url` | string | `https://paos-resource-manager.dev.x-era.com` | HTTP(S) Resource Registry；`PAOS_RESOURCE_REGISTRY_URL` 优先。空值只允许本地 Bundle 或显式静态 index。 |

只有显式执行 `paos skill search/install/update` 或 `paos forge-node install` 才会访问 Registry；
启动 PAOS 不会下载 Skill。

## 3. `forge.evidence`

| JSON 字段 | 类型 | 默认值 | 约束与含义 |
|:----------|:-----|:-------|:-----------|
| `requiredImageSources` | string[] | `[]` | 全局必需图像 source。task policy 非空时优先使用 task sources；二者都空时从 runtime context readiness 发现。 |
| `captureTimeoutS` | number | `5.0` | POST 前等待 before snapshot 的上限，必须大于 0。 |
| `postCaptureTimeoutS` | number | `5.0` | 观察 Gateway terminal 后等待新 sequence 的上限，必须大于 0。 |
| `connectionTimeoutS` | number | `2.0` | 每次 WebSocket connect timeout，必须大于 0。 |
| `maxArtifactBytes` | integer | `8388608` | 单个 image/state message 的最大实体大小，必须大于 0。 |
| `associationQuality` | literal | `best_effort` | 当前 PAOS observation association 的实现质量。 |

Source 解析优先级：

```text
task.verification.evidence_policy.required_sources（非空）
    > forge.evidence.requiredImageSources
```

## 4. `agents.verification`

| JSON 字段 | 类型 | 默认值 | 约束与含义 |
|:----------|:-----|:-------|:-----------|
| `serviceEnabled` | boolean | `true` | 是否创建独立 Verification Service。非 `off` task 要求为 true 且服务可用。 |
| `model` | string/null | `null` | null 时使用 `agents.defaults.model`。 |
| `provider` | string/null | `null` | null 时按 verifier model 自动匹配 Provider。显式值必须存在于 providers。 |
| `timeoutS` | number | `180.0` | 单次模型验证 timeout，必须大于 0。 |
| `evidenceRetention` | enum | `none` | `all | failed | none`。 |
| `maxReplansPerEpisode` | integer | `2` | 一个 AgentTask 最多追加的 PlanRevision 数，必须大于等于 0。 |
| `maxVerifierCallsPerRun` | integer | `50` | 当前 PAOS 进程 verifier call budget；0 表示代码层不施加该 budget。 |
| `replanTimeoutS` | number | `120.0` | 开始请求的 PlanRevision 的 deadline，必须大于 0。 |
| `serviceHost` | string | `127.0.0.1` | 子进程 HTTP 服务 bind host。 |
| `servicePort` | integer | `8100` | 范围 `1..65535`；同机多实例应使用不同端口。 |

Verification Service 启动 readiness 等待为有界操作。服务不可用时拒绝新建非 `off` AgentTask。

## 5. `agents.evolution`

| JSON 字段 | 类型 | 默认值 | 约束与含义 |
|:----------|:-----|:-------|:-----------|
| `enabled` | boolean | `true` | 启用显式 Skill 激活、经验账本和后台演化。故障不会阻塞任务执行。 |
| `scope` | literal | `verified_forge_lineage` | 消费有 semantic verdict 的 AgentTask lineage；持久化 literal 为兼容保持稳定。 |
| `promotionMode` | literal | `guarded_auto` | 只允许经过门控验证的自动晋升。 |
| `minSuccessfulEpisodes` | integer | `3` | 相同候选晋升所需的独立成功 AgentTask 数，至少为 1。 |
| `minLessonEpisodes` | integer | `3` | 聚类 Lesson 激活前所需的独立、工作流相关失败 AgentTask 数，至少为 1。 |
| `maxLessonsPerSkill` | integer | `8` | `activate_skill` 单次返回的 scoped lesson 上限，范围 `1..50`。 |
| `maxEvolutionCallsPerRun` | integer | `20` | 独立于 verifier 的后台反思调用预算；0 表示代码层不限制。 |
| `model` | string/null | `null` | null 时继承 verification model，再回退到 Agent 默认模型。 |
| `provider` | string/null | `null` | null 时继承 verification provider，再按 model 自动匹配。 |

启用后，根目录旧 `LESSONS.md` 保留，但不再全局注入，也不进入 Forge 验证。Skill 相关 lesson 按需从经验账本加载，并投影到 `skills/<name>/references/LESSONS.md`。显式 Skill 激活返回的适用 active 集合会随 AgentTask 冻结，只作为非权威建议提供给自动验证、后续 PlanRevision 与 review；它不能确定 criterion 或替代证据，没有激活 Skill 的任务不提供学习型 Lesson。与工作流无关的失败仅记录诊断，相关失败经过归一化和聚类后才能激活。门槛按不同 AgentTask 计数，不把 PlanRevision、review、重复 event 或 replay 作为独立支持。

Evolution model/provider 独立于 verifier call budget 解析：

```text
agents.evolution.model
  → agents.verification.model
  → agents.defaults.model

agents.evolution.provider
  → agents.verification.provider
  → 按选定 model 推断 provider
```

`enabled=false` 会关闭 episode 反思与晋升。`activate_skill` 仍作为显式加载工作流及 Forge binding
的入口；已有经验数据与 Skill sidecar 不会被修改或删除。

## 6. AgentTask 与 Tool API tools

`forge_task_create` 接收 `task_description` 和下述验证契约，返回 PAOS `task_id` 与首个不可变
PlanRevision 和冻结的 primary Skill binding。创建 task 必须传入本轮 primary `activate_skill`
ID，并重新校验其 Runtime candidate。`forge_tool_query` 可不带 task 作诊断；Action 与 task-owned
Session 必须带 `task_id`，并计入任务验证。

任务生命周期工具为 `forge_task_create`、`forge_task_get`、`forge_task_begin_revision`、
`forge_task_finalize`、`forge_task_cancel`。Tool 传输工具为 `forge_tool_context`、
`forge_tool_query`、`forge_tool_start_action`、`forge_tool_action_status`、
`forge_tool_action_result`、`forge_tool_cancel_action`、`forge_tool_start_session`、
`forge_tool_session_status`、`forge_tool_session_result`、`forge_tool_stop_session`。

`binding_id`、`task_id`、`revision_id`、execution record ID、PAOS `caller_id`、Gateway
`invocation_id` 与 `attempt_id` 严格区分。
全局只允许一个非终态 AgentTask；恢复时在同一 task 上追加 revision，并受
`maxReplansPerEpisode` 与 `replanTimeoutS` 限制。

## 7. `TaskVerificationContract`

| 字段 | 类型 | 默认值 | 说明 |
|:-----|:-----|:-------|:-----|
| `mode` | enum | `off` | `off | audit | enforce | recovery`。 |
| `goal` | string | `""` | 非 `off` 必填；会 trim。 |
| `success_criteria` | string[] | `[]` | 非 `off` 至少一项；每项非空。 |
| `constraints` | string[] | `[]` | 需在验证与 recovery 中保留的限制；每项非空。 |
| `evidence_policy` | object | 默认 semantic policy | 证据要求。 |

### `evidence_policy`

| 字段 | 类型 | 默认值 | 说明 |
|:-----|:-----|:-------|:-----|
| `profile` | string | `semantic_default` | 通用 profile 标签；当前不触发 action-specific 代码。 |
| `required_kinds` | string[] | `["rgb_image"]` | before 与 after 都必须存在每种 kind。`robot_state` 会要求 `/ws/state`。 |
| `required_sources` | string[] | `[]` | 对 image kind，before/after 均需包含每个 source。 |
| `minimum_association` | enum | `best_effort` | `best_effort | authoritative`；当前 PAOS 采集提供 best-effort evidence。 |

## 8. Mode 行为矩阵

| 情况 | `off` | `audit` | `enforce` | `recovery` |
|:-----|:------|:--------|:----------|:-----------|
| 需要 goal/criteria | 否 | 是 | 是 | 是 |
| 绑定 Action 的 best-effort Evidence Bundle | 是 | 是 | 是 | 是 |
| before 缺失是否阻止 Tool API Action | 否 | 否 | 否 | 否 |
| Verifier error | 不适用 | 记录，保留 execution 终态 | failed | failed |
| `inconclusive` | 不适用 | 记录，保留 execution 终态 | failed | failed |
| `replan_required` | 不适用 | 不恢复 | failed | `awaiting_replan` |

## 8.1 Skill Runtime 控制

Skill Runtime 路径由 PAOS 数据路径 helper 管理，不增加额外配置字段。使用
`paos skill search/install/update/remove/list/inspect/start/status/switch/logs/stop` 管理 Bundle 与
Runtime 生命周期，使用 `paos forge-node install/verify <skill-name> <node-id>` 管理独立锁定
Node；通过 `--archive <path>` 可安装独立获取的 Node 而不访问 Registry。启动 profile 要求
`PATH` 中存在 Dora CLI（v0.4.1 与 `dora-message` v0.7.0 是当前 Forge Skill 兼容基线），并
校验 required binaries、assets、环境变量、Gateway `/tools`，以及本次启动 profile 解析出的
Tool 列表：存在 `profiles.<name>.required_tools` 时用它，否则用 manifest 级 `required_tools`。
同一份列表也是 AgentTask 的 Skill allowlist，所以健康检查与任务绑定不会对同一 profile 暴露的
Tool 面产生分歧。RuntimeManager 需要时启动本地 Dora 服务。活动 Runtime manifest 的
`gateway_url` 是 Agent 使用的 Tool API URL。

## 9. `embodiments`

Embodiment 只配置知识拓扑，不选择执行 adapter：

| 字段 | 默认值 | 说明 |
|:-----|:-------|:-----|
| `mode` | `single` | `single | fleet`。 |
| `sharedWorkspace` | `~/.PhyAgentOS/workspaces/shared` | fleet 的 Agent shared workspace。 |
| `instances` | `[]` | 机器人知识 profile 列表。 |

Instance 字段：`robotId`、`workspace` 必填；`enabled=true`；`profileName` 与 `sharedEnvironment` 可选。额外字段被拒绝，因此旧 `driver` 字段必须删除。

## 10. 推荐配置

### 10.1 只验证执行链

```json
{
  "forge": {
    "requestTimeoutS": 10,
    "executionTimeoutS": 300
  },
  "agents": {
    "verification": {
      "serviceEnabled": false
    },
    "evolution": {
      "enabled": false
    }
  }
}
```

此配置只允许 `verification.mode=off` 的任务；仍需显式启动已安装 Skill Runtime。

### 10.2 长期运行的验证配置

```json
{
  "agents": {
    "verification": {
      "serviceEnabled": true,
      "model": "openrouter/openai/gpt-4o-mini",
      "provider": "openrouter",
      "timeoutS": 180,
      "evidenceRetention": "failed",
      "maxReplansPerEpisode": 2,
      "maxVerifierCallsPerRun": 50,
      "replanTimeoutS": 120,
      "serviceHost": "127.0.0.1",
      "servicePort": 8100
    },
    "evolution": {
      "enabled": true,
      "scope": "verified_forge_lineage",
      "promotionMode": "guarded_auto",
      "minSuccessfulEpisodes": 3,
      "minLessonEpisodes": 3,
      "maxLessonsPerSkill": 8,
      "maxEvolutionCallsPerRun": 20,
      "model": null,
      "provider": null
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

## 11. 配置检查

```bash
paos status
paos agent -m "调用 forge_tool_context 读取指定 Tool 的 schema、binding、readiness 与 frame profile，不执行 Action。"
```

`paos status` 检查本地 config、workspace、model 和 Provider；它不代替 `forge_tool_context` 的实时 Tool 检查。

## 后续阅读

- [用户手册](02-user-manual.md)
- [开发者手册](03-developer-manual.md)
- [Agent 经验与 Skill 自进化](05-agent-experience-and-skill-evolution.md)
- [运行手册](../user_manual/README.md)
- [统一 Forge Tool API 契约](../forge/UNIFIED_TOOL_API.md)

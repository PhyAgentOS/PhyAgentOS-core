# PhyAgentOS 开发者手册

[English](../en/03-developer-manual.md) · [文档索引](../README.md)

> 文档版本：1.0.0。

## 1. 开发不变量

1. 机器人执行只有一条物理路径：`ForgeToolClient → Gateway Tool API → ToolEndpoint`；
2. AgentTask 聚合规划、证据和判定，但不执行机器人；
3. `binding_id`、`task_id`、`revision_id`、record ID、`caller_id`、`invocation_id` 与 `attempt_id` 相互独立；
4. Forge ToolResult 与 invocation events 是权威执行事实；
5. Action/Session admission、cancel/stop 接受、timeout 和 `unknown` 都不能证明物理停止；
6. 通用 Agent tools、verification、experience、evolution 与动态 MCP 保持独立；
7. Runtime 制品必须通过有界归档与摘要校验后才能安装。

## 2. 模块地图

| 模块 | 职责 |
|:-----|:-----|
| `agent/loop.py` | 现有 Agent Loop、通用工具、动态 MCP、Forge tools 注册、上下文与生命周期 |
| `agent/tools/forge_tool_api.py` | 受治理的 Query/Action/Session Tool API Agent wrapper |
| `agent/tools/forge_task.py` | 五个 AgentTask 生命周期工具 |
| `forge/tool_client.py` | 严格异步 HTTP client 与响应校验 |
| `forge/binding.py`、`forge/task.py` | 不可变 Runtime/ToolSpec binding、AgentTask model/store、证据、验证与恢复 |
| `forge/observation.py`、`forge/evidence.py` | best-effort 图像/state 采集与 artifact 写入 |
| `skill_runtime/` | manifest、catalog、安全归档、installer、Registry、state、Runtime manager 与 availability |
| `agent/experience/` | activation、episode、assessment、Lesson、Skill candidate 与 evolution |
| `verification/` | 公共任务、证据、请求、verdict 契约与 Verification Service |

## 3. Forge Tool API client

`ForgeToolClient` 只接受 `ok=true` 且 `data` 为 object 的 JSON envelope。错误保留 HTTP status、
error code、retryability 以及响应中已有的 invocation identity。

| 操作 | HTTP 契约 |
|:-----|:----------|
| 列出 Tools | `GET /tools` → 200 |
| 读取 ToolSpec | `GET /tools/{tool_id}` → 200 |
| 读取 context | `GET /tools/{tool_id}/context` → 200 |
| 调用 Query | 解析 ToolSpec 后 `POST /tools/{endpoint_id}/{operation}:invoke` → 200 |
| 接纳 Action | `POST /tools/{tool_id}:invoke` → 202 |
| Action status | `GET /invocations/{invocation_id}` → 200 |
| Action result | `GET /invocations/{invocation_id}/result` → 200 或 pending 202 |
| 请求 cancel | `POST /invocations/{invocation_id}/cancel` → 200 或 accepted 202 |
| 接纳 Session | `POST /tools/{tool_id}:invoke` → 202 |
| 停止 Session | `POST /invocations/{invocation_id}/stop` → 200 或 accepted 202 |

路径组件经过 percent encoding。PAOS 生成 `caller_id`，并在 Action/Session admission 前与
intent 一同持久化。Admission 必须包含非空 `invocation_id`，Action 还要求 `attempt_id`。
Timeout 形成 unknown record，恢复不会重复 POST。

## 4. Agent tools

Task lifecycle：

- `forge_task_create(task_description, verification, activation_id)`；
- `forge_task_get(task_id)`；
- `forge_task_begin_revision(task_id, reason)`；
- `forge_task_finalize(task_id)`；
- `forge_task_cancel(task_id, reason?)`。

Tool transport：

- `forge_tool_context(tool_id)`；
- `forge_tool_query(tool_id, arguments, task_id?, timeout_ms?)`；
- `forge_tool_start_action(task_id, tool_id, arguments, timeout_ms?)`；
- `forge_tool_action_status(task_id, invocation_id)`；
- `forge_tool_action_result(task_id, invocation_id)`；
- `forge_tool_cancel_action(task_id, invocation_id)`；
- `forge_tool_start_session(task_id, tool_id, arguments, ownership)`；
- `forge_tool_session_status/result/stop_session(task_id, invocation_id)`。

诊断 Query 与绑定调用使用相同 HTTP 方法；每个变更或任务归因 wrapper 都在网络访问前校验
ownership 与冻结 binding。Tool wrapper 不能自行生成 Gateway identity 或 result。

## 5. AgentTask 契约与事务

`AgentTaskRecord` 包含稳定 ID、任务描述、`TaskVerificationContract`、状态、只追加
PlanRevision、evidence refs、verification attempts、取消状态和时间戳。每个 PlanRevision 都有
自己的 Tool records、verdict 和 verification attempts。

`AgentTaskStore` 使用 SQLite WAL 与 `BEGIN IMMEDIATE`。创建时在同一事务内查询非终态任务，
保证跨进程全局单活动槽位。更新写入完整的已校验 record 和 append-only event；业务代码不直接
修改表。

终态任务状态为 `succeeded`、`failed`、`cancelled`；非终态为 `executing`、`cancelling`、
`awaiting_replan`。Tool status `unknown` 对聚合记账是终态，但它表示失败，不表示已停止。

## 6. 绑定执行生命周期

1. 激活 primary Skill，把其 Runtime/ToolSpec candidate 冻结到 AgentTask revision 1；
2. 第一次绑定物理执行前进行 best-effort 前置采集；
3. 通过 ForgeToolClient 调用 Query 或接纳 Action/Session；
4. Admission 前持久化 caller intent，再保留 Gateway invocation/attempt 引用；
5. 仅根据权威 status/result 更新异步 record；
6. 全部 task-owned 执行终结后，finalize 进行后置采集；
7. 聚合 Tool records、evidence 与任务契约进行验证；
8. 持久化 task/revision verdict，并调度唯一终态 experience episode。

record 一旦终结，后续 observation 不会重写它。Cancellation response 会保存，但
`requested` 或 `accepted` 仍使任务保持 `cancelling`，直到核对并显式 finalize。

## 7. Verification 与 recovery

`TaskVerificationContract` 继续作为用户级公共契约。Verifier 接收 goal、criteria、
constraints、冻结的 Skill binding、PlanRevision、ToolExecutionRecord、Gateway 终态结果、
before/after evidence、任务历史与冻结的 Skill 作用域建议 Lesson。

在 recovery 模式下，合法 `replan_required` verdict 将任务置为 `awaiting_replan` 并设置
deadline。`begin_revision` 检查相同 `task_id`、replan budget、deadline 和任务状态，然后追加
revision。历史 attempts 对 experience analysis 保持可见。Verifier exception 会持久化为失败
attempt；audit 保留执行语义，enforce/recovery 则失败。

## 8. Evidence 与 retention

Evidence 路径相对工作区并原子写入。进入语义验证前，先检查 bundle 身份、关联质量、
完整性、采集时间窗口以及必需的 kind 和 source；再检查保留 artifact 的路径边界、字节大小、
SHA-256、media type 以及可适用的结构化 JSON。Evidence bundle 记录采集质量与错误，不把
best-effort 采集包装成权威事实。

Retention 可以按策略移除实体字节，但必须保留 task record、execution references、bundle
metadata 和审计所需 tombstone。

## 9. Skill Runtime 契约

`skill.yaml` 必须使用 `manifest_version: 2`，包含目录安全的 name/version、相对 Skill
document、HTTP(S) `gateway_url`、非空 required Tools、至少一个 profile，并拒绝未知字段。
Registry resolver 的 Node 必须包含 artifact identity、version、platform、architecture、
archive type（`executable_tar_gz` 或 `directory_tar_gz`）、entrypoint 与 SHA-256。
`executable_tar_gz` 归档只含一个由 entrypoint 命名的根目录 executable；`directory_tar_gz`
归档只含一个以 entrypoint 命名的根目录，内放同名 executable 及其运行时树（允许树内相对
符号链接）。

归档校验拒绝绝对/穿越路径、links（上述节点树内相对链接除外）、重复或冲突路径、超大文件、展开限制违规、清单缺项与摘要
不一致。Skill/Node installer 先 staging 与校验，再原子替换目标，并支持 rollback。

RuntimeManager：

1. 解析已安装 Skill 与 profile；
2. 物化锁定环境，不修改已安装 Node；摘要覆盖精确 dataflow 路径及其同目录全部普通文件的
   SHA-256；
3. 检查 Dora CLI、dataflow、必需文件和环境；
4. 拒绝接管已占用地址的非托管 Gateway；
5. 持久化 `starting`，并以 `bash <bundle>/start.sh <name> <version>` 执行可选 Bundle
   启动钩子，继承终端 stdio；
6. 检查本地 Dora 服务，需要时执行 `dora up`，再启动命名 flow；
7. 等待 flow、`GET /tools` 与所有 required Tool contexts；
8. 持久化 running/failed/stopped state 与生命周期日志。

dataflow 渲染会解析 `FORGE_RUNTIME_BIN`、`PAOS_SKILL_ROOT`、`PAOS_SKILL_NAME` 与
`PAOS_SKILL_VERSION`，两个 Skill identity 值也会注入 Dora 进程环境。start、stop、Skill
install/update 提交与 remove 共享按 Skill 的非阻塞跨进程锁。锁能证明启动仍在执行时，status
保留 `starting`；无锁的陈旧 `starting` 仍按原规则核对。

当前 Forge Skill 兼容基线为 Dora CLI v0.4.1 与 `dora-message` v0.7.0。RuntimeManager 要求
兼容的命令行为，但不强制精确语义版本；运维人员必须避免复用协议不匹配的 coordinator/daemon。
Dora 是主机 Runtime 前置条件，不是 Python dependency，也不属于 Skill Bundle。

存在被追踪的非终态 invocation、Session 或 task binding 时，正常 stop 会被拒绝。Force stop
记录 audit event，不改变 invocation truth。

## 10. Registry 与 availability

artifact 进入 cache 前必须具备 expected size 与 SHA-256。静态 index 直接提供两者；Registry
Node 可以省略重复的 digest 与 size 字段，此时已验证 Skill lock 提供 expected digest，客户端从
Registry 元数据或直接下载端点解析 size。Registry 一旦返回 digest，就必须与 lock 一致。断点续传
内容在安装前再次校验。Registry URL 为空时只允许本地 Bundle 或显式静态 index；
`PAOS_RESOURCE_REGISTRY_URL` 覆盖 `resourceRegistry.url`。
公网 Registry 按名称查询 Skill；CLI 指定的版本在 Node 解析前与下载 Bundle 的 manifest 校验，
不会拼接到 Registry URL。

`discover_active_runtime` 核对持久化 state、Dora flow、Gateway health 与 required Tool
contexts。availability provider 贯通 SkillsLoader、ExperienceCoordinator 与
SkillActivationManager。Skill 发现顺序为 workspace、installed、built-in。

## 11. Experience 与 evolution 接入

全部 Agent tool calls 继续记录。冻结 binding/版本字段关联 AgentTask、revision、invocation 与
attempt。Outcome source 将每个 revision verdict 映射到该 revision
最后一条 execution record，使恢复后的任务同时保留失败与成功语义尝试。

生成 Lesson 和 Skill update 继续经过去敏、作用域、支持门槛、抽象校验、managed block
替换、原子写、reload validation 与 rollback。Evolution failure 保持 fail-open。

## 12. 扩展工作流

新增机器人能力时：

1. 实现或打包 ToolEndpoint operation；
2. 发布带精确 schema 与 binding 的 Query/Action/Session ToolSpec；
3. 在 Gateway 定义 operation `max_concurrency`；
4. 在 manifest v2 Bundle 中加入锁定 Node 与 profile 引用；
5. 测试 binding 漂移、context、invoke、pending、terminal、cancel/stop、ownership 与 unknown；
6. 在 Skill 中加入工作流指导，不写入任务特定坐标或凭据。

不要创建第二套 PAOS 执行协议、Agent 直连 Dora 或跨 Tool lease。只有通用 task/Tool API tools
无法表达能力时，才应新增 Agent tool。

## 13. 测试门禁

```bash
ruff check PhyAgentOS tests
python -m compileall -q PhyAgentOS tests
pytest -q
```

测试应覆盖响应契约、Session ownership、pending/cancel/stop/timeout/unknown、单活动任务、
诊断 Query、不可变 binding、无 POST 恢复、revision recovery、evidence、按版本 episode、归档
攻击、事务回滚、Registry 校验、Runtime health/切换与模拟工作流。具体硬件/仿真测试以独立安装
匹配制品和 Dora 可用为条件。

## 后续阅读

- [Forge Tool API 接入契约](../forge/README_zh.md)
- [集成开发指南](../user_development_guide/README.md)
- [Agent 经验与 Skill 自进化](05-agent-experience-and-skill-evolution.md)

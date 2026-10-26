# 受治理的交互基础

[English](README.md) | [简体中文](README_zh.md)

这是独立于默认机器人开发流程的可选受限 Core 扩展。普通 Node/Skill 开发从
[TrackB](../user_development_guide/track_b/README_zh.md)开始；传输与 invocation 语义仍以
[Tool API 契约](../forge/README_zh.md)为准。

该基础将一个真实 `AgentTask` 连接到 Forge Session，以及有预算约束的观测/决策循环，
实现基于 Core `dev@3dcdc914`。不包含 Minecraft 操作、其 Bridge 或生产控制面的隔离，
也不改变 Gateway API 或 invocation 语义。

## 启用方式与公开入口

`forge.interaction.enabled` 默认为 `false`。启用后允许交互任务契约，**不授权真实网络控制面**。
当前仅接受显式注入的 `InProcessInteractionDeployment`，其中包含 `httpx.MockTransport`、
可信环境预算及决策 callable。不存在 `private_control_plane=true` 生产绕过开关。
未来生产 adapter 必须先证明网络/凭据隔离与 Endpoint 作用域约束，再接入这里。

交互协议没有 token 或 HMAC 认证层。当前权限来自可信进程内能力与测试部署 adapter，
协议本身不建立生产认证。

沿用以下工作流：

1. 使用正常的校验 installer 安装 Skill 归档并激活。
2. 创建 `verification.mode=enforce` 或 `recovery` 的绑定任务。
3. 使用冻结的 Session Tool 调用 `forge_tool_start_session`，指定 `ownership=task` 与
   `arguments={"world_id":"<authorized world>"}`。可信宿主提供预算、task/revision 身份、
   start/run ID 和 binding digest。
4. 读取 `forge_task_get`。原有 `data` 不变；可选同级字段 `interaction` 报告运行状态、
   停止/核对阻塞项及模型调用。
5. 通过 `forge_task_cancel`、Session stop 或 `/stop` 取消。

只有常驻 gateway 或交互 CLI 可以启动运行。单次 CLI/cron 上下文不得启用常驻 Supervisor。
普通 CLI 不注入测试环境，不能据此启动未经批准的真实游戏。AgentLoop 将运行中的 bus
生命周期标为常驻；聊天回复不拥有监控任务。关闭/重启会在 client 关闭前执行有界停止核对，
未解决运行继续保留记录。完成/阻塞通知持久化去重，采用至多入队一次语义，不保证突发崩溃下送达。

## Bundle 与协议契约

可选 `interaction.yaml` 声明 `interaction_contract_v1`：

```yaml
protocol: interaction_contract_v1
session_tool: grid.run
snapshot_tool: grid.snapshot
submission_tool: grid.submit
proposal_schema_file: proposal.json
snapshot_views: [snapshot, submission, history, run]
event_channel:
  mode: polling
  event_type: progress
  business_kind: interaction_changed
execution_policy:
  max_active_sessions_per_task: 1
  max_inflight_decisions_per_run: 1
  runner_mode: single_owner_no_hot_takeover
  completion_policy: quiesce_then_stop
access_policy: private_control_plane
```

三个 Tool 必须位于已有 `required_tools` 中，语义依次为 Session/Query/Action，并解析到同一
Endpoint。Proposal schema 是自包含的 JSON Schema Draft 2020-12 object，禁止未知 object
字段，不解析远端 schema 引用。Tool readiness 与 ToolSpec hash 继续由原 Forge binding resolver 检查。

普通归档清单覆盖 sidecar 与 schema。安装在校验后、原子提交前写入
`.paos-interaction-install.json`，Bundle 不得自行提供 receipt。普通 Bundle 不需要该 receipt；
缺少 receipt 的交互 Bundle 必须重新校验安装。激活拒绝同名 workspace override。
修改 sidecar/schema 后需重新安装与激活，不能替换运行中的契约。

业务 wire 契约沿用已审查原型：整数 executor cursor、`snapshot/submission/history/run` 视图、
相互独立的 start/run/step/submission 与 invocation ID，以及不可变接受/拒绝 receipt。
业务 payload 的标准 hash 使用排序、紧凑 UTF-8 JSON，禁止 NaN；submission hash 排除
`runner_epoch`。Query output 与 invocation result output 分别使用原有 Forge envelope。
`accepted` 不代表物理操作或根目标成功；receipt 的 `reason`、`execution_status` 保留原名与含义。

## 所有权、恢复与验证

`CoordinatorInteractionClient` 使用真实 Coordinator 的 intent 与 invocation 账本。
监控读取校验 task/run 作用域，不要求任务仍可执行。每次 operation 固定其 Runtime client，
并在派发前最终复核身份与取消状态。Stop/status 不会把旧 invocation ID 带到替换后的 Gateway。
模型不能通过 JSON arguments 提供 Runner 能力。

独立的 `.paos/interaction/interaction.sqlite3` 保存版本化 binding、run、step、模型 attempt、
submission 及只追加 audit event。Task event 账本中的持久标记防止扩展 journal 缺失时静默回退。
跨数据库崩溃恢复依赖 intent，不宣称两库原子提交；可在崩溃后恢复的进程锁阻止热接管。

未变化的 snapshot 不重复触发推理。模型 attempt、时间、token 和空闲进展均有预算。
`ProviderDecider` 使用新的纯数据上下文及 `tools=None`，拒绝 tool call 和模型自报用量。
工具注册表策略既检查公开 schema，也检查每次实际执行，包括子 agent、后续消息和同一模型响应
中的后续工具调用。未结束的不安全工具调用会阻止启动；被取消的不安全工具协程保留本地保护，
因为协程取消不能证明外部进程终结。这些检查覆盖 Core 模型路径，不构成操作系统/网络隔离边界。

Timeout、receipt 丢失、Gateway 404、Runtime 身份变化和未解决执行都会阻止新工作。
第一版**不自动重发 start 或 submission**，即使已经 `absent_confirmed`。
Unknown 不免除请求停止与有界核对的责任；无法确立安全状态时，run 保持 blocked 并保留资源所有权。
Gateway 停止确认或重复轮询不能修复丢失的 Gateway 控制交换，这仍是真实游戏接入的阻塞条件。

Finalize、验证错误、重新规划和资源释放都检查 interaction gate。取消在控制 I/O 前持久关闭 admission。
旧运行解决后，新 Session 需要新 PlanRevision。验证读取经过检查的初始/最终观测、不可变 receipt、
step 关联与持久 executor history，而非只使用机器人证据。证据沿用 artifact hash、lineage 和
EvidenceBundle 校验。使用 `required_kinds: [interaction_observation]`；交互 adapter 另行要求完整
过程轨迹，不将其插入旧 before/after kind 要求。一个根任务只产生一个带轨迹引用的去重 experience
episode；模型给出 `finish_candidate` 后，根目标仍可能验证失败。

## 兼容性与运行限制

Coordinator 的 `_append_execution` admission guard 同样适用于未启用交互的普通 AgentTask。
取消请求已持久化或任务离开 `executing` 后，intent 事务在派发前拒绝新 Query、Action 和 Session
record。这收紧了首次可执行检查后状态变化的普通任务行为，不会取消已经派发的调用，也不证明远端结果。

两个进程共享 interaction journal 且原进程仍拥有未解决运行时，第二个进程在恢复中抛出
`RunnerAlreadyOwnedError`。异常经 Coordinator 传播到 Agent 启动，导致第二个 Agent 启动失败，
原所有者继续。此启动失败路径属于交互基础的 v1 单 Runner 策略；没有热接管或受限聊天回退。
运维人员应避免在同一 workspace 启动第二个所有者，不得删除 owner lock 强行接管运行。

Supervisor 缺失或其监控任务未运行时，仅持久化取消不能保证物理停止或持续核对。
未解决运行保留阻塞项与资源所有权；同一 Supervisor 的通知不构成独立故障检测。
真实游戏接入前应增加外部健康监控，并建立通过授权控制面停止环境、核对执行事实的操作流程。
不得手动把 journal 标为 settled 或将其删除来释放资源。

`verified_extension` 当前每次检查都会 hash 已安装 Bundle 的全部文件，包括 finalize 中的重复检查。
小型测试环境可以接受，但包含大型游戏资产时可能昂贵。Manifest digest cache 属于后续工作，
必须具备可靠的变更失效机制，不能削弱篡改检测；v1 保留完整校验。

## 可复现验收

```sh
python -m pip install -e '.[dev]'
python -m pytest tests/test_interaction_foundation.py -q
python -m pytest tests -q
```

macOS 可用 `TMPDIR=/private/tmp` 避开既有 evidence 测试中未解析的 `/var` 与 `/private/var`
临时目录别名。设置 `LITELLM_LOCAL_MODEL_COST_MAP=True` 可使 Provider 成本表加载保持离线。

`tests/interaction_support.py` 提供 MockTransport 后的持久化 SQLite 测试 executor，安装真实校验
Skill Bundle，创建真实 activation 与 AgentTask，并使用独立最终位置 verifier。测试覆盖正常多步循环、
实际 AgentLoop 回复/停止、共享工具策略、迟到模型结果、根验证期间取消、传输不确定性、Runtime 替换、
进程所有权与恢复、扩展关闭/缺失、安装篡改、证据缺口，以及唯一根任务 experience episode。

真实游戏接入前，提供带认证的 Endpoint/Bridge adapter 和独立目标证据，修复游戏离线 watchdog，
证明 shell/HTTP/委派、别名与生命周期路由的隔离，并针对实际 Gateway 和游戏验证停止失败/丢包行为。
当前基础不提供 SSE、热故障转移、跨 Runtime 重新绑定、自动重发或 Subtask DAG。

# PhyAgentOS Core 开发者手册

[English](../en/03-developer-manual.md) · [文档索引](../README_zh.md)

> 文档版本：1.1.1。

本文面向 Core 实现：AgentTask 协调、持久化、binding、验证和 Runtime 管理。机器人、Node 与 Skill 开发请从 [TrackB 框架说明](../user_development_guide/track_b/README_zh.md)开始。HTTP 路由、身份和生命周期语义以 [Forge Tool API 契约](../forge/README_zh.md)为准。

## 1. 开发不变量

1. 机器人执行只有一条物理路径：`ForgeToolClient → Gateway Tool API → ToolEndpoint`；
2. AgentTask 聚合规划、证据和判定，但不执行机器人；
3. `binding_id`、`task_id`、`revision_id`、record ID、`caller_id`、`invocation_id` 与 `attempt_id` 相互独立；
4. Forge ToolResult 与 invocation events 是权威执行事实；
5. Action/Session admission、cancel/stop 接受、timeout 和 `unknown` 都不能证明物理停止；
6. 通用 Agent tools、verification、experience、evolution 与动态 MCP 保持独立；
7. Runtime 制品必须通过有界归档与摘要校验后才能安装；
8. Core 执行设置只改变回合怎么跑，不改变 provider/model 选择，也不会自动换成更便宜的模型。

## 2. 模块地图

| 模块 | 职责 |
|:-----|:-----|
| `agent/core/` | 七个 Core 组件（入口壳、回合、循环驱动、模型适配、提示组装、工具执行、SQLite 事件存储）；契约在 `types.py`、`events.py`、`protocols.py` |
| `agent/composition.py` | `compose_agent`：用注入的协作对象装配 Core，并固定能力顺序 |
| `agent/capabilities/` | Forge、Skill、通用工具、图片、MCP、子代理、按需工具搜索与受限程序执行的 Core 能力适配 |
| `tooling/` | 不依赖 Agent 运行时的共用工具实现；旧工具路径保留兼容导出 |
| `agent/services/skill_documents.py`、`agent/services/turn_activation.py` | Skill 文档发现与按回合的激活身份 |
| `agent/bootstrap.py`、`agent/services/forge_verifier.py` | 共享的 Forge 构造工厂与语义验证器；`agent_legacy/session_verifier.py` 只保留别名 |
| `agent/services/session_catalog.py` | `core.db` 中的会话 id、标题、按天序号与按会话的 provider/model/effort 选择 |
| `agent/services/context_compaction.py` | 手动与自动压缩共用的摘要与最近完整回合投影 |
| `agent/services/context_policy.py` | 自动先 trim 再 summary 的上下文缩减，以及一次有界 overflow 恢复 |
| `agent/services/turn_budget.py`、`agent/services/turn_control.py` | 按回合的 token/无进展预算与有界修正队列 |
| `agent/services/job_manager.py` | 按会话隔离的后台 Shell 任务、限长输出与 `job_status` / `job_wait` / `job_cancel` |
| `agent/services/usage.py` | 用量账本、下一次请求的 context 估算与回合/会话脚注格式 |
| `agent/services/background_runtime.py` | gateway 的 cron、heartbeat 与 channel 回合共用的一套 Core 装配，含冻结路由与有界准入 |
| `cli/commands.py` | 单一 Typer app、全局 callback 与命令组注册，包括唯一的 `agent` 命令 |
| `cli/command_groups/agent_commands.py` | Core 路径 `paos agent`（默认）：依赖构造、逐回合装配、`CoreAgentSession` 与进程生命周期 |
| `cli/command_groups/`（其余模块） | provider、gateway、Skill、Forge Node、channels 与 onboard/status 命令组，由 `commands.py` 统一注册 |
| `cli/configuration.py` | gateway 与 legacy 路径共用的配置加载和启动选择（provider/evolution） |
| `cli/legacy/legacy_agent.py`、`cli/legacy/legacy_support.py`、`cli/legacy/legacy_gateway.py` | 旧的 `paos agent --runtime legacy`、其终端交互、旧 prompt 会话 helper，以及 `paos gateway --runtime legacy` 选中的旧 gateway |
| `cli/interaction/interactive_session.py`、`cli/interaction/agent_output.py`、`cli/interaction/terminal_input.py`、`cli/interaction/terminal_display.py`、`cli/interaction/model_picker.py` | Core 的输入调度与取消、消息展示、终端输入、实时状态与工具区，以及 provider/model/effort 选择器 |
| `agent_legacy/loop.py` | 现有 Agent Loop、通用工具、动态 MCP、Forge tools 注册、上下文与生命周期 |
| `agent_legacy/tools/forge_tool_api.py` | 十个 Gateway Tool API 工具的兼容 wrapper |
| `agent_legacy/tools/forge_task.py` | 五个 AgentTask 生命周期工具的兼容 wrapper |
| `forge/tool_client.py` | 严格异步 HTTP client 与响应校验 |
| `forge/binding.py`、`forge/task.py` | 不可变 Runtime/ToolSpec binding、AgentTask model/store、证据、验证与恢复 |
| `forge/observation.py`、`forge/evidence.py` | best-effort 图像/state 采集与 artifact 写入 |
| `skill_runtime/` | manifest、catalog、安全归档、installer、Registry、state、Runtime manager 与 availability |
| `agent_legacy/experience/` | activation、episode、assessment、Lesson、Skill candidate 与 evolution |
| `verification/` | 公共任务、证据、请求、verdict 契约与 Verification Service |

## 3. Forge Tool API client

`ForgeToolClient` 只接受 `ok=true` 且 `data` 为 object 的 JSON envelope。错误保留 HTTP status、
error code、retryability 以及响应中已有的 invocation identity。

HTTP 路由、响应码及 pending/cancel/stop 语义统一维护在 [Forge Tool API 契约](../forge/README_zh.md#2-tool-发现与-context)。Client 实现应遵循该契约，本文不重复定义路由表。

路径组件经过 percent encoding。PAOS 生成 `caller_id`，并在 Action/Session admission 前与
intent 一同持久化。Admission 必须包含非空 `invocation_id`，Action 还要求 `attempt_id`。
Timeout 形成 unknown record，恢复不会重复 POST。

## 4. Agent tools

公开的 Agent 工具清单统一维护在 [API 契约](../forge/README_zh.md#6-agent-tools)。工具名、描述、JSON Schema 与操作位于 `agent/capabilities/forge_tool_api.py` 和 `agent/capabilities/forge_task_tools.py`；`agent_legacy/tools/` 只保留兼容 wrapper。传输处理放在 `forge/tool_client.py`，任务协调放在 `forge/task.py`。

Core 路径还注册 `agent/capabilities/skill_access_capability.py` 提供的 `activate_skill`。它读取已注册的 Skill 文档并返回本轮有效的 `activation_id`；不启动 Skill runtime，也不属于 Forge 工具。

诊断 Query 与绑定调用使用相同 HTTP 方法；每个变更或任务归因 wrapper 都在网络访问前校验
ownership 与冻结 binding。Tool wrapper 不能自行生成 Gateway identity 或 result。

可选 `timeout_ms` 不由 Agent 填默认值：Gateway 上限未知时模型省略该字段，由 Gateway 用自己
的默认超时。HTTP client timeout 是另一项设置，不能拿它代替。Core 的 prompt fragment 每步从
coordinator 实时读取全 workspace 的未解决 Action/Session blocker，只给 task/invocation 身份、
状态和 semantics；本 session 的 `succeeded` 不会掩盖更早的 unknown，`None` 表示 Gateway 身份
从未确认。它只说明当前阻塞，不代表已经解决。

## 5. AgentTask 契约与事务

`AgentTaskRecord` 包含稳定 ID、任务描述、`TaskVerificationContract`、状态、只追加
PlanRevision、evidence refs、verification attempts、取消状态和时间戳。每个 PlanRevision 都有
自己的 Tool records、verdict 和 verification attempts。

`AgentTaskStore` 使用 SQLite WAL 与 `BEGIN IMMEDIATE`。创建时在同一事务内查询非终态任务，
保证跨进程全局单活动槽位。更新写入完整的已校验 record 和 append-only event；业务代码不直接
修改表。

公开状态由 [AgentTask 状态契约](../forge/README_zh.md#8-agenttask-模型)定义。Store 转移必须保持这些语义；`unknown` Tool record 结束记账，但不证明物理停止。

Gateway 现在为自身的派发前请求校验返回明确的 `FORGE_TOOL_REQUEST_VALIDATION_REJECTED`
错误码。只有响应为 HTTP 400 且不带 invocation/attempt 身份时，PAOS 才把该 code 记为
`failed`。旧版或当前已发布的 Gateway 返回无 `error.code` 的裸 400（只有 `msg`）时仍保持
`unknown`；必须实际部署更新后的版本，这类拒绝才不会重现，因此不要宣称当前运行的 Gateway
已修。

## 6. 绑定执行生命周期

Core 协调激活与 binding、持久化 intent admission、权威响应记录和 finalize。Intent 必须先于网络派发持久化，Gateway 引用写回同一个 task revision。Finalize 等待 task-owned 执行终结，采集后置证据并应用验证，再调度 experience。

公开的 [binding 与身份](../forge/README_zh.md#7-bindingidentity-与关联)和 [AgentTask 生命周期](../forge/README_zh.md#8-agenttask-模型)统一维护在 API 契约。Cancellation response 只记录请求，聚合结果仍需核对与 finalize 确立。

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

Manifest 字段、Node 归档结构和启动 hook 环境见 [Bundle 与 Tool 接入参考](../user_development_guide/README_zh.md#4-构建-manifest-v2-skill-bundle)。Core 在 `skill_runtime/manifest.py`、归档校验器与 installer 中实现这些契约。

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

模型与机器人配置、新增 profile 或独立 Node 开发，请使用 [TrackB 三种开发模式](../user_development_guide/track_b/SKILL_DEVELOPMENT_zh.md)与 [Piper X 示例](../user_development_guide/PIPER_X_DEMO_zh.md)。

只有能力确实需要时，才扩展 Core 已有的通用 client、binding、coordinator 或 installer。保持 API 契约，并在受影响的实现边界补充有针对性的回归检查；不新增 Agent 直连 Dora 的另一条执行路径。

可选的[交互基础](../interaction/README_zh.md)是独立受限扩展，Core 改动也需遵守其部署限制。

## 13. CLI 入口归属与生命周期

`paos agent` 只在 `cli/commands.py` 注册一次。命令必须显式选择运行时；失败后不会切到另一条路径：

| 选择 | 归属 | 状态 |
|:-----|:-----|:-----|
| `--runtime core`（默认） | `cli/command_groups/agent_commands.py` | Core 路径；不 import legacy 包 |
| `--runtime legacy` | `cli/legacy/legacy_agent.py` | 旧实现；显式选择，用于对比 |

`commands.py` 保留 Typer app、全局 callback 和唯一的注册点；各命令组自己持有 Typer app 或 `register_*` 函数。`register_agent` 只在显式 `--runtime legacy` 分支里 import legacy runner；建 app 或跑 Core 都不会 import `agent_legacy` 或 `cli.legacy`。`configuration.py` 存放 gateway 与 legacy 路径共用的配置加载及启动时的 provider/evolution 选择。`agent_commands.py` 只负责 Core 路径：读配置、建依赖、每回合建一次装配、结束时关闭会话；它目前仍持有 `CoreAgentSession` 和 Core 依赖装配，不是单一职责模块。`interaction/interactive_session.py` 调度 Core 输入，`interaction/agent_output.py` 负责消息展示，`interaction/terminal_input.py` 负责 prompt_toolkit 输入和终端状态，`interaction/terminal_display.py` 持有实时状态与工具区，`interaction/model_picker.py` 存放共用的终端选择器。Core 的 prompt 代理使用原始 ANSI 直通，Rich 样式会正常显示，不再变成可见的 `?[36m`；模型、工具和异常文本在渲染前会去掉控制字符，转义序列只由 Rich 产生。

终端只有一个实时区域写入者：prompt_toolkit 的 prompt message 在输入行上方渲染回合阶段（准备、思考、调用工具、本地停止）、转圈和耗时，以及本回合的步骤行、隐藏数量和总数。这些行紧贴 session 行和输入行，不挂到终端底部的工具栏，因此请求块、状态和输入之间不会留下大段空白。没有第二个 `Live` 或 `status` 写入者，因此输入框、动画和工具区不会互相移动光标。动画只由 prompt_toolkit 的刷新定时器驱动；状态变化只来自已经持久化的事件和会话的提交/取消回调。工具调用按 `(session_id, turn_id, tool_call_id)` 建键，从 `tool.intent` 到 `tool.dispatched` 再到 `tool.settled` 原地更新，重复事件不会增加总数；新回合开始时清空旧行，避免继承上一回合状态。没有 prompt 时（单次 `-m`、启动阶段、重定向流），同一份状态按变化打印普通行，不做动画，也不输出转义序列。`/display_all_step` 决定实时区域只保留最近有限步骤，还是保留完整步骤记录，见“回合边界与步骤显示”；步骤行只有工具名、本地状态和限长错误摘要，不打印参数、结果或凭证。

session/provider/model/effort 一行（含 `context`）放在 prompt message 里、cyan 竖线输入标识上方；状态、工具和流式预览行同在 prompt message 中，紧贴它们和输入行。运行中通过 layout 的 `always_hide_cursor` 隐藏请求标识和真实光标，buffer 只读，Enter 被拦截；普通按键不进入 buffer，也不会被偷偷排进模型队列，回合开始前已有的草稿保留，回合结束恢复提示和光标。提交一条请求后，输入提示块在 `prompt_async` 完成最终渲染之后被精确清除，再由永久请求块接替，因此不会留下空竖线，也不会与请求块重复。非终端输入仍保持逐行文本行为：不写输入提示，提交后由永久请求块显示请求正文，全程不输出 ANSI 样式。

在终端里，不带参数的 `/model` 与 `/effort` 复用行内选择器。打开第二个 prompt_toolkit 应用前先暂停 reader（结束当前 prompt，且不再启动新 prompt），菜单返回后才恢复；Esc 或 Ctrl+C 只关闭菜单，会话和当前选择都不变。未提交的半行会带入下一条 prompt，不会因菜单丢失。每次模型命令结束（文本、菜单、reset、取消或失败）都从会话实际选择重发 session 标签。带参数的写法和非终端输入不经过选择器。

legacy 路径的生命周期留在 `legacy/legacy_agent.py`：建 `AgentLoop`，跑单次或终端流程，最后停 loop 并关 MCP；旧 prompt 会话 helper 在 `legacy/legacy_support.py`。下面的步骤只讲新的 Core 路径。

### 回合边界与步骤显示

每次普通请求和显式 `/resume`，命令层把该回合实际发送的 instruction 在 OS 输出前完整打印一次。
随后打印一条随终端宽度延伸、标题居中的分割线 `PhyAgentOS · Turn N`：`N` 是本会话的本地显示
序号，不是持久化的回合 id，这个标签同时是本轮 OS 区块的起点。工具行、答复、错误和用量脚注都在
区块内渲染，`End Turn N` 分割线在该区块最后一行之后打印，形状与起始分割线一致。边界只属于显示，
不新增事件、回合 id 或模型消息；重定向输出仍是同样的横线和标题文本，不带 ANSI。

session、provider/model 与 `context` 行渲染在请求输入上方。状态行不包含首轮请求提炼出的标题；
`derive_title()` 仍写入目录元数据，不带参数的 `/session` 仍列出该标题。永久请求记录用 cyan
竖线和深色背景色块取代 `You:`，请求正文不变，多行和长请求完整保留，重定向输出不带 ANSI 样式。
提交后输入提示块被清除，永久请求块是屏幕上唯一的请求标识。逐条的 `Queued` 与
`Processing` 行不再渲染；程序注入的排队输入仍只回执序号。

`/display_all_step` 取代 `/tools`，由本地处理。不带参数时切换设置。默认 `off` 只在实时区域保留
最近有限个已 settled 步骤，并给出隐藏数量与总数；`on` 立即完整打印本回合已有步骤，之后每个新
settled 步骤单独成行写入滚动记录。显示偏好跨回合保留直到再次切换，每回合自己的展开状态仍随
实时区域关闭。设置只改变显示：工具执行、`tool.*` 事件、已存结果和发给模型的上下文都不变。

回答渲染器会拦截 DSML 协议块。完整或未闭合的块不会当普通答复原文铺开；CLI 打印简短警告，原文
仍保留在事件日志。Core 不执行、也不解析 DSML 块，所以这个拦截不是工具调用路径，也不承诺这类
文本会被执行。

### Core 进程生命周期

1. 读取并校验配置，应用 `--workspace`，拒绝 fleet 模式。
2. 解析 provider/model/effort 选择；选择在回合真正开始时读取，排队中的提交因此会用运行前的最新选择，运行中的回合保留它开始时的选择。
3. 构造 Forge 组件：Skill catalog 与 runtime availability、`ForgeToolClient`、`AgentTaskCoordinator` 和可选验证器。
4. 在这个 coordinator 上做一次 Forge 未知 Action/Session 恢复，且在任何一个回合被提交之前完成。`reconcile_nonterminal()` 只发 Gateway GET 查询；不重发 Action 或 Session 的 POST，也不把仍未确认的 unknown 改成终态。Core 没有交互 supervisor，因此这里不会请求机器人停止。恢复本身失败时会报告一次并继续打开会话；未解决的 unknown 仍然阻塞新任务。
5. 创建 `SkillDocumentLoader` 与 `TurnSkillActivationManager`，把激活管理器注册给 coordinator，使 `forge_task_create` 能校验 `activation_id`。
6. 打开会话存储 `<workspace>/sessions/core.db`。
7. 建立共用工具能力，在装配前完成 MCP 发现，再用 `compose_agent` 建种子装配；Skill 访问在前，通用工具、图片、条件模式管理、MCP、spawn、有 MCP 工具时的 `tool_search` 与 code mode 打开时的 `execute_program` 居中，Forge 在后。
8. 跑单条消息或交互循环。每回合在真正开始时读取该 session 的选择并新建面向模型的装配；这份装配只服务一个回合，回合结束立即 `runtime.stop()`（不关 host），不再等下次 submit。store、Forge coordinator/client 与激活管理器保持共享。同一 session 的提交按登记顺序串行，不同 session 可并发；`cancel` 只取消当前装配，排队中的回合不会继承这次取消。
9. 只关闭一次：先拒绝新提交并让排队中的提交在开回合前退出，再停仍可能持有回合的 runtime，等已进入本 session 的提交返回，对每个已接受的 session 通知各能力，关闭规范能力一次，最后关 store。Forge client 与验证器由资源栈在会话之后关闭。

只要还有更早的 Action/Session 调用处于 unknown，`forge_task_create` 就拒绝建新任务，跨 session 的旧任务也算在内。拒绝结果会给出权威的 `task_id`、已确认的 `invocation_id`（Gateway 身份从未确认时明确写 `None`）、记录状态与语义，以及该用哪个 status 工具查询；不会建议去试列表工具，不会重发旧请求，也不会建议删 store 或靠重启清除。

运行中只用 Esc 停止：第一次 Esc 进入两秒窗口并显示黄色提示，第二次 Esc 通过原有 session cancel 路径请求本地取消。超时、其他按键、方向键或新回合都会清除已 arm 状态，单次 Esc 和方向/Alt 组合不会停止任务；模型菜单里的 Esc 只关闭菜单。取消会请求 Forge 取消该 session 的最新任务，但不能证明远端动作已停止；状态明确写"本地"和"远端未确认"。本地命令（`/help`、`/display_all_step`、`/correct`，以及被拒绝的未知斜杠命令）由 reader 立即处理；`/stop` 只返回提示，不取消也不发模型。非终端输入捕获不到 Esc，只能靠 Ctrl+C。普通文本和模型命令仍按 FIFO 顺序，模型命令只在回合之间执行；程序注入的排队消息只回执序号，不再打印逐条 `Queued`/`Processing` 消息，也不回显内容。

`agent/services/forge_verifier.py` 是两条路径共用的唯一验证器实现；`agent_legacy/session_verifier.py` 只是重新导出，原有 import 和模块 patch 仍然生效。

### 会话目录与身份

Core 把事件与目录元数据放在同一个 `core.db`。`SessionCatalog`
（`agent/services/session_catalog.py`）新增两张表：`session_catalog` 保存每个会话的标题和
最近一次 provider/model/effort 选择，`session_counters` 按本地日期分配序号。
`create_session()` 在同一个 `BEGIN IMMEDIATE` 事务里写序号与目录行，若生成的 id 已存在就继续
取号，因此并发进程和重启不会拿到同一个 `cli:YYYYMMDD-NN`。只存在于 `session_events` 的旧会话
仍按原 id 列出来；元数据按需补齐，不改名。标题由 `derive_title()` 从首轮输入本地提炼：
折叠空白、脱敏形似凭证的文本、按长度截断，不发模型请求。标题只是元数据，id 从不随之改变。
状态行不显示标题；目录和不带参数的 `/session` 列表仍显示。
`--session <id>` 走 `ensure_session()`，不分配新 id。不带参数的 `/session` 列出目录会话
（最近更新在前），`/session <id>` 复用指定会话，`/new` 在运行中的进程里分配新 id。这两个命令
都在本地处理，不提交模型回合。旧 JSONL 会话既不读取也不迁移。

### Context 记账与回合脚注

`UsageLedger`（`agent/services/usage.py`）只从已持久化事件取数，并按 `model_call_id` 幂等：
一个模型调用只计入一次，重复的 completed 只细化已有快照。失败或中断的 attempt 若确实报告了
用量，仍按真实消耗计入；没有报告的不会让历史变成不完整。已完成的调用若没有用量，会标为
不完整并在摘要里说明。
缺失字段保持 `None` 并显示为 `--`；只有 provider 明确给出 `0` 才显示零。`cached_tokens` 与
`reasoning_tokens` 只作参考：cache 已包含在 prompt tokens 内，reasoning 已包含在 completion
tokens 内，都不再加进输入、输出或合计。`total_tokens` 优先用 provider 的值，没有才用输入加
输出。只有每个计入的调用都同时报告 prompt 与 cached 时才显示缓存命中率。`estimate_context()`
以最新的持久化 `request.prepared` 快照为起点，补上其后的 assistant 与 tool 消息；只有
provider 报告的 prompt 大小仍对应未变化的请求时才标记为 `exact`。`ProviderService.resolve`
组合有效窗口：可靠的模型元数据优先，显式 `context_window_tokens` 只在上限更小时覆盖；元数据
未知时先看显式 cap；有 cap 用 configured 值。只有既无可靠元数据也无显式 cap 时才用文档化的
65,536 fallback，并记录 source=`fallback`。模型原始值与 effective guard 分开保存。模型轮次统计 provider 调用次数，不是内部推理步数，也不伪造推理步数。会话累计行
从已存事件重建，重新载入后仍可用。脚注只是终端渲染，绝不插入发给 provider 的消息历史。

### Prompt 投影、工具阶段与启动读取

发送请求前有三种只改请求副本的投影；持久事件不被改写。

- `project_skill_documents()` 按 `(Skill 名, 精确内容 sha256)` 建键。每个版本在请求里只保留
  一份完整文档；旧副本保留 activation metadata、内容 hash、字符数与省略标记。正文很短、去掉
  后净省不足 128 字符时保留副本。compact 摘要不是文档副本，投影只去重 history 里已有的全文；
  再次 `activate_skill` 会按新回合授权返回完整正文，不会自动读本地文档。tool-call id 与消息
  顺序保持不变。
- `project_forge_tool_results()` 只去重**完全相同且成功**的 Forge 读取结果，保留最新一份全文，
  旧副本换成 hash/reference 标记；标记不写旧 notes。只有序列化后净省至少 128 字符才折叠，
  短结果原样保留。错误、unknown/running/accepted/pending、非 JSON 文本和非成功结果原样
  保留；`phase`/`cancel_status` 不是已知 terminal 时不得当成功去重。受保护字段包括 task/invocation/action/session id、status、evidence、image/artifact
  引用和 unknown 语义。
- `PromptAssemblerImpl` 接受 `ToolVisibility` 钩子；`composition.py` 接入 Forge 工具阶段：
  primary Skill 激活前只显示读取/取消类工具，激活后才显示任务创建类工具；注册目录本身不变。
  阶段隐藏不能从注册目录删除工具。
- `SQLiteSessionStore.read_startup_snapshot()` 在一次 SQLite 读事务里同时读 recovery 与
  conversation；store 子类覆盖 `recover()` 时退回 `recover()` + `read_conversation()`。
  `SessionSnapshot` 保证两项来自同一视图。连续 4 次 CAS 都输给活跃 writer 时，本回合以
  `RuntimeStorageError` 失败关闭，不开新回合、也不带着可能过旧的 recovery history 继续。
  没有 lease 时活跃 writer 仍可能与读取竞争；保证的是不接收不完整历史，不是完全互斥。
- `utils/helpers.py` 按内容 hash 缓存 prompt 估算，使用有界映射；tokenizer 重新加载和
  `clear_prompt_estimate_cache()` 会清空对应条目，编辑过的 System/tool/message 不会复用旧计数。
  缓存是估算，不是 provider 账单。

### 流式契约

注入的 provider 提供可调用的 `chat_stream` 时，`ModelAdapter.stream()` 走流式；没有则保留
`complete` 路径，只产出终态响应。流式文本只含正文，并在 `model.attempt.completed` 之前以
step 级 `model.response.delta` 事件持久化；`reasoning_content` 从不流式输出。终态 completed
事件仍带完整正文与真实收到的 usage，历史只存一条由终态响应构造的完整 assistant 消息，展示层
追加 delta 后不再重复打印 completed 正文。工具调用只从完整的 `tool_call` 事件收集，参数没收
全之前循环不执行任何工具。取消时关闭返回的生成器即关闭 provider 流；取消或断线的流不会自动
重发。预算错误在第一个事件之前抛出，从而记为一次干净的失败尝试。失败或中断的 attempt 只带
provider 确实发来的 usage 键。

### 执行控制

`CorePerformanceConfig`（`config/schema.py`）从 `agents.defaults.performance` 读取；camelCase 是
保存形式，也接受 snake_case。CLI 入口把 `max_parallel_tools` 与 `cache_stable_prompts` 传给
`compose_agent()`，在自动压缩开启且有效窗口已知时构造 `ContextPolicy`，并用同一个对象构造
`TurnBudget`。这些设置都不选择 provider 或模型，入口也不会自行换成更便宜的模型。

`max_parallel_tools` 只对实现可选 `execute_batch` 扩展的执行器生效。
`ToolExecutorImpl.execute_batch` 把连续的 `parallel_safe=True` 注册放进有界 worker pool，其他调用
一律作为串行屏障，并且每个调用仍走 `execute()`，所以校验、策略和 `tool.intent` /
`tool.dispatched` / `tool.settled` 顺序不变。结果保持模型给出的调用顺序；从未启动的调用结算为
`cancelled`，不写成成功。写入、编辑、Shell、`spawn`、`job_wait`、`job_cancel` 或 Forge 工具都
不会与另一个调用重叠。

`cache_stable_prompts=True` 时，`PromptAssemblerImpl` 只把固定 system 指令放进 system 消息，并把
本步骤的实时 fragment 作为一条 runtime-context 消息附在历史之后。静态前缀在各步骤间逐字节
不变，Forge blocker 与其他实时事实仍每步重读；assembler 不会把这条 runtime 消息写进调用方历史。

`TurnBudget`（`agent/services/turn_budget.py`）只统计 provider 确实报告的用量。缺失的 total 不当
0，已知的部分合计不当成完整用量，这些都不是计费上限。`max_turn_tokens` 在已报告合计达到该值时
阻止新的模型调用；已经派发的请求不能追回。`repeated_call_limit` 在连续多次完全相同的（工具名、
参数、终态、结果文本）结果后停止回合；换成别的调用会重置计数，`*_status`、`*_wait` 与 `wait`
轮询不受此限制。`reserve_steps=1` 时，多步骤回合的最后一步变成隐藏工具、`tool_choice="none"` 的
摘要请求；它的文本随 FAILED 返回，因为部分进度不是完成。子代理用量通过 `TurnBudgetRegistry` 归属到父回合；
父回合未知或已被淘汰时，后续子请求 fail closed。

### 撤回

`/undo` 是 CLI 本地命令，不会发给模型。`CoreAgentSession.undo` 在回合运行或排队、压缩进行中时
拒绝，然后调用可选存储能力 `UndoTurnStore.undo_last_turn`（见
`PhyAgentOS/agent/core/protocols.py`）。store 选出最新一个未被撤回的已结束回合，追加一条
session 作用域的 `turn.reverted` 事件，`payload["reverted_turn_id"]` 记录目标回合（标记本身不带
`turn_id`，因此不进 `read_turn`，也不参与恢复），不删除或改写任何行。目标选择、校验与插入在同
一个 session 锁和同一个 `BEGIN IMMEDIATE` 事务里完成。`_conversation_from_events` 跳过被撤回的
回合，投影因此回落到更早 completed 回合的请求快照。已被 checkpoint 覆盖的回合返回
`block_reason="checkpoint"` 且不写标记；失败或中断的回合以 `context_reverted=False` 记录，使下一次
undo 能继续往上。`UndoOutcome.request_text` 优先读取 `turn.started` 中的原始文本，旧日志回退到
该回合第一份 `request.prepared` 快照（`input.accepted` 只存字符数），`TerminalInput.prefill` 把它
作为下一次 prompt 的默认文本。撤回后的
会话投影与 `_last_user_text` 都会跳过 Core 的 runtime-context 消息，所以回填的是用户原始请求，
不是实时 prompt 状态。状态栏遵循同一规则：`events.py` 的 `reverted_turn_ids` 被投影、`estimate_context` 与
`_last_request_tools` / `_last_request_system` 共用；`_ContextStatus.refresh` 会重读已存事件，
因此能看到 store 自己写入的撤回标记（用量 ledger 仍只由 `load` 播种）。

### 自动上下文缩减

`auto_compact` 打开且有效窗口已知时，`ContextPolicy`（`agent/services/context_policy.py`）在每次
模型请求前运行。它只有两个有界阶段，从不循环：先确定性地缩短旧的超长工具文本（不发模型请求；
只改结果正文，调用/结果配对仍合法，原文仍在只追加事件日志里），再通过 `compact_session` 对安全
的已完成前缀最多发一次摘要。当前用户任务与最近一组完整工具对保持原文。只有测量到的下一次请求
确实变小才返回缩减；回合未结束时它不提交 checkpoint，由回合所有者把返回消息写入
`context.compacted`。摘要失败不会抹掉已经生效的 trim。估算器测量真实的下一次请求，包含 system
文本与工具 schema。

`compaction_threshold` 默认是有效窗口的比例（`0.8`）；硬上限是 `capacity - reserve_tokens`。
摘要用量记录在 compaction attempt 上，并计入会话累计。provider 返回结构化的 `context_overflow`
时，循环向 policy 请求一次强制缩减（`force=True`），用新的 step id 重试一次模型调用，且不重放
任何工具调用。强制缩减没有让请求变小，或重试仍失败，回合失败。只有结构化错误码会触发该恢复；
仅凭失败文本不会。

### 手动压缩

手动压缩把 checkpoint 追加到会话独立的 checkpoint 投影表，不改写 `session_events`，原始事件
始终可读。`/compact` 只在回合之间执行；它为较早历史写摘要，并把最近若干个完整回合按原文保留。
每次摘要调用先写一行 `compaction_attempts`，结束时记录状态与 provider 确实报告的用量。在规划
阶段就被拒绝、尚未发 provider 请求的压缩不写尝试，也没有消耗。因此失败或被取消的尝试不会改变
会话投影和原上下文，但尝试记录与已知用量仍可见。成功和失败的摘要尝试都按实际报告的用量计入
会话累计；没有报告用量的失败尝试不加 token，但仍保留在尝试记录里。checkpoint 上抄录的用量
只是参考元数据，不会再次计入。压缩前（before）与压缩后（after）的估算，以及每次请求前的
预检，用的是同一个真实 token 估算器，并把 system 与当前可见工具一并计入，不再按字符数
估算；只有下一次请求的估算确实变小才写 checkpoint。UI 只在收到 `REQUEST_PREPARED` 时才
切换 checkpoint 投影，回合开始（`TURN_STARTED`）不回退到旧历史。压缩从不派发工具调用，
也不改 AgentTask 或 Forge 记录，因此压缩后未解决的 `unknown` 仍然阻塞新任务。

### Resume 与修正

`/resume [text]` 调用 `AgentRuntimeImpl.resume()`，由 store 的
`read_resume_history(turn_id=None)` 选源。store 取最新一个未被 undo 撤回的失败/中断终态回合；
已完成回合不能作为源，已撤回或被 checkpoint 覆盖的回合会被拒绝，会话仍有未结束回合时也会拒绝。
基底是该回合最新的 `request.prepared` 快照，之后记录的 completed assistant 消息与已结算工具结果
按合法的 call/result 顺序追加。未结算、unknown 与已取消调用只写成警告，绝不重放。`turn.started` 记录
`resumed_from_turn_id`。新的 request 文本是新的用户指令，不合并进已存快照；FAILED/INTERRUPTED
的 resume 结果不是完成任务。

`/correct <text>` 调用 `AgentRuntimeImpl.steer()`，按精确的 `(session_id, turn_id)` 入队到
`TurnControl`。循环只在下一步模型请求前的安全步骤边界 drain，并为实际消费的文本写一条
`input.steered` 事件。它不新开回合，不插入正在流式输出的答案，也不取消或撤销已派发的工作。
队列有界（16 条待处理、总计 4000 字符、单条 2000 字符）；回合结束时未消费的修正不再执行，
终态事件将其记录为 `unapplied_corrections`，CLI 明确提示未应用。
拒绝表示没有活动回合或队列已满，不表示修正已生效。
`TerminalInput` 在忙时通过 Ctrl+G 打开纠正编辑，普通按键仍被阻止；Enter 只返回合法 `/correct`，
Esc 恢复旧草稿，不请求停止。

若源回合之后已有未撤回的 completed 回合，resume 同样拒绝，不暗中分叉或丢弃后来的历史；
需要独立分支时应新建会话。

### Gateway 后台运行时

`paos gateway` 的 cron、heartbeat 与 channel 回合都来自
`agent/services/background_runtime.py` 的 `open_background_core()`，与 CLI 共用同一个
`CoreAgentSession`、store、Forge coordinator/client 和 Skill 激活管理器。`TurnBinding` 在回合
开始前冻结 channel、chat、回复目标与 thread 元数据；能力按 `session_id` 读取绑定（不用共享
`ContextVar`），后到的回合无法改变执行中回合的投递方向。最近的外部聊天目标持久化到
`<workspace>/.gateway_route.json`（只含 channel、chat 与 `message_thread_id`），启动时恢复；
加载和使用都会重新确认 channel 仍启用，因此不会使用失效目标。heartbeat 周期由 heartbeat
service 的 cycle lock 串行，两个周期不会重叠。同一 session 的回合严格串行；不同 session 按
配置并发，超过队列上限的会话被拒绝而不是无限增长。channel dispatcher 每回合最多发布一条
最终消息，内容是完成后的完整文本，流式 delta 不转发到 channel；如果 message 工具已经投递过，
就不再重复。Core CLI 也在同一个 job store 上注册 `CronCapability`，但从不启动 scheduler；
调度只在 `paos gateway` 里运行。关闭时先停 intake，结算已接受的回合，最后只关一次 Core。
未配置的 channel 不会启动。`--runtime legacy` 保留旧实现用于对比，只有真正选中时才导入；
构建或运行 Core gateway 都不会 import `agent_legacy` 或 `cli.legacy`。

已知 cron 存储行为：任务存在单个 `jobs.json` 和同目录 `.lock` 文件。`_store_lock()` 取阻塞
`flock`（Linux 经 `fcntl` 跨进程；没有 `fcntl` 的平台只在进程内串行），只在同步读改写期间
持有，绝不跨 await 的模型或工具调用。写入先落到临时文件，再用 `os.replace` 原子发布。任务
结束后，service 在锁内重新读取最新文件，只合并该任务自己的运行状态；外部新增、删除、停用或
修改调度都会保留。存储仍是单个 JSON 文件：本轮没有迁到 SQLite，也没有新增配置项。请只运行
一个 scheduler owner；这不构成多 gateway 的恰好一次保证。

### Skill 激活作用域

`activate_skill` 是 `agent/capabilities/skill_access_capability.py` 提供的 Core 工具。一次激活只属于一个 `session_id` 和一个 `turn_id`；上一回合、其他 session 或上一个进程的 id 都会被拒绝。激活记录只在内存里，重启后必须重新激活。一回合允许一个 primary Skill 和任意多个 supporting Skill。激活只读 Skill 文档，不启动 Skill runtime。

### Core 路径未覆盖的部分

`/correct` 与显式 `/resume` 已实现（见上一节）。长期 memory、experience/evolution、
managed interaction Sessions 以及 fleet 模式作为独立后续任务保留。Forge interaction supervisor
没有接进 Core CLI，因此不能把交互迁移说成已完成。旧 JSONL
会话不导入；Core 使用独立的 SQLite 文件。Provider 错误只给简短的脱敏诊断：保留异常类型和
HTTP 状态，不回显响应体与凭证。

### 通用工具与旧入口

Core CLI 与 gateway 共用 `open_core_session()` 注册文件、Shell（含可跟踪的后台任务）、网页
搜索/抓取、场景图、图片、MCP、`spawn`/`subagent_status`/`subagent_cancel`，以及按配置启用的
`tool_search` 与 `execute_program`。共用实现放在 `tooling/`，不得 import 新旧 Agent 运行时；
`agent_legacy/tools/` 的对应模块只保留兼容导出。旧入口显式保留用于回归对照，不要求本轮删除。
`agent_mode` 仅面向支持原有模式管理的 Provider；普通单 Provider 不提供虚假的模式管理工具。
图片模型与子代理在调用时读取本回合冻结的 provider/model 选择，路由使用显式会话身份。
MCP 在装配前发现工具并持有连接，进程关闭时释放；工具错误不能记录为成功。
单个 MCP 服务初始化失败会显示不含凭证的诊断，其余工具仍可用；超时或派发后断线返回
`unknown`，不自动重放。通用工具、图片分析与 MCP 大结果保留完整文件并向模型返回限长文本及路径。
`spawn` 使用独立子会话与现有 Core 组件，不运行旧的模型循环；传入 `child_session_id` 可以继续
同一父会话下已完成的子会话并复用其已存对话，`role` 只影响提示文本，`tool_allowlist` 只能收窄
原本已允许的通用工具。子代理不递归派生子代理，不注册 Forge 物理执行工具。子代理完成通知使用
创建时的父会话与回复目标；完成摘要仅在所属父会话后续请求中提供，通知失败不改变子代理实际
状态。关闭会取消等待中的通知并保留结果。

### 测试

相关用例覆盖文档发现、激活作用域、验证器别名、Core CLI 单条请求与逐回合装配，以及交互 FIFO、取消、展示、排队回执、本地命令和工具区记录。执行控制用例覆盖 performance 默认值与别名、并行屏障、cache-stable prompt 布局、自动上下文缩减与有界 overflow 重试、回合预算、后台任务、受限程序、resume 投影与修正（`test_core_performance_config.py`、`test_core_performance_entry.py`、`test_core_context_policy.py`、`test_core_background_jobs.py`、`test_core_code_execution.py`、`test_core_resume.py`、`test_core_steering.py`）。会话目录与手动压缩有本地用例，覆盖按天原子取号、标题提炼、只有事件的旧会话、选择持久化、checkpoint 投影、拒绝/失败的摘要、执行中的回合与工具调用配对保留；Core CLI 会话命令由 `test_cli_session_features.py` 覆盖。流式覆盖
fake provider 的 delta、tool_call、usage、done、error，含取消与无 `chat_stream` 回退。provider 层检查让真实 `AsyncOpenAI` 客户端走进程内 mock transport
与 SSE 流，确认完成前可见正文、只发一次 POST、只有一条历史消息，以及 usage/cache 处理；不联系
外部 provider。用量展示、provider usage 明细、流式取消/展示与 gateway 后台运行时
各有本地用例（`test_core_usage_display.py`、`test_provider_usage_details.py`、
`test_core_streaming_cancel.py`、`test_core_streaming_display.py`、
`test_core_background_runtime.py`、`test_core_background_capabilities.py`、
`test_core_scheduler_gateway.py`）。gateway 用例覆盖 route 恢复、冻结 heartbeat 目标、heartbeat
周期串行、单次回复抑制与 scheduler 接线，使用临时 store。这些都是本地测试：不联系外部
provider，也不做真实 channel 投递；cron 与 heartbeat 在本地驱动，本文不承诺具体用例数量。

终端行为用真实的 prompt_toolkit pipe input 和 Vt100 output 验证：session 行在输入框上方、实时行紧贴其上方、忙时隐藏请求竖线与光标并禁键、提交后清除输入提示块、草稿保留、第一次 Esc 提示、第二次 Esc 只取消一次、超时/其他键/方向键清除、菜单 Esc 不取消、resize 与菜单前暂停 reader；不只依赖 mock 的 TTY 开关。测试使用本地 fake provider、fake Forge client 和临时 store；不包含真实 provider 或 Gateway 请求。全量门禁仍是 `pytest`、`ruff check PhyAgentOS tests` 和 `compileall`。

## 14. 测试门禁

```bash
ruff check PhyAgentOS tests
python -m compileall -q PhyAgentOS tests
pytest -q
```

测试应覆盖响应契约、Session ownership、pending/cancel/stop/timeout/unknown、单活动任务、
诊断 Query、不可变 binding、无 POST 恢复、revision recovery、evidence、按版本 episode、归档
攻击、事务回滚、Registry 校验、Runtime health/切换、模拟工作流、CLI 运行时选择、Core 逐回合
装配、交互 FIFO/取消、Skill 文档优先级与激活作用域。具体硬件/仿真测试以独立安装匹配制品和
Dora 可用为条件。

## 后续阅读

- [Forge Tool API 接入契约](../forge/README_zh.md)
- [Bundle 与 Tool 接入参考](../user_development_guide/README_zh.md)
- [Agent 经验与 Skill 自进化](05-agent-experience-and-skill-evolution.md)

## 节点开发与实战示例

- [TrackB 框架说明](../user_development_guide/track_b/README_zh.md)
- [Piper X MuJoCo 二次开发](../user_development_guide/PIPER_X_DEMO_zh.md)

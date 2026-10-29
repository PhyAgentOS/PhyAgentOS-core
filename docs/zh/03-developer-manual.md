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
7. Runtime 制品必须通过有界归档与摘要校验后才能安装。

## 2. 模块地图

| 模块 | 职责 |
|:-----|:-----|
| `agent/core/` | 七个 Core 组件（入口壳、回合、循环驱动、模型适配、提示组装、工具执行、SQLite 事件存储）；契约在 `types.py`、`events.py`、`protocols.py` |
| `agent/composition.py` | `compose_agent`：用注入的协作对象装配 Core，并固定能力顺序 |
| `agent/capabilities/` | Forge 工具的原生定义与操作、到 Core `ToolOutcome` 的映射，以及 `activate_skill` |
| `agent/services/skill_documents.py`、`agent/services/turn_activation.py` | Skill 文档发现与按回合的激活身份 |
| `agent/bootstrap.py`、`agent/services/forge_verifier.py` | 共享的 Forge 构造工厂与语义验证器；`agent_legacy/session_verifier.py` 只保留别名 |
| `cli/commands.py` | 单一 Typer app、全局 callback 与命令组注册，包括唯一的 `agent` 命令 |
| `cli/command_groups/agent_commands.py` | Core 路径 `paos agent --runtime core`：依赖构造、逐回合装配、`CoreAgentSession` 与进程生命周期 |
| `cli/command_groups/`（其余模块） | provider、gateway、Skill、Forge Node、channels 与 onboard/status 命令组，由 `commands.py` 统一注册 |
| `cli/configuration.py` | gateway 与 legacy 路径共用的配置加载和启动选择（provider/evolution） |
| `cli/legacy/legacy_agent.py`、`cli/legacy/legacy_support.py` | 默认的旧 `paos agent`、其终端交互与旧 prompt 会话 helper |
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

## 5. AgentTask 契约与事务

`AgentTaskRecord` 包含稳定 ID、任务描述、`TaskVerificationContract`、状态、只追加
PlanRevision、evidence refs、verification attempts、取消状态和时间戳。每个 PlanRevision 都有
自己的 Tool records、verdict 和 verification attempts。

`AgentTaskStore` 使用 SQLite WAL 与 `BEGIN IMMEDIATE`。创建时在同一事务内查询非终态任务，
保证跨进程全局单活动槽位。更新写入完整的已校验 record 和 append-only event；业务代码不直接
修改表。

公开状态由 [AgentTask 状态契约](../forge/README_zh.md#8-agenttask-模型)定义。Store 转移必须保持这些语义；`unknown` Tool record 结束记账，但不证明物理停止。

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
| `--runtime legacy`（默认） | `cli/legacy/legacy_agent.py` | 现有路径；仍是默认 |
| `--runtime core` | `cli/command_groups/agent_commands.py` | 新 Core 路径；显式启用，缺口见下文 |

`commands.py` 保留 Typer app、全局 callback 和唯一的注册点；各命令组自己持有 Typer app 或 `register_*` 函数。`configuration.py` 存放 gateway 与 legacy 路径共用的配置加载及启动时的 provider/evolution 选择。`agent_commands.py` 只负责 Core 路径：读配置、建依赖、每回合建一次装配、结束时关闭会话；它目前仍持有 `CoreAgentSession` 和 Core 依赖装配，不是单一职责模块。`interaction/interactive_session.py` 调度 Core 输入，`interaction/agent_output.py` 负责消息展示，`interaction/terminal_input.py` 负责 prompt_toolkit 输入和终端状态，`interaction/terminal_display.py` 持有实时状态与工具区，`interaction/model_picker.py` 存放共用的终端选择器。Core 的 prompt 代理使用原始 ANSI 直通，Rich 样式会正常显示，不再变成可见的 `?[36m`；模型、工具和异常文本在渲染前会去掉控制字符，转义序列只由 Rich 产生。

终端只有一个实时区域写入者：prompt_toolkit 的底栏渲染回合阶段（准备、思考、调用工具、本地停止）、转圈和耗时，以及本回合最近 4 次工具调用、隐藏数量和总数。没有第二个 `Live` 或 `status` 写入者，因此输入框、动画和工具区不会互相移动光标。动画只由 prompt_toolkit 的刷新定时器驱动；状态变化只来自已经持久化的事件和会话的提交/取消回调。工具调用按 `(session_id, turn_id, tool_call_id)` 建键，从 `tool.intent` 到 `tool.dispatched` 再到 `tool.settled` 原地更新，重复事件不会增加总数；新回合开始时清空旧行，避免继承上一回合状态。没有 prompt 时（单次 `-m`、启动阶段、重定向流），同一份状态按变化打印普通行，不做动画，也不输出转义序列。`/tools` 按需打印脱敏记录：只有工具名、本地状态和限长错误摘要，不打印参数、结果或凭证。

session/provider/model/effort 一行放在 prompt message 里、输入框上方；底部工具栏只在回合运行中挂上。运行中通过 layout 的 `always_hide_cursor` 隐藏 `You:` 和真实光标，buffer 只读，Enter 被拦截；普通按键不进入 buffer，也不会被偷偷排进模型队列，回合开始前已有的草稿保留，回合结束恢复提示和光标。非终端输入仍保持逐行文本行为。

在终端里，不带参数的 `/model` 与 `/effort` 复用行内选择器。打开第二个 prompt_toolkit 应用前先暂停 reader（结束当前 prompt，且不再启动新 prompt），菜单返回后才恢复；Esc 或 Ctrl+C 只关闭菜单，会话和当前选择都不变。未提交的半行会带入下一条 prompt，不会因菜单丢失。每次模型命令结束（文本、菜单、reset、取消或失败）都从会话实际选择重发工具栏标签。带参数的写法和非终端输入不经过选择器。

legacy 路径的生命周期留在 `legacy/legacy_agent.py`：建 `AgentLoop`，跑单次或终端流程，最后停 loop 并关 MCP；旧 prompt 会话 helper 在 `legacy/legacy_support.py`。下面的步骤只讲新的 Core 路径。

### Core 进程生命周期

1. 读取并校验配置，应用 `--workspace`，拒绝 fleet 模式。
2. 解析 provider/model/effort 选择；选择在回合真正开始时读取，排队中的提交因此会用运行前的最新选择，运行中的回合保留它开始时的选择。
3. 构造 Forge 组件：Skill catalog 与 runtime availability、`ForgeToolClient`、`AgentTaskCoordinator` 和可选验证器。
4. 在这个 coordinator 上做一次 Forge 未知 Action/Session 恢复，且在任何一个回合被提交之前完成。`reconcile_nonterminal()` 只发 Gateway GET 查询；不重发 Action 或 Session 的 POST，也不把仍未确认的 unknown 改成终态。Core 没有交互 supervisor，因此这里不会请求机器人停止。恢复本身失败时会报告一次并继续打开会话；未解决的 unknown 仍然阻塞新任务。
5. 创建 `SkillDocumentLoader` 与 `TurnSkillActivationManager`，把激活管理器注册给 coordinator，使 `forge_task_create` 能校验 `activation_id`。
6. 打开会话存储 `<workspace>/sessions/core.db`。
7. 用 `compose_agent` 建种子装配；`SkillAccessCapability` 在前，`ForgeCapability` 在后。
8. 跑单条消息或交互循环。每回合在真正开始时读取该 session 的选择并新建面向模型的装配；这份装配只服务一个回合，回合结束立即 `runtime.stop()`（不关 host），不再等下次 submit。store、Forge coordinator/client 与激活管理器保持共享。同一 session 的提交按登记顺序串行，不同 session 可并发；`cancel` 只取消当前装配，排队中的回合不会继承这次取消。
9. 只关闭一次：先拒绝新提交并让排队中的提交在开回合前退出，再停仍可能持有回合的 runtime，等已进入本 session 的提交返回，对每个已接受的 session 通知各能力，关闭规范能力一次，最后关 store。Forge client 与验证器由资源栈在会话之后关闭。

只要还有更早的 Action/Session 调用处于 unknown，`forge_task_create` 就拒绝建新任务，跨 session 的旧任务也算在内。拒绝结果会给出权威的 `task_id`、已确认的 `invocation_id`（Gateway 身份从未确认时明确写 `None`）、记录状态与语义，以及该用哪个 status 工具查询；不会建议去试列表工具，不会重发旧请求，也不会建议删 store 或靠重启清除。

运行中只用 Esc 停止：第一次 Esc 进入两秒窗口并显示黄色提示，第二次 Esc 通过原有 session cancel 路径请求本地取消。超时、其他按键、方向键或新回合都会清除已 arm 状态，单次 Esc 和方向/Alt 组合不会停止任务；模型菜单里的 Esc 只关闭菜单。取消会请求 Forge 取消该 session 的最新任务，但不能证明远端动作已停止；状态明确写"本地"和"远端未确认"。本地命令（`/help`、`/tools`、`/correct`，以及被拒绝的未知斜杠命令）由 reader 立即处理；`/stop` 只返回提示，不取消也不发模型。非终端输入捕获不到 Esc，只能靠 Ctrl+C。普通文本和模型命令仍按 FIFO 顺序，模型命令只在回合之间执行；程序注入的排队消息只回执序号和处理通知，不回显内容。

`agent/services/forge_verifier.py` 是两条路径共用的唯一验证器实现；`agent_legacy/session_verifier.py` 只是重新导出，原有 import 和模块 patch 仍然生效。

### Skill 激活作用域

`activate_skill` 是 `agent/capabilities/skill_access_capability.py` 提供的 Core 工具。一次激活只属于一个 `session_id` 和一个 `turn_id`；上一回合、其他 session 或上一个进程的 id 都会被拒绝。激活记录只在内存里，重启后必须重新激活。一回合允许一个 primary Skill 和任意多个 supporting Skill。激活只读 Skill 文档，不启动 Skill runtime。

### Core 路径未覆盖的部分

通用工具、流式回复、`/correct`、memory 与 evolution、managed interaction Sessions、fleet 模式，以及 cron、heartbeat、channels 入口都还没迁移。Forge interaction supervisor 没有接进 Core CLI，因此不能把交互迁移说成已完成。旧 JSONL 会话不导入；Core 使用独立的 SQLite 文件。Provider 错误只给简短的脱敏诊断：保留异常类型和 HTTP 状态，不回显响应体与凭证。

### 测试

相关用例覆盖文档发现、激活作用域、验证器别名、Core CLI 单条请求与逐回合装配，以及交互 FIFO、取消、展示、排队回执、本地命令和工具区记录。终端行为用真实的 prompt_toolkit pipe input 和 Vt100 output 验证：session 行在输入框上方、忙时隐藏 You 与光标并禁键、草稿保留、第一次 Esc 提示、第二次 Esc 只取消一次、超时/其他键/方向键清除、菜单 Esc 不取消、resize 与菜单前暂停 reader；不只依赖 mock 的 TTY 开关。测试使用本地 fake provider、fake Forge client 和临时 store；不包含真实 provider 或 Gateway 请求。全量门禁仍是 `pytest`、`ruff check PhyAgentOS tests` 和 `compileall`。

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

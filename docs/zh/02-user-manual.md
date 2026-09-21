# PhyAgentOS 用户手册

[English](../en/02-user-manual.md) · [文档索引](../README.md)

> 文档版本：1.0.0。

## 1. 安装与初始化

PhyAgentOS 支持 Python 3.11 和 3.12。Forge Gateway、Dora、机器人驱动、仿真资产与锁定 Node
制品在机器人 Skill 需要时独立部署。

```bash
git clone https://github.com/PhyAgentOS/PhyAgentOS-core.git
cd PhyAgentOS-core
python -m pip install -e .
paos onboard
```

开发环境：

```bash
python -m pip install -e ".[dev]"
pytest
ruff check PhyAgentOS tests
```

默认配置位于 `~/.PhyAgentOS/config.json`，默认工作区为
`~/.PhyAgentOS/workspace`。

### 托管 Skill profile 所需的 Dora CLI

运行通用 Agent、搜索 Skill 或完成 `paos skill install` 不需要 Dora。`paos skill start` 需要
主机预先安装 Dora CLI，因为 RuntimeManager 使用 `dora` 命令管理所选 profile。PhyAgentOS
1.0.0 以 Dora CLI v0.4.1 及 `dora-message` v0.7.0 作为 Forge Skill 兼容基线；可复现部署
应固定该精确版本。

Linux 或 macOS 使用带版本的官方 installer：

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/dora-rs/dora/releases/download/v0.4.1/dora-cli-installer.sh | sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://github.com/dora-rs/dora/releases/download/v0.4.1/dora-cli-installer.ps1 | iex"
```

已经安装 Rust toolchain 时：

```bash
cargo install dora-cli --version 0.4.1 --locked
```

installer 修改 `PATH` 后请打开新 shell，再验证 executable：

```bash
dora --version
# dora-cli 0.4.1
# dora-message: 0.7.0
```

RuntimeManager 会检查兼容命令接口，但不会强制语义版本，也不会自动升级 Dora。运维人员必须
保证 CLI、coordinator、daemon 与锁定 Skill Node 处于同一协议代际。当前已发布 Forge Skill
Node 使用消息格式 v0.7.0；Dora v0.5.0 使用 v0.8.0，会在注册阶段拒绝这些 Node。

Python package `dora-rs` 是 Python node/operator API，不能替代 Dora CLI 安装。Coordinator
和 daemon 尚未运行时，`dora check` 报告不可用属于预期行为。`paos skill start` 会执行该检查，
并在需要时通过 `dora up` 启动服务；Runtime 启动后，`dora check` 应当成功。各平台细节见
[Dora v0.4.1 官方 Release](https://github.com/dora-rs/dora/releases/tag/v0.4.1)中的平台制品与
校验值；不要使用可能选择更新 CLI 的无版本 installer。

## 2. 配置模型与 Forge

无需编辑 JSON，即可管理已注册的 Provider：

```bash
paos provider list                    # 普通表格；加 --json 输出机器可读结果
paos provider configure               # TTY 中打开行内键盘选择器
paos provider configure openrouter    # 输入密钥和 endpoint，测试后获取并多选模型
paos provider show                    # 选择供应商，脱敏查看
paos provider test                    # 选择供应商，发送最小测试请求
paos provider use                     # 选择供应商及已保存模型，设为默认
paos provider use openrouter --model anthropic/claude-sonnet-4
paos provider login                   # 选择 OAuth 供应商并登录
paos provider remove                  # 选择供应商并清除存储的配置
```

除 OAuth login 外，以上管理命令都支持 `--config /path/to/config.json`。
在 TTY 中，`configure`、`show`、`test`、`use`、`remove`、`login` 省略供应商名称时会
打开相应选择器。也可直接传名称（不区分大小写）；非交互环境必须显式指定名称。
向导用上下键选择、Enter 确认；Esc 或 Ctrl+C 取消时不保存。仅当 stdin、stdout 均为 TTY 时
启动，不进入全屏页面。OAuth 状态只代表本地存在凭据，实际有效性请用 `provider test` 验证。
`remove` 不撤销共享 OAuth token，也不修改已有进程使用的环境变量。
`remove` 会持久化清空该供应商的 API Key、API Base、Header、默认模型和模型列表，保留
供应商字段并标记 `enabled: false`，防止环境变量或共享登录凭据将它自动恢复。
`list` 和 `configure` 中仍可看到该内置供应商，但状态为未配置；`use`、`test` 的选择器不再列出它。
移除当前默认供应商时也会清空全局供应商、模型和推理设置，请用 `provider use` 重新选择。
重新执行 `provider configure` 并成功保存后恢复启用。Ollama 的内置地址仅作为配置建议，
不会使空配置自动变成已配置。

输入 API Key、API Base 和可选 Header 后，向导会询问是否测试连接并获取模型列表。
确认后用上下键定位、空格多选要添加的模型，再按 Enter 确认并选择默认模型。
已添加模型保存在该 Provider 的 `models` 列表中，再次配置时可继续添加。
获取列表不会发送聊天请求；要验证具体模型能否对话，请执行 `provider test`。
跳过测试或接口不支持模型列表时，可选择已保存模型或手动输入模型 ID；OAuth 和 Azure
目前使用此方式，Azure 需填写部署名称。认证或网络失败时不保存本次配置。
`paos provider use <provider>` 在 TTY 中会展示已保存模型供选择，非交互环境仍可用 `--model`。
`provider show` 展示已保存模型，长 API Key 显示前后各 4 位，中间隐藏；短密钥全部隐藏。

Docker、CI 和远程服务器应明确指定 Provider、模型，并通过 stdin、环境变量或挂载的
Secret 文件传入密钥。不提供明文 `--api-key` 参数：

```bash
paos provider configure openrouter --model anthropic/claude-sonnet-4 \
  --api-key-stdin < /run/secrets/openrouter_api_key
paos provider configure openrouter --model anthropic/claude-sonnet-4 \
  --api-key-env OPENROUTER_API_KEY
paos provider configure custom --api-base http://localhost:8000/v1 --model gpt-5 \
  --api-key-file /run/secrets/custom_api_key --headers-file /run/secrets/headers.json
```

`--headers-file` 读取键和值均为字符串的 JSON 对象。向导隐藏 API Key 和 Header 值，错误信息
不回显服务端响应正文。启动时也可通过 `PAOS_<PROVIDER>_API_KEY` 提供凭据，例如
`PAOS_OPENROUTER_API_KEY`；支持对应供应商的标准变量，如 `OPENAI_API_KEY`、
`ANTHROPIC_API_KEY`。网关别名不会借用 OpenAI 的密钥。
运行时环境凭据不落盘，只有明确配置该 Provider 时才保存。

启动参数只影响新进程：

```bash
paos agent --provider openai --model gpt-5 --reasoning-effort high
paos gateway --provider openrouter --model anthropic/claude-sonnet-4
```

Agent 内 `/help` 列出全部命令。使用 `/provider [list|<provider>]`、
`/model [list|<编号>|<model-id>]`、`/effort [list|<档位>|none]` 和 `/status` 查看或切换当前会话。
终端聊天中直接输入 `/model` 会打开模型选择器，展示所有已配置供应商下保存的模型并标注
供应商。上下键选择、Enter 确认后立即切换当前会话的供应商和模型，下一条消息使用新选择。
Esc 或 Ctrl+C 取消选择并返回聊天。`/model list` 只查看列表；非终端渠道保留文字列表与编号选择。
`/model list` 展示当前 Provider 已保存模型及编号，例如 `/model 2` 选择第 2 个模型。
旧配置中的默认模型也会保留为选项。列表不代表已经验证聊天可用；不兼容的模型路由、
思考程度会被拒绝，保留原选择。`none` 表示移除显式思考程度，采用模型默认行为。
Custom endpoint 或 Azure 部署别名的能力未知时应使用 `none`；显式指定思考程度需要可识别的
推理模型名称。

终端聊天输入 `/effort` 显示行内列表：上下键移动、Enter 确认、Esc / Ctrl+C 取消。
`/effort list` 查看当前模型可选档位。菜单和启动校验使用同一套供应商及模型能力判断，
从 `minimal / low / medium / high / xhigh / max` 中仅展示支持的档位。
额外档位需要本地目录明确支持且适配器可映射；网关可选档位可能与原厂接口不同。
支持 `max` 的自适应 Claude 使用 `output_config.effort`，传统 Claude 使用思考预算。
`none` 始终表示模型默认行为，不代表关闭思考。详见[推理强度](reasoning-effort.md)。

优先级为 **会话覆盖 → 进程启动参数 → 配置默认值**。每轮请求及其重试、工具调用固定使用
开始时的 Provider、模型和思考程度。切换只影响同一会话的后续请求；其他会话、正在运行的
子 Agent、Forge 任务、verification、evolution、记忆整理、Cron 和 Heartbeat 继续使用启动配置。
切换不会重启或停止进程，也不会改写全局默认值。`/provider reset` 清除会话覆盖；`/new` 只清理
会话上下文，保留当前选择。会话覆盖在进程退出后失效。永久默认值请通过 `paos provider use`
设置，只影响未来启动的进程。

使用现有 Docker CLI service：

```bash
docker compose run --rm -T phyagentos-cli provider configure openrouter \
  --model anthropic/claude-sonnet-4 --api-key-stdin < /path/to/secret
docker compose run --rm -T phyagentos-cli provider use openrouter
docker compose up -d phyagentos-gateway
docker compose run --rm phyagentos-cli agent --provider openai --model gpt-5 --reasoning-effort high
```

先配置一个模型 Provider 和 Forge timeout/evidence policy。Runtime 不是配置开关，而由显式
启动的 Skill profile 决定。配置以 camelCase 保存，也接受 snake_case。

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.PhyAgentOS/workspace",
      "model": "openrouter/openai/gpt-4o-mini",
      "provider": "openrouter"
    },
    "verification": {
      "serviceEnabled": true,
      "evidenceRetention": "failed",
      "maxReplansPerEpisode": 2
    },
    "evolution": {
      "enabled": true,
      "minSuccessfulEpisodes": 3,
      "minLessonEpisodes": 3
    }
  },
  "providers": {
    "openrouter": {"apiKey": "YOUR_API_KEY"}
  },
  "forge": {
    "requestTimeoutS": 10,
    "pollIntervalS": 0.5,
    "executionTimeoutS": 300,
    "evidence": {
      "requiredImageSources": ["front"],
      "associationQuality": "best_effort"
    }
  },
  "resourceRegistry": {"url": "https://paos-resource-manager.dev.x-era.com"}
}
```

活动 Skill Runtime manifest 是 Gateway URL 的唯一来源。也可以通过
`PAOS_RESOURCE_REGISTRY_URL` 提供 Registry URL；空 URL 只允许本地 Bundle 或显式静态
index。启动 PAOS 不会下载 Skill。

## 3. 安装并运行 Skill Runtime

使用已配置 Registry 或 schema v3 静态 package index：

```bash
paos skill search <skill-name>
paos skill install <skill-name> --version <version>
# 或：paos skill install <skill-name> --index /path/to/index.json
# 或：paos skill install /path/to/<skill-name>-<version>.tar.gz --local

paos skill list
paos skill inspect <skill-name>
paos skill start <skill-name> --profile <profile>
paos skill status <skill-name>
paos skill switch <other-skill-name> --profile <profile>
```

`install` 校验归档大小、SHA-256、内嵌文件清单、manifest v2 与锁定 Node，全部通过后才原子
替换 Skill。Registry 省略重复的 Node 摘要字段时，以已验证 Skill lock 中的摘要为准，并在传输
前解析 Node 下载大小。公网 Registry 按 Skill 名称解析当前制品；`--version` 会在下载 Bundle 后、
下载 Node 或提交安装前校验 manifest 版本。

Bundle 可以在 Dora 启动前提供 `start.sh`；PAOS 以
`bash <bundle>/start.sh <skill-name> <skill-version>` 执行并继承终端 stdio。此类 Bundle 要求
`PATH` 中存在 Bash；Bash 缺失或钩子非零退出会记录为 `failed`，且不会启动 Dora。无钩子
Bundle 继续使用常规跨平台启动路径。外部资源下载等长时间钩子执行期间状态保持 `starting`；
钩子成功后，PAOS 才启动指定 Dora profile，并检查 Gateway `/tools` 与全部 required Tool
context。同一 Skill 的重叠生命周期变更会立即报忙。
使用 `paos skill logs <skill-name>` 查看生命周期日志，使用
`paos skill stop <skill-name>` 停止。存在非终态 AgentTask 时 `switch` 会拒绝执行；目标 Runtime
通过就绪校验后才会被选中，共用 Gateway 的目标启动失败时会恢复先前 Runtime。运行中的 Agent
会在下一次 activation 或 Forge Tool 调用前跟随持久化选择。

Node 制品可以独立管理：

```bash
paos forge-node install <skill-name> <node-id>
# 或安装独立获取的本地归档
paos forge-node install <skill-name> <node-id> --archive /path/to/<node>.tar.gz
paos forge-node verify <skill-name> <node-id>
```

具体 Forge Skill、Node、模型与仿真资源独立于 PhyAgentOS 分发，仅在需要时安装。

## 4. 启动 PAOS

使用已安装机器人 Skill 时，先启动托管 Skill Runtime/Gateway，再启动 Agent。
`paos skill start` 检查 Dora CLI，并在本地 Dora coordinator 和 daemon 尚未 ready 时自动启动。
Agent 只从显式活动 Skill 的 manifest 获取 Gateway URL。

```bash
paos status
paos skill start <skill-name> --profile <profile>
paos skill status <skill-name>
paos agent

# 单条请求
paos agent -m "检查运动 Tool context，将夹爪向前移动 5 cm，并验证结果。"

# 长期运行消息渠道、Cron、Heartbeat 与 Agent
paos gateway
```

## 5. 检查 Tool context

调用 Tool 前使用 `forge_tool_context(tool_id)`。它返回 ToolSpec，以及实时 binding、
readiness、endpoint status 和机器人 frame 信息。Agent 必须遵循精确 input schema，不能猜测
frame 或单位约定。

活动 Skill 工作流声明允许的 Tool ID 以及对应 Query、Action 或 Session semantics；不得用
binding 中不存在的相似 Tool 替代。

## 6. 使用诊断 Query 或绑定执行

诊断 Query 使用同一 Tool API，但不计入用户任务验证：

```text
forge_tool_query(tool_id, arguments)
```

对于用户可见的多调用任务：

1. 在本轮调用 `activate_skill(name, role="primary")`；
2. 把 activation ID 传给 `forge_task_create(task_description, verification, activation_id)`；
3. 把返回的 `task_id` 传给所有相关 Query、Action 或 Session；
4. 按返回的 `invocation_id` 核对每个异步执行；未知 admission 后不得猜测 ID 或重复调用；
5. 停止 task-owned Session，再调用 `forge_task_finalize(task_id)`。

全局最多一个非终态 AgentTask。诊断 Query 不占此槽位，所有执行仍按 Gateway operation 的
`max_concurrency` 竞争。

## 7. 定义验证

`audit`、`enforce` 或 `recovery` 必须提供 goal 和至少一项 success criterion：

```json
{
  "mode": "recovery",
  "goal": "夹爪相对初始位姿向前移动 5 cm。",
  "success_criteria": [
    "末端最终位姿在声明 frame 中近似向前 5 cm。",
    "机器人未报告碰撞或移动失败。"
  ],
  "constraints": ["保持末端方向不变。"],
  "evidence_policy": {
    "required_kinds": ["rgb_image"],
    "required_sources": ["front"],
    "minimum_association": "best_effort"
  }
}
```

| 模式 | 行为 |
|:-----|:-----|
| `off` | 根据绑定 Tool execution facts 派生任务结果。 |
| `audit` | 记录语义验证，同时保留执行派生结果。 |
| `enforce` | 语义验证决定成功；缺失或非法验证时 fail closed。 |
| `recovery` | 与 enforce 相同；`replan_required` 允许有预算的新 PlanRevision。 |

finalize 返回 `awaiting_replan` 时调用
`forge_task_begin_revision(task_id, reason)`，并继续使用同一 task ID。不要创建第二个任务，也
不要重试物理效果未知的 invocation。

## 8. 取消与 unknown 结果

`forge_tool_cancel_action(invocation_id)` 发出取消请求。`requested` 或 `accepted` 只确认控制
消息处理；应继续读取 status/result，直到 Gateway 报告已知终态。`unknown` 和本地 timeout
可用于任务记账终结，但不能证明物理停止。

`forge_task_cancel(task_id, reason)` 为全部非终态绑定 Action 请求取消，并将任务置为
`cancelling`。随后应核对 invocation，必要时检查现场，再显式 finalize。存在不确定 invocation
时 Runtime stop 保持门控，除非运维人员明确 force。

## 9. Experience、activation 与 evolution

注册 Skill 与工作流匹配时，在第一次工作流工具调用前使用 `activate_skill(name, role)`。
Skill 发现优先级为 workspace、已安装、内置；Runtime availability 参与激活资格判断。

Experience 记录全部 Agent tool calls，并把 AgentTask、PlanRevision、invocation 引用、验证和
显式 Skill activation 归入一个 episode。Scoped Lesson 只是建议，不能替代任务 criteria 或
evidence。Evolution fail-open，反思错误不会改变执行或验证。

## 10. 持久化与 retention

```text
<workspace>/
├── .paos/agent_tasks/tasks.sqlite3
├── .paos/evolution/experience.sqlite3
├── .paos/evolution/revisions/<skill>/
├── skills/<skill>/
└── artifacts/agent_tasks/<task_id>/
    ├── before_snapshot.json
    ├── after_snapshot.json
    ├── evidence_bundle.json
    └── evidence/
```

备份时先停止 PAOS，将 SQLite 及 WAL/SHM 文件、完整 artifact 和 Skill revision 目录一起备份。
`evidenceRetention` 控制验证后的证据保留，不删除 execution record 或 evolution 历史。

## 11. 故障排查

| 现象 | 检查项 |
|:-----|:-------|
| Tool 不存在或未就绪 | 运行 `forge_tool_context`，检查 ToolSpec、binding、Endpoint 与 Runtime profile。 |
| Skill 无法安装 | 确认 Skill Bundle 元数据包含 size、SHA-256，每个 Node lock 包含 SHA-256，且 Registry/index Node 能解析为具有明确大小的直接下载。 |
| Skill 无法启动 | 运行 `paos skill status` 与 `logs`，检查 Dora、dataflow、assets、nodes 和 Gateway `/tools`。 |
| Dora 报告消息格式 v0.7.0 与 v0.8.0 不匹配 | 停止不匹配的 coordinator/daemon，安装 Dora CLI v0.4.1，确认 `dora-message: 0.7.0` 后重新启动 Skill。 |
| 已有活动任务 | 使用 `forge_task_get` 读取已知任务，完成或取消它，不要编辑 SQLite。 |
| Action result 为 pending | 使用相同 `invocation_id` 继续核对 status/result。 |
| Action result 为 unknown | 检查 Gateway、Dora 与物理现场，不要盲目重试。 |
| Verification 失败 | 检查任务 criteria、Tool records、evidence bundle 与 verifier availability。 |
| 未加载 Skill Lesson | 确认显式 activation、Runtime availability 和符合条件的 active scoped Lessons。 |

## 后续阅读

- [Forge 配置参考](04-forge-configuration-reference.md)
- [运行手册](../user_manual/README.md)
- [Forge Tool API 接入契约](../forge/README_zh.md)

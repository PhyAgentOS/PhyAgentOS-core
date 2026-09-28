# PhyAgentOS 集成开发指南

[English](README_en.md) · [文档索引](../README.md)

> 版本：1.0.0

## 1. 选择接入点

| 能力 | 接入点 |
|:-----|:-------|
| 机器人读取或计算 | Gateway Query ToolSpec + ToolEndpoint operation |
| 机器人物理效果 | Gateway Action ToolSpec + ToolEndpoint operation |
| 有状态能力生命周期 | Gateway Session ToolSpec + ToolEndpoint operation |
| Dora nodes 与部署资产 | manifest v2 Skill Bundle 与锁定 Node artifacts |
| 工作流说明 | 由 SkillsLoader 发现的 `SKILL.md` |
| 用户任务成功 | 通用 `TaskVerificationContract` 与 AgentTask finalize |
| 新模型 Provider | 现有 provider registry/configuration |
| 非机器人 Agent 能力 | 现有 Agent ToolRegistry 或动态 MCP |

不要把 Agent 代码直接连接到机器人 SDK、Dora node、仿真器，或统一 Tool API 之外的旧式
Gateway Session/Policy route。

## 2. 定义 ToolSpec

每个 ToolSpec 包含稳定 `tool_id`、implementation/endpoint binding、operation、
`semantics: query|action|session`、description、严格 input/output JSON schema、readiness，以及空间
输入所需 robot frame profile。

```yaml
tool_id: motion.resolve_relative_pose
implementation_id: motion.integration
endpoint_id: motion.relative_pose
operation: resolve
semantics: query
description: Resolve a relative end-effector delta into an absolute target pose.
input_schema:
  type: object
  additionalProperties: false
  required: [translation_frame, translation_m]
  properties:
    translation_frame: {enum: [tcp, base]}
    translation_m:
      type: object
      additionalProperties: false
      required: [x, y, z]
      properties:
        x: {type: number}
        y: {type: number}
        z: {type: number}
output_schema:
  type: object
robot_frame_profile:
  base_frame: arm_base
  tool_frame: tcp
```

同步读取或不产生机器人效果的确定性解析使用 Query；有界物理效果使用 Action；显式有所有权
的有状态生命周期使用 Session。在执行方
定义 Endpoint operation `max_concurrency`，PAOS 不创建跨 Tool lease。

## 3. 实现 Query、Action 与 Session 行为

Query 从 ToolSpec 解析并调用：

```text
POST /tools/{endpoint_id}/{operation}:invoke → HTTP 200
```

Action admission 使用：

```text
POST /tools/{tool_id}:invoke → HTTP 202 + invocation_id + attempt_id
GET  /invocations/{invocation_id}
GET  /invocations/{invocation_id}/result
POST /invocations/{invocation_id}/cancel
```

Session admission 使用同一 `POST /tools/{tool_id}:invoke` 契约，通过通用 invocation routes
核对，并以 `POST /invocations/{invocation_id}/stop` 停止。必须声明 task-owned、shared 或
runtime-owned；一个 owner 不得停止另一个 owner 的 Session。

Action status/result 必须暴露明确生命周期；result pending 时可返回 HTTP 202。Cancel accepted
只表示控制处理。无法恢复执行事实时应返回显式 unknown，不能伪造 cancelled 或 success。

Input/output 必须是有限 JSON 并满足 ToolSpec。空间 Tool 必须说明 frame、unit、tolerance 与
orientation behavior，避免 Agent 无法通过 `forge_tool_context` 检查的隐藏默认值。

## 4. 构建 manifest v2 Skill Bundle

已安装 Skill Bundle 包含：

```text
<skill>/
├── skill.yaml
├── SKILL.md
├── start.sh                    # 可选，Dora 启动前准备外部资源
├── archive-manifest.json       # 打包脚本生成
├── profiles/<profile>/dataflow.yaml
├── profiles/<profile>/...
└── assets/...
```

最小 manifest 结构：

```yaml
manifest_version: 2
name: example-skill
version: "1.0.0"
description: Example robot workflow.
skill_document: SKILL.md
gateway_url: http://127.0.0.1:19002
required_tools: [example.query, example.action]
profiles:
  sim:
    dataflow: profiles/sim/dataflow.yaml
    required_binaries: [gateway, example_node]
    required_assets: [assets/scene.xml]
    required_environment: []
    environment: {}
artifacts:
  resolver: registry
  nodes:
    gateway:
      artifact_id: gateway-1.0.0-linux-x86_64
      version: "1.0.0"
      platform: linux
      arch: x86_64
      artifact_type: executable_tar_gz
      entrypoint: gateway
      sha256: <64-character-sha256>
```

所有路径必须相对并包含在 Bundle 内。每个 Node archive 具有 lock 指定的 SHA-256。
`executable_tar_gz` 只包含一个 lock 指定文件名的根目录 executable；`directory_tar_gz`
只包含一个以 `entrypoint` 命名的根目录，同名 executable 及其运行时树置于其中（仅允许
解析不出树的相对符号链接）。installer 在 receipt 中另行记录解包后 binary hash。
Bundle archive inventory 需要覆盖每个文件及 SHA-256；links、路径穿越、冲突、过度展开和未列出
内容会被拒绝。

Bundle 如需在启动前下载权重或准备其他外部资源，可在根目录提供 `start.sh`。PAOS 使用
`bash <bundle>/start.sh <name> <version>` 调用它，不改变工作目录并继承终端 stdio；脚本应从
自身路径解析 Bundle 内文件，支持重复执行，并在失败时返回非零退出码。此类 Bundle 要求主机
`PATH` 中存在 Bash。`PAOS_SKILL_NAME` 与 `PAOS_SKILL_VERSION` 可用于 dataflow 占位符，
也会进入 Dora 进程环境。

## 5. 打包、发布与本地闭环

### 5.1 构建并验证 Bundle

仓库脚本会重新生成 `archive-manifest.json`，使用固定元数据构建确定性归档，再通过
`ArchiveValidator` 安全解包复核：

```bash
python scripts/package_skill.py /path/to/example-skill --output-dir dist/skills
```

输出文件名取自 `skill.yaml` 的 `name` 与 `version`，并打印归档 SHA-256 和字节数。已有同名
输出默认不覆盖；仅在确认尚未发布时使用 `--force`。打包脚本拒绝 links，并排除版本控制、缓存
与 `node_modules` 目录；发布源码目录不得包含凭据、预签名 URL、本机缓存、日志或运行状态。

上传前使用与用户相同的公开命令完成本地闭环：

```bash
paos skill install dist/skills/example-skill-1.0.0.tar.gz --local
paos forge-node verify example-skill gateway
paos skill inspect example-skill
paos skill start example-skill --profile sim
paos skill status example-skill
paos skill stop example-skill
```

本地 Bundle 与 Registry Bundle 使用相同的归档、manifest 和 Node lock 校验；缺失 Node 仍需
通过配置的 Registry 或静态 index 解析。Installer 先 staging、校验，再原子替换，失败则
rollback。不得要求调用方关闭摘要校验。

### 5.2 不可变发布顺序

1. 先发布并登记所有 Node artifacts。`executable_tar_gz` 归档根目录只能包含一个与
   `entrypoint` 同名的 executable；`directory_tar_gz` 归档根目录只能包含一个与
   `entrypoint` 同名的目录（内含同名 executable 与运行时树）。最终归档 SHA-256 必须写入
   Skill lock。
2. 固定 `skill.yaml` 的 name/version、profiles 与 Node locks，执行打包，并保存输出的 Bundle
   SHA-256 与 `size_bytes`。
3. 将 Bundle 上传到不可覆盖、长期有效的 HTTPS 对象键。上传后从最终 URL 回读并重新校验
   SHA-256 与大小；修正已发布内容必须递增版本，不能覆盖原对象。
4. 在 Resource Registry 登记当前 Skill 的 name、URL、SHA-256 与大小，并保证每个 Node
   `artifact_id` 均可通过 Node 端点解析。也可以发布等价的 schema v3 静态 index。
5. 从干净 PAOS HOME 通过 Registry 重跑安装、启动、状态和停止命令，确认没有依赖源码仓绝对
   路径或开发机缓存。

公网 Registry 按名称返回当前 Skill 条目，不提供历史版本子路径。`paos skill install
<name> --version <version>` 中的版本是客户端约束：Bundle 下载后先校验 manifest version，
不匹配时在 Node 下载和安装提交前失败。因此旧版本必须通过不可变 URL、静态 index 或本地归档
另行保存，不能把 `--version` 当作 Registry 历史版本查询。

## 6. 设计 Dora profile

当前分发的 Forge Skill profile 应使用 Dora CLI v0.4.1 与 `dora-message` v0.7.0 开发和验收。
Skill lock 与主机基线整体升级前，Node 构建必须保持在同一协议代际。

Dataflow 为每个 Node 定义明确 inputs/outputs，并使用 Gateway profile 声明的 Tool request/
response ports。必需 executable 从不可变 Runtime environment 解析；assets 保留在 Skill Bundle
中并使用可重定位路径。

RuntimeManager 创建确定 flow name，校验 Dora 与必需文件，启动 flow，再等待 Gateway
`/tools` 与全部 required Tool context。Manifest URL 已有 Gateway 监听时不会静默接管。

Tool API 作为物理执行面时，应在 profile 禁用 Gateway Agent API：

```yaml
agent:
  enabled: false
tools:
  enabled: true
```

## 7. 编写工作流说明

`SKILL.md` 应说明何时激活、检查哪些 context、Query → Action/Session 顺序、task binding、
ownership、终态核对、
verification checkpoint 与安全恢复规则。不得嵌入 secret、Registry URL、任务特定坐标或绕过
Gateway/verification 的指令。

验证型工作流应在本轮激活 primary Skill，再由 activation 创建一个 AgentTask，把全部相关
Query/Action/Session 绑定到同一 task，全部 task-owned 执行终结后 finalize，并只在 recovery
verdict 允许时追加 PlanRevision。

## 8. Evidence 与 verification

机器人能力接入应暴露 Tool execution facts，而不是编写 action-specific verifier code。PAOS 在
AgentTask finalize 时采集配置的 image/state source 并应用通用 verification contract。
Tool output schema 应包含有用的终态 result semantics、final state/error 和相关 tolerance。

未来若引入 authoritative evidence，应显式升级 evidence contract；不能用约定把 best-effort
WebSocket association 提升为权威。

## 9. Fake Gateway 与 conformance 测试

进入真机或仿真前，使用 mock HTTP transport 测试：

- Tool list/spec/context 与 Query binding resolution；
- activation candidate 复核以及 ToolSpec/Runtime 漂移拒绝；
- Action HTTP 202 admission 及 invocation/attempt identity；
- Session admission、ownership、status/result 与 stop；
- pending status/result 与已知终态；
- cancel requested/accepted 不产生虚假停止；
- timeout/unknown 不盲目重试；
- endpoint concurrency rejection；
- 诊断 Query 与绑定调用经过相同 routes；
- AgentTask 单活动限制、revisions、evidence 与聚合 verification；
- archive traversal/link/collision/digest 攻击与事务 rollback；
- 有/无 `start.sh` 的启动、Bash 缺失与钩子非零退出；
- Skill identity 注入 dataflow/Dora 环境，以及 profile 内容或 dataflow 路径变化后的重新物化；
- 同一 Skill 的 start/stop/install/remove 跨进程冲突；
- Runtime start/status/log/stop 与 availability 传播。

随后完成模拟工作流。真实机器人或 MuJoCo 验收必须记录确切 Bundle、node digests、profile 与
环境。

## 10. 接入验收清单

- [ ] Tool semantics 与 schema 明确、严格；
- [ ] Frame、unit、tolerance 与 readiness 可检查；
- [ ] Gateway operation 负责 `max_concurrency`；
- [ ] Query/Action/Session 使用文档 HTTP 契约；
- [ ] Skill/Runtime/ToolSpec binding 被冻结，并在每次受治理执行前复核；
- [ ] Invocation/attempt ID 与 PAOS task ID 分离；
- [ ] Cancel、stop、timeout、unknown 不推断物理停止，也不触发盲目 POST 重试；
- [ ] Bundle/Node artifacts 有不可变 size/digest metadata；
- [ ] Bundle 经仓库打包脚本和本地安装闭环验证，Registry 的 Skill 与全部 Node 端点可解析；
- [ ] 可选启动钩子的参数、失败状态、重复执行与外部资源摘要经过验证；
- [ ] Runtime profile 从干净环境启动并使全部 Tool context ready；
- [ ] Tool-only profile 禁用 Gateway Agent API；
- [ ] 通用 Agent tools、verification、experience、evolution 不需要能力专用分支。

## 后续阅读

- [开发者手册](../zh/03-developer-manual.md)
- [通信架构](COMMUNICATION.md)
- [Forge Tool API 契约](../forge/README_zh.md)

# Agent 经验与 Skill 自进化

> 文档版本：1.1.1。本文描述基于 AgentTask record 的 Agent 校验、经验、Lesson 与 Skill 演化链。

## 1. 目标与边界

PhyAgentOS 在任务/工作流粒度学习，而不是从某一次孤立 tool call 直接学习。一个已经终结并具有语义验证结果的 AgentTask 及其全部 PlanRevision 可以形成一个 `TaskEpisode`，后台反思再用它支持可复用 Skill 工作流或作用域 Lesson。

```text
Skill 激活
  → AgentTask + PlanRevisions + ToolInvocations
  → 任务语义结果
  → 去敏 TaskEpisode
  → 后台异步反思
      ├─ 可复用成功工作流 → SkillCandidate
      └─ 工作流相关失败   → FailureObservation → LessonCluster
```

该链路不修改 AgentTask 状态、Forge Tool calls、Gateway payload、证据采集、recovery 顺序或下层执行。初始化、持久化、模型调用、校验或写入失败时，evolution fail-open，原任务结果保持不变。

首个 outcome provider 是 Forge。`TaskOutcomeSource` 协议允许后续 Runtime 产生相同 `TaskOutcomeEnvelope`，而无需修改 Lesson 或 Skill 演化逻辑。

## 2. Skill 激活与任务归因

`agents.evolution.enabled=true` 时，Agent 上下文包含 Skill 摘要，并要求在首次工具调用前检查匹配项。匹配的工作流通过以下工具激活：

```text
activate_skill(name, role="primary" | "supporting")
```

激活规则：

- `name` 必须是精确注册的 hyphen-case Skill 名称，不能传路径；
- 同名时 workspace Skill 优先于 installed 与 built-in Skill，installed Skill 优先于 built-in；
- unavailable Skill 不能激活；
- 每个 turn 最多一个 primary，可有多个 supporting Skill；
- 同一 Skill 不能在 turn 内切换 role；
- 只有 primary Skill 可被自动更新；
- primary 与 supporting Skill 都可以接收相关失败归因；
- 通过文件工具读取 `SKILL.md` 不算激活。

返回值包含完整 Skill 文档、activation ID、来源、内容 digest 和适用的 active Lesson。Digest 将归因固定到当前 turn 实际使用的 revision；新晋升 revision 从后续 turn 的 summary 开始可见。

AgentLoop 记录有序工具名和参数字段名，不记录参数值。Forge AgentTask 要求本轮 primary activation；创建时重新校验并冻结精确 Skill 版本、Runtime、manifest/工作流 hash 与所需 ToolSpec。后续绑定 Query/Action/Session records 与 Gateway invocation references 随执行加入；无任务诊断 Query 不归因到 task episode。

Binding 同时冻结每个已激活 Skill 当时返回的适用 active Lesson。自动验证、后续 PlanRevision 和 review 都按 task ID 解析 Lesson 上下文，因此即使经验账本后来变化，也使用同一组有界内容。这些 Lesson 只是非权威工作流建议：可以提示检查点，但不能确定 criterion 状态、替代执行事实或证据，也不能作为 evidence reference。没有激活 Skill 的任务传入空 Lesson 集合。

## 3. Outcome 与 Episode 分类

`AgentTaskOutcomeSource` 读取已持久化 AgentTask，生成：

- 去敏后的 goal 与 criteria；
- 最终 semantic verdict 和逐 criterion 状态；
- 每个 PlanRevision 的 Query/Action/Session semantics、input 字段名、execution status、verdict 与去敏 reason；
- 不透明 task、revision、invocation、attempt 与 evidence fingerprint；
- primary/supporting activations 与去敏 workflow trace。

学习内容不复制原始工具输出、input 值、evidence locator、endpoint、凭据、绝对路径或可执行 Gateway ID。Task、revision、invocation 与 attempt identity 只保留为内部不透明 reference，不能进入生成的 Lesson 或 Skill。

Outcome 策略：

| 结果 | 经验行为 |
|:-----|:---------|
| 最终 `success`，所有 criterion `satisfied` | 可支持可复用 Skill candidate |
| 最终 `failure` 或 `replan_required` | 可产生工作流相关 failure observation |
| 失败/replan revision 后最终成功 | `mixed`：可同时支持 recovery 工作流和失败 observation |
| `inconclusive`、非法 verdict、Verifier/服务错误 | 只诊断，不产生可晋升经验 |
| `verification=off` | 不进行语义学习 |
| 人工 review | 不生成新的 episode 或支持计数 |

数据库对 AgentTask 设置唯一约束，因此 PlanRevision、重复 completion event、进程 replay 和 review attempt 始终属于同一个 episode 与同一个独立支持单位。

## 4. 后台反思

Coordinator 同步创建 episode 与 pending job，再以 `asyncio` task 运行模型反思。进程重启后，中断 job 恢复为 pending；临时失败按有限策略重试，最终失败保持可观察，但不影响 Forge。

反思模型接收结构化去敏 episode、active candidates、active Lessons、Lesson clusters 和注册 Skill catalog。任务文本、trace、verdict 文本与 evidence label 都被明确视为不可信数据。响应必须通过 `experience_assessment_v1` 校验。

Evolution 调用使用独立的 `maxEvolutionCallsPerRun`，不占 `maxVerifierCallsPerRun`。预算耗尽时延后 pending work，而不是把任务改成失败。

## 5. Lesson 相关性与 Observation

失败或 replan attempt 不会自动等价为 Skill 失败。每个 observation proposal 都带 `LessonEligibility`：

| Decision/reason | 处理 |
|:----------------|:-----|
| `related / workflow_related` | 可规范化并进入聚类 |
| `unrelated / task_unsatisfiable` | 只记录诊断 |
| `unrelated / verifier_limit` | 只记录诊断 |
| `unrelated / evidence_limit` | 只记录诊断 |
| `unrelated / external_or_infrastructure` | 只记录诊断 |
| `unrelated / user_constraint` | 只记录诊断 |
| `uncertain / unknown` | 只记录诊断 |

符合条件的 `FailureObservation` 包含可用时的 Skill 绑定、workflow key、canonical pattern key、泛化 pattern summary、`applies_when`、`does_not_apply_when` 与 recovery principle。它不得包含某次任务的具体答案、对象名称/值、坐标、选项、endpoint、Gateway ID、原始 input 或工具输出。

未激活 Skill 时，observation 保持在 unbound cluster。后续 Skill candidate 的精确 workflow-key 匹配可以绑定该 cluster；不同 workflow 不会合并。

## 6. Lesson 聚类与激活

反思模型优先匹配同 Skill、同 workflow 的现有 cluster；否则根据 Skill/unbound 作用域、workflow key 和规范化 pattern key 派生稳定 cluster identity。首期不使用 embedding 或向量数据库。

Cluster 状态：

| 状态 | 含义 |
|:-----|:-----|
| `collecting` | 独立 AgentTask 少于 `minLessonEpisodes`，或反证后重新打开 |
| `blocked` | 合成、抽象或内容校验失败 |
| `activated` | 已存在通过校验的 `ScopedLesson` |

支持计数在 `(cluster_id, root_task_id)` 上唯一，其中稳定 root reference 标识 AgentTask。默认 `minLessonEpisodes=3` 时，前两次相关失败保持 collecting，第三个不同 task 才调度合成。

合成模型只接收 cluster 中的规范化 observations。Lesson proposal 必须包含：

- 明确适用与不适用条件；
- 不随任务变化的 failure mode；
- 可复用检查、决策原则或恢复建议；
- 可选的同作用域 superseded Lessons。

激活必须同时通过：

1. 静态内容策略；
2. `lesson_abstraction_validation_v1`：`reusable=true`、`contains_specific_answer=false`、无 `unsupported_literals` 且置信度至少 `0.8`。

静态策略拒绝凭据、endpoint、绝对路径、可执行 ID、Action Manifest 副本、固定 action/input、绕过验证指令、prompt injection、答案/选项表达、坐标与固定数值答案。拒绝后 cluster 保持 blocked，永不进入 Agent 上下文。

## 7. Scoped Lesson 检索与生命周期

Active `ScopedLesson` 记录：

- Skill 与 workflow 作用域；
- `applies_when` 与 `does_not_apply_when`；
- failure mode、recommendation 与 severity；
- cluster 与 supporting episode IDs；
- observation count 和 counterexample lineage；
- `active`、`inactive`、`superseded` 或 `retired` 状态。

`activate_skill` 只查询绑定到该 Skill 的 active Lesson。轻量 term-overlap 评分会比较当前 task summary 与适用/排除文本，再按更强重叠、更多 observations 与更新时间排序，最多返回 `maxLessonsPerSkill` 条。Unbound、collecting、blocked、inactive、superseded 与 retired 内容都不注入。

更窄且通过校验的新 Lesson 只能 supersede 同 Skill/workflow 作用域内的 active Lesson。成功 episode 可以标记直接反证的 active Lesson；达到配置的独立成功门槛后，该 Lesson retired，cluster 重新打开以接收更窄证据。

稳定安全约束保存在 operator 管理的 `AGENTS.md` 或 `EMBODIED.md`；evolution 不写入这些文件。

## 8. Skill Candidate 与受控晋升

语义成功或成功恢复且可复用的 episode 可以创建或合并 `SkillCandidate`。Candidate 按 Skill 名和 workflow key 聚合；近义重复应复用现有 Skill/candidate。

晋升规则：

- 每个 AgentTask episode 最多支持一次；
- 默认 `minSuccessfulEpisodes=3`，需要三个独立成功；
- update 必须指向已激活 primary Skill；
- 已有 primary Skill 的任务不能创建替代 Skill；
- 同 workflow active Lesson 和反思 conflict 会阻止晋升；
- 结构、内容策略与 reload 校验必须通过。

Learned workflow 内容只允许 trigger/description、preconditions、通用 steps、verification checkpoints、recovery guidance 与 applicability boundaries。禁止 scripts、assets、Action Manifest 副本、固定 Gateway action/ID、endpoint、凭据和绕过 Forge/Verifier 的指令。

新 Skill 写入 `workspace/skills/<name>/SKILL.md`，且 `always: false`。更新已有 workspace Skill 时保留人工正文，只替换标记区块：

```text
<!-- paos:learned-workflow:start -->
...
<!-- paos:learned-workflow:end -->
```

Built-in Skill 永不原地修改：基线先归档，再复制为 workspace override，强制非 always-on，然后加入 managed block。写入使用原子替换，旧 workspace revision 归档，随后从 workspace reload；任何失败都会回滚原文件。

## 9. 持久化与投影

```text
<workspace>/.paos/evolution/
├── experience.sqlite3
└── revisions/<skill>/

<workspace>/skills/<skill>/
├── SKILL.md
└── references/LESSONS.md
```

`experience.sqlite3` 是事实源，采用 SQLite WAL，保存 binding、episode、reflection job、observation、cluster、AgentTask 唯一支持、cluster job、scoped Lesson、candidate、event 和 migration metadata。

`references/LESSONS.md` 为人工审阅原子生成，分开展示 active/历史 Lesson 与 collecting/blocked cluster，并显示独立支持与校验状态。人工修改该投影不会改变事实源。

根目录 `LESSONS.md` 保留。Evolution 首次启动时，将旧 `- Lesson:` 条目一次性导入为 inactive、unbound record。启用 evolution 时，根目录文件既不是全局 Agent context，也不作为 Forge Verifier 输入。数据库中 pre-cluster active Lesson 也会降级，并依据已知 source roots 重建；只有重新合成和校验后才能 active。关闭 evolution 会恢复原来的全局 Agent context、Verifier 输入和 Verifier append 行为，但不删除 evolution 数据。

## 10. 可观测性与扩展

结构化事件包括 episode created、assessment completed、eligibility rejected、observation clustered、cluster supported/blocked/activated、Lesson superseded/retired、candidate supported/blocked/promoted、validation rejected、budget deferred、built-in baseline archived 和 revision rolled back。运行日志使用 ID 与有限摘要，不记录原始敏感值。

接入另一个可信任务 outcome provider 时：

1. 实现 `TaskOutcomeSource.build(task_ref)`；
2. 返回包含 semantic verdict、criteria、lineage 与不透明引用的 `task_outcome_envelope_v1`；
3. 保持 AgentTask 级幂等与去敏；
4. 通过 `ExperienceCoordinator` 调度 completion；
5. 不把 provider 私有执行字段放入 Lesson 或 Skill 契约。

该边界允许其他可信任务 provider 复用 Lesson 聚类与 Skill 晋升，而无需修改 Forge Tool API。

## 后续阅读

- [框架介绍](01-framework-introduction.md)
- [用户手册](02-user-manual.md)
- [Core 开发者手册](03-developer-manual.md)
- [Forge 配置参考](04-forge-configuration-reference.md)
- [文档索引](../README_zh.md)

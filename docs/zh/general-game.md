# General Game

本运行时将任务转化为阶段目标，通过有限轮次的 Actor 执行，再把真实观察和执行回执
交回 Planner。模型、Target 适配器、SessionRunner、MemoryStore 和 SkillRuntimeResult
均使用 Core 现有接口。游戏服务、benchmark 和任务数据由部署方提供。

```text
TargetSpec + SessionSpec + GeneralGameSkillRuntime
                       ↓
              preflight → SessionRunner
                       ↓
        Planner → 阶段 → Actor（1–3 轮）
           ↑               ↓ 每轮一个 ActionSpec
           └── 回执 ← TargetSessionHandle → game adapter
                       ↓
               SkillRuntimeResult
```

## 启动会话

执行 `pip install -e .` 安装 Core，启动目标游戏桥接服务，再准备以下配置：

| 文件 | 内容 |
|:-----|:-----|
| `target.yaml` | Core TargetSpec，引用已注册的 runtime、adapter 和有效 runtime contract；`supported_skillruntimes` 包含 `general_game`。 |
| `session.yaml` | Core SessionSpec，引用目标和 `general_game`，设置任务描述、步数及执行超时。 |
| `actions.json` | 非空对象，键为允许的原语动作类型，值为动作描述和参数说明。 |
| `success.json` | 基于观察的成功条件，所有条件必须成立。原生星露谷和 Minecraft Target 必须提供。 |

```yaml
session_id: game-session
target_ref: configured_game
skillruntime_ref: general_game
task_description: Complete the configured task
execution:
  max_steps: 100
timeouts:
  execute_timeout_s: 300
```

```bash
paos general-game --workspace ./workspace --target ./target.yaml --session ./session.yaml \
  --actions ./actions.json --success-checks ./success.json --model YOUR_MODEL \
  --api-base http://localhost:8000/v1
```

模型服务需要认证时设置 `GAME_AGENT_API_KEY`。命令加载包内的
`templates/configs/skillruntimes/general_game.yaml`，通过 preflight 后交给 SessionRunner，
输出 Core SessionResult；任务失败时退出码为 1。游戏桥接服务需要提前启动。

成功条件使用观察字段的点分路径，默认精确相等，例如 `{"stardew.position": [1, 0]}`。
严格数值上界写成 `{"stardew.time": {"$lt": 1700}}`；缺失字段、非数值和达到上界都不算成功。
模型提出 `finish` 仅表示结束请求，不能替代任务验证。

## Python 注册

```python
from PhyAgentOS.game_agents.stardew import register_general_game

register_general_game(
    provider_factory,
    model="YOUR_MODEL",
    action_catalog={"move": {"params": {"dx": "integer", "dy": "integer"}}},
    verify=lambda observation, feedback: observation["stardew"]["position"] == [1, 0],
)
```

`provider_factory` 每次返回新的 LLMProvider，或负责管理它的异步上下文管理器。
注册函数为每个会话创建独立运行状态；后续使用既有 preflight、SkillRuntimeRegistry
和 SessionRunner。Target 沿用原注册名称，复杂目标通过 Python 检查器实现。

## 执行与记忆

Actor 每轮只发一个原语；取消、时间预算、步数限制、规划次数及持续无进展都会停止循环。
回执保留动作前后观察和真实 Target 反馈，桥接返回 `fatal` 时直接结束会话。

Planner 和 Actor 分别读取 `workspace/game_agent/{planner,actor}/memory/` 下的记忆快照。
显式启用 `--evolve` 后，会话结束时依据回执生成候选经验，写入 HISTORY.md；候选保持
`unverified` 状态，不自动进入 MEMORY.md。候选生成失败不改写任务执行结果。

## 验证范围

执行 `python -m pytest tests/game_agents/stardew`。测试覆盖真实 Core 会话、适配器、Provider API、
CLI 和结果写入，外部游戏与模型 HTTP 服务使用模拟响应。实际游戏运行仍需游戏桥接和
模型服务；当前链路处理结构化观察，不直接消费原始图像。

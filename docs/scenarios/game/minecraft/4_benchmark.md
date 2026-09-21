# PhyAgentOS x Minecraft — Tech-Tree Benchmark

> 阅读路径：Minecraft benchmark 集成。
> 本页介绍执行器无关（executor-independent）的 Minecraft Tech-Tree benchmark，
> 以及它如何嵌入 PhyAgentOS 的 target / skillruntime / session / watchdog 模型。

---

## 状态

| 组件 | 状态 | 说明 |
|---|---|---|
| 审定后的 manifest | Ready | 跨 6 个层级共 40 个 obtain-item 任务 |
| 程序化 evaluator | Ready | 背包/状态检查，无 LLM/VLM 裁判；与 runtime verifier 共用同一解析器 |
| 执行器无关 harness | Ready | agent 与 world adapter 均为注入式 |
| mineflayer bridge adapter | 示例 | 薄 HTTP 转发器；将 setup 委托给 bridge 的 `POST /benchmark/reset` |
| Runtime 粘合层 | Ready | `MinecraftTargetWorldAdapter` 把真实 OS target 包装成 benchmark `WorldAdapter` |
| OS session runner 集成 | 外部 wrapper | 在 benchmark 核心之外，用 `TARGETS.md`、`SKILLRUNTIME.md`、`SESSIONS.md` 和 watchdog 包裹 |

---

## CLI：预热、运行和保存结果

本机 Paper 与 bridge 的完整启动命令见 [01_linux_start.md](01_linux_start.md)。
bridge 启动并在 Paper 控制台执行 `op paos` 后，下面两个命令均不需要 LLM
provider。

先运行固定预热：

```bash
paos minecraft warmup \
  --url http://127.0.0.1:3001 \
  --output-dir outputs/minecraft-skill-graph
```

预热按 W01–W07 顺序各执行一次独立 reset。单次无混杂观测即可将成功或失败
claim 标记为 `verified`，不执行第二次 seed 验证、curriculum 或探索任务。
Mineflayer 不支持 seed 控制，manifest 会明确记录
`backend_seed_control=false`。

运行指定 benchmark：

```bash
paos minecraft benchmark \
  --url http://127.0.0.1:3001 \
  --graph-dir outputs/minecraft-skill-graph/benchmark_graph \
  --output-dir outputs/minecraft-benchmark \
  --tasks wooden.obtain_oak_log,stone.obtain_cobblestone \
  --trials 1 \
  --run-id smoke-001
```

运行全部 40 个任务时用 `--all` 代替 `--tasks`：

```bash
paos minecraft benchmark \
  --url http://127.0.0.1:3001 \
  --graph-dir outputs/minecraft-skill-graph/benchmark_graph \
  --output-dir outputs/minecraft-benchmark \
  --all \
  --trials 1 \
  --run-id full-001
```

参数说明：

| 命令 | 参数 | 说明 |
|---|---|---|
| `warmup` | `--output-dir/-o` | 必填；必须是尚未产生图谱的输出根目录 |
| `warmup` | `--url/-u` | bridge URL，默认 `http://127.0.0.1:3001` |
| `benchmark` | `--graph-dir` | 必填；预热生成的 `benchmark_graph/` |
| `benchmark` | `--output-dir/-o` | 必填；episode 结果根目录 |
| `benchmark` | `--tasks` | 逗号分隔 task id；默认 `wooden.obtain_oak_log` |
| `benchmark` | `--all` | 忽略 `--tasks`，执行 manifest 中全部任务 |
| `benchmark` | `--trials` | 每个任务运行次数，默认 1 |
| `benchmark` | `--run-id` | 批次 ID；省略时自动生成，设置后便于复现 |
| `benchmark` | `--url/-u` | bridge URL，默认 `http://127.0.0.1:3001` |

查看安装版本的准确帮助：

```bash
paos minecraft --help
paos minecraft warmup --help
paos minecraft benchmark --help
```

产物结构：

```text
outputs/minecraft-skill-graph/
├── warmup_frozen/        # 只读冻结图谱，后续 benchmark 不修改
└── benchmark_graph/      # 从冻结图谱复制出的同步沉淀图谱

outputs/minecraft-benchmark/
└── <run-id>/
    ├── summary.json
    └── <task-id>/trial-01.json
```

每个 benchmark episode 结束后，会先把 evidence/claim 写入 SQLite，再原子刷新
`serving_graph.json`、`evidence.jsonl`、`graph_manifest.json` 和
`graph.sha256`，然后才开始下一个 episode。

---

## 这个 Benchmark 测什么

Tech-Tree benchmark 衡量 agent 能否沿着一条进阶路径获取标准 Minecraft 物品：

```
Wooden -> Stone -> Iron -> Gold/Redstone -> Diamond -> Armor
```

每个任务包含：

- 稳定的任务 id，如 `wooden.obtain_oak_log`；
- 一个 tier（层级）和 family（族）；
- 确定性的 setup 描述；
- 一个 `target_item`（目标物品）；
- 一个程序化的成功判据。

任务集源自 MineStudio 风格的 task configs 和常见 Minecraft 科技树里程碑，经过审定后作为 PhyAgentOS 独立重实现。它**不是** MineStudio、MCU、MineEvolve、TeamCraft 或 VPT 的官方 benchmark 协议。

这个 benchmark 的范围是有意收窄的。它是一个**执行层 benchmark**：在隔离的 arena 中放置所需材料或目标方块、通过 setup 暴露目标坐标、并预置任务声明的前置工具。它衡量 agent 能否在目标已定位、前置条件已就位后，执行低级 Minecraft 动作（局部导航、挖掘、拾取、合成、放置、熔炼）。

它**不**衡量开放世界探索、资源搜索、视觉目标定位、高层规划、长程推理、多智能体协同，也不衡量真正的跨任务科技树依赖攀爬。这里的 tier 只是难度与类别标签，**不是** agent 必须跨越任务去发现或执行的依赖链。

---

## 架构

```
manifest.json
  -> loader.py
      -> TechTreeTask
          -> world_adapter.reset(task.setup)
          -> 注入的 agent_fn(task, world_adapter)
          -> world_adapter.observe()
          -> evaluator.py
              -> BenchmarkResult
```

benchmark 核心不 import 任何 PhyAgentOS 的 session 或 agent 机制。它可以被 OS session、一个直接脚本、一个 policy runner，或另一个项目的 Minecraft 控制器包裹。

---

## 与 PhyAgentOS 运行时文件的关系

benchmark 本身比 `SESSIONS.md` 更底层。

| 层 | 职责 |
|---|---|
| `TARGETS.md` | 声明 Minecraft target 和 bridge 端点 |
| `SKILLRUNTIME.md` | 声明哪个 runtime 可以执行 session |
| `SESSIONS.md` | 排队一个具体的 benchmark episode 交给 watchdog 执行 |
| WatchdogSupervisor | 认领 session 并运行所选 runtime |
| `minecraft_techtree` benchmark | 定义 setup、任务元数据与确定性判分 |

一个 OS 原生的 benchmark wrapper 应创建 session，并在 `runtime_hints` 中引用本 benchmark；但 `minecraft_techtree` 包本身应保持对 watchdog 和 session schema 的独立。

---

## 公开 API

```python
from PhyAgentOS.benchmarks.minecraft.techtree import (
    evaluate_task,
    list_tasks,
    load_task,
    run_task,
)

task = load_task("stone.craft_furnace")
print(task.target_item)

verdict = evaluate_task(task, {"inventory": {"furnace": 1}})
print(verdict.success)
```

用任意 agent 运行：

```python
def agent_fn(task, world):
    # 这里允许任何实现：LLM、脚本 agent、policy 等
    return {"ok": True}


result = run_task("wooden.obtain_oak_log", agent_fn, world_adapter)
print(result.success, result.reward)
```

Skill Graph 的 Python API：

```python
from PhyAgentOS.game_agents.minecraft import (
    build_scripted_agent,
    run_benchmark_tasks,
    run_warmup,
)

warmup = run_warmup(world_adapter, "outputs/minecraft-skill-graph")
results = run_benchmark_tasks(
    ["wooden.obtain_oak_log"],
    build_scripted_agent,
    world_adapter,
    graph_dir=warmup["mutable_dir"],
    results_dir="outputs/minecraft-benchmark",
    trials=1,
    run_id="smoke-001",
)
```

world adapter 的接口被刻意保持得很小：

```python
class WorldAdapter:
    def reset(self, setup):
        ...

    def observe(self):
        ...
```

---

## 可选的 mineflayer bridge adapter

包内附带一个示例 adapter：

```python
from PhyAgentOS.benchmarks.minecraft.techtree.adapters.adapter import (
    MineflayerBridgeAdapter,
)

world = MineflayerBridgeAdapter("http://127.0.0.1:3000")
```

它是一个薄 HTTP 转发器：`reset(setup)` 把整个 `WorldSetup` 序列化后 POST 给 bridge 的 `POST /benchmark/reset` 端点，再返回 `GET /state` 的观察。它还会在 reset 前后通过 `POST /phase` 标记阶段。`loader.py`、`evaluator.py`、`harness.py` 均不 import 它。

有界的场景隔离逻辑位于 bridge 侧，而非 adapter 侧。`POST /benchmark/reset` 会把 bot 传送到固定的 arena 原点、清空一个有界盒、铺设固定地板、标记边界、清空背包、发放 setup 物品（含附魔 NBT），并在相对 arena 原点的坐标处放置任务方块。这避免了在 bot 周围复用任意旧地形，同时保持 benchmark 核心与 adapter 无关。bridge 的 `/state` 响应还会暴露 `inventory_items` 字段，供 evaluator 直接判分。

---

## 在真实 OS target 上运行 benchmark

benchmark 核心是执行器无关的；一个薄的 runtime 粘合模块把生产用的 `MinecraftTarget` 连接到 benchmark 的 `WorldAdapter` 接口，使一个 episode 能跑在真实 OS 栈上，而非手写的 mock：

```python
from PhyAgentOS.runtime.benchmark.minecraft_glue import (
    MinecraftTargetWorldAdapter,
    make_action_agent_fn,
    task_verify_descriptors,
)
from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget
from PhyAgentOS.benchmarks.minecraft.techtree import run_task

target = MinecraftTarget({"bridge_url": "http://127.0.0.1:3001"})
world = MinecraftTargetWorldAdapter(target)
agent_fn = make_action_agent_fn([{"type": "dig", "params": {"x": 1, "y": 2, "z": 3}}])

result = run_task("wooden.obtain_oak_log", agent_fn, world)
print(result.success, result.reward)
```

`MinecraftTargetWorldAdapter.reset` 把 `/benchmark/reset` POST 给 target 已使用的同一个 bridge，然后 observe。`observe` 委托给 `MinecraftTarget.observe()`，其 `inventory.hotbar` 形态可被统一后的 evaluator 直接判分。`task_verify_descriptors` 把任务的成功判据渲染成 runtime verifier 的 `has_item:...` 词汇，使 TaskPlan 能用单一事实模型自我校验，而不是两套。

---

## 任务层级

| 层级 | 数量 | 示例 |
|---|---:|---|
| Wooden | 10 | `wooden.obtain_oak_log`、`wooden.craft_crafting_table` |
| Stone | 8 | `stone.obtain_cobblestone`、`stone.craft_furnace` |
| Iron | 9 | `iron.obtain_raw_iron`、`iron.smelt_iron_ingot` |
| Gold-Redstone | 5 | `gold_redstone.obtain_redstone`、`gold_redstone.craft_clock` |
| Diamond | 4 | `diamond.obtain_diamond`、`diamond.craft_enchanting_table` |
| Armor | 4 | `armor.craft_iron_chestplate`、`armor.craft_diamond_chestplate` |

任务族：

| 族 | 数量 | 判分 |
|---|---:|---|
| `dig_pickup` | 10 | 背包含有掉落的目标物品 |
| `crafting_inventory` | 5 | 背包含有合成出的物品 |
| `crafting_table` | 23 | 背包含有合成出的物品 |
| `smelting` | 2 | 背包含有熔炼出的物品 |

完整任务列表位于：

```text
PhyAgentOS/benchmarks/minecraft/techtree/manifest.json
```

---

## 任务表

### Wooden

| id | target_item | family |
|---|---|---|
| `wooden.obtain_oak_log` | `oak_log` | `dig_pickup` |
| `wooden.obtain_dirt` | `dirt` | `dig_pickup` |
| `wooden.obtain_grass_block` | `grass_block` | `dig_pickup` |
| `wooden.craft_oak_planks` | `oak_planks` | `crafting_inventory` |
| `wooden.craft_stick` | `stick` | `crafting_inventory` |
| `wooden.craft_crafting_table` | `crafting_table` | `crafting_inventory` |
| `wooden.craft_chest` | `chest` | `crafting_table` |
| `wooden.craft_ladder` | `ladder` | `crafting_table` |
| `wooden.craft_bow` | `bow` | `crafting_table` |
| `wooden.craft_wooden_pickaxe` | `wooden_pickaxe` | `crafting_table` |

### Stone

| id | target_item | family |
|---|---|---|
| `stone.obtain_cobblestone` | `cobblestone` | `dig_pickup` |
| `stone.craft_stone_pickaxe` | `stone_pickaxe` | `crafting_table` |
| `stone.craft_stone_axe` | `stone_axe` | `crafting_table` |
| `stone.craft_stone_shovel` | `stone_shovel` | `crafting_table` |
| `stone.craft_stone_sword` | `stone_sword` | `crafting_table` |
| `stone.craft_furnace` | `furnace` | `crafting_table` |
| `stone.craft_stonecutter` | `stonecutter` | `crafting_table` |
| `stone.craft_torch` | `torch` | `crafting_inventory` |

### Iron

| id | target_item | family |
|---|---|---|
| `iron.obtain_coal` | `coal` | `dig_pickup` |
| `iron.obtain_raw_iron` | `raw_iron` | `dig_pickup` |
| `iron.smelt_iron_ingot` | `iron_ingot` | `smelting` |
| `iron.craft_iron_pickaxe` | `iron_pickaxe` | `crafting_table` |
| `iron.craft_iron_axe` | `iron_axe` | `crafting_table` |
| `iron.craft_iron_shovel` | `iron_shovel` | `crafting_table` |
| `iron.craft_iron_sword` | `iron_sword` | `crafting_table` |
| `iron.craft_bucket` | `bucket` | `crafting_table` |
| `iron.craft_shears` | `shears` | `crafting_inventory` |

### Gold-Redstone

| id | target_item | family |
|---|---|---|
| `gold_redstone.obtain_raw_gold` | `raw_gold` | `dig_pickup` |
| `gold_redstone.smelt_gold_ingot` | `gold_ingot` | `smelting` |
| `gold_redstone.obtain_redstone` | `redstone` | `dig_pickup` |
| `gold_redstone.craft_clock` | `clock` | `crafting_table` |
| `gold_redstone.craft_compass` | `compass` | `crafting_table` |

### Diamond

| id | target_item | family |
|---|---|---|
| `diamond.obtain_diamond` | `diamond` | `dig_pickup` |
| `diamond.obtain_obsidian` | `obsidian` | `dig_pickup` |
| `diamond.craft_diamond_pickaxe` | `diamond_pickaxe` | `crafting_table` |
| `diamond.craft_enchanting_table` | `enchanting_table` | `crafting_table` |

### Armor

| id | target_item | family |
|---|---|---|
| `armor.craft_iron_helmet` | `iron_helmet` | `crafting_table` |
| `armor.craft_iron_chestplate` | `iron_chestplate` | `crafting_table` |
| `armor.craft_diamond_helmet` | `diamond_helmet` | `crafting_table` |
| `armor.craft_diamond_chestplate` | `diamond_chestplate` | `crafting_table` |

---

## 报告规范

报告应包含：

- benchmark 名称与 manifest 版本；
- 任务 id 与层级拆分；
- 使用的 agent 或 runtime；
- 使用的 world adapter；
- 每个任务的重复次数；
- 成功率与平均 reward；
- 失败分类（如有）；
- 可复现性说明，包括 Minecraft 版本、bridge 版本与 setup。

**不要**把这些数字直接与 MineStudio、MCU、MineEvolve、TeamCraft 或 VPT 的排行榜相比。本 benchmark 是一个经审定的、执行器无关的、独立的 obtain-item 重实现，面向 OS 集成与可重复的程序化判分。

---

## 局限

| 局限 | 影响 |
|---|---|
| 独立审定子集 | 源自 MineStudio 风格配置与科技树层级后，重实现为独立的程序化 mineflayer benchmark；非 MineStudio、MCU、MineEvolve、TeamCraft 或 VPT 官方协议，分数不可与它们的公开数字相比 |
| 执行层范围 | 材料或目标方块放置在 agent 附近，目标坐标通过 setup 提供，前置工具已预置；分数衡量 setup 之后的低级物理执行 |
| 非探索/规划 benchmark | 不衡量资源搜索、视觉目标定位、高层规划、长程推理或真正的科技树依赖攀爬 |
| 层级只是标签 | tier 描述任务类别与难度；任务彼此隔离，不要求 agent 跨任务遍历依赖链 |
| 程序化背包/状态判分 | 规避 VLM 裁判，但会遗漏视觉或语义上的细微差别 |
| 仅 arena 场景隔离 | bridge reset 使用固定的清理后 arena 与相对任务放置；不重建完整世界种子或自然地形 |
| 执行不确定性 | 报告结果时请使用重复试验 |
| bridge reset 绑定 mineflayer | benchmark 核心仍与 adapter 无关；非 mineflayer 的世界可直接实现 `WorldAdapter` |

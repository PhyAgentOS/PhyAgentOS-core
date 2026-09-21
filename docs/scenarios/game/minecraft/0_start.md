# Minecraft Target 部署文档

## 架构概览

<p>
<details open>
<summary><b>📐 架构图</b></summary>

```
┌─────────────────────────────────────────────────────┐
│ Windows 11                                           │
│  Minecraft Java Edition (1.20.4)                     │
│       ↑ localhost:25565                              │
│  mineflayer bridge (Node.js)  ← bot: paos           │
│       ↑ localhost:3001 (HTTP API)                    │
│  ngrok tunnel  →  https://xxxx.ngrok-free.dev        │
└──────────────────┬──────────────────────────────────┘
                   │ HTTPS
┌──────────────────▼──────────────────────────────────┐
│ Linux 云端 — PhyAgentOS                              │
│                                                      │
│  路径 A: Runtime Session Protocol（标准多场景）      │
│    MinecraftTarget → TargetSessionHandle             │
│    → MinecraftSkillRuntime → SessionRunner           │
│    → WatchdogSupervisor ← SESSIONS.md                │
│                                                      │
│  路径 B: Direct Bridge API（Minecraft 推荐）         │
│    Agent exec curl → HTTP /state + /action           │
│    结合 minecraft-navigation skill                   │
│                                                      │
│  ↑ Agent (Planner/Critic) — 对话式下达任务            │
└─────────────────────────────────────────────────────┘
```

</details>
</p>

**关键设计**：
- MinecraftTarget 是 OS 中一个干净的 `BaseLocalTarget` 实现，不包含任何 Minecraft 协议代码
- mineflayer bridge 是独立的外部服务（Node.js），OS 只通过 HTTP 连接它
- 无 pyCraft 依赖

---

## 一、环境要求

| 组件 | 位置 | 说明 |
|------|------|------|
| Minecraft Java Edition | Windows 11 | 推荐 1.20.4（mineflayer 稳定支持。1.21.5 协议太新，暂不支持） |
| Node.js | Windows 11 | 18+ |
| ngrok | Windows 11 | 免费版即可，需注册账号 |
| Python | Linux 云端 | ≥ 3.11，httpx 已内置 |

---

## 二、Windows 11 端部署

<p>
<details>
<summary><b>2.1 安装 Node.js</b></summary>

```powershell
winget install OpenJS.NodeJS.LTS
```

如果 winget 不可用，浏览器打开 `https://nodejs.org/en/download`，下载 Windows Installer (.msi)，双击安装。

安装完成后**关闭并重新打开 PowerShell**，验证：

```powershell
node --version   # → v22.x
npm --version    # → 10.x
```

</details>
</p>

<p>
<details>
<summary><b>2.2 – 2.5 创建 bridge 工程 &amp; 安装依赖</b></summary>

### 2.2 创建 bridge 工程

```powershell
mkdir E:\mc_bridge
cd E:\mc_bridge
```

### 2.3 创建 package.json

```powershell
@'
{
  "name": "mc-bridge",
  "private": true,
  "dependencies": {
    "mineflayer": "^4.23.0",
    "mineflayer-pathfinder": "^2.4.5",
    "mineflayer-collectblock": "^1.4.1",
    "express": "^4.21.0"
  }
}
'@ | Out-File -Encoding utf8 package.json
```

### 2.4 获取 bridge_server.js

在 Linux 服务器上，文件位于 `docs/scenarios/game/minecraft/bridge_server.js`。

用 scp 复制到 Windows：
```powershell
scp user@linux-server:/path/PhyAgentOS/docs/scenarios/game/minecraft/bridge_server.js E:\mc_bridge\
```

### 2.5 安装 npm 依赖

```powershell
npm install
```

</details>
</p>

<p>
<details>
<summary><b>2.6 安装 ngrok</b></summary>

1. 从 https://ngrok.com/download 下载 Windows 版本
2. 注册 ngrok 免费账号（无需绑卡）
3. 获取 authtoken：https://dashboard.ngrok.com/get-started/your-authtoken
4. 配置：
```powershell
ngrok config add-authtoken <你的token>
```

</details>
</p>

<p>
<details>
<summary><b>2.7 启动 Minecraft 1.20.4</b></summary>

mineflayer 的协议库对新版支持有延迟。**推荐创建 1.20.4 游戏实例**：

```
Minecraft Launcher → 安装配置 → 新建配置
  → 名称: "1.20.4"，版本选 release 1.20.4
  → 创建 → 启动
```

进入单人世界，按 `Esc` → **对局域网开放** (Open to LAN) → 允许作弊: 开。

聊天框显示 `本地游戏已在端口 25565 上托管`。

> 备选：使用 Paper 1.20.4 服务器。`java -jar paper-1.20.4-496.jar nogui`，客户端和 bridge 都连 `localhost:25565`。

</details>
</p>

<p>
<details>
<summary><b>2.8 启动 bridge</b></summary>

```powershell
cd E:\mc_bridge
$env:MC_HOST="localhost"
$env:MC_PORT="25565"
$env:BOT_NAME="paos"
$env:MC_VERSION="1.20.4"
$env:API_PORT="3001"
$env:VIEWER_PORT="3007"
node bridge_server.js
```

成功输出：
```
[bridge] Bot spawned: paos
[bridge] HTTP API listening on port 3001
[bridge] 3D viewer (first-person) on http://localhost:3007
```

Bot 生成后会自动传送到你身边（无需 OP，直接移动实体坐标）。

验证 bridge API：
```powershell
# 另开一个 PowerShell
curl.exe http://localhost:3001/health
# → {"ok":true,"bot_spawned":true,"uptime_seconds":5}
```

</details>
</p>

<p>
<details>
<summary><b>2.9 建立 ngrok 公网隧道</b></summary>

```powershell
# 再开一个 PowerShell
ngrok http 3001 --region=ap
```

输出：
```
Forwarding  https://abc123.ap.ngrok-free.app → http://localhost:3001
```

**记下这个 ngrok URL**，Linux 云端将通过它连接。

</details>
</p>

---

## 三、Linux 云端 — 连接验证

### 3.1 确认 OS 代码就绪

```bash
python -m pytest tests/runtime/test_minecraft_target.py tests/runtime/test_minecraft_skill_runtime.py -q
# → 26 passed
```

### 3.2 快速测试

```python
from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget

# bridge_url 填 §2.9 获得的 ngrok 地址
t = MinecraftTarget({"bridge_url": "https://abc123.ap.ngrok-free.app"})

t.build()                         # HTTP GET /health → 验证 bridge 可达
obs = t.reset({})                 # 初始观察

# 聊天
t.step({"type": "chat", "params": {"message": "Hello from PhyAgentOS!"}})

# 移动 bot 到绝对坐标
t.step({"type": "move", "params": {"dx": 10, "dy": 64, "dz": 0, "absolute": True}})

# 查看状态
print(obs["info"]["position"])    # {"x": 100.5, "y": 64.0, "z": 200.0}
print(obs["info"]["health"])      # 20.0

t.close()
```

<p>
<details>
<summary><b>3.3 观察空间</b></summary>

```python
{
    "state":  np.array([x, y, z, yaw, pitch, health, hunger, held_slot], dtype=float32),
    "image":  np.zeros((224, 224, 3), dtype=uint8),    # 无渲染
    "info": {
        "position":   {"x": 100.5, "y": 64.0, "z": 200.5},
        "rotation":   {"yaw": 90.0, "pitch": 0.0},
        "health":     20.0,
        "hunger":     20,
        "dimension":  "overworld",
        "on_ground":  True,
        "world":      {"time": 6000, "raining": false},
        "player_list": ["paos", "your_username"],
    },
    "nearby_blocks": [
        {"name": "grass_block", "position": {"x": 100, "y": 63, "z": 200}},
    ],
    "nearby_entities": [
        {"type": "pig", "uuid": "...", "health": 10},
    ],
    "inventory": {
        "hotbar": [{"slot": 0, "name": "stone_pickaxe", "count": 1}],
    },
    "inventory_items": [
        {"name": "minecraft:stone_pickaxe", "count": 1},
    ],
}
```

> `inventory_items` 是完整背包的扁平列表（含 hotbar 以外的槽位），
> 供 tech-tree benchmark evaluator 直接判分。`inventory.hotbar` 仅含
> 快捷栏 9 格，供 agent 读取当前手持物。

</details>
</p>

---

## 四、动作空间（17 种）

所有动作通过 `POST /action` 发给 bridge，bridge 用 mineflayer API 执行。

### 移动与视角

```python
# 路径规划到绝对坐标
t.step({"type": "move", "params": {"dx": 100, "dy": 64, "dz": 200, "absolute": True}})
# 相对移动
t.step({"type": "move", "params": {"dx": 5, "dz": 0}})
# 转向
t.step({"type": "look", "params": {"yaw": 90.0, "pitch": 0.0}})
```

### 按键控制

```python
t.step({"type": "jump",     "params": {"duration_ms": 500}})
t.step({"type": "sneak",    "params": {"start": True}})
t.step({"type": "sprint",   "params": {"start": True}})
```

### 方块操作

```python
# 挖掘指定坐标的方块（异步，mineflayer 自动处理时序）
t.step({"type": "dig",   "params": {"x": 100, "y": 63, "z": 200}})
# 放置方块到指定面（face: 0=下 1=上 2=北 3=南 4=西 5=东）
t.step({"type": "place", "params": {"x": 100, "y": 63, "z": 200, "face": 1}})
```

### 实体交互

```python
t.step({"type": "attack",   "params": {"target_type": "pig"}})
t.step({"type": "interact", "params": {"entity_id": "..."}})
```

### 物品操作

```python
t.step({"type": "use",         "params": {}})
t.step({"type": "select_slot", "params": {"slot": 0}})
t.step({"type": "drop",        "params": {}})
```

### 聊天

```python
t.step({"type": "chat", "params": {"message": "hello"}})
```

### 高级动作

```python
t.step({"type": "collect", "params": {"block_type": "oak_log", "count": 10}})
t.step({"type": "craft",   "params": {"recipe_id": "crafting_table", "count": 1}})
t.step({"type": "equip",   "params": {"item": "stone_pickaxe", "destination": "hand"}})
t.step({"type": "smelt",   "params": {"input": "raw_iron", "fuel": "coal", "count": 1}})
```

---

## 五、配置 TARGETS.md

```yaml
targets:
  - id: minecraft_java_env
    target_class: local
    target_kind: game
    workspace: workspaces/minecraft
    enabled: true
    supported_skills: [minecraft_navigate, minecraft_mine, minecraft_build]
    runtime:
      target_runtime: MinecraftTargetRuntime
      target_adapter: target_adapter://minecraft_adapter
      runtime_contract_ref: configs/runtime/contracts/minecraft.runtime.yaml
    config:
      bridge_url: "https://abc123.ap.ngrok-free.app"   # ← ngrok 公网地址
      verify_ssl: false
      step_delay: 0.1
```

<p>
<details>
<summary><b>📋 字段说明</b></summary>

- `target_class: local` — 本地进程内 target（非 remote WebSocket）
- `target_kind: game` — TargetSpec schema 规定的游戏场景类型
- `runtime_contract_ref` — 运行时契约文件（必填）
- 无需 `target_endpoint`（local target 不需要）

</details>
</p>

---

## 六、配置 SKILLRUNTIME.md（Runtime Skill Registry）

> **注意区分**：`SKILLRUNTIME.md` 是 watchdog 使用的 runtime skill 注册表（YAML，定义可执行的 skill runtime），
> `SKILLS.md` 是 Agent 使用的技能列表（Markdown，列出 Agent 可加载的方法论指导）。
> 两者名字相似但用途完全不同。

```yaml
skills:
  - id: minecraft_navigate
    runtime: MinecraftSkillRuntime
    runtime_kind: builtin
    loop_mode: open_loop_step
    agent_exposure: none
    supported_target_kinds: [game]
    observation_contract:
      observation_type: environment_only
    requires:
      environment_outputs: []
```

<p>
<details>
<summary><b>📋 字段说明</b></summary>

- `runtime_kind: builtin`（原 `category: builtin`）
- `supported_target_kinds: [game]`（原 `supported_targets: [minecraft_java_env]`，按 target_kind 匹配而非 target ID）
- `loop_mode`、`agent_exposure`、`observation_contract` 均为 SkillSpec schema 必填字段

</details>
</p>

### MinecraftSkillRuntime 注册

Runtime skill 必须在 `runtime_registry.py` 中注册才能被 watchdog 使用：

```python
# PhyAgentOS/runtime/watchdog/runtime_registry.py
from PhyAgentOS.runtime.skillruntime.game.minecraft_skill_runtime import MinecraftSkillRuntime
register_skill_runtime("MinecraftSkillRuntime", MinecraftSkillRuntime)
```

**忘记注册的典型症状**：`SKILL_RUNTIME_MISSING: unsupported skill runtime: MinecraftSkillRuntime`。

---

## 七、完整 pipeline：Agent 下发任务

<p>
<details open>
<summary><b>路径 A：Runtime Session Protocol（标准多场景路径）</b></summary>

### 7.1 首次配置

```bash
# 1. 填入 bridge_url（ngrok 公网地址）
#    编辑 workspaces/minecraft/TARGETS.md，将 bridge_url 改为实际值
#    bridge_url: "https://xxxx.ngrok-free.app"

# 2. 启动
paos agent --workspace workspaces/minecraft
```

### 7.2 Agent 写入 SESSIONS.md

Agent 系统提示词自动注入 TARGETS.md/SKILLS.md/RUNTIME.md/EMBODIED.md。
收到用户指令后，Agent 按照 RUNTIME.md 格式用 `write_file` 写入 SESSIONS.md：

```yaml
sessions:
  - session_id: move_fwd_5
    target_ref: target://minecraft_java_env
    skill_ref: skill://minecraft_navigate
    task_description: "往前走5步"
    status: pending
    execution: {max_steps: 1}
    runtime_hints:
      perception_queries:
        - type: move
          params: {forward: 5}
```

### 7.3 WatchdogSupervisor 执行

```
1. 轮询 SESSIONS.md → 发现 pending session
2. Preflight 校验（合同 + adapter + skill 兼容性）
3. 绑定 MinecraftTarget + MinecraftSkillRuntime
4. SessionRunner.start():
     target.build()              # HTTP GET /health → 验证 bridge 可达
     target.start_session(ctx)   # → observe()
     SkillRuntime.run_builtin_loop():
       for each perception_query:
         handle.action_chunk()   # SafetyClampBridge 检测 dict action 自动透传
                                 # → MinecraftAdapter.to_executable_action_chunk()
                                 # → MinecraftTarget.action_chunk() → HTTP POST /action
         handle.observe()        # HTTP GET /state → 获取最新观察
         校验 action_ok/action_result
         直到 done/max_steps
     → SkillRuntimeResult
5. ResultWriter → ENVIRONMENT.md + LESSONS.md
```

### 7.4 Agent 验证

Agent 读取 ENVIRONMENT.md 验证执行结果，成功则回复用户，失败则读 LESSONS.md 分析原因、调整策略、重写 SESSIONS.md 重试。

</details>
</p>

<p>
<details>
<summary><b>路径 B：Direct Bridge API（Minecraft 推荐路径）</b></summary>

Runtime session protocol 对 Minecraft 不必要——preflight 检查链（perception → sensor → runtime）复杂且经常失败。
Minecraft 场景下推荐使用 bridge HTTP API 直接调用，Agent 可结合 `minecraft-navigation` skill 高效导航。

```bash
# 查询 bot 状态
curl -s https://xxxx.ngrok-free.dev/state

# 直接执行动作
curl -s -XPOST https://xxxx.ngrok-free.dev/action \
  -H 'Content-Type: application/json' \
  -d '{"type":"move","params":{"dx":10,"dy":0,"dz":0}}'
```

> Agent 使用 `exec` 工具调用 curl 命令即可。详见 `skills/minecraft-navigation/SKILL.md`。

</details>
</p>

---

## 八、OS 文件清单

| 文件 | 行数 | 说明 |
|------|------|------|
| `runtime/targets/game/minecraft_target.py` | 272 | MinecraftTarget（HTTP 客户端，继承 BaseLocalTarget） |
| `runtime/adapters/minecraft/minecraft_adapter.py` | 83 | Observation/Action 归一化 |
| `runtime/skillruntime/game/minecraft_skill_runtime.py` | 260 | Episode 驱动循环（含 entity 解析 + 到达检测），继承 BuiltinSkillRuntime |
| `runtime/adapters/bridges.py` | +5 | SafetyClampBridge 对 dict-based game action 自动透传 |
| `runtime/targets/factory.py` | +4 | 注册 MinecraftTargetRuntime |
| `runtime/adapters/factory.py` | +3 | 注册 minecraft_adapter |
| `runtime/watchdog/runtime_registry.py` | +2 | 注册 MinecraftSkillRuntime |
| `templates/configs/runtime/embodied/minecraft.md` | 63 | Minecraft 专属 EMBODIED.md（16 种动作 + Critic Guidance），自动部署 |
| `templates/configs/runtime/contracts/minecraft.runtime.yaml` | 34 | 运行时契约文件（safety/action_contract） |
| `tests/runtime/test_minecraft_target.py` | 16 tests | 单元测试（Mock HTTP bridge） |
| `tests/runtime/test_minecraft_skill_runtime.py` | 3 tests | Skill runtime 测试 |
| `docs/scenarios/game/minecraft/bridge_server.js` | 390 | mineflayer bridge（部署到 Windows；含 `/benchmark/reset`、`/phase` 端点） |
| `benchmarks/minecraft/techtree/` | — | 执行器无关 tech-tree benchmark（40 任务 + 程序化判分） |
| `runtime/benchmark/minecraft_glue.py` | — | 运行时↔benchmark 粘合：`MinecraftTargetWorldAdapter` |

**使用方式**：

```bash
# 一切通过 paos agent 完成，不再有独立的 minecraft 子命令
paos agent --workspace workspaces/minecraft
# → Agent 上下文自动包含 EMBODIED.md/ENVIRONMENT.md/LESSONS.md
# → 对话式下达任务，Agent 自动写 SESSIONS.md，watchdog 执行
```

---

## 九、已知限制

| 限制 | 说明 | 状态 |
|------|------|------|
| Minecraft 版本 | mineflayer 稳定支持 1.20.4。1.21.5 协议太新，minecraft-data 暂不支持 | 待上游更新 |
| 无图像观察 | `observation.image` 返回零数组。3D viewer 见 `todo_list.md` §1 | 待实现 |
| bridge_url 需手动填入 | 模板中 `bridge_url: ""`，首次使用需编辑 `workspaces/minecraft/TARGETS.md` 填入 ngrok URL | 使用前必做 |
| ngrok 域名变化 | 免费版每次重启域名随机，需更新 `bridge_url` | ngrok 限制 |
| bridge 重启需重连 | bridge 断开后 bot 从世界消失，需重新 `node bridge_server.js` | 使用习惯 |
| ~~dig/place Vec3 bug~~ | ~~`bot.blockAt({x,y,z})` 传普通对象导致 `pos.floored is not a function`~~ | ✅ 已修复（2026-06-12） |
| ~~Runtime session 不可靠~~ | ~~Minecraft 用 runtime session protocol 频繁 preflight 失败（7/7）~~ | ✅ 改用 bridge API 直连 |
| ~~MinecraftSkillRuntime 未注册~~ | ~~`SKILL_RUNTIME_MISSING` 错误~~ | ✅ 已在 `runtime_registry.py` 注册 |

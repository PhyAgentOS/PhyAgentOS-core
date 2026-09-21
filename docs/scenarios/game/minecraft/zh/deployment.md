# Minecraft Target 部署文档

> 阅读路径 2：**快速跑通系统** — 从零部署 Minecraft Game Agent 完整链路。
> 返回 [用户手册 §2.6.7](../../../../zh/02-user-manual.md#267-minecraft-game-agent)

---

## 架构概览

```
[Windows 11]
  Minecraft Java Edition (推荐 1.20.4)
       ↑ localhost:25565
  mineflayer bridge (Node.js)           ← 机器人引擎，bot 名: paos
       ↑ localhost:3001 (HTTP API)
  ngrok tunnel                          ← 公网暴露
       ↓

[Linux 云端 — PhyAgentOS]
  MinecraftTarget                       ← HTTP 客户端，连接 ngrok URL
       ↑
  MinecraftSkillRuntime                 ← episode 驱动循环
       ↑
  WatchdogSupervisor                    ← session 监督器
       ↑
  Agent (Planner/Critic)               ← 通过 SESSIONS.md 下发任务
```

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

### 2.1 安装 Node.js

```powershell
winget install OpenJS.NodeJS.LTS
```

如果 winget 不可用，浏览器打开 `https://nodejs.org/en/download`，下载 Windows Installer (.msi)，双击安装。

安装完成后**关闭并重新打开 PowerShell**，验证：

```powershell
node --version   # → v22.x
npm --version    # → 10.x
```

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
    "prismarine-viewer": "^1.26.0",
    "express": "^4.21.0"
  }
}
'@ | Out-File -Encoding utf8 package.json
```

### 2.4 获取 bridge_server.js

bridge 源码位于 PhyAgentOS 仓库的 `docs/scenarios/game/minecraft/bridge_server.js`。

用 scp 复制到 Windows：
```powershell
scp user@linux-server:/path/PhyAgentOS/docs/scenarios/game/minecraft/bridge_server.js E:\mc_bridge\
```

### 2.5 安装 npm 依赖

```powershell
npm install
```

### 2.6 安装 ngrok

1. 从 https://ngrok.com/download 下载 Windows 版本
2. 注册 ngrok 免费账号（无需绑卡）
3. 获取 authtoken：https://dashboard.ngrok.com/get-started/your-authtoken
4. 配置：
```powershell
ngrok config add-authtoken <你的token>
```

### 2.7 启动 Minecraft 1.20.4

mineflayer 的协议库对新版支持有延迟。**推荐创建 1.20.4 游戏实例**：

```
Minecraft Launcher → 安装配置 → 新建配置
  → 名称: "1.20.4"，版本选 release 1.20.4
  → 创建 → 启动
```

进入单人世界，按 `Esc` → **对局域网开放** (Open to LAN) → 允许作弊: 开。

聊天框显示 `本地游戏已在端口 25565 上托管`。

> 备选：使用 Paper 1.20.4 服务器。`java -jar paper-1.20.4-496.jar nogui`，客户端和 bridge 都连 `localhost:25565`。

### 2.8 启动 bridge

```powershell
cd E:\mc_bridge
$env:MC_HOST="localhost"
$env:MC_PORT="25565"
$env:BOT_NAME="paos"
$env:MC_VERSION="1.20.4"
$env:BRIDGE_PORT="3001"
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

### 2.9 建立 ngrok 公网隧道

```powershell
# 再开一个 PowerShell
ngrok http 3001 --region=ap
```

输出：
```
Forwarding  https://abc123.ap.ngrok-free.app → http://localhost:3001
```

**记下这个 ngrok URL**，Linux 云端将通过它连接。

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

### 3.3 观察空间

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
        "world":      {"time": 6000, "raining": False},
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

# 放置方块到指定面
# face: 0=下 1=上 2=北 3=南 4=西 5=东
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

### 聊天与命令

```python
t.step({"type": "chat", "params": {"message": "hello"}})
```

### 高级动作

```python
# 自动采集 10 个橡木原木（mineflayer-collectblock）
t.step({"type": "collect", "params": {"block_type": "oak_log", "count": 10}})

# 合成工作台
t.step({"type": "craft",   "params": {"recipe_id": "crafting_table", "count": 1}})

# 装备物品
t.step({"type": "equip",   "params": {"item": "stone_pickaxe", "destination": "hand"}})

# 使用附近熔炉烧炼
t.step({"type": "smelt",   "params": {"input": "raw_iron", "fuel": "coal", "count": 1}})
```

---

## 五、配置 TARGETS.md

```yaml
targets:
  - id: minecraft_java_env
    type: game
    workspace: workspaces/minecraft
    enabled: true
    supported_skillruntimes: [minecraft_navigate, minecraft_mine, minecraft_build]
    runtime:
      target_runtime: MinecraftTargetRuntime
      target_endpoint: targetws://local/minecraft_java_env
      target_adapter: target_adapter://minecraft_adapter
    perception:
      enabled: false
    config:
      bridge_url: "https://abc123.ap.ngrok-free.app"   # ← ngrok 公网地址
      step_delay: 0.1
      verify_ssl: false
```

## 六、配置 SKILLRUNTIME.md

```yaml
skills:
  - id: minecraft_navigate
    category: builtin
    runtime: MinecraftSkillRuntime
    supported_targets: [minecraft_java_env]
    requires:
      environment_outputs: [player_position, nearby_blocks]
```

---

## 七、完整 pipeline：Agent 下发任务

### 7.1 Agent 写入 SESSIONS.md

```yaml
sessions:
  - session_id: sess_mc_demo
    target_ref: target://minecraft_java_env
    skillruntime_ref: skillruntime://minecraft_navigate
    task_description: "go to (100, 64, 200), say hello, mine 5 oak logs"
    status: pending
    timeouts:
      execute_timeout_s: 300
    execution:
      max_steps: 50
    runtime_hints:
      perception_queries:
        - type: move
          params: {dx: 100, dy: 64, dz: 200, absolute: true}
        - type: chat
          params: {message: "Arrived at target!"}
        - type: collect
          params: {block_type: "oak_log", count: 5}
```

### 7.2 WatchdogSupervisor 执行

```
1. 读取 SESSIONS.md → 解析 SessionSpec
2. 绑定 MinecraftTarget + MinecraftSkillRuntime
3. MinecraftSkillRuntime.run():
     target.build()           # HTTP GET /health
     target.reset(ctx)        # → observe()
     loop:
       observe()              # HTTP GET /state
       adapter.to_runtime_observation()
       pick_action(plan)      # 从 runtime_hints 读取
       target.step(action)    # HTTP POST /action → mineflayer
       直到 done/success/max_steps
4. SessionResult → ENVIRONMENT.md + LESSONS.md
```

---

## 八、OS 文件清单

| 文件 | 行数 | 说明 |
|------|------|------|
| `runtime/targets/game/minecraft_target.py` | 182 | MinecraftTarget（HTTP 客户端，继承 BaseLocalTarget） |
| `runtime/adapters/minecraft/minecraft_adapter.py` | 83 | Observation/Action 归一化 |
| `runtime/skillruntime/game/minecraft_skill_runtime.py` | 115 | Episode 驱动循环 |
| `runtime/targets/factory.py` | +4 | 注册 MinecraftTargetRuntime |
| `runtime/adapters/factory.py` | +3 | 注册 minecraft_adapter |
| `tests/runtime/test_minecraft_target.py` | 16 tests | 单元测试（Mock HTTP bridge） |
| `tests/runtime/test_minecraft_skill_runtime.py` | 3 tests | Skill runtime 测试 |
| `docs/scenarios/game/minecraft/bridge_server.js` | 390 | mineflayer bridge（部署到 Windows；含 `/benchmark/reset`、`/phase` 端点） |

**测试结果**：26 passed, 0 failed。

---

## 九、已知限制

| 限制 | 说明 |
|------|------|
| Minecraft 版本 | mineflayer 稳定支持 1.20.4。1.21.5 协议太新，minecraft-data 暂不支持 |
| 无图像观察 | `observation.image` 返回零数组。如需视觉，通过 prismarine-viewer (3007) 截图 |
| ngrok 域名变化 | 免费版每次重启域名随机，需更新 TARGETS.md 中的 `bridge_url` |
| bridge 重启需重连 | bridge 断开后 bot 从世界消失，需重新 `node bridge_server.js` |
| SSL 证书 | ngrok 免费版证书不完整，config 中设置 `"verify_ssl": false` |

---

> 下一步：[使用指南](usage.md) — CLI 终端控制、游戏对话监听、bot 传送

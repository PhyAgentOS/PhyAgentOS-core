# Minecraft Target Deployment Guide

> Reading Path 2: **Get the system running** — Deploy the Minecraft Game Agent end-to-end.
> Back to [User Manual §2.6.7](../../../../en/02-user-manual.md#267-minecraft-game-agent)

---

## Architecture Overview

```
[Windows 11]
  Minecraft Java Edition (recommended 1.20.4)
       ↑ localhost:25565
  mineflayer bridge (Node.js)           ← Bot engine, bot name: paos
       ↑ localhost:3001 (HTTP API)
  ngrok tunnel                          ← Public internet exposure
       ↓

[Linux Cloud — PhyAgentOS]
  MinecraftTarget                       ← HTTP client, connects to ngrok URL
       ↑
  MinecraftSkillRuntime                 ← Episode drive loop
       ↑
  WatchdogSupervisor                    ← Session supervisor
       ↑
  Agent (Planner/Critic)               ← Issues tasks via SESSIONS.md
```

**Key design**:
- MinecraftTarget is a clean `BaseLocalTarget` implementation with zero Minecraft protocol code
- mineflayer bridge is an independent external service (Node.js); OS only connects via HTTP
- No pyCraft dependency

---

## 1. Prerequisites

| Component | Location | Notes |
|-----------|----------|-------|
| Minecraft Java Edition | Windows 11 | Recommended 1.20.4 (stable mineflayer support; 1.21.5 protocol too new) |
| Node.js | Windows 11 | 18+ |
| ngrok | Windows 11 | Free tier; requires sign-up |
| Python | Linux cloud | ≥ 3.11, httpx built-in |

---

## 2. Windows 11 Setup

### 2.1 Install Node.js

```powershell
winget install OpenJS.NodeJS.LTS
```

If winget is unavailable, open `https://nodejs.org/en/download` in browser, download Windows Installer (.msi), and install.

**Close and reopen PowerShell** after installation, then verify:

```powershell
node --version   # → v22.x
npm --version    # → 10.x
```

### 2.2 Create Bridge Project

```powershell
mkdir E:\mc_bridge
cd E:\mc_bridge
```

### 2.3 Create package.json

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

### 2.4 Obtain bridge_server.js

The bridge source is at `docs/scenarios/game/minecraft/bridge_server.js` in the PhyAgentOS repo.

Copy to Windows via SCP:
```powershell
scp user@linux-server:/path/PhyAgentOS/docs/scenarios/game/minecraft/bridge_server.js E:\mc_bridge\
```

### 2.5 Install npm Dependencies

```powershell
npm install
```

### 2.6 Install ngrok

1. Download Windows version from https://ngrok.com/download
2. Sign up for free ngrok account (no credit card required)
3. Get your authtoken: https://dashboard.ngrok.com/get-started/your-authtoken
4. Configure:
```powershell
ngrok config add-authtoken <your-token>
```

### 2.7 Launch Minecraft 1.20.4

mineflayer's protocol library lags behind new versions. **Create a 1.20.4 game instance**:

```
Minecraft Launcher → Installations → New Installation
  → Name: "1.20.4", Version: release 1.20.4
  → Create → Launch
```

Enter a single-player world → Esc → **Open to LAN** → Allow Cheats: ON.

Chat should show "Local game hosted on port 25565".

> Alternative: Use Paper 1.20.4 server. `java -jar paper-1.20.4-496.jar nogui`, connect both client and bridge to `localhost:25565`.

### 2.8 Start Bridge

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

Expected output:
```
[bridge] Bot spawned: paos
[bridge] HTTP API listening on port 3001
[bridge] 3D viewer (first-person) on http://localhost:3007
```

The bot auto-teleports to you after spawning (no OP needed; moves entity coordinates directly).

Verify bridge API:
```powershell
# Open another PowerShell
curl.exe http://localhost:3001/health
# → {"ok":true,"bot_spawned":true,"uptime_seconds":5}
```

### 2.9 Establish ngrok Tunnel

```powershell
# Open another PowerShell
ngrok http 3001 --region=ap
```

Output:
```
Forwarding  https://abc123.ap.ngrok-free.app → http://localhost:3001
```

**Note this ngrok URL** — the Linux cloud will connect through it.

---

## 3. Linux Cloud — Verification

### 3.1 Confirm OS Code Ready

```bash
python -m pytest tests/runtime/test_minecraft_target.py tests/runtime/test_minecraft_skill_runtime.py -q
# → 26 passed
```

### 3.2 Quick Test

```python
from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget

# bridge_url = the ngrok address from §2.9
t = MinecraftTarget({"bridge_url": "https://abc123.ap.ngrok-free.app"})

t.build()                         # HTTP GET /health → verify bridge reachable
obs = t.reset({})                 # Initial observation

# Chat
t.step({"type": "chat", "params": {"message": "Hello from PhyAgentOS!"}})

# Move bot to absolute coordinates
t.step({"type": "move", "params": {"dx": 10, "dy": 64, "dz": 0, "absolute": True}})

# Check status
print(obs["info"]["position"])    # {"x": 100.5, "y": 64.0, "z": 200.0}
print(obs["info"]["health"])      # 20.0

t.close()
```

### 3.3 Observation Space

```python
{
    "state":  np.array([x, y, z, yaw, pitch, health, hunger, held_slot], dtype=float32),
    "image":  np.zeros((224, 224, 3), dtype=uint8),    # No rendering
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

> `inventory_items` is the full inventory flattened (all slots, not just the
> 9 hotbar slots), for the tech-tree benchmark evaluator. `inventory.hotbar`
> is hotbar-only, for the agent to read the currently held item.

---

## 4. Action Space (17 types)

All actions sent to bridge via `POST /action`; bridge executes via mineflayer API.

### Movement & Looking

```python
# Pathfind to absolute coordinates
t.step({"type": "move", "params": {"dx": 100, "dy": 64, "dz": 200, "absolute": True}})

# Relative movement
t.step({"type": "move", "params": {"dx": 5, "dz": 0}})

# Turn
t.step({"type": "look", "params": {"yaw": 90.0, "pitch": 0.0}})
```

### Key Controls

```python
t.step({"type": "jump",     "params": {"duration_ms": 500}})
t.step({"type": "sneak",    "params": {"start": True}})
t.step({"type": "sprint",   "params": {"start": True}})
```

### Block Operations

```python
# Mine block at coordinates (async, mineflayer handles timing)
t.step({"type": "dig",   "params": {"x": 100, "y": 63, "z": 200}})

# Place block on specified face
# face: 0=down 1=up 2=north 3=south 4=west 5=east
t.step({"type": "place", "params": {"x": 100, "y": 63, "z": 200, "face": 1}})
```

### Entity Interaction

```python
t.step({"type": "attack",   "params": {"target_type": "pig"}})
t.step({"type": "interact", "params": {"entity_id": "..."}})
```

### Item Operations

```python
t.step({"type": "use",         "params": {}})
t.step({"type": "select_slot", "params": {"slot": 0}})
t.step({"type": "drop",        "params": {}})
```

### Chat & Commands

```python
t.step({"type": "chat", "params": {"message": "hello"}})
```

### Advanced Actions

```python
# Auto-collect 10 oak logs (mineflayer-collectblock)
t.step({"type": "collect", "params": {"block_type": "oak_log", "count": 10}})

# Craft crafting table
t.step({"type": "craft",   "params": {"recipe_id": "crafting_table", "count": 1}})

# Equip item
t.step({"type": "equip",   "params": {"item": "stone_pickaxe", "destination": "hand"}})

# Smelt with a nearby furnace
t.step({"type": "smelt",   "params": {"input": "raw_iron", "fuel": "coal", "count": 1}})
```

---

## 5. TARGETS.md Configuration

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
      bridge_url: "https://abc123.ap.ngrok-free.app"   # ← ngrok public URL
      step_delay: 0.1
      verify_ssl: false
```

## 6. SKILLRUNTIME.md Configuration

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

## 7. Full Pipeline: Agent Task Dispatch

### 7.1 Agent Writes SESSIONS.md

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

### 7.2 WatchdogSupervisor Execution

```
1. Read SESSIONS.md → Parse SessionSpec
2. Bind MinecraftTarget + MinecraftSkillRuntime
3. MinecraftSkillRuntime.run():
     target.build()           # HTTP GET /health
     target.reset(ctx)        # → observe()
     loop:
       observe()              # HTTP GET /state
       adapter.to_runtime_observation()
       pick_action(plan)      # Read from runtime_hints
       target.step(action)    # HTTP POST /action → mineflayer
       until done/success/max_steps
4. SessionResult → ENVIRONMENT.md + LESSONS.md
```

---

## 8. OS File Manifest

| File | Lines | Description |
|------|-------|-------------|
| `runtime/targets/game/minecraft_target.py` | 182 | MinecraftTarget (HTTP client, extends BaseLocalTarget) |
| `runtime/adapters/minecraft/minecraft_adapter.py` | 83 | Observation/Action normalization |
| `runtime/skillruntime/game/minecraft_skill_runtime.py` | 115 | Episode drive loop |
| `runtime/targets/factory.py` | +4 | Register MinecraftTargetRuntime |
| `runtime/adapters/factory.py` | +3 | Register minecraft_adapter |
| `tests/runtime/test_minecraft_target.py` | 16 tests | Unit tests (Mock HTTP bridge) |
| `tests/runtime/test_minecraft_skill_runtime.py` | 3 tests | Skill runtime tests |
| `docs/scenarios/game/minecraft/bridge_server.js` | 390 | mineflayer bridge (deploy to Windows; includes `/benchmark/reset`, `/phase` endpoints) |

**Test results**: 26 passed, 0 failed.

---

## 9. Known Limitations

| Limitation | Description |
|------------|-------------|
| Minecraft version | mineflayer supports 1.20.4 stably; 1.21.5+ not yet supported by minecraft-data |
| No image observation | `observation.image` returns zero array; use prismarine-viewer (3007) for screenshots |
| ngrok domain changes | Free tier assigns random subdomain on restart; update `bridge_url` in TARGETS.md |
| Bridge restart needed | Bot despawns when bridge disconnects; restart `node bridge_server.js` |
| SSL certificate | Free ngrok has incomplete certs; set `"verify_ssl": false` in config |

---

> Next: [Usage Guide](usage.md) — CLI control, in-game chat listener, bot teleporting

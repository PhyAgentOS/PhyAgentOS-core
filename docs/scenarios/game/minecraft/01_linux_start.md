# PhyAgentOS × Minecraft — Linux 本地部署文档

> 适用场景：Minecraft 服务端、mineflayer bridge、PhyAgentOS 都运行在同一台 Linux 机器上。
> 区别于 [0_start.md](0_start.md) 中的 Windows + ngrok 远程部署方案，本文档走的是 **本机 localhost 直连**。

---

## 一、架构概览

```text
[同一台 Linux]
  ① Minecraft 服务器 (Java, 1.20.4, offline-mode=false)   :25565
        ↑ localhost
  ② mineflayer bridge (Node, bridge_server.js)            :3001 (HTTP) / :3007 (3D viewer)
        ↑ localhost
  ③ PhyAgentOS → MinecraftTarget(bridge_url=http://localhost:3001)
```

**关键区别**：
- 不需要 `ngrok`
- 不需要 Windows Minecraft Launcher
- 推荐直接使用 `Paper 1.20.4`
- `bridge_url` 直接填 `http://localhost:3001`
- `verify_ssl` 基本不用管，因为 localhost 没有证书问题
- Minecraft 服务端运行在 Linux，而不是 Windows 客户端或 Windows 本地世界

---

## 二、环境要求

| 组件 | 说明 |
|------|------|
| Java 17 或 21（GA 构建） | 启动 Paper 1.20.4；版本字符串不要带 `-internal` |
| Node.js 22+ | 启动 mineflayer bridge |
| Python 3.11+ | 运行 PhyAgentOS |
| Minecraft 服务端 | 推荐 `Paper 1.20.4` |

如果要启用 `prismarine-viewer`，需要注意 `canvas` 是原生依赖，Linux 上通常还需要系统级开发包。
另外，Linux 单机方案里推荐服务端使用：

```text
online-mode=false
```

否则 mineflayer 的离线 bot `paos` 通常无法直接加入。

建议目录结构：

```text
/home/you/mc_server      # Minecraft 服务端目录
/home/you/mc_bridge      # bridge 工程目录
/path/to/PhyAgentOS      # 本仓库
```

---

## 三、安装系统依赖

### 3.1 安装 Java / Node.js / Python

以 Ubuntu / Debian 为例：

```bash
sudo apt update
sudo apt install -y openjdk-17-jre-headless python3 python3-pip
```

Node.js 建议使用 22+。如果系统源版本偏旧，推荐用 `nvm`：

```bash
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.nvm/nvm.sh
nvm install 22
nvm use 22
```

检查版本：

```bash
java -version
node --version
npm --version
python3 --version
```

### 3.2 安装 `canvas` 编译依赖

`prismarine-viewer` 依赖 `canvas`。如果你的 Node 版本或平台没有可直接使用的预编译包，`npm install` 时会走本地编译，因此需要先安装这些系统包。

Ubuntu / Debian 常用依赖：

```bash
sudo apt update
sudo apt install -y \
  build-essential \
  pkg-config \
  libcairo2-dev \
  libpango1.0-dev \
  libjpeg-dev \
  libgif-dev \
  librsvg2-dev
```

如果后续 `npm install` 仍报原生编译错误，再补：

```bash
sudo apt install -y python3 make g++
```

说明：
- `canvas` 负责 viewer 所需的图形能力
- `prismarine-viewer` 启动失败时，常见根因就是这里缺系统依赖

---

## 四、启动 Minecraft 服务端

如果你已经准备好了 Paper 服务端目录，例如：

```text
/home/sicko/mc_server
```

如果还没有服务端目录，可以按下面的最小流程准备：

```bash
mkdir -p ~/mc_server
cd ~/mc_server
echo "eula=true" > eula.txt
```

`Paper 1.20.4` 的 `paper.jar` 需要你自行下载到这个目录中。

首次启动：

```bash
cd ~/mc_server
java -jar paper.jar nogui
```

然后检查并修改：

```bash
grep '^online-mode=' server.properties
```

将 `server.properties` 中这些项设置为：

```text
online-mode=false
enforce-secure-profile=false
spawn-protection=0
```

说明：
- 这样 mineflayer 的 bot `paos` 才能直接加入
- 如果保持 `online-mode=true`，通常需要正版登录链路，当前这条 Linux 本地方案不依赖它

正式启动：

```bash
cd ~/mc_server
java -jar paper.jar nogui
```

成功标志：

```text
Done (...)! For help, type "help"
```

默认监听端口应为：

```text
25565
```

如果要检查端口配置：

```bash
grep '^server-port=' server.properties
```

---

## 五、准备 bridge 工程

示例目录：

```bash
mkdir -p /home/sicko/mc_bridge
cd /home/sicko/mc_bridge
```

把仓库中的 bridge 文件复制过来：

```bash
cp /path/to/PhyAgentOS/docs/scenarios/game/minecraft/bridge_server.js .
```

如果目录里还没有 `package.json`，可使用最小依赖：

```json
{
  "dependencies": {
    "canvas": "^3.2.3",
    "express": "^5.2.1",
    "mineflayer": "^4.37.1",
    "mineflayer-collectblock": "^1.6.0",
    "mineflayer-pathfinder": "^2.4.5",
    "prismarine-viewer": "^1.33.0"
  }
}
```

仓库已经提交 `package-lock.json`，安装依赖时使用锁文件：

```bash
cd /home/sicko/mc_bridge
npm ci
```

如果你只想先确认依赖是否完整，可以单独安装 `canvas` 看是否成功：

```bash
cd /home/sicko/mc_bridge
npm install canvas
```

如果这里失败，通常不是 `bridge_server.js` 的问题，而是系统级编译依赖没装全。

---

## 六、启动 bridge

在 bridge 目录运行：

```bash
cd /home/sicko/mc_bridge
MC_HOST=localhost \
MC_PORT=25565 \
MC_VERSION=1.20.4 \
BOT_NAME=paos \
BRIDGE_PORT=3001 \
VIEWER_PORT=3007 \
node bridge_server.js
```

成功输出类似：

```text
[bridge] Starting for Minecraft 1.20.4
[bridge] HTTP API listening on port 3001
[bridge] Bot spawned: paos (MC 1.20.4)
[bridge] 3D viewer (first-person) on http://localhost:3007
```

bot 首次加入后，在 Paper 服务端控制台执行：

```text
op paos
```

普通动作不一定需要管理员权限，但 benchmark reset 会执行 `/tp`、`/fill`、
`/give`、`/clear` 和 `/setblock`，缺少 OP 权限时无法可靠初始化 arena。

说明：
- `3001` 是 HTTP bridge API
- `3007` 是浏览器第一人称 viewer

---

## 七、本地验证

### 7.1 检查 bridge 健康状态

```bash
curl http://localhost:3001/health
```

期望返回：

```json
{"ok":true,"bot_spawned":true,"uptime_seconds":5}
```

### 7.2 检查状态观察

```bash
curl http://localhost:3001/state
```

返回中应至少包含：
- `bot.position`
- `world.time`
- `nearby_blocks`
- `players`
- `last_chats`

### 7.3 测试动作执行

聊天：

```bash
curl -X POST http://localhost:3001/action \
  -H 'Content-Type: application/json' \
  -d '{"type":"chat","params":{"message":"Hello from Linux bridge"}}'
```

转头：

```bash
curl -X POST http://localhost:3001/action \
  -H 'Content-Type: application/json' \
  -d '{"type":"look","params":{"yaw":90,"pitch":0}}'
```

前进：

```bash
curl -X POST http://localhost:3001/action \
  -H 'Content-Type: application/json' \
  -d '{"type":"move","params":{"forward":3}}'
```

### 7.4 打开浏览器观察视角

浏览器访问：

```text
http://localhost:3007
```

---

## 八、连接 PhyAgentOS

本地 Linux 方案下，不再使用 ngrok 地址，而是直接使用：

```text
http://localhost:3001
```

例如：

```bash
cd /path/to/PhyAgentOS
paos minecraft say "说你好" --url http://localhost:3001
```

或在 Python 中：

```python
from PhyAgentOS.runtime.targets.game.minecraft_target import MinecraftTarget

t = MinecraftTarget({
    "bridge_url": "http://localhost:3001",
    "verify_ssl": False,
})

t.build()
obs = t.reset({})
print(obs["info"]["position"])
```

---

## 九、长期运行建议

### 方案 A：使用 `tmux`

服务端：

```bash
tmux new -s mc_server
cd /home/sicko/mc_server
java -jar paper.jar nogui
```

bridge：

```bash
tmux new -s mc_bridge
cd /home/sicko/mc_bridge
MC_HOST=localhost \
MC_PORT=25565 \
MC_VERSION=1.20.4 \
BOT_NAME=paos \
BRIDGE_PORT=3001 \
VIEWER_PORT=3007 \
node bridge_server.js
```

### 方案 B：使用 `systemd`

如果后续需要开机自启，可以再补 `mc_server.service` 和 `mc_bridge.service`。
第一阶段建议先用 `tmux` 跑通，排障更直接。

---

## 十、常见问题

### 1. `curl http://localhost:3001/health` 连接失败

说明 bridge 没起来，优先检查：
- `node bridge_server.js` 是否仍在运行
- `MC_PORT` 是否和 Minecraft 服务端端口一致
- Minecraft 服务端是否已启动

### 2. `npm install` 卡在 `canvas`

优先检查是否缺少系统依赖：

```bash
sudo apt install -y \
  build-essential \
  pkg-config \
  libcairo2-dev \
  libpango1.0-dev \
  libjpeg-dev \
  libgif-dev \
  librsvg2-dev
```

然后重试：

```bash
cd /home/sicko/mc_bridge
npm install
```

如果日志里出现类似：

```text
Package 'pangocairo' not found
```

通常说明 `libpango1.0-dev` 或 `pkg-config` 没装好。

### 3. `bot_spawned: false`

说明 HTTP 服务已启动，但 bot 还没真正连进 Minecraft 世界。
检查：
- `MC_VERSION` 是否与服务端兼容
- 服务端是否允许连接
- 控制台里是否有 `Kicked` / `Disconnected`

### 4. 浏览器能打开 `3007` 但画面不动

优先检查：
- bot 是否真的在世界中移动
- bridge 控制台是否有异常
- `prismarine-viewer` 是否成功在 `spawn` 后启动

### 5. `collectBlock` 报错

当前 bridge 已内置：
- `chestLocations`
- `chestsToOpen`
- `tempChests`

如果仍异常，先查看 bridge 控制台中的：

```text
[bridge] collect failed: ...
[bridge] collectBlock threw: ...
```

---

## 十一、推荐使用顺序

推荐按这个顺序排查：

1. 先启动 `mc_server`
2. 再启动 `mc_bridge`
3. 用 `curl /health` 和 `curl /state` 检查
4. 用 `POST /action` 做最小动作验证
5. 打开 `http://localhost:3007` 观察 viewer
6. 最后再接 `paos minecraft say`

---

## 十二、Skill Graph 预热与 benchmark

这两个入口不需要 LLM provider，默认使用确定性的 Mineflayer baseline：

```bash
paos minecraft warmup \
  --url http://127.0.0.1:3001 \
  --output-dir outputs/minecraft-skill-graph

paos minecraft benchmark \
  --url http://127.0.0.1:3001 \
  --graph-dir outputs/minecraft-skill-graph/benchmark_graph \
  --output-dir outputs/minecraft-benchmark \
  --tasks wooden.obtain_oak_log \
  --run-id smoke-001
```

预热固定执行 W01-W07，每项一次独立 reset/trial。单次无混杂观测即可把对应的
成功或失败 claim 标记为 `verified`，不会做第二次 seed 验证。完成后生成只读的
`warmup_frozen/` 和从它派生的 `benchmark_graph/`。benchmark 每个 episode
结束后同步更新后者，不生成探索任务；Mineflayer 不支持世界 seed 控制，这一点会
写入所有 manifest 和结果。

完整参数、Python API 和产物结构见 [4_benchmark.md](4_benchmark.md)。可用下面
三条命令随时查看当前安装版本的帮助：

```bash
paos minecraft --help
paos minecraft warmup --help
paos minecraft benchmark --help
```

# LIBERO-10 仿真评测快速启动

按顺序完成环境准备、Skill 与 Node 安装、π0.5 权重与模型 API 配置，然后运行 libero_10 测评。每条命令下方列出了应检查的状态与注意事项。

本文包含两条路线，前 5 步共用，之后按需选择：

- **pi05（纯策略）**：动作全部由 π0.5 产生，agent 只做调度与汇报；
- **gpt6_pi05（GPT-6 监督）**：动作仍由 π0.5 产生，GPT-6 只在检查点决定放行多少步 / 改写偏置 / 给 eef 目标 / 停止。

## 0. 安装并配置 PAOS

先完整按照 PhyAgentOS-core 官方 Quickstart 的操作（含 Dora CLI 0.4.1 的安装）。

[打开官方 Quickstart](../../README_zh.md#quick-start)

**完成标准：PAOS 环境安装完成，能够通过 paos agent 进行对话。**

### 切换到 libero runtime 分支

libero 0.3.4 Skill 依赖 per-profile `required_tools`、`PAOS_SKILL_PROFILE` 透传和
`openai_responses` provider，须使用专用 runtime 分支：

```bash
export PAOS_CORE="$HOME/PhyAgentOS-core"
export RUNTIME_URL="https://gitlab.ex-ai.cn/PhyAgentOS/framework/phyagentos.git"
export RUNTIME_BRANCH="qinhan/libero-0.3.4-runtime"

if [ -d "$PAOS_CORE/.git" ]; then
  git -C "$PAOS_CORE" remote set-url origin "$RUNTIME_URL"
  git -C "$PAOS_CORE" fetch origin "$RUNTIME_BRANCH"
  git -C "$PAOS_CORE" checkout -B "$RUNTIME_BRANCH" "origin/$RUNTIME_BRANCH"
else
  git clone --branch "$RUNTIME_BRANCH" "$RUNTIME_URL" "$PAOS_CORE"
fi

cd "$PAOS_CORE"
python -m pip install -e .
hash -r
```

**检查结果：**

```bash
git -C "$PAOS_CORE" branch --show-current
git -C "$PAOS_CORE" rev-parse HEAD
dora --version
```

应分别显示 `qinhan/libero-0.3.4-runtime`、
`e3f1adee1ceab2f09a27b1c0706c2260981ace0e`、`dora-cli 0.4.1`。
commit 对不上时不要安装 libero 0.3.4。

## 1. 安装 Node 与 Skill

Skill 归档和它锁定的 Node 制品都要装好，缺一个都会在启动时报缺文件。

### 安装 libero Skill

```bash
paos skill install libero --version 0.3.4-ubuntu20.1
```

**检查结果：** 显示安装成功。若注册表里还没有 0.3.4，改用本地包：

```bash
paos skill install /abs/path/libero-0.3.4-ubuntu20.1.tar.gz --local --yes
```

### 安装 Node（node id 用 skill.yaml 里的锁名）

```bash
paos forge-node install libero gateway
paos forge-node install libero libero_benchmark
paos forge-node install libero lerobot_runner
```

**检查结果：** 每条命令各自成功。`libero_benchmark` 制品约 540 MB，首次下载需要时间。

**注意：** 注册表访问不了时可用本地制品代替，例如
`paos forge-node install libero libero_benchmark --archive /abs/path/libero_benchmark-1.0.1-ubuntu20.1-linux-x86_64.tar.gz`。

### 检查版本、profile 与制品

```bash
paos skill inspect libero
paos forge-node verify libero gateway
paos forge-node verify libero libero_benchmark
```

**检查结果：** 版本为 0.3.4-ubuntu20.1（或更高），profile 共 6 个
（act / gpt6 / gpt6_pi05 / kai0 / lingbot_va / pi05），两条 verify 均通过。

**注意：** 不要用 0.3.1-ubuntu20.1——那一版的 profile `dataflow.yaml` 漏了节点级 env
（`TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD`、`CUDA_VISIBLE_DEVICES` / `TMPDIR` / `PI05_*` 转发），
`policy.yaml` 还把权重写成构建机绝对路径，换机器必挂。0.3.4 起这些问题都已修复。

## 2. 准备 LIBERO 场景资产

benchmark 节点不含场景数据，需要宿主提供官方 bddl / init_states / assets。

### 写入 `~/.libero/config.yaml`

```yaml
benchmark_root: /abs/path/to/LIBERO/libero/libero
bddl_files:     /abs/path/to/LIBERO/libero/libero/bddl_files
init_states:    /abs/path/to/LIBERO/libero/libero/init_files
assets:         /abs/path/to/LIBERO/libero/libero/assets
```

**检查结果：** 上面四个路径都存在，且 `bddl_files` 下能找到 libero_10 的 bddl 文件。

## 3. 设置运行环境变量

每次开新终端都要设。

```bash
export PAOS_TMP=$HOME/paos-tmp
mkdir -p "$PAOS_TMP"
export TMPDIR=$PAOS_TMP TEMP=$PAOS_TMP TMP=$PAOS_TMP

# 选一张空闲卡
export CUDA_VISIBLE_DEVICES=0

# 本机回环必须绕过代理（漏了这行 start 会卡 15 分钟，最后报 /tools unavailable）
export no_proxy="127.0.0.1,localhost,::1"
export NO_PROXY="$no_proxy"
```

**检查结果：** `nvidia-smi` 里这张卡的显存基本是空的。

**注意：** `TMPDIR` 要留够空间：节点是 onefile 打包，启动时在这里解包。

**注意：** `no_proxy` 这两行不能省。机器上只要配了代理（`http_proxy` / `https_proxy` /
`ALL_PROXY`）而 `no_proxy` 里没有 `127.0.0.1`，健康检查请求就到不了本机 gateway。

## 4. 准备 π0.5 权重

权重不打进 Skill，用环境变量指路。

```bash
export PI05_MODEL_DIR=/abs/path/to/pi05_libero_finetuned_v044
export PI05_TOKENIZER_DIR=/abs/path/to/paligemma-3b-pt-224-tokenizer
```

**检查结果：** 两个目录都存在且非空：`$PI05_MODEL_DIR` 下应有 `config.json`、
`model.safetensors`（约 7.5 GB）、`policy_preprocessor.json`；`$PI05_TOKENIZER_DIR` 下应有
`tokenizer_config.json` 等。

**缺失时下载（固定 revision）：**

```bash
python ~/.PhyAgentOS/skills/libero/scripts/download_pi05.py \
  --model-dir "$PI05_MODEL_DIR" --tokenizer-dir "$PI05_TOKENIZER_DIR"
```

**检查结果：** 结束时打印两个目录的绝对路径。只检查不下载就加 `--verify-only`。

**注意：** tokenizer 来自 gated 仓库 `google/paligemma-3b-pt-224`，下载前要接受许可并设置
`HF_TOKEN`；已有本地副本就不用设。

**注意：** 节点真正读的是 profile `policy.yaml` 里的 `pretrained_path` / `tokenizer_path`
（0.3.4 起写为 `${PI05_MODEL_DIR}` / `${PI05_TOKENIZER_DIR}`，由 `dataflow.yaml` 的节点 env
转发）。启动钩子校验通过、但 dora 日志里策略节点报找不到权重目录时，先查这一处。

## 5. 配置 LLM API

`paos agent` 本身是一个 LLM 循环，没有可用的 API key 会直接停在
`Error: No API key configured. Set one in ~/.PhyAgentOS/config.json under providers section`。
paos 只从 `~/.PhyAgentOS/config.json` 读，不在别处找。不要在录屏里贴 key，用隐藏输入写入：

```bash
python - <<'PY'
import json, getpass
from pathlib import Path

key = getpass.getpass("Paste API key (输入不回显): ").strip()

cfg = {
    "agents": {
        "defaults": {
            "provider": "custom",
            "model": "deepseek-flash",
            "max_tokens": 8192,
            "temperature": 1.0,
            "maxToolIterations": 400
        }
    },
    "providers": {
        "custom": {
            "apiKey": key,
            "apiBase": "https://newapi.x-era.com/v1"
        }
    }
}

path = Path.home() / ".PhyAgentOS" / "config.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
path.chmod(0o600)
print("wrote", path)
PY
```

**检查结果：** 打印出 `wrote /…/.PhyAgentOS/config.json`。接着确认能通：

```bash
paos agent -m "你好"
```

**注意：** 这里选哪个模型只影响 agent 的调度质量，不影响 π0.5 的动作，也不影响成功率；
想省额度就用 `custom` + `deepseek-flash`。要用 GPT-6 做监督模型，把 provider 换成
`openai_responses`、model 换成 `gpt-6-astra-phyagentos`（两种键名都接受）。该模型只挂在
专有分组的通道上：普通分组的 key 只能看到 `gpt-6-astra`，直接配 `gpt-6-astra-phyagentos`
会拿到 `503 model_not_found`——先用 `apiBase + /models` 列表确认这把 key 能看到哪个 id 再填。

## 6. gpt6_pi05 路线的额外准备

pi05 路线跳过本节，直接进 §7。

### 宿主必须有 `uv`

`gpt6_pi05` 比 `pi05` 多两个随包 Python 节点（`image_vision` ×2、`vla_bridge`），它们的启动
脚本先用 `uv` 建 venv 再执行，所以宿主 PATH 里必须有 `uv`（缺 uv 不会在启动时直接报错，
而是 start 一直不出结果，15 分钟后报 `Runtime health check timed out`）：

```bash
command -v uv && uv --version || echo "缺 uv"
```

**检查结果：** 打印出版本号。缺 uv 时（不需要 sudo）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
command -v uv && uv --version
```

### 预热两个 Python 节点（必须手工做一次）

```bash
cd "$HOME/.PhyAgentOS/skills/libero/nodes/image_vision" && uv sync --quiet && echo "OK image_vision"
cd "$HOME/.PhyAgentOS/skills/libero/nodes/vla_bridge" && uv sync --quiet && echo "OK vla_bridge"
```

**检查结果：** 两行分别打印 `OK image_vision`、`OK vla_bridge`。

**注意：** 第一次会下 400 MB 左右的依赖，慢是正常的。这一步必须在启动前做完：
`startup_timeout_s` 只有 900 秒，而启动流程里同时还要把 π0.5 权重加载进显存。

### 选定 suite（必须写在这里，不是 run 的参数）

```yaml
# ~/.PhyAgentOS/skills/libero/profiles/gpt6_pi05/benchmark.yaml
suite: libero_10
```

**检查结果：** launcher 起 flow 时按这一行决定加载哪个 suite；改完必须先 stop 再重新 start。

**注意：** `vlm_vla.benchmark.run` 的 `suite` 参数**不会**切换已加载的 suite——
benchmark.yaml 写的是什么，run 跑的就是什么 suite 的 task。`task_ids` / `init_state_ids` /
`num_runs` / `max_steps` / `seed` 这几个参数仍然是 run 参数说了算。

## 7. 启动 Profile 并确认 Tool 就绪

### 启动前环境变量自检（必须通过）

Dora 0.4.1 会在 `dora start` 时展开 dataflow 里的 `${...}`。以下变量必须在**当前这个
shell** 里全部设置；重新开终端后要重新执行 §3 / §4 的 export。缺任何一个时，Dora 可能报
`nodes[...] env: data did not match any variants of untagged enum EnvValue`。

```bash
python - <<'PY'
import os
import urllib.request

required = [
    "TMPDIR",
    "CUDA_VISIBLE_DEVICES",
    "PI05_MODEL_DIR",
    "PI05_TOKENIZER_DIR",
]
missing = [name for name in required if not os.environ.get(name)]
if missing:
    raise SystemExit("缺少环境变量，不要启动：" + ", ".join(missing))
for name in required:
    print(f"{name}={os.environ[name]}")

# 代理检查：本机 gateway 不能被代理接管
proxies = urllib.request.getproxies()
if proxies and not urllib.request.proxy_bypass("127.0.0.1"):
    raise SystemExit(
        "检测到代理但没有绕过 127.0.0.1：" + str(proxies) +
        "。先执行 export no_proxy=\"127.0.0.1,localhost,::1\" NO_PROXY=\"$no_proxy\" 再启动。"
    )
print("proxy bypass 127.0.0.1: OK")
PY
```

**检查结果：** 打印四行变量值和 `proxy bypass 127.0.0.1: OK`。任何一行缺失时先回到
§3 / §4 设置，不要继续启动。

### 启动

```bash
paos skill start libero --profile pi05
# 或
paos skill start libero --profile gpt6_pi05
```

**检查结果：** 命令返回后 flow 已起，接着用下一步的 status 确认 Tool 全部就绪。

**注意：** `paos skill start` 阻塞到 flow 就绪：节点是 onefile 打包，要先在 `TMPDIR` 里解包，
pi05 冷启动（A100 + beegfs）实测约 7.5–8 分钟，gpt6_pi05（π0.5 + 两个 image_vision +
vla_bridge）8 分钟上下，其中 π0.5 权重加载占绝大部分；缓存已热的机器会快很多。
这期间 `State: starting`、`Gateway GET /tools: unavailable` 都是正常的。不要在这期间
Ctrl-C，否则没有释放的生命周期锁会让后面每条命令都报
`Error: Skill 'libero' has another lifecycle operation in progress`。

### 检查 Tool 就绪

不要只看启动命令的返回值，先确认 Runtime 与 Tool 状态。

```bash
paos skill status libero
```

**检查结果：** `State: running`、`Gateway GET /tools: ready`，且对应 profile 的 Tool 均为
ready：

- pi05：`libero.benchmark.describe`、`libero.benchmark.run`、`libero.policy` 共 3 个；
- gpt6_pi05：`vlm_vla.benchmark.describe`、`vlm_vla.benchmark.run`、`vla.set_mode`、
  `vla.decide`、`vla.get_status`、`vision.get_frame`、`vision.get_frame_wrist` 共 7 个。

**注意（gpt6_pi05）：** 第一个 episode 里常见一次 `C2 reason=policy_starved`——π0.5 还在把
权重装进显存，10 秒内一个动作都没吐出来，bridge 就把 episode 停住并开了检查点。这是正常
的，agent 答一次 `student` 就继续；不想让它出现在正式记录里，先照 §8.2 热身一集。

## 8. 跑 libero_10 测评

两条路线的结果文件都在 forge_runtime 环境目录下：

```bash
find ~/.PhyAgentOS/forge_runtime/environments -name 'libero_10_*.json' -newermt '-2 hour' | sort
```

**检查结果：** 路径形如
`.../environments/libero/<profile>/<hash>/launch/profiles/<profile>/results/libero_10_<时间戳>_gateway-<id>.json`。

### 8.1 pi05：用 agent 驱动测评

```bash
paos agent -m '用 libero skill 的 pi05 profile 跑 libero_10 测评：先 libero.policy 开 Session 并确认 running；
再 libero.benchmark.describe 读实时能力表；然后 libero.benchmark.run，
参数 task_ids=[0,1,2,3,4,5,6,7,8,9]，init_state_ids=[0,1,2,3,4]，num_runs=1，max_steps=520，seed=0。
跑到终态后读 benchmark 自己的结果文件，报告每个 task 的成功率、总成功率、每集 termination 和 num_steps，
最后停掉 policy Session。' --session cli:pi05-libero10
```

**检查结果：** agent 报告 `success_rate`、成功/总集数，以及每个 episode 的 task id /
init id / success / termination / num_steps。

**注意：** 这是 50 集（10 任务 × 5 初始状态），A100 上实测约 40–60 分钟。先用单集冒烟
（`task_ids` 和 `init_state_ids` 各给一个、`max_steps` 给 60）确认链路，再上全量。

### 8.2 gpt6_pi05：监督式测评

监督入口只能是 `set_mode(mode=stop)`：先送 `policy`/`student` 再起批次就是非监督基线，
一个检查点都不会开，agent 永远等不到要它决策的地方——这个差别是静默的。

**先热身一集。** π0.5 是懒加载，首集几乎必然出现一次 `C2 reason=policy_starved`。正式跑
之前空跑一集让策略把权重读进显存，这一集的结果丢掉：

```bash
paos agent -m '用 libero skill 的 gpt6_pi05 profile 热身：先 vla.set_mode(mode=stop)，
再起 vlm_vla.benchmark.run（suite=libero_10、task_ids=[0]、init_state_ids=[0]、num_runs=1、
max_steps=60、seed=0），C0 直接 vla.decide(mode=student) 放行，然后 vla.set_mode(mode=student)
把这一集交回策略，等它结束就停掉 Session。这一集只看链路，不看成绩。' --session cli:gpt6pi05-warmup
```

**正式批次：每个 task 单独一次调用。** `maxToolIterations` 是一次 agent 调用的工具调用
上限（默认 400），一集连带轮询和决策约 20–40 次工具调用，把 50 集塞进一次调用中途一定
被截断。正确口径是 10 次调用、每次 5 集：

```bash
for t in 0 1 2 3 4 5 6 7 8 9; do
  paos agent -m "用 libero skill 的 gpt6_pi05 profile 跑 libero_10 的 task $t 的 5 个初始状态：
  先 vlm_vla.benchmark.describe 确认 suite 是 libero_10；vla.set_mode(mode=stop) 后起 vlm_vla.benchmark.run，
  参数 suite=libero_10、task_ids=[$t]、init_state_ids=[0,1,2,3,4]、num_runs=1、max_steps=800、seed=0；
  每个检查点按 SKILL.md 附录 A §4.4 来：先 vla.get_status，需要看图时 vision.get_frame（max_age_ms=60000），
  每集最多 4 次决策、每段不超过 15 步，预算用完后立刻 vla.set_mode(mode=student) 交回剩余，
  永远不要让 episode 停在未回答的检查点上；少轮询，每集都等到终态再进下一集；
  批次结束后报告每集的 task/init/success/termination/num_steps 和总成功率，最后停掉 Session。
  判定只看 results/libero_10_*.json。" --session "cli:gpt6pi05-t${t}"
done
```

**注意：** `max_steps` 用 **800**。这是 libero_10 的历史基线口径（π0.5 在 libero_10 上
50 集 0.92 的那一批就是 800 步）；用 520 会把本来能做完的长程任务判成 `timed_out`，
成功率不能拿去和基线比。

**注意：** `maxToolIterations` 建议抬到 1000。400 跑一个 task 的 5 集勉强够，但 agent
轮询多一点就会被截断；真被截断了也不用重跑，去 `results/libero_10_*.json` 回收已经跑完
的集数。

**注意：** 单集是随机样本，不是成绩。π0.5 每次推理会重新采样动作，同一
(task, init, seed) 跑两次结果可能不同；bridge 自报的步数和 benchmark 记的 `num_steps`
也不一致（两套计步口径），别当故障。

## 9. 停止 Skill

```bash
paos skill stop libero
```

**检查结果：** 状态回到 stopped，节点进程退出、显存释放。

**注意：** 换 profile 前必须先停干净：三个 profile 的节点 id 和端口都一样，两个 flow
同时跑会互相干扰（表现为后起的那个 action server 一直报 `ACTION_NO_ROBOT_STATE`）。

**排障：** 如果 `stop` / `start` 报 `another lifecycle operation in progress`，说明有一个
卡住的 `paos skill start` 还占着 `~/.PhyAgentOS/run/skills/.locks/libero.lock`：
`ps -ef | grep 'skill start libero'` 找到它、`kill -9` 掉，再 `paos skill stop libero --force`。

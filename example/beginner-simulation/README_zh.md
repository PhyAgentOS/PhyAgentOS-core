# 从自然语言对话到可编辑的仿真流程

[English](README.md) | [简体中文](README_zh.md)

先体验熟悉的对话 CLI，再在 MuJoCo 中运行 Piper 小任务，把查看器替换为 MP4 录制节点，最后修改一份本地节点副本并重新接入。无需真实机器人。

**验证状态：** 三种原生 Tool API 流程已在 Linux x86_64 验证通过。[功能报告与视频](VALIDATION_zh.md)记录了结果、首次失败及适用范围；本次未验证由模型驱动的对话操作。

## 1. 从熟悉的对话开始

`paos agent` 类似编程 Agent CLI：接受目标、展示工具活动、支持多轮追问。**Skill** 打包工作流程、机器人工具及连接可执行 **Node** 的 **dataflow**；**profile** 选择其中一种具体配置。

在包含本示例的仓库根目录操作，使用 Linux x86_64、Python 3.11 或 3.12：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
paos onboard
paos provider configure openrouter
paos provider use openrouter --model anthropic/claude-sonnet-4
paos agent
```

可按[模型配置指南](../../docs/zh/02-user-manual.md#2-配置模型与-forge)选择自己支持的服务商和模型，在隐藏输入中填写密钥。先问：“解释已安装的 Skill 和运行中的 profile 有什么区别，暂时不要启动任何东西。”按 Ctrl+D 退出。聊天需要模型连接，后面的确定性动作脚本不需要模型账号。

## 2. 生成本地教程 Skill 并运行查看器 profile

安装带 libx264 的 ffmpeg、ffprobe、GLFW/OpenGL 以及 libxkbcommon-x11-0、libxcb-xkb1 依赖。准备可用桌面显示，或使用下方的无桌面服务器方法。在已激活的 venv 中，从仓库根目录执行：

```bash
python -m pip install 'dora-rs==0.4.1' 'dora-rs-cli==0.4.1'
dora --version
ffmpeg -version
paos skill install robot-control-sim --version 0.1.2
paos skill inspect robot-control-sim
BEGINNER_ROOT="$PWD/example/beginner-simulation"
BEGINNER_WORK="$PWD/work/beginner-simulation"
BEGINNER_PYTHON="$VIRTUAL_ENV/bin/python"
mkdir -p "$BEGINNER_WORK"
python "$BEGINNER_ROOT/build_skill.py" \
  --upstream "$HOME/.PhyAgentOS/skills/robot-control-sim" \
  --output "$BEGINNER_WORK/recording-skill" --python "$BEGINNER_PYTHON" \
  --video "$BEGINNER_WORK/simulation.mp4"
python scripts/package_skill.py "$BEGINNER_WORK/recording-skill" --output-dir "$BEGINNER_WORK/dist"
paos skill install "$BEGINNER_WORK/dist/robot-control-sim-beginner-0.1.2.tar.gz" --local
paos skill start robot-control-sim-beginner --profile mujoco-piper
paos skill status robot-control-sim-beginner
python "$BEGINNER_ROOT/exercise.py" --report "$BEGINNER_WORK/viewer-exercise.json"
paos skill stop robot-control-sim-beginner
```

构建脚本不修改已安装的原始包，而是生成两个本地对比 profile，并为 Tool 控制输入显式设置 queue_size=1000，缓解已发布 profile 的消息丢失问题（[Gateway #13](https://github.com/PhyAgentOS/adapter-forge-gateway/issues/13)）。有限队列不能修复所有传输或结果查询竞态，动作失败仍会让测试失败。查看器会显示仿真机械臂。脚本先读取关节及限位，再复位，让 joint1 相对初始位置移动 0.2 弧度，最后再次复位。预期报告 `"passed": true`，三个 Action 都成功，关节误差不超过 0.03 弧度。HTTP 接收回执不代表动作成功。

也可在 Skill 运行时打开 `paos agent`，输入：“在当前 Piper 仿真中读取关节及限位，复位，让 joint1 到 0.2 弧度，其余关节保持初始值，再复位。逐个等待 Action 最终结果；失败时报告原因，不自动重放结果不确定的动作。”保留独立的功能报告，切换 profile 前停止当前 Skill。

本示例锁定 robot-control-sim 0.1.2、九个 [Node 制品](../robot-control-sim-piper-x/node-locks.json)和 [Dora 0.4.1](https://github.com/dora-rs/dora/releases/tag/v0.4.1)，不可混用 Forge 2.x / Dora 1.x。Registry 的 `--version` 校验当前下载条目，并非历史版本查询。若当前条目已升级，使用保留且校验过的官方包：`paos skill install /absolute/path/robot-control-sim-0.1.2.tar.gz --local`。

## 3. 把查看器替换为录制节点

Agent/Tool API → Gateway → 运动与控制节点 → MuJoCo 的链路不变，只把观察端 image_viewer 换成 video_recorder，输入为 `mujoco/image/angle`。构建脚本只读原始 Skill，创建新的 `robot-control-sim-beginner`，保留查看器 profile，增加 `mujoco-piper-record`。

```bash
paos skill start robot-control-sim-beginner --profile mujoco-piper-record
python "$BEGINNER_ROOT/exercise.py" --report "$BEGINNER_WORK/recording-exercise.json"
```

使用独立 PAOS 实例时，将 upstream 改成该实例的实际 Skill 路径。保留 venv 的 Python 路径，不要把符号链接解析为缺少依赖的基础解释器。

等 `simulation.mp4` 出现：录制器默认收集 900 帧，以 30 fps 生成 30 秒视频。渲染较慢时收集时间更长；这里是固定帧率，不代表测量出的物理时间。然后执行：

```bash
ffprobe -v error -count_frames -show_entries stream=codec_name,width,height,nb_read_frames,duration \
  -of json "$BEGINNER_WORK/simulation.mp4"
paos skill stop robot-control-sim-beginner
```

用播放器打开 MP4。预期获得机械臂的 H.264 视频及独立的动作成功报告。视频本身不能证明任务成功。再次录制请使用新的报告和视频路径，已有文件不会被静默替换。

## 4. 修改本地节点副本并重新接入

```bash
cp -R "$BEGINNER_ROOT/nodes/video_recorder" "$BEGINNER_WORK/video-recorder-custom"
```

编辑 `video-recorder-custom/recorder.py`，把 `transform_rgb` 替换为：

```python
def transform_rgb(width: int, height: int, rgb: bytes) -> bytes:
    result = bytearray(rgb)
    rows = min(24, height)
    result[: width * rows * 3] = b"\xff\x00\x00" * (width * rows)
    return bytes(result)
```

它为每帧增加红色顶条，不发送机器人控制指令。显式指定改过的源码重新构建，确认上一轮 profile 已停止后安装：

```bash
python "$BEGINNER_ROOT/build_skill.py" \
  --upstream "$HOME/.PhyAgentOS/skills/robot-control-sim" \
  --output "$BEGINNER_WORK/custom-skill" --python "$BEGINNER_PYTHON" \
  --video "$BEGINNER_WORK/custom-simulation.mp4" \
  --recorder-source "$BEGINNER_WORK/video-recorder-custom/recorder.py"
python scripts/package_skill.py "$BEGINNER_WORK/custom-skill" --output-dir "$BEGINNER_WORK/custom-dist"
paos skill install "$BEGINNER_WORK/custom-dist/robot-control-sim-beginner-0.1.2.tar.gz" --local
paos skill start robot-control-sim-beginner --profile mujoco-piper-record
python "$BEGINNER_ROOT/exercise.py" --report "$BEGINNER_WORK/custom-exercise.json"
```

等 `custom-simulation.mp4` 出现，用 ffprobe 检查并打开观看，然后执行 `paos skill stop robot-control-sim-beginner`。红条证明改过的源码重新接入；`custom-exercise.json` 验证原控制链路仍然有效。视频、报告、源码副本和安装包都在 `BEGINNER_WORK` 下。

生成的启动器使用构建时的 venv Python 绝对路径；移动该环境后需要重新构建。这是本地编辑示例，不会修改或发布注册平台上的 Node。正式发布需在所属仓库修改源码，生成新版本和摘要并更新 Skill 锁定信息，详见 [Track B](../../docs/user_development_guide/track_b/README_zh.md)。

## 无桌面 Linux、故障处理及录制接口

服务器上安装 Xvfb、xauth，先打开独立显示环境的 shell，再激活 venv 并在该 shell 内运行上述流程：

```bash
xvfb-run -a -s '-screen 0 1280x800x24 -nolisten tcp' bash
```

保持 shell 打开直到 Skill 已停止，不要关闭其他人的显示服务或共享 Dora coordinator。19002 端口必须可用；停掉本教程的上一轮 profile，不要结束无关服务。

没有图像、编码失败、Action 失败或超时，都属于失败。保留报告并检查日志，不能通过换机器或放宽限位凑出成功。已有输出应改用新路径。窗口启动失败检查 DISPLAY、GLFW/OpenGL 和仿真日志；版本或摘要不符应取得正确官方制品，不能跳过校验。

录制器接收单行 Forge Image Arrow StructArray，字段为 width/height/encoding/step/data。支持 rgb8/bgr8/rgba8/bgra8/mono8 及行填充，拒绝深度与压缩图像，不提供 ToolEndpoint。VIDEO_FPS 为 1–60（默认 30），VIDEO_MAX_FRAMES 为 1–18000（默认 900）；输出 H.264/yuv420p，奇数尺寸补齐到偶数。解码、真实编码及清理测试：

```bash
python -m unittest discover -s tests -p 'test_beginner_*.py' -v
```

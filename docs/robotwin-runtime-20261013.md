# RoboTwin VLM＋VLA 可运行实验基线（2026-10-13）

> 本文保留 2026-10-13 的部署与实验记录。新增单回合入口、修正后的参数/计数约定与停止流程见
> [真实 benchmark 部署说明](robotwin-benchmark-deployment.md)。下文文件哈希与测试数对应历史快照，
> 不代表后续修正的代码版本。历史批次新增要求：设置共享的 `RSI_WORKER_LEASE_DIR`。


## 本分支用途

保存现阶段实际部署过的 PAOS＋RoboTwin **DeepSeek VLM＋π0.5 VLA** 代码，供团队审阅、复用和继续排查。基于 `feature/benchmark` 的 `cbf956b0e5e176a185cda9264f93c2850e4e1c3d`，集成 candidate-aware bridge、真实私有适配器、任务内冻结经验及 v12 校验/停止修复。

这是研究用运行基线，**不是 RSI 已验证有效的版本，也不是纯 VLM＋IK 的实现**。后续纯 VLM＋IK、多粒度记录、1/3/5轮经验演化属于下一阶段工作。

## 代码入口

- `example/robotwin-vla-bridge/`：执行前可见 π0.5 待执行候选动作，关节空间有界修正及工具接口。
- `example/robotwin-rsi-benchmark/`：真实 RoboTwin 回合、DeepSeek图像决策、反思冻结、任务作用域、配对评测、审计与清理。
- `PhyAgentOS/forge/benchmark/private.py`：仅宿主注入的真实适配器，非公开HTTP入口，不放宽已有授权/绑定与停止协议。
- `experiments/robotwin-eslp/`：保留先前 ESLP 尝试及历史说明；不是当前批次默认策略。

VLM 每次接收当前三相机原图、机器人状态、一条待执行候选，以及上一段结果和下一段意图。Student段最多10步，edit段5步，安全限幅保持。`gpt6_pi05` 只是历史 runtime profile 名；当前 VLM 请求模型ID是 **`deepseek-flash`**，不表示在用GPT。

Fixed条件是本任务一次开发失败后生成的冻结 episodic experience，新会话显式载入；None条件为空。它不是累计5轮演化，不跨任务复用，也不削弱核心的多案例泛化阈值。

## 运行环境与启动

本仓库只提供源码，不包含 RoboTwin/XPolicyLab、机器人资产、π0.5 权重、CUDA依赖、Forge节点运行环境或模型凭据。已部署环境为 Ubuntu20.04/RTX3090 和迁移后的 Ubuntu22.04/A100；不保证裸机克隆后一条命令即可运行。

需先准备：

1. RoboTwin/XPolicyLab、SAPIEN及对应GPU架构可运行的 cuRobo，π0.5/openpi环境与合法获取的权重。
2. PAOS Python环境与Forge物理runtime；`RSI_BASE/parallel10/homes/env_L02.sh` 和 `RSI_BASE/parallel10/homes/L02` 的节点/技能及本机模型配置。
3. 筛选参考 `screening_reference.json`、`CHECKPOINT_REFERENCE.json`，按脚本要求放到 `RSI_BASE/lesson_fixed4_20261012_v4/`。它们是本地实验输入，不随发布上传。
4. `RSI_CORE`、`RSI_BASE`、`RSI_AGENT_PYTHON`、`RSI_XPOLICY`，以及实际模型路径和运行时标签。历史绝对路径必须按本地环境检查，不能未经验收替换依赖。
5. 在机器本地配置 DeepSeek凭据及兼容API endpoint；不得把这些配置提交Git。

在上述环境已准备的服务器上，使用新的、尚不存在的批次目录：

```bash
export RSI_CORE=/path/to/PhyAgentOS-core
export RSI_BASE=/path/to/prepared-rsi-runtime
export RSI_AGENT_PYTHON=/path/to/paos-venv/bin/python
export RSI_XPOLICY=/path/to/XPolicyLab
export RSI_RUNTIME_LABEL='actual host / OS / GPU'
export RUN_ROOT=/path/to/new-isolated-batch

"$RSI_AGENT_PYTHON" "$RSI_CORE/example/robotwin-rsi-benchmark/deploy_dual.py" \
  --core "$RSI_CORE" --root "$RUN_ROOT" --base "$RSI_BASE" --host liuy
```

启动前审阅生成的 `MANIFEST.json`。`--host liuy/67` 是历史预置的任务/GPU/端口分配，不是自动识别硬件；需确认它们属于本机空闲资源。启动会产生真实模型请求与机器人仿真：

```bash
bash "$RSI_CORE/example/robotwin-rsi-benchmark/launch_dual.sh" "$RUN_ROOT"
```

默认每机2任务、48正式回合，另有开发回合；两机合计96正式计划。只需要诊断时，不直接运行默认全量批次。启动脚本仅检查空闲显存和端口，不自动停止其他作业。

## 修复与已知限制

- 严格JSON、schema、候选绑定和决策语义校验；最多4次带具体错误反馈重生成，不默认放行，不伪造模型漏掉的判断。
- Student携带edit等组合在原生提交前拒绝；工具断连后的停止必须验证本任务实际执行器退出。
- 保留实际STOP_PROOF和原生结果；未知交付不重发、有效机器人失败不为得到成功而重跑。模型与Skill只清理自身归属。
- 原正式计划96回合中70个有效原生结果均失败；26个未形成有效结果，不能当机器人失败或宣称96全部完成。失败批次中存在真实模型请求、经验注入和部分执行edit，但没有成功率收益证据。
- 订书机lesson曾将可视外壳压缩当成完成线索，与原生接触位置判定不一致。模型也常因缺几何/关节方向信息而不介入。
- `edit.delta_joints` 是**加到候选绝对关节目标上的额外偏置**，不是“保持候选已有增量”。将 `candidate.delta_from_qpos` 再复制进去会额外偏移。此发布保留已运行语义，不偷偷修改实验策略。
- 无完整未来动作chunk、FK/IK空间纠正；当前task-local反思只选取有限文本决策，经验质量与控制贡献仍待改进。
- 长历史查询和源服务器磁盘满曾使部分实验中断。需持续检查磁盘与原生结果，不把旧进度文件视为运行证明。

## 已有运行证据与发布验证

历史独立无VLM诊断：订书机seed102，π0.5直连和每10步全放行均98步成功，两次推理输入/随机键/完整动作输出一致；seed2001直连400步失败。这只证明该已知成功种子下透传链路可行，不是DeepSeek监督或RSI收益证明。

发布源码的三个关键文件与服务器 v12_recovery 副本SHA256一致：

| 文件 | SHA256 |
|---|---|
| `example/robotwin-rsi-benchmark/controller.py` | `9206464a1004c0a4a320aaa91addefac90685db5022dbcc2c224d6d9d70dbb42` |
| `example/robotwin-rsi-benchmark/experiment.py` | `95c9525da4c637d896a1306e6d3ff062ef4900d34f2260f49e8cd79a356c6da8` |
| `PhyAgentOS/forge/benchmark/private.py` | `a40deea8e9c48f4f0a06bc77edd99acb1034219264606a23e0a1ca3925fd9c10` |

不上传个人HOME、Session/SQLite、原始API请求/图片日志、密钥、模型配置、权重、虚拟环境或服务器数据。发布验证不启动新的GPU回合或付费模型请求。

本次发布在9服务器独立CPU测试目录，以待发布源码验证：

- benchmark、private adapter、supervision、decision repair、双机及分片专项：72 passed。
- interaction、AgentTask集成、skill runtime manifest/manager、experience演化及candidate bridge相关：106 passed，另5 subtests passed。
- 合计178项测试及5个subtests通过；不是全仓库测试，也不是纯VLM＋IK或机器人成功率验证。
- 新启动脚本通过 `bash -n`；213个Python源码通过AST语法检查；发布文件扫描未发现高置信凭据模式或运行态产物。这不是完整秘密检测保证。

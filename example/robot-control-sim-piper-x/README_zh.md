# Piper X MuJoCo Skill 开发示例

[English](README.md) | [简体中文](README_zh.md)

完整步骤见 [开发手册 demo](../../docs/user_development_guide/PIPER_X_DEMO_zh.md)。

本例基于已安装的公开 `robot-control-sim` 0.1.2 和固定版本的 AgileX 官方 Piper X 模型生成派生 Skill，
保留九个原始 Node locks，不下载或执行真机驱动。

- `build_skill.py`：可移植模型/profile 生成器，拒绝已有输出目录。
- `node-locks.json`：原始 Node 依赖声明。
- `validate_model.py`：URDF/MJCF 正运动学及仿真伺服检查。
- `accept_pipeline.py`：无需 LLM 的原生 Tool API 执行链诊断。
- `motion-fixtures.json`：合成的可达关节与末端位姿目标。
- `headless/`：可选 Linux Xvfb 钩子，需要空闲的 display `:92`。

生成制品、本地日志和下载的模型资产放在被忽略的 `work/` 中。
生成 Skill 包含 AgileX 模型许可证，上游 Skill 文件保留原声明。
协议与源码入口见 [TrackB 框架说明](../../docs/user_development_guide/track_b/README_zh.md)。

生成器专用于 Piper X；其他机器人需按教程核对模型、关节/坐标映射及验收清单，
不能把它当成通用 URDF 导入器。教程也提供 `--only joint-a` 与 `--only eef-a` 单目标诊断。

本地安装不等于平台登记或发布。复用匹配的既有 Node 身份，按目标平台流程提交新 Skill，
经过审核发布后验证消费者 API。修改 Node 源码时需发布新 Node 版本，并更新 Skill locks。

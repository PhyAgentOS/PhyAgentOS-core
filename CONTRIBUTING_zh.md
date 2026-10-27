# 参与 PhyAgentOS 开发

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING_zh.md)

欢迎报告问题、完善文档和扩展物理执行能力。建议在选择实现方式前先阅读开发手册，
并将开发重心优先放在 **TrackB：基于 Forge/Dora 的执行框架、Node 与 Skill** 上。
这是扩展机器人和环境支持的推荐起点；同时也欢迎修复和改进 Core。

## 当前开放范围

资源平台目前**暂不支持外部贡献者提交贡献，也不开放外部贡献者修改已有登记仓库的流程**。
这些能力需等待审批服务完成并开放后再支持。当前资源登记与发布由管理员维护，
不作为面向外部贡献者的提交入口。

现阶段可参考本文与开发手册进行本地开发和验证。手册中涉及平台提交、审核和发布的步骤，
是相应能力开放后的流程说明，不代表当前已经提供外部入口。GitHub 源码评审另行遵循各仓库维护者的规则；
拥有 fork 或提交 PR 不等同于获得平台审批、仓库写权限或资源发布权限。

## 先阅读开发手册

| 目标 | 阅读入口 |
| --- | --- |
| 理解 TrackB 架构与仓库职责 | [TrackB 框架说明](docs/user_development_guide/track_b/README_zh.md)及[仓库目录](docs/user_development_guide/track_b/REPOSITORIES_zh.md) |
| 理解消息、单位和节点接口 | [公共协议与重要节点类型](docs/user_development_guide/track_b/PROTOCOLS_zh.md) |
| 理解 Skill、Tool、Endpoint 和 Gateway 的关系 | [Runtime 关系说明](docs/user_development_guide/track_b/RUNTIME_zh.md) |
| 选择二次开发模式 | [Skill 开发手册](docs/user_development_guide/track_b/SKILL_DEVELOPMENT_zh.md) |
| 复用现有节点接入仿真机器人 | [Piper X MuJoCo 示例](docs/user_development_guide/PIPER_X_DEMO_zh.md) |
| 查询 Bundle 字段与 Tool 接入细节 | [Bundle 与 Tool 接入参考](docs/user_development_guide/README_zh.md) |
| 修改与测试 Core | [Core 开发者手册](docs/zh/03-developer-manual.md) |

## 建议优先参与的方向

- **机器人与环境接入：** 机器人模型、关节与坐标系映射、仿真场景和部署 profile。
- **可复用节点：** 设备与传感器 Adapter，控制、规划、感知 Operator，以及兼容的 ToolEndpoint provider。
- **Skill：** 组装兼容节点，提供有明确文档的 Tool，并补充可复现的工作流和示例。
- **集成质量：** 协议一致性、错误处理、有针对性的测试、故障排查和双语文档。

按能力需求选择改动范围最小的开发模式：

1. **修改已有 profile 的配置：** 兼容节点已支持所需行为时，优先通过配置完成。
2. **增加 profile 分支：** 新机器人或场景可以复用兼容节点时，新增部署选项。
3. **独立开发 Node 并组装 Skill：** 需要新的设备协议、算法或接口时，进入源码开发。

[Skill 开发手册](docs/user_development_guide/track_b/SKILL_DEVELOPMENT_zh.md)说明了各模式的难度、
能力要求与具体流程。[Piper X 示例](docs/user_development_guide/PIPER_X_DEMO_zh.md)提供完整参考；
复用前请核对其中的版本和模型假设。

## 开发准备与后续提交流程

1. 先复现基线，按改动范围记录相互兼容的 Core、Forge、Dora、Skill 和 Node 版本。
   新增接口或涉及多个仓库时，建议先通过
   [Issue](https://github.com/PhyAgentOS/PhyAgentOS-core/issues)讨论范围。
2. 根据仓库目录找到所属源码仓库。Node 改动归属对应节点仓库；Core 及其文档改动归属本仓库。
   每次改动保持明确、集中的目标。
3. 在本地副本或个人 fork 中开发。若所属仓库维护者接受源码 PR，可从个人 fork 向约定的目标分支提交。
   这一源码评审流程与尚未开放的外部平台贡献提交、仓库修改流程相互独立。
4. 按改动选择必要检查。机器人接入应核对模型、关节和坐标系的一致性，验证启动、代表性的原生 Tool 调用、
   失败处理与停止；尝试真机执行前先完成仿真验证。说明最终行为与验证范围的限制。
5. 配置或 API 改动同步更新文档。仓库 `README.md` 使用英文，`README_zh.md` 使用中文，顶部互相链接。
   Node 仓库应说明节点数量及入口、是否支持 Tool、Endpoint ID 与 operation、Tool API 映射；
   未提供的能力也应明确注明。参见
   [Node README 检查项](docs/user_development_guide/track_b/SKILL_DEVELOPMENT_zh.md)。
6. 保留模型与源码许可证，遵守资源版本不可变原则。源码改动导致 Node 制品变化时，应发布新版本并更新
   Skill locks。本地安装、平台登记与发布是独立步骤。外部贡献者需等待审批服务及相关流程开放，
   再按平台公布的说明提交资源或申请修改已有登记仓库。

请勿在贡献中包含凭据、私有环境配置或无关的本地日志。

维护者发布版本时，请遵循 [GitHub 与 PyPI 发布流程](docs/RELEASING_zh.md)。

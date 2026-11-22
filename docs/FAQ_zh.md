# 常见问题（FAQ）

[English](FAQ.md) | [简体中文](FAQ_zh.md) · [返回主页](../README_zh.md)

<a id="resource-registry"></a>
## Skill 与 Node 下载应使用哪个资源服务？

当前托管 dev Registry API 为 `https://paos-resource-platform.dev.x-era.com`。
配置时只填写 API 根地址，不要追加 `/v1` 或前端页面路径：

```bash
export PAOS_RESOURCE_REGISTRY_URL=https://paos-resource-platform.dev.x-era.com
```

长期使用时，在 `~/.PhyAgentOS/config.json` 中，
按本文的完整配置示例设置 `resourceRegistry.url`。非空的 `PAOS_RESOURCE_REGISTRY_URL`
**优先于配置文件**，已有的旧环境变量也要更新或取消。修改本文档不会自动迁移
用户已有配置或旧版 Core 的默认地址。

例如，安装 [LIBERO 快速启动](../example/quickstart/libero-quickstart.md) 使用的精确版本：

```bash
paos skill install libero --version 0.3.4-ubuntu20.1
```

旧地址 `paos-resource-manager.dev.x-era.com` 若返回 **HTTP 503
`no healthy upstream`**，表示该域名的网关没有健康的后端实例。先切换到当前 API
再重试，不能据此判断 Skill 版本不存在。公开 Skill／Node 查询和安装无需平台
管理员密码或 GitHub token；运行环境、系统和模型要求仍以对应 Skill 的快速启动为准。

<a id="configuration"></a>
## 完整配置示例在哪里？

配置文件位于 `~/.PhyAgentOS/config.json`；其他 Provider 和配置选项见[用户手册](zh/02-user-manual.md)。

配置保存为 camelCase，同时也接受 snake_case 输入。

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.PhyAgentOS/workspace",
      "model": "openrouter/openai/gpt-4o-mini",
      "provider": "openrouter"
    },
    "verification": {
      "serviceEnabled": true,
      "evidenceRetention": "failed",
      "maxReplansPerEpisode": 2,
      "maxVerifierCallsPerRun": 50
    },
    "evolution": {
      "enabled": true,
      "scope": "verified_forge_lineage",
      "promotionMode": "guarded_auto",
      "minSuccessfulEpisodes": 3,
      "minLessonEpisodes": 3,
      "maxLessonsPerSkill": 8,
      "maxEvolutionCallsPerRun": 20
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "YOUR_API_KEY"
    }
  },
  "forge": {
    "requestTimeoutS": 10,
    "pollIntervalS": 0.5,
    "executionTimeoutS": 300,
    "evidence": {
      "requiredImageSources": ["front"],
      "captureTimeoutS": 5,
      "postCaptureTimeoutS": 5,
      "connectionTimeoutS": 2,
      "maxArtifactBytes": 8388608,
      "associationQuality": "best_effort"
    }
  },
  "resourceRegistry": {
    "url": "https://paos-resource-platform.dev.x-era.com"
  }
}
```

`front` 只是示例。`resourceRegistry.url` 用于选择通用制品仓库；全部制品都从本地 Bundle
或显式静态 index 安装时可留空。PAOS 只连接到已显式启动且健康的 Skill Runtime manifest
所声明的 Gateway URL；启动 Agent 不会隐式启动或下载某个具体 Skill。

<a id="session-memory"></a>
## 会话记忆如何归档和恢复？

`contextWindowTokens` 仅控制归档阈值，不代表模型上下文上限；非正值关闭自动归档。当进程已加载 LiteLLM 时，请求会按其本地 catalog 解析配置中的模型 ID（包括原生 `anthropic/`、`openai/` 前缀），在已知模型窗口中预留输出空间，并保留最近一轮完整对话。网关路由必须有自己的 catalog 条目，不继承原生模型窗口。仅运行 `custom` / `openai_responses` 的进程不会为此检查额外加载 LiteLLM；若其他路径也未加载它，catalog 请求窗口保护不会启用，但分块归档仍然有效。未知部署别名不会被归档阈值强行限制请求窗口。如果已知窗口容不下当前轮次或固定指令，PAOS 会说明原因，保留原始会话。图片按每张约 2,048 tokens 的额度估算，不再把 base64 当作文本计数；实际视觉用量因模型和分辨率而异。

记忆归档采用自动工具选择，严格校验 `save_memory` 的返回结果；返回纯文本时仅追加一次格式重试。超大历史按预算分片处理，在预算允许时优先保留完整消息，并持久化进度以支持失败续跑。归档失败会显示在回复中，自动重试暂停 60 秒。`/new` 首先将完整原始会话备份到 `<workspace>/sessions/archives/`；即使模型归档失败，仍会开启新会话并告知备份路径。原始备份保留对话和待完成的归档检查点，供后续恢复；备份不等于已完成记忆摘要。

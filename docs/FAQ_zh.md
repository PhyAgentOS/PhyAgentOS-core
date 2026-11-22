# 常见问题（FAQ）

[English](FAQ.md) | [简体中文](FAQ_zh.md) · [返回主页](../README_zh.md)

<a id="resource-registry"></a>
## Skill 与 Node 下载应使用哪个资源服务？

Core v1.2.0 从 `https://paos-resource-platform.dev.x-era.com` 下载公开的 Skill 和 Node。当前终端可通过环境变量指定 Registry 的 API 根地址。

```bash
export PAOS_RESOURCE_REGISTRY_URL=https://paos-resource-platform.dev.x-era.com
```

长期使用时，将地址写入 `~/.PhyAgentOS/config.json` 的 `resourceRegistry.url`。Core 优先读取已赋值的 `PAOS_RESOURCE_REGISTRY_URL`，其次读取配置文件，最后使用内置地址。旧地址 `paos-resource-manager.dev.x-era.com` 返回 HTTP 503 时，更新当前生效的配置后重新下载。公开制品支持匿名访问。

安装包含 11 个策略配置的 LIBERO，并查看运行要求。

```bash
paos skill install libero --version 0.3.9-ubuntu20.1
paos skill inspect libero
```

`--version` 用于核对 Registry 安装包的版本。复现 6 个策略配置的快速入门示例时，从对应版本条目的 `download_url` 下载归档，再从本地安装。[LIBERO 0.3.4 Registry 条目](https://paos-resource-platform.dev.x-era.com/v1/skills/libero?version=0.3.4-ubuntu20.1) · [LIBERO 快速入门](../example/quickstart/libero-quickstart.md)

```bash
paos skill install ./libero-0.3.4-ubuntu20.1.tar.gz --local
```

<a id="configuration"></a>
## 完整配置示例在哪里？

在 `~/.PhyAgentOS/config.json` 中配置模型服务和运行选项。[用户手册](zh/02-user-manual.md)

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

将 `requiredImageSources` 设置为 Skill 提供的相机名称，例如 `front`。托管 Registry 由 `resourceRegistry.url` 指定，安装时也可选择本地 Bundle 或静态包索引。运行 `paos skill start` 启动 Skill，Agent 连接到该运行实例就绪后声明的 Gateway。

<a id="session-memory"></a>
## 会话记忆如何归档和恢复？

将 `contextWindowTokens` 设为正数即可启用自动归档。PAOS 按 Token 预算切分长对话，优先保留完整消息，每处理一块就保存一次进度。归档器核对 `save_memory` 的结果，对纯文本回复进行一次格式重试。归档错误会显示在对话中，自动重试间隔为 60 秒。

已加载 LiteLLM 的进程会从本地模型目录读取上下文窗口，为模型输出预留空间，并保留最近一轮完整对话。原生 `anthropic/`、`openai/` 模型和网关路由各自使用对应的目录条目。直接调用 `custom` 或 `openai_responses` 的进程也支持分块归档。请求超出已知窗口时，PAOS 会给出原因并保留会话。每张图片按约 2,048 个 Token 预估，实际用量取决于模型和分辨率。

输入 `/new` 可将原始对话和归档进度备份到 `<workspace>/sessions/archives/`，并开始新会话。Skill 保留的 Action 或 Session 可按调用标识向 Gateway 再次查询，随后更新运行绑定。

```bash
paos skill reconcile robot-control-sim
```

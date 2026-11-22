# Frequently asked questions (FAQ)

[English](FAQ.md) | [简体中文](FAQ_zh.md) · [Back to the homepage](../README.md)

<a id="resource-registry"></a>
## Which Resource Registry should I use for Skill and Node downloads?

Core v1.2.0 downloads public Skill and Node bundles from `https://paos-resource-platform.dev.x-era.com`. To select the Registry for a shell, set its API origin.

```bash
export PAOS_RESOURCE_REGISTRY_URL=https://paos-resource-platform.dev.x-era.com
```

For a persistent setting, use `resourceRegistry.url` in `~/.PhyAgentOS/config.json`. Core reads a populated `PAOS_RESOURCE_REGISTRY_URL` first, then the configuration file, then the built-in URL. To switch from the legacy `paos-resource-manager.dev.x-era.com` endpoint after an HTTP 503 response, update the active setting and retry. Public downloads support anonymous access.

Install the eleven-profile LIBERO release and inspect its runtime requirements.

```bash
paos skill install libero --version 0.3.9-ubuntu20.1
paos skill inspect libero
```

The `--version` option checks the version of the Registry bundle. To reproduce the six-profile quickstart, download the archive from the `download_url` in its versioned Registry entry and install it locally. [LIBERO 0.3.4 Registry entry](https://paos-resource-platform.dev.x-era.com/v1/skills/libero?version=0.3.4-ubuntu20.1) · [LIBERO quickstart](../example/quickstart/libero-quickstart.md)

```bash
paos skill install ./libero-0.3.4-ubuntu20.1.tar.gz --local
```

<a id="configuration"></a>
## Where can I find a full configuration example?

Configure providers and runtime options in `~/.PhyAgentOS/config.json`. [User manual](en/02-user-manual.md)

Core writes camelCase keys and also accepts snake_case input.

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

Set `requiredImageSources` to a camera name from your Skill, such as `front`. Use `resourceRegistry.url` for a hosted registry or choose a local bundle or a static package index at install time. Start the Skill explicitly with `paos skill start`. The Agent connects to the Gateway declared by that healthy runtime.

<a id="session-memory"></a>
## How are session memories archived and recovered?

Set `contextWindowTokens` to a positive archival threshold to enable automatic archival. PAOS divides long histories into bounded chunks, keeps complete messages together when they fit, and saves a checkpoint after each chunk. The archiver checks the `save_memory` result, gives plain-text responses one format-repair retry and reports archival errors in the conversation. Automatic retries pause for 60 seconds.

When LiteLLM is loaded, PAOS uses its local catalog to resolve model windows, reserves room for output and retains the latest complete turn. Native `anthropic/` and `openai/` IDs use their catalog entries. Gateway routes use their own entries. Direct `custom` and `openai_responses` processes use bounded archival independently of catalog lookup. A request that exceeds a known window reports the cause and preserves the session. Image budgeting uses about 2,048 tokens per image, with actual usage set by the model and resolution.

Use `/new` to back up the raw conversation and its archival checkpoint under `<workspace>/sessions/archives/` and start a new session. For a Skill with a retained Action or Session, query the Gateway again by invocation ID and reconcile its runtime bindings.

```bash
paos skill reconcile robot-control-sim
```

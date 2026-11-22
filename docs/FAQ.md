# Frequently asked questions (FAQ)

[English](FAQ.md) | [简体中文](FAQ_zh.md) · [Back to the homepage](../README.md)

<a id="resource-registry"></a>
## Which Resource Registry should I use for Skill and Node downloads?

Use the hosted dev Registry API at `https://paos-resource-platform.dev.x-era.com`.
Set the API origin only, without `/v1` or a frontend page path:

```bash
export PAOS_RESOURCE_REGISTRY_URL=https://paos-resource-platform.dev.x-era.com
```

For persistent configuration, set `resourceRegistry.url` in `~/.PhyAgentOS/config.json`
as shown in the configuration example below. A non-empty
`PAOS_RESOURCE_REGISTRY_URL` takes precedence over that file, so update or unset any
old environment override as well. Existing configuration and older Core defaults
are not automatically migrated by changing this README.

For example, install the exact LIBERO version used by the
[LIBERO quickstart](../example/quickstart/libero-quickstart.md):

```bash
paos skill install libero --version 0.3.4-ubuntu20.1
```

If the legacy endpoint `paos-resource-manager.dev.x-era.com` returns **HTTP 503
`no healthy upstream`**, its gateway has no healthy backend. Switch the Registry
URL to the current API before retrying; this error does not establish that a Skill
version is missing. Public Skill/Node lookup and installation do not require a
platform administrator password or a GitHub token. Follow each Skill's quickstart
for its runtime, operating system and model requirements.

<a id="configuration"></a>
## Where can I find a full configuration example?

The configuration file is `~/.PhyAgentOS/config.json`; see the [user manual](en/02-user-manual.md) for other providers and options.

The configuration file is serialized in camelCase; snake_case keys are also accepted.

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

The `front` source is only an example. `resourceRegistry.url` selects a generic package registry;
it may be empty when all artifacts are installed from local bundles or a supplied static index.
PAOS connects only to the Gateway URL in the manifest of the explicitly started, healthy Skill
Runtime. It never starts or downloads a concrete Skill merely because the Agent starts.

<a id="session-memory"></a>
## How are session memories archived and recovered?

`contextWindowTokens` controls the archival threshold, not the model context limit; a nonpositive value disables automatic archival. When LiteLLM is already loaded, requests resolve the configured model ID against its local catalog (including native `anthropic/` and `openai/` prefixes), reserve output space within the known window, and keep the latest complete turn. Gateway routes require their own catalog entry; they do not inherit a native model limit. Direct-only `custom` / `openai_responses` processes do not load LiteLLM just for this check, so catalog-based request protection is inactive unless another path has loaded it; bounded archival still applies. Unknown deployment aliases do not receive an invented hard limit from the archival threshold. If known-window protection cannot fit the current turn or fixed instructions, PAOS reports the cause without deleting the session. Image inputs receive an approximate 2,048-token allowance per image rather than counting base64 as text; actual vision usage varies by model and resolution.

Memory archival uses automatic tool selection with validated `save_memory` results and one format-repair retry for plain-text responses. Large histories are processed in bounded fragments with durable progress checkpoints, preferring complete messages when they fit. Archival failures appear in the reply and retries pause for 60 seconds. `/new` first saves the complete raw session under `<workspace>/sessions/archives/`; if LLM archival fails, it still starts a new session and reports the backup path. Raw backups retain the original conversation and any pending archival checkpoint for later recovery; a backup is not a completed memory summary.

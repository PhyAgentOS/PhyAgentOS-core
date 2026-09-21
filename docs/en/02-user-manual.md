# PhyAgentOS User Manual

[中文](../zh/02-user-manual.md) · [Documentation index](../README.md)

> Documentation version: 1.0.0.

## 1. Install and initialize

PhyAgentOS supports Python 3.11 and 3.12. Forge Gateway, Dora, robot drivers, simulator assets,
and locked node artifacts are deployed separately when a robot Skill needs them.

```bash
git clone https://github.com/PhyAgentOS/PhyAgentOS-core.git
cd PhyAgentOS-core
python -m pip install -e .
paos onboard
```

For development:

```bash
python -m pip install -e ".[dev]"
pytest
ruff check PhyAgentOS tests
```

The default configuration is `~/.PhyAgentOS/config.json`; the default workspace is
`~/.PhyAgentOS/workspace`.

### Dora CLI for managed Skill profiles

Dora is not needed to run the general Agent, search for a Skill, or complete `paos skill install`.
It is a host prerequisite for `paos skill start`, because RuntimeManager uses the `dora` command to
manage the selected profile. PhyAgentOS 1.0.0 uses Dora CLI v0.4.1 and `dora-message` v0.7.0 as its
Forge Skill compatibility baseline; pin this exact release for reproducible deployments.

Linux or macOS, using the versioned official installer:

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/dora-rs/dora/releases/download/v0.4.1/dora-cli-installer.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://github.com/dora-rs/dora/releases/download/v0.4.1/dora-cli-installer.ps1 | iex"
```

With an existing Rust toolchain:

```bash
cargo install dora-cli --version 0.4.1 --locked
```

Open a new shell if the installer changed `PATH`, then verify the executable:

```bash
dora --version
# dora-cli 0.4.1
# dora-message: 0.7.0
```

RuntimeManager verifies the command interface but does not enforce the semantic version or upgrade
Dora automatically. Operators must keep the CLI, coordinator, daemon, and locked Skill Nodes on
the compatible protocol generation. The currently published Forge Skill Nodes use message format
v0.7.0; Dora v0.5.0 uses v0.8.0 and rejects those Nodes during registration.

The Python package `dora-rs` is the Python node/operator API and is not a substitute for installing
the Dora CLI. Before a coordinator and daemon are running, `dora check` is expected to report that
they are unavailable. `paos skill start` runs this check and starts them with `dora up` when needed;
after a Runtime has started, `dora check` should succeed. See the
[official Dora v0.4.1 release](https://github.com/dora-rs/dora/releases/tag/v0.4.1) for platform
assets and checksums. Do not follow an unversioned installer that can select a newer CLI.

## 2. Configure the model and Forge

Manage registered providers without editing JSON:

```bash
paos provider list                    # plain table; add --json for automation
paos provider configure               # inline keyboard selector in a TTY
paos provider configure openrouter    # enter credentials, test and select models from the API
paos provider show                    # select a provider and view masked settings
paos provider test                    # select a provider and send a minimal test request
paos provider use                     # select a provider and saved model as the default
paos provider use openrouter --model anthropic/claude-sonnet-4
paos provider login                   # select an OAuth provider and log in
paos provider remove                  # select a provider and clear stored settings
```

All management commands except OAuth login accept `--config /path/to/config.json`.
In a TTY, omitting the provider name from `configure`, `show`, `test`, `use`, `remove`, or `login`
opens the appropriate picker. Explicit names are case-insensitive and remain required outside a TTY.
The wizard uses Up/Down and Enter; Esc or Ctrl+C cancels without saving. It runs only when
stdin and stdout are terminals and never opens a full-screen UI. OAuth status means local
credentials exist, not that they have been validated. `remove` does not revoke shared OAuth
tokens or alter environment variables used by running processes.
`remove` persistently clears the provider's API key, API base, headers, default model and saved
models. It retains the provider field with `enabled: false` so environment credentials or shared
OAuth tokens cannot automatically restore it. `list` and `configure` show it as unconfigured;
the `use` and `test` pickers omit it. Removing the current default also clears the global provider,
model and reasoning effort; run `provider use` to select a new default. Successfully saving through
`provider configure` enables it again. Ollama's built-in endpoint is only a configuration suggestion
and does not make empty settings count as configured.

After API Key, API Base and optional headers, the wizard asks whether to test the connection
and fetch models. Use Up/Down and Space to select models to add, then Enter to confirm and
choose a default. Choices are saved in the provider's `models` list; configuring again can add more.
Listing models does not send a chat request; use `provider test` to verify a specific model.
If you skip discovery or the endpoint does not support it, select a saved model or enter an ID
manually. OAuth and Azure currently use this fallback; Azure requires a deployment name.
Authentication or network failures leave configuration unchanged.
`paos provider use <provider>` offers saved models in a TTY; `--model` remains available for scripts.
`provider show` includes saved models and displays the first and last four characters of long API
keys with the middle masked. Short keys are fully masked.

For Docker, CI and remote servers, select a provider and model explicitly and read secrets from
stdin, an environment variable, or a mounted file. There is no plaintext `--api-key` option:

```bash
paos provider configure openrouter --model anthropic/claude-sonnet-4 \
  --api-key-stdin < /run/secrets/openrouter_api_key
paos provider configure openrouter --model anthropic/claude-sonnet-4 \
  --api-key-env OPENROUTER_API_KEY
paos provider configure custom --api-base http://localhost:8000/v1 --model gpt-5 \
  --api-key-file /run/secrets/custom_api_key --headers-file /run/secrets/headers.json
```

`--headers-file` reads a JSON object of string header names/values. API keys and header values
are hidden in the wizard; errors never include server response bodies. Environment credentials
can also be provided at startup as `PAOS_<PROVIDER>_API_KEY` (for example,
`PAOS_OPENROUTER_API_KEY`). Standard matching provider variables such as `OPENAI_API_KEY` and
`ANTHROPIC_API_KEY` are accepted too. Gateway aliases do not borrow `OPENAI_API_KEY`.
Runtime environment values are not written to configuration unless explicitly configuring that provider.

Startup overrides apply only to the new process:

```bash
paos agent --provider openai --model gpt-5 --reasoning-effort high
paos gateway --provider openrouter --model anthropic/claude-sonnet-4
```

In the Agent, `/help` lists all commands. Use `/provider [list|<provider>]`,
`/model [list|<number>|<model-id>]`, `/effort [list|<level>|none]`, and `/status` to inspect or change
the current session. `/model list` numbers the current provider's saved models; `/model 2` selects
the second entry. Defaults from older configurations remain available. Listing is not a chat
availability check. Unsupported model routes or effort settings are rejected
without changing the session. `none` removes the explicit effort setting and uses the model default.
Custom endpoints and Azure deployment aliases with unknown capabilities should use `none`;
choose a recognized reasoning model ID to request an explicit effort.

Bare `/effort` in terminal chat shows an inline list: Up/Down moves, Enter confirms, and
Esc or Ctrl+C cancels. `/effort list` displays the current model's supported levels.
The menu and startup validation use the same provider/model capabilities: `minimal`, `low`,
`medium`, `high`, `xhigh`, or `max` appear only when supported. Extra levels require explicit
local catalog support and a working adapter mapping. Gateway choices can differ from native
provider choices. For adaptive Claude, supported `max` maps to `output_config.effort`; legacy
Claude continues to use thinking budgets. `none` always means model default, not disabled thinking.

In terminal chat, bare `/model` opens a picker of saved models across all configured providers,
labelled by provider. Use Up/Down and Enter to switch both provider and model for the current
session; the next message uses that choice. Esc or Ctrl+C cancels selection and returns to chat.
`/model list` only displays the list. Non-terminal channels retain text lists and numbered selection.

Precedence is **session override → process startup override → config default**. Each running turn
keeps its provider/model/effort through all retries and tool calls. Changes affect subsequent turns
of the same session only: other sessions, running subagents, Forge tasks, verification, evolution,
memory consolidation, Cron and Heartbeat continue using startup settings. Changes do not restart or
stop processes and never save global defaults. `/provider reset` clears session overrides;
`/new` only clears conversation history and keeps the session selection. Overrides expire when the
process exits. Use `paos provider use` to set persistent defaults for future processes.

Docker examples using the existing CLI service:

```bash
docker compose run --rm -T phyagentos-cli provider configure openrouter \
  --model anthropic/claude-sonnet-4 --api-key-stdin < /path/to/secret
docker compose run --rm -T phyagentos-cli provider use openrouter
docker compose up -d phyagentos-gateway
docker compose run --rm phyagentos-cli agent --provider openai --model gpt-5 --reasoning-effort high
```

Configure one supported model provider and the Forge timeout/evidence policy. Runtime selection is
not a configuration switch; it follows the Skill profile that you start explicitly. Configuration
is serialized in camelCase and also accepts snake_case.

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
      "maxReplansPerEpisode": 2
    },
    "evolution": {
      "enabled": true,
      "minSuccessfulEpisodes": 3,
      "minLessonEpisodes": 3
    }
  },
  "providers": {
    "openrouter": {"apiKey": "YOUR_API_KEY"}
  },
  "forge": {
    "requestTimeoutS": 10,
    "pollIntervalS": 0.5,
    "executionTimeoutS": 300,
    "evidence": {
      "requiredImageSources": ["front"],
      "associationQuality": "best_effort"
    }
  },
  "resourceRegistry": {"url": "https://paos-resource-manager.dev.x-era.com"}
}
```

The active Skill Runtime manifest is the only source of the Gateway URL. The Registry URL can also
be supplied with `PAOS_RESOURCE_REGISTRY_URL`; an empty URL permits only local bundles or an
explicit static index. Starting PAOS never downloads a Skill.

## 3. Install and run a Skill Runtime

Use a configured Registry or a schema-v3 static package index:

```bash
paos skill search <skill-name>
paos skill install <skill-name> --version <version>
# or: paos skill install <skill-name> --index /path/to/index.json
# or: paos skill install /path/to/<skill-name>-<version>.tar.gz --local

paos skill list
paos skill inspect <skill-name>
paos skill start <skill-name> --profile <profile>
paos skill status <skill-name>
paos skill switch <other-skill-name> --profile <profile>
```

`install` verifies archive size, SHA-256, the embedded file inventory, manifest v2, and locked
nodes before atomically replacing a Skill. A verified Skill lock supplies a Node digest when the
Registry omits that duplicate field; the Node download size is resolved before transfer. The
public Registry resolves a Skill by name; `--version` is checked against the downloaded manifest
before any Node download or installation commit.

Before Dora starts, a Bundle may run `start.sh` as
`bash <bundle>/start.sh <skill-name> <skill-version>` with terminal stdio inherited. These Bundles
require Bash on `PATH`; a missing Bash or non-zero hook exit records `failed` and prevents Dora from
starting. Bundles without the hook keep the normal cross-platform path. Status remains `starting`
while a long hook, such as an external asset download, is active. After the hook succeeds, PAOS
launches the named Dora profile and checks Gateway `/tools` and every required Tool context.
Overlapping lifecycle changes for the same Skill are rejected immediately. Inspect lifecycle output with
`paos skill logs <skill-name>`; stop with `paos skill stop <skill-name>`. `switch` refuses to run
while an AgentTask is non-terminal, verifies the target before publishing it, and restores the
previous Runtime if a same-Gateway target cannot start. A running Agent follows the persisted
selection before its next activation or Forge Tool call.

Node artifacts can be managed independently:

```bash
paos forge-node install <skill-name> <node-id>
# or install an independently obtained local archive
paos forge-node install <skill-name> <node-id> --archive /path/to/<node>.tar.gz
paos forge-node verify <skill-name> <node-id>
```

Concrete Forge Skills, nodes, models, and simulator assets are distributed separately from
PhyAgentOS and installed only when needed.

## 4. Start PAOS

Start the managed Skill Runtime/Gateway before the Agent when using an installed robot Skill.
`paos skill start` checks the Dora CLI and starts the local Dora coordinator and daemon when they
are not already ready. The Agent obtains the Gateway URL only from the explicitly active Skill
manifest.

```bash
paos status
paos skill start <skill-name> --profile <profile>
paos skill status <skill-name>
paos agent

# one request
paos agent -m "Inspect the motion Tool context, move the gripper forward 5 cm, and verify the result."

# long-running channels, Cron, Heartbeat, and Agent
paos gateway
```

## 5. Inspect Tool context

Before a Tool call, use `forge_tool_context(tool_id)`. It returns the ToolSpec together with live
binding, readiness, endpoint status, and robot frame information. The Agent must use the exact
input schema and must not infer frame or unit conventions.

The active Skill workflow declares the allowed Tool IDs and their Query, Action, or Session
semantics. Do not substitute a similar-looking Tool that is absent from its binding.

## 6. Use diagnostic Query or bound execution

A diagnostic Query uses the same Tool API but is not included in user-task verification:

```text
forge_tool_query(tool_id, arguments)
```

For a user-visible multi-call task:

1. Call `activate_skill(name, role="primary")` during the current turn.
2. Pass its activation ID to `forge_task_create(task_description, verification, activation_id)`.
3. Pass the returned `task_id` to every contributing Query, Action, or Session.
4. Reconcile every asynchronous invocation by its returned `invocation_id`; never invent or retry it after an unknown admission.
5. Stop task-owned Sessions, then call `forge_task_finalize(task_id)`.

Only one AgentTask may be non-terminal globally. Diagnostic Query does not occupy this slot, and all
execution still competes according to Gateway operation `max_concurrency`.

## 7. Define verification

For `audit`, `enforce`, or `recovery`, provide a goal and at least one success criterion:

```json
{
  "mode": "recovery",
  "goal": "The gripper is 5 cm forward from its starting pose.",
  "success_criteria": [
    "The final end-effector pose is approximately 5 cm forward in the declared frame.",
    "The robot reports no collision or motion failure."
  ],
  "constraints": ["Keep orientation unchanged."],
  "evidence_policy": {
    "required_kinds": ["rgb_image"],
    "required_sources": ["front"],
    "minimum_association": "best_effort"
  }
}
```

| Mode | Behavior |
|:-----|:---------|
| `off` | Derives the task result from bound Tool execution facts. |
| `audit` | Records semantic verification while preserving the execution-derived result. |
| `enforce` | Semantic verification controls success and fails closed on missing/invalid verification. |
| `recovery` | Same as enforce; `replan_required` permits a bounded new PlanRevision. |

If finalization returns `awaiting_replan`, call
`forge_task_begin_revision(task_id, reason)` and continue using the same task ID. Do not create a
second task or retry an invocation whose physical effect is unknown.

## 8. Cancellation and unknown outcomes

`forge_tool_cancel_action(invocation_id)` requests cancellation. A response such as `requested` or
`accepted` confirms only control-message handling. Continue checking status/result until Gateway
reports a known terminal result. `unknown` and local timeout are terminal for task accounting but
do not prove physical stop.

`forge_task_cancel(task_id, reason)` requests cancellation for all non-terminal bound Actions and
moves the task to `cancelling`. Reconcile the invocations, inspect physical state if necessary, and
finalize explicitly. Runtime stop remains gated while uncertain invocations are tracked unless an
operator deliberately uses force.

## 9. Experience, activation, and evolution

Use `activate_skill(name, role)` before the first workflow tool call when a registered Skill
matches. Workspace, installed, and built-in Skills are discovered in that priority order. Runtime
availability is part of activation eligibility.

Experience records all Agent tool calls and associates AgentTask, PlanRevisions, invocation
references, verification, and explicit Skill activation with one episode. Scoped Lessons are
advisory and cannot replace task criteria or evidence. Evolution is fail-open; a reflection error
does not alter execution or verification.

## 10. Persistence and retention

```text
<workspace>/
├── .paos/agent_tasks/tasks.sqlite3
├── .paos/evolution/experience.sqlite3
├── .paos/evolution/revisions/<skill>/
├── skills/<skill>/
└── artifacts/agent_tasks/<task_id>/
    ├── before_snapshot.json
    ├── after_snapshot.json
    ├── evidence_bundle.json
    └── evidence/
```

Back up SQLite files together with their WAL/SHM files while PAOS is stopped, plus the complete
artifact and Skill revision trees. `evidenceRetention` controls evidence after verification; it
does not delete execution records or evolution history.

## 11. Troubleshooting

| Symptom | Check |
|:--------|:------|
| Tool not found or not ready | Run `forge_tool_context`; confirm ToolSpec, binding, Endpoint, and Runtime profile. |
| Skill will not install | Confirm Skill bundle metadata has size and SHA-256, every Node lock has a SHA-256, and each Registry/index Node resolves to a sized direct download. |
| Skill will not start | Run `paos skill status` and `logs`; verify Dora, dataflow paths, assets, nodes, and Gateway `/tools`. |
| Dora reports message format v0.7.0 versus v0.8.0 | Stop the mismatched coordinator/daemon, install Dora CLI v0.4.1, verify `dora-message: 0.7.0`, then start the Skill again. |
| Another task is active | Read the known task with `forge_task_get`; finish or cancel it instead of editing SQLite. |
| Action result is pending | Continue status/result reconciliation using the same `invocation_id`. |
| Action result is unknown | Inspect Gateway, Dora, and physical state; do not retry blindly. |
| Verification fails | Inspect task criteria, Tool records, evidence bundle, and verifier availability. |
| No Skill Lessons load | Confirm explicit activation, Runtime availability, and eligible active scoped Lessons. |

## Next reading

- [Forge Configuration Reference](04-forge-configuration-reference.md)
- [Operations Manual](../user_manual/README_en.md)
- [Forge Tool API Integration Contract](../forge/README.md)

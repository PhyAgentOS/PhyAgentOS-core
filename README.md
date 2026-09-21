<div align="center">
  <img src="docs/imgs/logo_en.png" alt="PhyAgentOS" width="560">

  <h3>Recursive Self-Improvement Infrastructure for Physical Agents</h3>

  <p>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-core/stargazers">
      <img src="https://img.shields.io/github/stars/PhyAgentOS/PhyAgentOS-core?style=social" alt="Stars">
    </a>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-core/network/members">
      <img src="https://img.shields.io/github/forks/PhyAgentOS/PhyAgentOS-core?style=social" alt="Forks">
    </a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/Python-≥3.11-3776AB?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/Version-v1.0.0-47A882" alt="Version">
    <img src="https://img.shields.io/badge/License-MIT-3DA639" alt="License">
    <a href="https://arxiv.org/pdf/2607.16636">
      <img src="https://img.shields.io/badge/Tech_Report-arXiv-b31b1b?logo=arxiv&logoColor=white" alt="Tech Report">
    </a>
    <a href="https://phy-agent-os.net/">
      <img src="https://img.shields.io/badge/Website-online-FF6B35" alt="Website">
    </a>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS-core">
      <img src="https://img.shields.io/badge/PRs-Welcome-2EA44F" alt="PRs">
    </a>
  <p>
    <a href="https://space.bilibili.com/3546880296355920?spm_id_from=333.1007.0.0">
      <img src="https://img.shields.io/badge/Bilibili-00A1D6?logo=bilibili&logoColor=white" alt="Bilibili">
    </a>
    <a href="https://www.xiaohongshu.com/user/profile/673d83e3000000001c01a183">
      <img src="https://img.shields.io/badge/Xiaohongshu-FF2442?logo=xiaohongshu&logoColor=white" alt="Xiaohongshu">
    </a>
    <a href="https://x.com/phyagentos">
      <img src="https://img.shields.io/badge/X-000000?logo=x&logoColor=white" alt="X">
    </a>
    <a href="https://www.linkedin.com/in/phyagent-os-252372401/">
      <img src="https://img.shields.io/badge/LinkedIn-0A66C2?logo=linkedin&logoColor=white" alt="LinkedIn">
    </a>
    <a href="https://discord.gg/YJztZ4wUM">
      <img src="https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white" alt="Discord">
    </a>
  </p>
  </p>
  <p>
    <sub><a href="README.md">English</a> · <a href="README_zh.md">中文</a> · <a href="docs/README.md">Documentation</a></sub>
  </p>
</div>

---

PhyAgentOS is an agent framework for embodied tasks. The Agent plans high-level Tool calls, the Forge Tool API reports what Gateway executed, the observation collector captures before/after evidence, and the task-level verifier decides whether the user-visible goal was actually achieved.

## 📢 Changelog

| Version | Date | Update |
|:--------|:-----|:-------|
| ![v1.0.0](https://img.shields.io/badge/v1.0.0-47A882) | 2026-08-30 | Initial stable release of PhyAgentOS. |
| ![v0.2.3](https://img.shields.io/badge/v0.2.3-47A882) | 2026-08-27 | Forge Skills can be installed and managed independently, activated into immutable AgentTask bindings, and used through governed Query, Action, and Session Tool API lifecycles with recovery and version-scoped experience. |
| ![v0.2.2](https://img.shields.io/badge/v0.2.2-47A882) | 2026-08-21 | Unified Forge execution on the Query/Action Tool API and added AgentTask aggregation, a verifiable Skill Runtime, Resource Registry integration, and the move-arm-by-ee Skill while retaining Agent verification and evolution. |
| ![v0.2.1](https://img.shields.io/badge/v0.2.1-47A882) | 2026-08-14 | Added verified task experience, explicit workflow Skill activation, guarded Skill evolution, clustered scope-aware Lessons, and Skill-scoped advisory context for semantic verification. |
| ![v0.2.0](https://img.shields.io/badge/v0.2.0-47A882) | 2026-08-03 | Introduced the Forge execution architecture with Forge Gateway 1.0.0, immutable execution and evidence contracts, system-level semantic verification, Planner-owned recovery, crash-safe SQLite orchestration, and complete removal of the legacy Runtime execution chain. |
| ![v0.1.7](https://img.shields.io/badge/v0.1.7-47A882) | 2026-07-05 | Added benchmarking for policy-loop and target-native builtin paths, plus the Agent verification and failure-recovery service. |
| ![v0.1.6](https://img.shields.io/badge/v0.1.6-47A882) | 2026-06-27 | Added BEHAVIOR-1K support, `SessionVerifier`, and the explicit session-verification tool. |
| ![v0.1.5](https://img.shields.io/badge/v0.1.5-47A882) | 2026-06-11 | Cleaned protocol files and documentation, moved game scenarios to the `general-game-agent` branch, and focused the main line on simulation and real-robot work. |
| ![v0.1.4](https://img.shields.io/badge/v0.1.4-11648A) | 2026-06-05 | Improved onboarding, documented communication protocols, refined coding standards, and prepared game-agent benchmarking. |
| ![v0.1.3](https://img.shields.io/badge/v0.1.3-11648A) | 2026-05-25 | Established the strict `PolicySkillRuntime` / `BuiltinSkillRuntime` separation and advanced game-agent benchmarking. |
| ![v0.1.2](https://img.shields.io/badge/v0.1.2-11648A) | 2026-05-20 | Introduced the perception plugin system with sensor/perception configuration and auditable environment writeback. |
| ![v0.1.1](https://img.shields.io/badge/v0.1.1-11648A) | 2026-05-18 | Delivered the Session-Centered Runtime MVP with the initial dummy simulation pipeline. |
| ![v0.1.0](https://img.shields.io/badge/v0.1.0-11648A) | 2026-04-29 | Released the hackathon baseline with the plugin HAL and early ReKep, SAM3, grasping, and VLN workflows. |

## Why PhyAgentOS?

<table>
<tr><td width="32">🧭</td><td width="190"><b>One execution boundary</b></td><td>Robot actions enter through one versioned Forge Gateway contract; the Agent never reaches into a policy, simulator, Dora node, or hardware SDK.</td></tr>
<tr><td>🔎</td><td><b>Evidence before verdict</b></td><td>Validated images and optional robot state are captured around bound Actions and stored with source, sequence, time, size, digest, and retention metadata.</td></tr>
<tr><td>🧠</td><td><b>Action-agnostic verification</b></td><td>The verifier receives the goal, criteria, constraints, execution facts, evidence, lineage history, and optional Skill-scoped advisories—never an action-specific verification switch. Advisories cannot replace criteria or evidence.</td></tr>
<tr><td>🧱</td><td><b>Crash-safe task aggregation</b></td><td>SQLite transactions persist AgentTask, PlanRevision, Query records, and Gateway invocation references without creating a second physical execution protocol.</td></tr>
<tr><td>🔄</td><td><b>Planner-owned recovery</b></td><td>A recovery verdict appends a bounded PlanRevision to the same task; unknown effects are reconciled and never retried blindly.</td></tr>
<tr><td>📚</td><td><b>Scoped experience</b></td><td>Verified AgentTasks support reusable workflow Skills and clustered Lessons; unrelated failures remain diagnostics and learned guidance is loaded only with the matching Skill.</td></tr>
</table>

## Architecture

```text
User / Channel / Scheduled Event
              │
              ▼
      AgentLoop + Planner
              │  AgentTask-bound or unbound call
              ▼
       ForgeToolClient ─────────► AgentTask SQLite + evidence
              │                         │
              │ HTTP Tool API           ▼
              ▼                  ForgeTaskVerifier
 Gateway /tools → ToolInvocation        │
              │                  verdict / PlanRevision
              ▼
 ToolEndpoint → Dora → robot/simulator

terminal AgentTask ─────► Experience Coordinator ──► evolution ledger
                                │                         │
                         Skill candidates          scoped Lessons
                                └──────────► workspace Skills
```

The system keeps three records separate:

1. **Execution** — what Query ran or what Gateway ToolInvocation was accepted and how it terminated.
2. **Evidence** — what PAOS observed before the first bound Action and after all bound Actions.
3. **Verdict** — whether each system-level success criterion is satisfied.

## Core features

| Area | Current capability |
|:-----|:-------------------|
| Forge contract | One Query/Action/Session Tool API plane through `/tools` and `/invocations`. |
| Async orchestration | Query is synchronous; Action and Session admission return invocation IDs whose state is reconciled through `/invocations`. |
| Identity validation | Agent `task_id`, `revision_id`, Query record ID, Gateway `invocation_id`, and `attempt_id` remain distinct. |
| Evidence | Async `/ws/images` and `/ws/state` collection with bounded latest-frame buffers, media validation, SHA-256, and per-source sequence boundaries. |
| Verification | `off`, `audit`, `enforce`, and `recovery` modes with structured per-criterion verdicts. |
| Recovery | Bounded append-only PlanRevisions on the same task, with deadlines and no blind retry of unknown effects. |
| Persistence | SQLite WAL AgentTask event log plus workspace-relative evidence; existing evolution data remains readable. |
| Task experience | Explicit Skill activation, redacted AgentTask episodes, asynchronous reflection, clustered scoped Lessons, and guarded Skill promotion. |
| Skill Runtime | Manifest-v2 bundles, SHA-256 inventories, safe transactional installation, named Dora profiles, persistent health state, and explicit Registry resolution. |
| Agent platform | CLI and multi-channel gateway, provider abstraction, tools, skills, MCP, memory, Cron, Heartbeat, and knowledge workspaces. |

## 5-minute quick start

### 1. Install

```bash
git clone https://github.com/PhyAgentOS/PhyAgentOS-core.git
cd PhyAgentOS-core
python -m pip install -e .

# Development and tests
python -m pip install -e ".[dev]"
```

Python 3.11 or 3.12 is recommended. Concrete Forge Skills and their Runtime artifacts are
distributed separately.

Dora is not required for the general Agent or for `paos skill install`. It is required on `PATH`
when `paos skill start` launches a managed Forge Skill profile. PhyAgentOS 1.0.0 uses Dora CLI
v0.4.1 with `dora-message` v0.7.0 as its Forge Skill compatibility baseline. On Linux or macOS,
install that exact release:

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/dora-rs/dora/releases/download/v0.4.1/dora-cli-installer.sh | sh
dora --version
# dora-cli 0.4.1
# dora-message: 0.7.0
```

See the [user manual](docs/en/02-user-manual.md#dora-cli-for-managed-skill-profiles) for Windows,
Cargo installation, and lifecycle checks.

### 2. Initialize the workspace

```bash
paos onboard
```

This creates `~/.PhyAgentOS/config.json` and the default workspace at `~/.PhyAgentOS/workspace`.

### 3. Configure a provider and Forge

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
    "url": "https://paos-resource-manager.dev.x-era.com"
  }
}
```

The `front` source is only an example. `resourceRegistry.url` selects a generic package registry;
it may be empty when all artifacts are installed from local bundles or a supplied static index.
PAOS connects only to the Gateway URL in the manifest of the explicitly started, healthy Skill
Runtime. It never starts or downloads a concrete Skill merely because the Agent starts.

Atlas Cloud can be selected as an optional OpenAI-compatible provider without changing the
default configuration. Use a model ID from the live Atlas Cloud catalog:

```json
{
  "agents": {
    "defaults": {
      "model": "Qwen/Qwen3-235B-A22B-Instruct-2507",
      "provider": "atlas"
    }
  },
  "providers": {
    "atlas": {
      "apiKey": "YOUR_ATLASCLOUD_API_KEY"
    }
  }
}
```

### 4. Start the Agent

Start the required installed Skill Runtime first, then choose one of the PAOS entry points:

```bash
# Interactive CLI
paos agent

# One request; the Agent may create an AgentTask and bind Tool API calls to it
paos agent -m "Inspect Forge capabilities, then place the object in the target area and verify the visible result."

# Long-running channels, Cron, Heartbeat, Agent, and Forge Tool API integration
paos gateway
```

Use `paos status` to inspect the local model/workspace configuration. Use
`forge_tool_context` for a live ToolSpec, binding, readiness, endpoint status, and frame profile.

## Verification modes

| Mode | Task contract | Final result | Recovery |
|:-----|:--------------|:-------------|:---------|
| `off` | Goal and criteria optional | Follows Gateway execution status | Never |
| `audit` | Goal and at least one criterion required | Preserves execution-derived terminal state; records verdict/error | Never |
| `enforce` | Goal and at least one criterion required | Verdict controls success; missing evidence, invalid output, errors, and `inconclusive` fail closed | Never |
| `recovery` | Goal and at least one criterion required | Same fail-closed behavior; `replan_required` enters recovery | Planner appends a PlanRevision |

A typical non-`off` contract looks like this:

```json
{
  "mode": "recovery",
  "goal": "The red block is inside the tray.",
  "success_criteria": [
    "The red block is visibly within the tray boundary.",
    "No other object has been displaced outside the workspace."
  ],
  "constraints": [
    "Do not move the blue block."
  ],
  "evidence_policy": {
    "required_kinds": ["rgb_image"],
    "required_sources": ["front"],
    "minimum_association": "best_effort"
  }
}
```

## Agent-facing Forge tools

| Tool | Purpose |
|:-----|:--------|
| `forge_task_create/get/begin_revision/finalize/cancel` | Manage the PAOS task aggregate and user-level verification lifecycle. |
| `forge_tool_context` | Read the live ToolSpec, binding, readiness, Endpoint status, and frame profile. |
| `forge_tool_query` | Invoke a synchronous Query, optionally bound to an AgentTask. |
| `forge_tool_start_action` | Admit an asynchronous Action and retain its Gateway invocation identity. |
| `forge_tool_action_status/result/cancel_action` | Reconcile or request cancellation without treating acceptance as physical stop. |
| `forge_tool_start_session` | Start a task-owned, shared, or runtime-owned Session under the binding policy. |
| `forge_tool_session_status/result/stop_session` | Reconcile a Session and stop it only when the caller owns that lifecycle. |

The context tool is always available for diagnostics. Task and mutating tools require one healthy,
explicitly active Skill Runtime and an immutable Skill binding created from the current turn's
primary `activate_skill` result.

## Forge Skill Runtime

Installed Skills are managed explicitly. Registry downloads require either
`resourceRegistry.url`, `PAOS_RESOURCE_REGISTRY_URL`, or a supplied static index; local bundles are
never replaced until their manifest, archive inventory, and locked node executables validate.

```bash
paos skill search
paos skill install <skill-name> --version <version>
# Or install a local, independently obtained bundle
paos skill install /path/to/<skill-name>-<version>.tar.gz --local
paos skill inspect <skill-name>
paos skill start <skill-name> --profile <profile>
paos skill status <skill-name>
# With no non-terminal AgentTask, select another installed Runtime
paos skill switch <other-skill-name> --profile <profile>
paos skill logs <skill-name>
paos skill stop <skill-name>

# Verify the executable locked by the installed Skill manifest
paos forge-node install <skill-name> <node-id> --archive /path/to/<node>.tar.gz
paos forge-node verify <skill-name> <node-id>
```

Each Forge Skill bundle declares its workflow document, required Tool IDs, named runtime profiles,
and exact platform/architecture Node locks. Each locked archive has an exact SHA-256 and contains
one named root-level executable. For Registry Node downloads, the verified Skill lock supplies the
digest when the Registry omits that duplicate field, and the exact size is resolved before the
download begins; installation records and verifies the extracted binary hash.
`python scripts/package_skill.py <bundle-dir> --output-dir <directory>` creates a deterministic
bundle for publication. The PhyAgentOS source and release packages do not
bundle concrete Forge Skills, Forge nodes, models, or simulation assets; obtain only the Skills
needed for a deployment and install them explicitly.
The [integration development guide](docs/user_development_guide/README_en.md#5-package-publish-and-close-the-local-loop)
documents Bundle layout, local validation, immutable publication order, and Registry acceptance.

## Task experience and Skill evolution

When `agents.evolution.enabled` is true, the Agent checks the registered Skill summaries before
its first tool call. `activate_skill(name, role)` loads the complete workflow and applicable
scoped Lessons while recording an auditable task-to-Skill binding. A turn may have one primary
Skill and multiple supporting Skills; only the primary can be updated automatically. Reading a
`SKILL.md` directly does not create this binding. Verified AgentTasks are reflected on
asynchronously:

- workflow-related semantic failures first form normalized observations; three independent
  AgentTasks with the same failure pattern are required before an abstract scoped lesson can become
  active and project to `skills/<name>/references/LESSONS.md`;
- task impossibility, verifier/evidence limitations, infrastructure failures, and uncertain causes
  remain diagnostic-only and never become Skill lessons;
- semantic successes support a Skill candidate;
- three independent successful AgentTasks promote a validated workspace Skill revision;
- inconclusive, invalid, review-only, and `verification=off` outcomes never train a Skill.

The applicable active Lessons returned by activated Skills are frozen with the AgentTask binding.
Automatic verification, later PlanRevision verification, and review use that same scoped set as
advisory workflow context. The verifier must ground every criterion and verdict in the task
contract, execution facts, and valid evidence; a Lesson cannot satisfy a criterion or serve as an
evidence reference. If no Skill was activated, no learned Lesson is supplied.

Evolution is fail-open and does not change Forge submission, execution, evidence, verification,
or recovery. Built-in Skills remain immutable; promoted changes are written as workspace overrides
with revision history under `.paos/evolution/`.

## Persistence and workspace

```text
~/.PhyAgentOS/workspace/
├── AGENTS.md / SOUL.md / USER.md / TOOLS.md / SKILLS.md
├── EMBODIED.md / ENVIRONMENT.md / LESSONS.md / TASK.md
├── .paos/agent_tasks/tasks.sqlite3
├── .paos/evolution/experience.sqlite3
├── .paos/evolution/revisions/<skill>/
├── skills/<skill>/SKILL.md
├── skills/<skill>/references/LESSONS.md
└── artifacts/agent_tasks/<task_id>/
    ├── evidence_bundle.json
    ├── before_snapshot.json / after_snapshot.json
    └── evidence/
```

`EMBODIED.md`, `ENVIRONMENT.md`, and SceneGraph remain knowledge surfaces. They are not execution queues. With evolution enabled, the root `LESSONS.md` is retained as legacy/human-authored material but is not injected into Agent turns or Forge verification; only the activated task's frozen active scoped Lessons may accompany verification as non-authoritative advice. The experience database is authoritative for learned Lessons. PAOS no longer reads or generates the former Runtime Markdown queue files.

## Project structure

```text
PhyAgentOS/
├── PhyAgentOS/agent/          # AgentLoop, tools, memory, experience, verifier integration
├── PhyAgentOS/forge/          # Tool API client, AgentTask aggregation, and observations
├── PhyAgentOS/skill_runtime/  # Bundle validation/install and explicit Dora lifecycle
├── PhyAgentOS/verification/   # Public contracts, request builder, engine, service
├── PhyAgentOS/channels/       # Messaging channels
├── PhyAgentOS/config/         # Configuration schema and loading
├── PhyAgentOS/templates/      # Agent knowledge/workspace templates
└── docs/                      # English, Chinese, operations, integration, Forge docs
```

## Documentation

| Document | Audience | Description |
|:---------|:---------|:------------|
| [Changelog](CHANGELOG.md) | Everyone | Detailed release notes grouped by Added, Changed, and Security |
| [Documentation index](docs/README.md) | Everyone | Bilingual reading paths and document map |
| [Framework introduction](docs/en/01-framework-introduction.md) | Architects and users | Design, boundaries, lifecycle, and current scope |
| [User manual](docs/en/02-user-manual.md) | Operators and users | Installation, configuration, tasks, artifacts, and troubleshooting |
| [Developer manual](docs/en/03-developer-manual.md) | Contributors | Contracts, invariants, extension points, and tests |
| [Forge configuration reference](docs/en/04-forge-configuration-reference.md) | Deployers | Exact Forge, evidence, verification, and task fields |
| [Agent experience and Skill evolution](docs/en/05-agent-experience-and-skill-evolution.md) | Users and developers | Skill activation, episodes, Lesson clustering, promotion, persistence, and guardrails |
| [Operations manual](docs/user_manual/README_en.md) | Operations | Startup, monitoring, restart, cancellation, and incident handling |
| [Integration guide](docs/user_development_guide/README_en.md) | Integrators | Connecting Gateway actions without action-specific verifier code |
| [Unified Forge Tool API contract](docs/forge/UNIFIED_TOOL_API.md) | Gateway/PAOS developers | Query/Action/Session Tool API, immutable Skill binding, AgentTask, Runtime, verification, and recovery |

## Development

```bash
python -m pip install -e ".[dev]"
pytest
ruff check PhyAgentOS tests
python -m compileall -q PhyAgentOS tests
```

Optional black-box tests may use `FORGE_GATEWAY_URL` to connect to a running compatible Gateway. Tests and PAOS documentation must not mutate the Gateway source or configuration.

## Contributing

PRs and Issues are welcome! Check our development roadmap here → [Dev Plan](https://phy-agent-os.net/docs/developer-guide/).

---

<div align="center">

Jointly developed by **Sun Yat-sen University HCP Lab** & **Peng Cheng Laboratory** & **X-Era Lab**

<br>

<img src="docs/imgs/HCP.jpg" alt="HCP" height="128">
&nbsp;&nbsp;&nbsp;
<img src="docs/imgs/Pengcheng.png" alt="Pengcheng" height="128">
&nbsp;&nbsp;&nbsp;
<img src="docs/imgs/logo-xera-mark.png" alt="X-Era Lab" height="128">

<br>
<sub>MIT License · Copyright © 2025-2026 PhyAgentOS</sub>

</div>

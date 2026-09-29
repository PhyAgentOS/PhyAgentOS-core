# PhyAgentOS Framework Introduction

[中文](../zh/01-framework-introduction.md) · [Documentation index](../README.md)

> Documentation version: 1.0.0 · implementation baseline: 2026-08-30 source, schemas, and tests.

## 1. Positioning

PhyAgentOS is an Agent framework for embodied tasks. The Agent interprets a user goal, selects
Forge tools, defines task-level success, and decides whether to continue or recover. Forge Gateway
owns Tool execution, ToolEndpoint selection, Dora integration, and robot or simulator access.

The boundary keeps cognitive planning separate from physical effects. General Agent tools,
verification, task experience, evolution, Skill activation, and dynamic MCP tools remain part of
the Agent platform; robot execution uses one Forge Query/Action/Session Tool API.

## 2. One physical execution plane

```text
User / Channel / Scheduled Event
              │
              ▼
      AgentLoop + Planner
              │  AgentTask-bound call or diagnostic Query
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
```

Bound calls and diagnostic Query use the same Gateway endpoints. A diagnostic Query does not
occupy the AgentTask slot; Action and task-owned Session require a frozen task binding. PAOS does
not add a cross-Tool resource lease; the selected endpoint operation's `max_concurrency` decides
admission.

## 3. Three kinds of fact

| Fact | Owner | Question |
|:-----|:------|:---------|
| Execution | Forge Gateway | Which Query completed, or which Action/Session invocation was accepted and how did it terminate? |
| Evidence | PAOS observation collector | What was observed before and after the task-owned physical executions? |
| Verdict | PAOS verification | Did the complete set of bound calls satisfy the user goal, criteria, and constraints? |

An Action or Session admission response is not completion. Cancellation/stop acceptance, a local timeout, or an
`unknown` outcome does not prove that physical execution stopped and cannot justify a blind retry.

## 4. Identity model

The following identifiers are intentionally different:

- `task_id`: one user-visible AgentTask aggregate;
- `binding_id`: one immutable Skill/Runtime/ToolSpec snapshot;
- `revision_id`: one immutable planning generation within that task;
- Query/execution `record_id`: one PAOS record attached to a revision;
- `caller_id`: one PAOS-generated identity persisted before asynchronous admission;
- `invocation_id`: one Gateway-owned asynchronous Action or Session invocation;
- `attempt_id`: one Gateway execution attempt.

They must not be copied into one another or treated as aliases. Forge remains authoritative for
Tool execution facts; AgentTask stores references and task-level interpretation.

## 5. AgentTask lifecycle

Only one AgentTask may be non-terminal globally. A task starts with revision 1 and can contain
multiple bound Queries, Actions, and Sessions. PlanRevisions are append-only; prior execution records and
verification attempts are never rewritten.

```text
executing ── finalize success ──► succeeded
    │
    ├── finalize failure ───────► failed
    ├── recovery verdict ───────► awaiting_replan ── begin revision ──► executing
    └── cancel request ─────────► cancelling ── reconcile + finalize ─► cancelled/failed
```

Task tools create, read, revise, finalize, and cancel this aggregate. Tool API tools read Tool
context, invoke Query/Action/Session, reconcile asynchronous status/result, and request cancel/stop.

## 6. Verification and recovery

Verification modes are `off`, `audit`, `enforce`, and `recovery`.

- `off` derives success from bound execution facts and does not call semantic verification.
- `audit` records a verdict while preserving execution-derived terminal semantics.
- `enforce` requires a valid task contract and fails closed on missing evidence, invalid output,
  service errors, or inconclusive verification.
- `recovery` has the same fail-closed behavior and may return `replan_required`.

Recovery appends a bounded PlanRevision to the same `task_id`. The Planner receives unmet
criteria, preserved constraints, guidance, evidence references, and a deadline, then selects Tools
again. Unknown Action effects must be reconciled using a persisted invocation ID before another
effect is attempted; PAOS never repeats an unknown admission POST.

## 7. Evidence

PAOS collects configured image sources and optional robot state over Gateway WebSockets. It uses
bounded latest-frame buffers, media and size checks, SHA-256, source sequence boundaries, and
workspace-relative artifact references. Collection is best-effort: Forge ToolResult and events are
the authoritative execution facts.

Before-capture runs once before the first bound physical execution. Finalization waits for all
task-owned executions to be terminal before after-capture and aggregate verification. Query-only tasks can still carry Tool
facts but do not fabricate an Action capture window.

## 8. Skill Runtime

Skill Runtime manages installed manifest-v2 bundles and explicit named Dora profiles. Bundles use
safe archive extraction, SHA-256 inventories, exact single-entrypoint node locks, transactional replacement,
persistent state, lifecycle logs, and Gateway `/tools` health checks.

Skill discovery priority is workspace override, installed Skill, then built-in Skill. A healthy
active Runtime makes its Skill available to `SkillsLoader`, activation, experience, and evolution.
The active manifest is the only Gateway URL source.

Registry downloads require `resourceRegistry.url`, `PAOS_RESOURCE_REGISTRY_URL`, or an explicit
static index. Downloads happen only after an explicit CLI command and confirmation. Concrete Forge
Skills, nodes, models, and simulation assets are not included in the PhyAgentOS distribution.

## 9. Experience and evolution

ExperienceCoordinator records all Agent tool calls and associates explicit Skill activations,
AgentTask frozen binding/version, PlanRevision verdicts, ToolInvocation references, verification
attempts, and evidence references with one redacted task episode.

Semantic successes can support guarded Skill candidates. Workflow-related semantic failures can
form normalized observations and scope-aware Lesson clusters. Infrastructure, evidence, verifier,
unsatisfiable-task, and uncertain failures remain diagnostic-only. Evolution is fail-open and
cannot change execution or verification results.

## 10. Persistence

```text
<workspace>/
├── .paos/agent_tasks/tasks.sqlite3
├── .paos/evolution/experience.sqlite3
├── .paos/evolution/revisions/<skill>/
├── skills/<skill>/SKILL.md
└── artifacts/agent_tasks/<task_id>/
    ├── before_snapshot.json
    ├── after_snapshot.json
    ├── evidence_bundle.json
    └── evidence/
```

Runtime installation and lifecycle state live under the configured PAOS data paths, separate from
AgentTask and evolution persistence. Existing evolution data is never removed by Runtime cleanup.

## 11. Code map

| Area | Path |
|:-----|:-----|
| Agent loop and general tools | `PhyAgentOS/agent/` |
| Tool API client, binding, and AgentTask | `PhyAgentOS/forge/tool_client.py`, `PhyAgentOS/forge/binding.py`, `PhyAgentOS/forge/task.py` |
| Agent-facing Forge tools | `PhyAgentOS/agent/tools/forge_tool_api.py`, `forge_task.py` |
| Skill Runtime | `PhyAgentOS/skill_runtime/` |
| Built-in Agent workflow Skills | `PhyAgentOS/skills/` |
| Verification | `PhyAgentOS/verification/`, `PhyAgentOS/agent/session_verifier.py` |
| Experience and evolution | `PhyAgentOS/agent/experience/` |

## 12. Implemented scope

The current runtime supports Query, Action, and Session through the unified Tool API. Cross-Tool
resource leases, implicit Registry downloads, and bundled concrete robot/simulator artifacts are
outside the implemented contract.

## Next reading

- [User Manual](02-user-manual.md)
- [Developer Manual](03-developer-manual.md)
- [Forge Tool API Integration Contract](../forge/README.md)
- [Agent Experience and Skill Evolution](05-agent-experience-and-skill-evolution.md)

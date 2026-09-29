# Forge Tool API Integration Contract

[中文](README_zh.md) · [Documentation index](../README.md)

> Applies to PhyAgentOS 1.0.0.

## 1. Execution boundary

```text
AgentTask-bound call / diagnostic Query
        → ForgeToolClient
        → Gateway /tools → ToolInvocation → ToolEndpoint
        → Dora and robot nodes
```

PAOS supports Query, Action, and Session semantics. AgentTask aggregates a user goal, but Gateway
remains the physical execution owner. Diagnostic Query may run without a task; Action and
task-owned Session calls require a frozen AgentTask binding. The selected Endpoint operation
enforces `max_concurrency`; PAOS does not introduce a cross-Tool Resource/Control lease.

## 2. Tool discovery and context

```text
GET /tools
GET /tools/{tool_id}
GET /tools/{tool_id}/context
```

A ToolSpec declares stable identity, implementation/Endpoint binding, operation, `query|action|session`
semantics, strict input/output schema, readiness, and robot frame profile. Context is read live
before invocation; callers do not infer frame, unit, readiness, or binding.

## 3. Query contract

`forge_tool_query` reads the configured ToolSpec, verifies `semantics=query`, then invokes:

```text
POST /tools/{endpoint_id}/{operation}:invoke
Content-Type: application/json

{
  "arguments": {},
  "caller_id": "paos:<task-or-diagnostic-identity>",
  "timeout_ms": 10000
}
```

Success is HTTP 200 with `{ "ok": true, "data": { ... } }`. PAOS generates the caller ID. A bound Query creates a terminal PAOS
ToolExecutionRecord under the active PlanRevision. An unbound Query returns the same Gateway data
without task attribution.

## 4. Action contract

Admission:

```text
POST /tools/{tool_id}:invoke
→ HTTP 202
→ data.invocation_id + data.attempt_id
```

Reconciliation:

```text
GET  /invocations/{invocation_id}
GET  /invocations/{invocation_id}/result
POST /invocations/{invocation_id}/cancel
```

Result HTTP 202 means pending. Cancel HTTP 200/202 means the cancellation request was processed or
accepted; it does not prove stop. A timeout means the remote state is unknown. An explicit
`unknown` terminal outcome closes PAOS accounting as a failure but remains physically uncertain and
must not trigger a blind retry.

Before admission PAOS persists an Action intent and its generated caller ID. Every returned
invocation/attempt identity is retained. A timeout or transport error leaves an `unknown` record;
recovery may only read a persisted invocation ID and never repeats the POST blindly.

## 5. Session contract

A Tool with `semantics=session` is admitted through the same invoke route and returns HTTP 202 plus
an invocation ID. Status and result use the common `/invocations/{id}` routes; an owned Session is
stopped with `POST /invocations/{id}/stop`.

PAOS records Session ownership as `task`, `shared`, or `runtime`. A task can stop only its own
Session, shared Sessions remain live across task finalization, and runtime-owned Sessions are
managed outside the Agent. Non-terminal task-owned Sessions block finalization and Runtime stop
also accounts for all active Sessions.

## 6. Agent tools

| Tool | Contract |
|:-----|:---------|
| `forge_tool_context` | Read ToolSpec and live context. |
| `forge_tool_query` | Invoke synchronous Query; diagnostic without a task or governed with `task_id`. |
| `forge_tool_start_action` | Admit an asynchronous Action for a bound `task_id`. |
| `forge_tool_action_status` | Read invocation phase/status. |
| `forge_tool_action_result` | Read pending or terminal result. |
| `forge_tool_cancel_action` | Request cancellation without asserting stop. |
| `forge_tool_start_session` | Admit a bound Session with explicit ownership. |
| `forge_tool_session_status/result/stop_session` | Reconcile or stop an owned Session. |
| `forge_task_create` | Create the one active AgentTask and revision 1. |
| `forge_task_get` | Read task, revisions, Tool records, evidence, and verdict. |
| `forge_task_begin_revision` | Append a revision after an allowed recovery verdict. |
| `forge_task_finalize` | Capture after evidence and apply aggregate task verification. |
| `forge_task_cancel` | Cancel non-terminal Actions and stop task-owned Sessions. |

The diagnostic context tool remains available without a Runtime. Governed tools require exactly
one healthy active Skill Runtime. Existing general Agent tools and dynamic MCP tools remain
registered independently.

## 7. Binding, identity, and correlation

`activate_skill` reads the installed workflow and previews the current Runtime. Task creation
revalidates that candidate and freezes the Skill name/version, manifest and workflow hashes,
profile, Runtime instance, Gateway identity, required ToolSpec hashes and Node artifact IDs. Every
task execution rechecks membership, semantics, readiness, ToolSpec hash, and Runtime identity.

| Identity | Owner | Meaning |
|:---------|:------|:--------|
| `task_id` | PAOS | Stable task aggregate |
| `revision_id` | PAOS | Immutable planning generation |
| `binding_id` | PAOS | Immutable Skill/Runtime/ToolSpec snapshot |
| `record_id` | PAOS | Bound Query, Action, or Session record |
| `caller_id` | PAOS | Idempotency/correlation identity persisted before admission |
| `invocation_id` | Gateway | Asynchronous Action or Session lifecycle |
| `attempt_id` | Gateway | Execution attempt |

Correlation is explicit. IDs are not aliases and are not derived from one another.

## 8. AgentTask model

Only one AgentTask may be non-terminal globally; diagnostic Query does not occupy the slot. Creation and
updates use SQLite WAL and immediate transactions. A task contains an append-only list of
PlanRevisions. Each revision contains Tool records, a semantic verdict, and verification attempts.

```text
executing
  ├─ finalize → succeeded | failed
  ├─ recovery verdict → awaiting_replan → begin_revision → executing
  └─ cancel → cancelling → reconcile → finalize → cancelled | failed
```

Once a Tool record is terminal, later observations do not rewrite its execution fact. A recovery
revision keeps the same task ID and is bounded by replan count and deadline.

## 9. Evidence and verification

PAOS performs best-effort capture before the first bound Action and after every bound Action reaches
terminal accounting state. Evidence artifacts include source, phase, sequence, timestamps, media
metadata, size, SHA-256, and workspace-relative references. Capture errors are recorded rather than
hidden. Bundle identity, quality, capture-window, policy requirements, and retained artifact bytes
are validated before the verifier receives the task context.

`forge_task_finalize` aggregates all bound Tool facts and applies the task contract:

- `off`: execution-derived result;
- `audit`: record semantic verdict, preserve execution-derived result;
- `enforce`: semantic verdict controls success and fails closed;
- `recovery`: enforce semantics plus bounded `replan_required`.

Forge ToolResult and events are authoritative for execution. The PAOS verifier decides only whether
the user-level task is complete. Its context includes the frozen Skill binding, PlanRevisions,
ToolExecutionRecords, Gateway terminal results, before/after evidence, and scoped advisory Lessons.

## 10. Experience and evolution

The terminal AgentTask is adapted into one redacted episode. It references the frozen Skill
binding and version, PlanRevision verdicts, ToolInvocation/attempt fingerprints, and evidence without
persisting raw outputs, credentials, endpoints, or physical parameters in learned content.

Lessons and failure clusters are scoped to the bound Skill version. Evolution is fail-open and
never alters Gateway facts, AgentTask terminal state, or verification attempts.

## 11. Skill Runtime and distribution

Skill Runtime installs and manages manifest-v2 Bundles. Installation requires safe contained
paths, bounded extraction, SHA-256 file inventory, strict manifest validation, staging, atomic
replacement, and rollback. Each Node lock fixes artifact ID, version, platform, architecture,
archive type (`executable_tar_gz`: one root-level executable; `directory_tar_gz`: one root
directory named after the entrypoint holding the executable and its runtime tree), the entrypoint,
and SHA-256. Static-index downloads carry size and digest.
Registry Node downloads use the verified Skill lock as the digest authority and resolve an exact
size from Registry metadata or the direct-download endpoint before entering the cache. Installation
is explicit and confirmed by default.

RuntimeManager requires Dora CLI on `PATH` (v0.4.1 with `dora-message` v0.7.0 is the current Forge
Skill compatibility baseline).
It materializes an environment whose digest covers the selected dataflow path and profile file
contents. `PAOS_SKILL_NAME` and `PAOS_SKILL_VERSION` are available both to Dora processes and as
rendered dataflow placeholders.

Startup persists `starting`, then runs `bash <bundle>/start.sh <name> <version>` with inherited stdio
when the optional hook exists. Hook-enabled Bundles require Bash. After the hook succeeds,
RuntimeManager starts local Dora services when needed, launches the named profile, waits for Gateway
`/tools` and all required Tool contexts, and persists status/logs. A healthy active Runtime
contributes Skill availability; its manifest is the only source of the Gateway URL. Per-Skill
cross-process locking rejects overlapping lifecycle mutations.

Normal stop is rejected while tracked non-terminal invocations, Sessions, or task bindings exist.
Force stop records an audit event and does not change execution truth.

Concrete Forge Skills, nodes, models, and simulation assets are not part of the PhyAgentOS source
or Python distribution. They are obtained independently for local testing or deployment. The
packaging helper creates deterministic Skill archives and the installer verifies them before
commit.

## 12. Conformance

An integration is conformant when it covers Tool discovery/context, Query response, Action and
Session admission, pending and terminal result, cancellation/stop, timeout/unknown, no-POST
recovery, endpoint concurrency, immutable AgentTask binding/revisions, ownership, evidence,
aggregate verification, version-scoped experience, Bundle security, transactional installation,
Runtime health, safe switch, and availability propagation.

Mock Gateway tests are sufficient for code and contract acceptance. Hardware/MuJoCo acceptance is
recorded separately with exact artifact digests and environment.

## Related documentation

- [Framework Introduction](../en/01-framework-introduction.md)
- [Configuration Reference](../en/04-forge-configuration-reference.md)
- [Integration Development Guide](../user_development_guide/README_en.md)
- [Operations Manual](../user_manual/README_en.md)

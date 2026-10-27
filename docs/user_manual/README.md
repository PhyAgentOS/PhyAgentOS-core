# PhyAgentOS Operations Manual

[English](README.md) | [简体中文](README_zh.md)

> Version: 1.1.1

This is the operations reference for startup, reconciliation, stop, recovery and backups. Development starts with the [contribution guide](../../CONTRIBUTING.md). If the optional [interaction extension](../interaction/README.md) is enabled, also follow its journal/Runner ownership and shutdown limits; ordinary robot procedures do not replace them.

## 1. Runtime model

```text
User/Channel → AgentLoop → Forge task and Tool API tools
                                  │
                   bound call or diagnostic Query
                                  ▼
                         ForgeToolClient
                                  ▼
Gateway /tools → ToolInvocation → ToolEndpoint → Dora → robot/simulator

bound calls → immutable Skill binding → AgentTask SQLite → verification/experience
```

Gateway owns execution. PAOS owns user-task aggregation and semantic verdicts. Skill Runtime owns
the explicit lifecycle of installed Bundle profiles; it does not replace Gateway execution truth.

## 2. Pre-deployment checks

### PAOS host

- Python 3.11 or 3.12 and the intended v1.1.1 environment are installed.
- `paos status` resolves the expected config, workspace, model, and provider.
- Workspace, PAOS data paths, and artifact paths have sufficient permissions and disk space.
- Verification provider credentials are available when non-`off` tasks are allowed.

### Skill Runtime

- Skill Bundle metadata includes size and SHA-256; every Node lock has an exact SHA-256 and resolves
  to a sized direct download.
- Required binaries are executable, required assets exist, and required environment variables are set.
- Dora CLI v0.4.1 with `dora-message` v0.7.0, the current Forge Skill compatibility baseline, is
  installed and on `PATH`; `dora --version` reports both expected versions. Installation is
  documented in the [user manual](../en/02-user-manual.md#dora-cli-for-managed-skill-profiles).
- The profile Gateway address is not occupied by an unmanaged process.

### Forge Gateway

- `GET /tools` returns a successful object envelope.
- Required ToolSpecs and `/tools/{tool_id}/context` are present and ready.
- Endpoint operation `max_concurrency` matches the robot's safe concurrency.
- ToolSpec semantics, schemas, bindings, and readiness match the installed Skill manifest.

### Persistence

- `.paos/agent_tasks`, `.paos/evolution`, Agent conversation history, and Skill Runtime state are on durable storage.
- Backup and retention procedures do not delete evolution data when rotating robot evidence.

## 3. Startup and health

For a managed Skill profile:

```bash
paos skill inspect <skill-name>
paos skill start <skill-name> --profile <profile>
paos skill status <skill-name>
paos skill switch <other-skill-name> --profile <profile>
paos agent
# or: paos gateway
```

`paos skill start` runs `dora check` and invokes `dora up` if the local coordinator and daemon are
not ready; operators do not need to start them separately. After startup, `dora check` should
succeed.

Healthy Runtime status requires persisted `running`, a live named Dora flow, Gateway `/tools`, and
ready context for every Tool resolved from the selected profile's `required_tools` override, or the manifest-wide `required_tools` when the profile omits it. Use `paos skill logs <name>` for lifecycle and
Dora launch logs. Runtime switching is permitted only with no non-terminal AgentTask; the target
must become ready before selection, and a failed same-Gateway switch restores the previous
Runtime. Long-running Agents follow the persisted selection before the next activation or Tool
call.

The Agent uses only the Gateway identity and URL of this managed active Runtime. `paos status`
checks local configuration only; use `forge_tool_context` for live Tool readiness.

## 4. Task monitoring

Record these identities separately:

| Identity | Owner | Use |
|:---------|:------|:----|
| `task_id` | PAOS | User-visible aggregate and verification |
| `binding_id` | PAOS | Frozen Skill version, Runtime, and ToolSpec set |
| `revision_id` | PAOS | Append-only planning generation |
| `record_id` | PAOS | One bound Query, Action, or Session record |
| `caller_id` | PAOS | Persisted before asynchronous admission |
| `invocation_id` | Gateway | One asynchronous Action or Session lifecycle |
| `attempt_id` | Gateway | One execution attempt |

Use `forge_task_get(task_id)` for aggregate state and Tool records. Use
the task-bound Action or Session status/result tools for execution truth. A result endpoint may
return HTTP 202 while pending.

Expected task states:

| State | Operator meaning |
|:------|:-----------------|
| `executing` | Planning or bound Tool calls continue. |
| `cancelling` | Cancellation was requested; physical stop is not yet proven. |
| `awaiting_replan` | Verification permits a bounded new PlanRevision before its deadline. |
| `succeeded` / `failed` / `cancelled` | PAOS aggregate is terminal. Inspect invocation facts separately when needed. |

## 5. Cancellation and stop

For one Action, call `forge_tool_cancel_action(task_id, invocation_id)`, then continue status/result
reconciliation. For all Actions bound to a task, call `forge_task_cancel(task_id, reason)`, reconcile
each invocation, and inspect physical state when effects are uncertain. Task cancellation also
stops task-owned Sessions; shared and runtime-owned Sessions retain their independent lifecycle.

Never report `requested`, `accepted`, a timeout, or `unknown` as proof of physical stop. Do not
retry the motion until effect reconciliation is complete.

Stop a managed Runtime only after tracked invocations and Sessions are terminal and no task binding remains:

```bash
paos skill stop <skill-name>
```

`--force` is reserved for an operator who has independently assessed the physical system. Before
stopping the managed Dora flow it makes best-effort cancel/stop requests for tracked Actions and
Sessions, then records each request and unresolved reference in the Runtime audit. Acceptance does
not prove termination and Gateway invocation results are not rewritten.

## 6. Graceful shutdown

1. Stop admitting new user tasks.
2. Read the active AgentTask and reconcile every Action/Session invocation.
3. Finalize or cancel/finalize the AgentTask.
4. Stop PAOS channels or Agent.
5. Stop the Skill Runtime profile.
6. Stop shared infrastructure only if no other profile uses it.

## 7. Crash restart

After a PAOS restart, open the persisted AgentTask with its known `task_id`. Do not recreate or
resubmit an Action or Session from local intent. Query every persisted `invocation_id` and update the record
from Gateway status/result. If Gateway can no longer resolve an invocation, treat the effect as
unknown and escalate to physical-state inspection.

`paos skill status <name>` reconciles Runtime state against Dora and Gateway health. It can move a
persisted starting/running state to failed when the flow or Tool contexts are unavailable; restart
only after diagnosing the previous flow.

## 8. Backup and disk management

With PAOS stopped, back up the database together with WAL/SHM files and the referenced trees:

```text
<workspace>/.paos/agent_tasks/tasks.sqlite3*
<workspace>/.paos/evolution/experience.sqlite3*
<workspace>/.paos/evolution/revisions/
<workspace>/artifacts/agent_tasks/
<workspace>/skills/
```

Also retain installed Bundle/Node manifests, Runtime state, and lifecycle logs according to the
deployment's PAOS data-path policy. Evidence retention may prune entity files after verification;
it must not remove task records, invocation references, or evolution history.

## 9. Failure layers

| Layer | Typical symptom | First action |
|:------|:----------------|:-------------|
| Registry/install | Digest, size, manifest, or lock failure | Correct the signed metadata or artifact; do not bypass validation. |
| Runtime | Dora flow or Gateway health unavailable | Inspect `paos skill status` and `logs`. |
| Tool context | Tool missing, unbound, or not ready | Inspect ToolSpec, Endpoint, frame, and required profile. |
| Admission | HTTP/contract failure | Preserve any returned invocation ID; determine whether Gateway accepted work. |
| Execution | pending, failed, cancelled, or unknown | Reconcile using the same invocation ID and inspect physical state when uncertain. |
| Evidence | before/after source missing | Inspect source readiness and bundle errors; keep ToolResult authoritative. |
| Verification | invalid/inconclusive/service error | Inspect task contract, evidence, provider, and mode semantics. |
| Evolution | reflection or promotion blocked | Inspect evolution events; execution remains unaffected. |

## 10. Operational acceptance checklist

- [ ] Package and runtime version report 1.1.1.
- [ ] General Agent tools and dynamic MCP tools remain registered.
- [ ] Required Skill Bundle and all node artifacts verify.
- [ ] Managed Runtime reaches ready and all Tool contexts are healthy.
- [ ] Diagnostic Query and bound Query/Action/Session use the same Gateway Tool API.
- [ ] Action/Session admission, pending, terminal, cancel/stop, timeout, ownership, and unknown behavior are exercised.
- [ ] One-active-AgentTask enforcement and PlanRevision recovery are exercised.
- [ ] Evidence and task-level verification complete for a bound workflow.
- [ ] Experience records AgentTask, Skill activation, verification, and invocation references.
- [ ] Backups include AgentTask and evolution persistence.
- [ ] Concrete Skill/hardware acceptance is recorded separately with exact assets, nodes, and environment.

## Next reading

- [User Manual](../en/02-user-manual.md)
- [Core Communication Architecture](../user_development_guide/COMMUNICATION.md)
- [Forge Tool API Contract](../forge/README.md)

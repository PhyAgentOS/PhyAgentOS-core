# PhyAgentOS Core Developer Manual

[中文](../zh/03-developer-manual.md) · [Documentation index](../README.md)

> Documentation version: 1.1.1.

This manual covers Core implementation: AgentTask coordination, persistence, binding, verification and Runtime management. For robot, Node and Skill development, start with the [TrackB framework guide](../user_development_guide/track_b/README.md). The [Forge Tool API contract](../forge/README.md) is the canonical reference for routes, identities and lifecycle semantics.

## 1. Development invariants

1. Robot execution has one physical path: `ForgeToolClient → Gateway Tool API → ToolEndpoint`.
2. AgentTask aggregates planning, evidence, and verdicts; it never executes the robot.
3. `binding_id`, `task_id`, `revision_id`, record ID, `caller_id`, `invocation_id`, and `attempt_id` are distinct.
4. Forge ToolResult and invocation events are authoritative execution facts.
5. Action/Session admission, cancel/stop acceptance, timeout, and `unknown` do not prove physical stop.
6. General Agent tools, verification, experience, evolution, and dynamic MCP remain independent.
7. Runtime artifacts are installed only after bounded archive and digest verification.

## 2. Module map

| Module | Responsibility |
|:-------|:---------------|
| `agent/loop.py` | Existing Agent loop, general tools, dynamic MCP, Forge tool registration, context, and lifecycle |
| `agent/tools/forge_tool_api.py` | Governed Query/Action/Session Tool API Agent wrappers |
| `agent/tools/forge_task.py` | Five AgentTask lifecycle tools |
| `forge/tool_client.py` | Strict asynchronous HTTP client and response validation |
| `forge/binding.py`, `forge/task.py` | Immutable Runtime/ToolSpec binding, AgentTask models/store, evidence, verification, and recovery |
| `forge/observation.py`, `forge/evidence.py` | Best-effort image/state collection and artifact writing |
| `skill_runtime/` | Manifest, catalog, safe archive, installer, Registry, state, Runtime manager, and availability |
| `agent/experience/` | Activation, episodes, assessment, Lessons, Skill candidates, and evolution |
| `verification/` | Public task, evidence, request, and verdict contracts plus verification service |

## 3. Forge Tool API client

`ForgeToolClient` accepts only JSON object envelopes with `ok=true` and object-valued `data`.
Errors preserve HTTP status, error code, retryability, and any returned invocation identity.

Routes, response codes and pending/cancel/stop semantics are defined in the [Forge Tool API contract](../forge/README.md#2-tool-discovery-and-context). Keep the client implementation aligned with that contract rather than introducing a second route definition here.

Path components are percent-encoded. PAOS supplies `caller_id` and persists it with the intent
before Action/Session admission. Admission must contain a non-empty `invocation_id`; Action also
requires `attempt_id`. A timeout leaves an unknown record and recovery never repeats the POST.

## 4. Agent-facing tools

The public Agent-facing tool catalog is maintained in the [API contract](../forge/README.md#6-agent-tools). Implement wrappers in `agent/tools/forge_tool_api.py` and `agent/tools/forge_task.py`; keep transport handling in `forge/tool_client.py` and coordination in `forge/task.py`.

Diagnostic Query and bound calls invoke the same HTTP methods. Every mutating or task-attributed
wrapper validates ownership and the frozen Tool binding before network access. Tool wrappers must
never invent a Gateway identity or result.

## 5. AgentTask contracts and transactions

`AgentTaskRecord` contains a stable ID, task description, `TaskVerificationContract`, status,
append-only PlanRevisions, evidence references, verification attempts, cancellation state, and
timestamps. Each `PlanRevision` contains its own Tool records, verdict, and verification attempts.

`AgentTaskStore` uses SQLite WAL and `BEGIN IMMEDIATE`. Creation queries for any non-terminal task
inside the same transaction, enforcing one global active slot across processes. Updates write the
complete validated record and an append-only event. Callers do not modify tables directly.

The [AgentTask state contract](../forge/README.md#8-agenttask-model) defines public states. Store transitions must preserve those semantics; an `unknown` Tool record closes accounting without proving physical stop.

## 6. Bound execution lifecycle

Core coordinates activation/binding, durable intent admission, authoritative response recording and finalization. Keep intent persistence before transport dispatch and write Gateway references back to the same task revision. Finalization waits for task-owned executions, collects after-evidence and applies verification before scheduling experience.

The public [binding/identity](../forge/README.md#7-binding-identity-and-correlation) and [AgentTask lifecycle](../forge/README.md#8-agenttask-model) definitions are maintained in the API contract. Cancellation responses remain records of requests until reconciliation and finalization establish the aggregate result.

## 7. Verification and recovery

`TaskVerificationContract` remains the public user-level contract. The verifier receives goal,
criteria, constraints, the frozen Skill binding, PlanRevisions, ToolExecutionRecords, Gateway
terminal results, before/after evidence, task history, and frozen Skill-scoped advisory Lessons.

In recovery mode, a valid `replan_required` verdict moves the task to `awaiting_replan` with a
deadline. `begin_revision` checks the same `task_id`, replan budget, deadline, and task state, then
appends a revision. Earlier attempts remain visible to experience analysis. Verifier exceptions are
persisted as failed attempts; audit preserves execution semantics, while enforce/recovery fail.

## 8. Evidence and retention

Evidence paths are workspace-relative and written atomically. Before semantic verification, the
bundle identity, association quality, completeness, capture window, and required kinds and sources
are checked. Retained artifacts are then validated for path containment, byte size, SHA-256, media
type, and structured JSON where applicable. The evidence bundle records capture quality and errors
rather than presenting best-effort collection as authoritative.

Retention can remove entity bytes according to policy, but it must preserve the task record,
execution references, bundle metadata, and tombstone information required for audit.

## 9. Skill Runtime contracts

Manifest fields, Node archive layouts and startup-hook environment are documented in the [Bundle and Tool integration reference](../user_development_guide/README.md#4-build-a-manifest-v2-skill-bundle). Core implements these contracts in `skill_runtime/manifest.py`, archive validators and installers.

Archive validation rejects absolute/traversing paths, links outside the node-tree cases above,
duplicate/colliding paths, oversized
files, expansion-limit violations, missing inventory entries, and digest mismatches. Skill and Node
installers stage content, validate it, then atomically replace the target with rollback support.

RuntimeManager:

1. resolves the installed Skill and profile;
2. materializes the locked environment without mutating installed nodes; its digest covers the
   exact dataflow path and SHA-256 of every regular file beside that dataflow;
3. checks the Dora CLI, dataflow, required files, and environment;
4. refuses to adopt an unmanaged Gateway already using the address;
5. persists `starting` and runs the optional Bundle start hook as
   `bash <bundle>/start.sh <name> <version>` with inherited stdio;
6. checks the local Dora services, runs `dora up` when needed, and starts the named flow;
7. waits for flow, `GET /tools`, and all required Tool contexts;
8. persists running/failed/stopped state and lifecycle logs.

Dataflow rendering resolves `FORGE_RUNTIME_BIN`, `PAOS_SKILL_ROOT`, `PAOS_SKILL_NAME`, and
`PAOS_SKILL_VERSION`; the two Skill identity values are also supplied to Dora process environments.
Start, stop, Skill install/update commit, and removal share a non-blocking per-Skill cross-process
lock. Status preserves `starting` while that lock proves startup is still active, while a stale
unlocked `starting` state is reconciled normally.

The current Forge Skill compatibility baseline is Dora CLI v0.4.1 with `dora-message` v0.7.0.
RuntimeManager requires compatible command behavior but does not enforce an exact semantic version;
operators must prevent a mismatched coordinator/daemon from being reused. Dora is a host runtime
prerequisite, not a Python dependency and not part of a Skill Bundle.

A normal stop is rejected while non-terminal invocations, Sessions, or task bindings remain
tracked. Force stop records an audit event and does not change invocation truth.

## 10. Registry and availability

Artifacts require an expected size and SHA-256 before entering the cache. Static indexes provide
both values directly. A Registry Node may omit its duplicate digest and size fields: the verified
Skill lock supplies the expected digest, and the client resolves the size from Registry metadata or
the direct-download endpoint. Any Registry digest that is present must match the lock. Resumed
downloads are verified again before installation. An empty Registry URL permits only local bundles
or an explicit static index. `PAOS_RESOURCE_REGISTRY_URL` overrides `resourceRegistry.url`.
The public Registry lookup is name-based. A requested CLI version is validated against the
downloaded Skill manifest before Node resolution rather than appended to the Registry URL.

`discover_active_runtime` reconciles persisted state, Dora flow, Gateway health, and required Tool
contexts. Its availability provider flows through SkillsLoader, ExperienceCoordinator, and
SkillActivationManager. Skill discovery order is workspace, installed, built-in.

## 11. Experience and evolution integration

All Agent tool calls remain recorded. Frozen binding/version fields attach AgentTask, revision,
invocation, and attempt references. The outcome source
maps each revision verdict to its last execution record so a recovered task preserves both failed
and successful semantic attempts.

Generated Lessons and Skill updates remain subject to redaction, scope, support thresholds,
abstraction checks, managed-block replacement, atomic writes, reload validation, and rollback.
Evolution failures remain fail-open.

## 12. Extension workflows

For robot/model configuration, new profiles, or new Nodes, follow the [three TrackB extension modes](../user_development_guide/track_b/SKILL_DEVELOPMENT.md) and [Piper X example](../user_development_guide/PIPER_X_DEMO.md).

Core changes should extend the existing generic client, binding, coordinator or installer only when the capability requires it. Preserve the API contract and add focused regression coverage at the affected implementation boundary. Do not add an alternate Agent-to-Dora execution path.

The opt-in [interaction foundation](../interaction/README.md) is a separate restricted extension; its deployment limits also apply to Core changes.

## 13. Test gates

```bash
ruff check PhyAgentOS tests
python -m compileall -q PhyAgentOS tests
pytest -q
```

Tests should cover response contracts, Session ownership, pending/cancel/stop/timeout/unknown
semantics, one active task, diagnostic Query, immutable bindings, no-POST recovery, revision
recovery, evidence, version-scoped episodes, archive attacks, transactional rollback, Registry
verification, Runtime health/switching, and mocked workflows. Concrete hardware/simulator tests are
conditional on independently installed matching artifacts and Dora availability.

## Next reading

- [Forge Tool API Integration Contract](../forge/README.md)
- [Bundle and Tool Integration Reference](../user_development_guide/README.md)
- [Agent Experience and Skill Evolution](05-agent-experience-and-skill-evolution.md)

## Node development references

- [TrackB framework guide](../user_development_guide/track_b/README.md)
- [Piper X MuJoCo development example](../user_development_guide/PIPER_X_DEMO.md)

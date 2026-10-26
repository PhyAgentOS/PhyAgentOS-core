# PhyAgentOS Core Communication Architecture

[中文](COMMUNICATION_zh.md) · [Documentation index](../README.md)

> Version: 1.1.1

This reference covers Core message, observation, verification and persistence boundaries. For Forge Node messages and dataflow ports, use [TrackB protocols](track_b/PROTOCOLS.md). HTTP routes and invocation semantics have one canonical definition in the [API contract](../forge/README.md).

## 1. Communication boundaries

PhyAgentOS separates six boundaries:

1. user/channel ↔ AgentLoop messages;
2. Agent tools ↔ AgentTaskCoordinator;
3. ForgeToolClient ↔ Gateway Query/Action/Session Tool API;
4. observation collector ↔ Gateway image/state WebSockets;
5. verifier ↔ isolated Verification Service;
6. AgentTask/experience/Runtime ↔ their own persistent stores.

The boundaries share opaque references, not execution ownership.

## 2. User-message boundary

Channels publish `InboundMessage` objects to the Agent bus. AgentLoop builds context, invokes the
model and tools, then emits `OutboundMessage`. Tool calls use registered JSON schemas. The existing
file, directory, shell, web, messaging, image, Scene Graph, Cron, Spawn, Agent Mode, Skill
activation, and dynamic MCP tools follow this same loop.

AgentTask's `origin_session_key` associates completion experience with the originating Agent
conversation. It is not a Gateway execution identifier.

## 3. AgentTask boundary

Task lifecycle tools are listed in the [API contract](../forge/README.md#6-agent-tools).

These tools call AgentTaskCoordinator and never call Dora or a robot. The coordinator uses
transactional SQLite to enforce one non-terminal AgentTask and stores append-only PlanRevisions,
Tool records, evidence references, and verification attempts.

Diagnostic Query may omit `task_id`; governed Query and every Action/Session include it. The
wrapper checks the immutable Skill/Runtime/ToolSpec binding and creates or updates the matching
Tool record around the same Gateway request. This is aggregation, not another execution plane.

## 4. Gateway HTTP boundary

The [API contract](../forge/README.md#2-tool-discovery-and-context) defines discovery, Query, Action and Session routes and response envelopes. Core keeps transport handling in `ForgeToolClient`; timeout leaves the remote outcome unknown.

Returned invocation identities must be retained even if subsequent local persistence or tracking fails. Verification and experience components do not replace the Gateway execution owner.

## 5. Identity boundary

Namespaces and correlation fields are defined in the [binding and identity contract](../forge/README.md#7-binding-identity-and-correlation). Components exchange explicit stored references; they must not derive a Gateway identity from a PAOS task or revision ID.

## 6. Invocation terminal semantics

Use the [Action](../forge/README.md#4-action-contract), [Session](../forge/README.md#5-session-contract) and [AgentTask](../forge/README.md#8-agenttask-model) contracts for terminal and cancellation behavior. Core persists request/response observations and reconciles them before finalization; local cancellation or transport completion cannot establish physical stop.

## 7. Evidence WebSocket boundary

PAOS connects to configured image and optional state streams using bounded connection and capture
timeouts. Messages are treated as untrusted input. Image media, decoded size, sequence, phase,
source, local receive time, and SHA-256 are validated before persistence.

For ordinary robot workflows, the collector captures before the first bound Action and during
finalization after task-owned Actions/Sessions reach terminal accounting state. Evidence association is best-effort; Gateway ToolResult and invocation events
remain authoritative for execution.

## 8. Verification boundary

The Agent-side verifier sends resolved public task contracts, normalized Tool facts, evidence,
history, and frozen scoped Lessons to the isolated Verification Service. Lessons are untrusted,
non-authoritative advice. The service cannot invoke Gateway, create a PlanRevision, or change
execution records.

Verifier output must validate as the versioned verdict contract. AgentTaskCoordinator applies
`off`, `audit`, `enforce`, or `recovery` semantics and persists every attempt.

## 9. Experience boundary

ExperienceCoordinator receives an AgentTask completion reference. `AgentTaskOutcomeSource` builds a
redacted envelope containing workflow structure, semantic verdicts, field names, and opaque task,
revision, invocation, attempt, and evidence references. Raw arguments, results, credentials,
endpoints, and physical coordinates are not copied into learned content.

The episode is unique per AgentTask. PlanRevisions, repeated completion notifications, reviews,
and replay do not create independent support.

## 10. Persistence boundary

| Store | Content |
|:------|:--------|
| `.paos/agent_tasks/tasks.sqlite3` | AgentTask records and append-only events |
| `artifacts/agent_tasks/<task_id>/` | Before/after snapshots, bundle metadata, evidence entities |
| `.paos/evolution/experience.sqlite3` | Bindings, episodes, Lessons, candidates, jobs, events |
| Skill Runtime state path | Installed Runtime state, invocation/Session IDs, task bindings, and audit events |
| Skill Runtime logs path | Lifecycle and Dora launch logs |

SQLite updates and artifact writes are transactional or atomic within their own boundary. A
Gateway response is not rolled back because local experience processing failed; evolution is
fail-open.

## 11. Skill Runtime and Registry boundary

Registry/index clients return artifact metadata and downloads. Cache and installers require size
and SHA-256, then validate archive inventories and the Node archive contract (single root
executable, or a single root directory tree for `directory_tar_gz`) before atomic installation.
RuntimeManager starts a named Dora flow and observes Gateway `/tools`; it never calls an alternate
Gateway Agent API.

The active Runtime availability provider supplies Skill visibility and Gateway URL to the Agent.
It does not mutate AgentTask or experience data.

## 12. Trust rules

- Treat ToolSpec, Gateway responses, WebSocket payloads, task text, and learned text as untrusted data.
- Never log credentials or raw sensitive task inputs.
- Never infer stop from cancel acceptance, timeout, or unknown.
- Never bypass digest or archive safety checks.
- Never place a second execution API between Agent tools and ForgeToolClient.
- Keep Runtime force-stop and destructive artifact cleanup as explicit operator actions.

## Next reading

- [Bundle and Tool Integration Reference](README.md)
- [Operations Manual](../user_manual/README.md)
- [Forge Tool API Contract](../forge/README.md)

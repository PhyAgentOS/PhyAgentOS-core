# Agent Experience and Skill Evolution

> Documentation version: 1.1.1. This manual describes the implemented Agent-side validation, experience, Lesson, and Skill-evolution path over AgentTask records.

## 1. Purpose and boundary

PhyAgentOS learns at task/workflow granularity rather than from an isolated tool call. A completed, semantically verified AgentTask with all of its PlanRevisions may become one `TaskEpisode`. Background reflection can then support a reusable Skill workflow or a scoped Lesson.

```text
Skill activation
  → AgentTask + PlanRevisions + ToolInvocations
  → semantic task outcome
  → redacted TaskEpisode
  → asynchronous reflection
      ├─ successful reusable workflow → SkillCandidate
      └─ workflow-related failure     → FailureObservation → LessonCluster
```

This path does not modify AgentTask state, Forge Tool calls, Gateway payloads, evidence collection, recovery ordering, or lower-level execution. If initialization, persistence, a model call, validation, or a write fails, evolution fails open and the original task outcome is preserved.

The initial outcome provider is Forge. The `TaskOutcomeSource` protocol allows a later runtime to produce the same `TaskOutcomeEnvelope` without changing Lesson or Skill evolution.

## 2. Skill activation and task attribution

When `agents.evolution.enabled=true`, Agent context includes Skill summaries and tells the Agent to check them before its first tool call. A matching workflow is activated through:

```text
activate_skill(name, role="primary" | "supporting")
```

Activation rules:

- `name` must be an exact registered hyphen-case Skill name; paths are rejected;
- workspace Skills override installed and built-in Skills with the same name; installed Skills override built-ins;
- unavailable Skills cannot be activated;
- a turn may have one primary Skill and multiple supporting Skills;
- the same Skill cannot change role within the turn;
- only the primary Skill may be automatically updated;
- primary and supporting Skills may receive relevant failure attribution;
- reading `SKILL.md` through a file tool does not count as activation.

The result contains the complete Skill document, activation ID, source, content digest, and applicable active Lessons. The digest fixes attribution to the revision used by the current turn; a promoted revision becomes visible through the summary on a later turn.

AgentLoop records ordered tool names and argument field names, not argument values. A Forge AgentTask requires the current turn's primary activation; creation revalidates and freezes the exact Skill version, Runtime, manifest/workflow hashes, and required ToolSpecs. Bound Query/Action/Session records and Gateway invocation references are attached as execution proceeds. Diagnostic Query without a task is not attributed to a task episode.

The binding also freezes the applicable active Lessons returned by each activated Skill. Automatic verification, every later PlanRevision, and review resolve Lesson context through the task ID, so they see the same bounded set even if the Lesson ledger changes afterward. These Lessons are non-authoritative workflow advisories: they may suggest a check, but they cannot establish a criterion status, replace execution facts or evidence, or be cited as evidence references. A task without an activated Skill supplies an empty Lesson set.

## 3. Outcome and episode classification

`AgentTaskOutcomeSource` reads the persisted AgentTask and produces:

- redacted goal and criteria;
- final semantic verdict and per-criterion statuses;
- each PlanRevision's Query/Action/Session semantics, input field names, execution status, verdict, and redacted reason;
- opaque task, revision, invocation, attempt, and evidence fingerprints;
- primary/supporting activations and the redacted workflow trace.

It does not copy raw tool output, input values, evidence locators, endpoints, credentials, absolute paths, or executable Gateway IDs into learned content. Task, revision, invocation, and attempt identities remain opaque internal references and cannot appear in generated Lessons or Skills.

The outcome policy is:

| Outcome | Experience behavior |
|:--------|:--------------------|
| Final `success`, every criterion `satisfied` | May support a reusable Skill candidate |
| Final `failure` or `replan_required` | May produce workflow-related failure observations |
| Failed/replanned revision followed by final success | `mixed`: may support both recovery workflow and failure observations |
| `inconclusive`, invalid verdict, verifier/service error | Diagnostic only; no promotable experience |
| `verification=off` | No semantic learning |
| Manual review | Does not create another episode or support count |

The database has a unique AgentTask constraint. PlanRevisions, duplicate completion events, process replay, and review attempts therefore remain one episode and one unit of independent support.

## 4. Background reflection

The coordinator creates the episode and pending job synchronously, then runs model-backed reflection in an `asyncio` task. On restart, interrupted jobs return to pending. Transient job failures retry with bounded attempts; a final failure remains observable but does not affect Forge.

The reflection model receives structured, redacted episode data, active candidates, active Lessons, Lesson clusters, and the registered Skill catalog. Task text, traces, verdict text, and evidence labels are explicitly treated as untrusted data. The response must validate as `experience_assessment_v1`.

Evolution calls use `maxEvolutionCallsPerRun`, which is independent of `maxVerifierCallsPerRun`. When the budget is exhausted, pending work is deferred rather than converted into a task failure.

## 5. Lesson eligibility and observations

A failed or replanned attempt is not automatically a Skill failure. Every proposed observation carries `LessonEligibility`:

| Decision/reason | Handling |
|:----------------|:---------|
| `related / workflow_related` | Eligible for normalization and clustering |
| `unrelated / task_unsatisfiable` | Diagnostic only |
| `unrelated / verifier_limit` | Diagnostic only |
| `unrelated / evidence_limit` | Diagnostic only |
| `unrelated / external_or_infrastructure` | Diagnostic only |
| `unrelated / user_constraint` | Diagnostic only |
| `uncertain / unknown` | Diagnostic only |

An eligible `FailureObservation` contains a Skill binding when available, workflow key, canonical pattern key, generalized pattern summary, `applies_when`, `does_not_apply_when`, and a recovery principle. It must not contain a concrete task answer, object name/value, coordinate, option, endpoint, Gateway ID, raw input, or tool output.

Without an activated Skill, the observation remains in an unbound cluster. An exact workflow-key match from a later Skill candidate can bind that cluster; unrelated workflows are never merged.

## 6. Lesson clustering and activation

The reflection model first tries to match an existing cluster with the same Skill and workflow. Otherwise, a stable cluster identity is derived from Skill/unbound scope, workflow key, and normalized pattern key. The initial implementation does not use embeddings or a vector database.

Each cluster has one of these states:

| State | Meaning |
|:------|:--------|
| `collecting` | Fewer than `minLessonEpisodes` independent AgentTasks, or reopened after counterevidence |
| `blocked` | Synthesis or abstraction/content validation failed |
| `activated` | A validated `ScopedLesson` exists |

Support is unique on `(cluster_id, root_task_id)`, where the stable root reference identifies an AgentTask. With the default `minLessonEpisodes=3`, the first two related failures remain collecting. The third distinct task schedules synthesis.

Synthesis receives only normalized observations from the cluster. A Lesson proposal must include:

- explicit applicable and non-applicable conditions;
- the invariant failure mode;
- a reusable check, decision principle, or recovery recommendation;
- optional same-scope Lessons that it supersedes.

Activation requires both:

1. static content policy approval; and
2. `lesson_abstraction_validation_v1` with `reusable=true`, `contains_specific_answer=false`, no `unsupported_literals`, and confidence at least `0.8`.

The static policy rejects credentials, endpoints, absolute paths, executable IDs, Action Manifest copies, fixed action/input assignments, bypass-verification instructions, prompt injection, answer/option phrases, coordinates, and fixed numeric answers. A rejected cluster stays blocked and is never loaded into Agent context.

## 7. Scoped Lesson retrieval and lifecycle

An active `ScopedLesson` records:

- Skill and workflow scope;
- `applies_when` and `does_not_apply_when`;
- failure mode, recommendation, and severity;
- cluster and supporting episode IDs;
- observation count and counterexample lineage;
- `active`, `inactive`, `superseded`, or `retired` status.

`activate_skill` queries only active Lessons bound to that Skill. A lightweight term-overlap score compares the current task summary with applicability and exclusion text, then prefers stronger overlap, more observations, and newer updates. At most `maxLessonsPerSkill` entries are returned. Unbound, collecting, blocked, inactive, superseded, and retired material is not injected.

A validated narrower Lesson may supersede only an active Lesson in the same Skill/workflow scope. Successful episodes can identify directly contradicted active Lessons. After the configured independent-success threshold, the Lesson is retired and its cluster is reopened for narrower evidence.

Stable safety constraints stay in operator-owned `AGENTS.md` or `EMBODIED.md`; the evolution subsystem never writes those files.

## 8. Skill candidates and guarded promotion

A semantically successful or successfully recovered reusable episode may create or merge a `SkillCandidate`. Candidates are grouped by Skill name and workflow key; synonymous duplicates should reuse an existing Skill/candidate.

Promotion rules:

- each AgentTask episode contributes at most once;
- the default `minSuccessfulEpisodes=3` requires three independent successes;
- an update must target the activated primary Skill;
- a task with a primary Skill cannot create a replacement Skill;
- active same-workflow Lessons and reflection conflicts block promotion;
- structural, content-policy, and reload validation must pass.

Learned workflow content is restricted to a trigger/description, preconditions, generalized steps, verification checkpoints, recovery guidance, and applicability boundaries. Scripts, assets, Action Manifest copies, fixed Gateway actions/IDs, endpoints, credentials, and Forge/Verifier bypass instructions are forbidden.

New Skills are written to `workspace/skills/<name>/SKILL.md` with `always: false`. Existing workspace Skills retain human-authored text; only the marked block is replaced:

```text
<!-- paos:learned-workflow:start -->
...
<!-- paos:learned-workflow:end -->
```

Built-in Skills are never modified in place. Their baseline is archived, copied into a workspace override, forced to non-always-on behavior, and then given the managed block. Writes are atomic, old workspace revisions are archived, the Skill is reloaded from workspace, and any failure rolls back the prior file.

## 9. Persistence and projections

```text
<workspace>/.paos/evolution/
├── experience.sqlite3
└── revisions/<skill>/

<workspace>/skills/<skill>/
├── SKILL.md
└── references/LESSONS.md
```

`experience.sqlite3` is the source of truth and uses SQLite WAL. It stores bindings, episodes, reflection jobs, observations, clusters, unique AgentTask support, cluster jobs, scoped Lessons, candidates, events, and migration metadata.

`references/LESSONS.md` is generated atomically for review. It separates active/historical Lessons from collecting/blocked clusters and reports independent support and validation state. Manual edits to this projection are not authoritative.

The root `LESSONS.md` is preserved. On first evolution startup, legacy `- Lesson:` entries are imported once as inactive, unbound records. While evolution is enabled, the root file is neither global Agent context nor Forge Verifier input. Pre-cluster active database Lessons are also deactivated and must be reconstructed from known source roots, synthesized, and validated before reactivation. Disabling evolution restores the former global Agent-context, verifier-input, and verifier-append behavior without deleting evolution data.

## 10. Observability and extension

Structured events include episode creation, assessment completion, eligibility rejection, observation clustering, cluster support/block/activation, Lesson supersession/retirement, candidate support/block/promotion, validation rejection, budget deferral, built-in baseline archive, and revision rollback. Operational logs use IDs and bounded summaries rather than raw sensitive values.

To add another trusted task outcome provider:

1. implement `TaskOutcomeSource.build(task_ref)`;
2. return `task_outcome_envelope_v1` with semantic verdict, criteria, lineage, and opaque references;
3. preserve AgentTask-level idempotency and redaction;
4. schedule completion through `ExperienceCoordinator`;
5. do not place provider-private execution fields into Lesson or Skill contracts.

This boundary also allows other trusted task providers to reuse Lesson clustering and Skill promotion without changing the Forge Tool API.

## Next reading

- [Framework Introduction](01-framework-introduction.md)
- [User Manual](02-user-manual.md)
- [Core Developer Manual](03-developer-manual.md)
- [Forge Configuration Reference](04-forge-configuration-reference.md)
- [Documentation Index](../README.md)

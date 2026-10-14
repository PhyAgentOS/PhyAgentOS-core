# Governed benchmark sessions

A root `AgentTask` represents one entire benchmark experiment. It owns one Forge
Session. Rounds contain flat `EpisodeSubtask` records, each with its own attempt,
worker slot, world epoch, observation versions, model attempts and DecisionSteps.
A PlanRevision is **not** an environmental episode. Benchmark roots cannot replan.

This opt-in Core integration has an in-process reference executor, verified through
the real Forge Tool API client and AgentTaskCoordinator. An experimental
[RoboTwin private deployment adapter](robotwin-benchmark-deployment.md) connects it
to a separately installed physical runtime. Core itself does not install RoboTwin,
GPU workers, or a production isolation/authentication layer.

## Entry points

`forge.benchmark.enabled` defaults to `false`. Enabling it does not authorize network
execution. The first deployment adapter is explicitly injected
`InProcessBenchmarkDeployment`, restricted to `httpx.MockTransport`. A separately
host-injected trusted deployment adapter is required for a real Gateway; the
RoboTwin example uses `PrivateBenchmarkDeployment` for that purpose.

Activate an installed benchmark Skill, then use the existing public task tools:

```json
{
  "task_description": "Evaluate the frozen case manifest over two rounds",
  "activation_id": "activation_…",
  "verification": {
    "mode": "enforce",
    "goal": "Complete authoritative benchmark evaluation",
    "success_criteria": ["evaluation_complete"],
    "evidence_policy": {
      "profile": "benchmark_evaluation",
      "required_kinds": ["benchmark_report"]
    }
  },
  "benchmark_plan": {
    "cases": [
      {"case_key": "pick-cube-seed-1", "environment": {"seed": 1}},
      {"case_key": "pick-cube-seed-2", "environment": {"seed": 2}}
    ],
    "rounds": 2,
    "parallelism": 2,
    "max_decisions": 32,
    "episode_timeout_s": 300,
    "model_timeout_s": 60
  }
}
```

`forge_tool_start_session(task_id, session_tool, arguments={})` starts the frozen
experiment. All run identities, worker reservations and budgets are host-owned.
Only gateway / interactive CLI lifecycles may start it; one-shot CLI is rejected.
The supervisor outlives the chat reply and publishes deduplicated completion/blocking
notifications through the AgentLoop bus. No chat processing lock covers its work.

`forge_task_get` preserves `data` and adds a sibling `benchmark` summary.
`forge_task_subtasks(task_id, offset=0, limit=100)` lists children; supply
`subtask_id` to read one child. Existing task cancel, Session stop and `/stop` close
admission durably before requesting cleanup. Tool definitions and live execution
checks both restrict shell, MCP and subagent paths while the controlled Skill or an
unresolved benchmark is present. In-flight uncontrolled tools prevent Session start.

## Optional Skill contract

Do not change `skill.yaml`'s schema. Add `benchmark.yaml` and a self-contained,
strict JSON Schema for the environment's bounded proposal:

```yaml
protocol: benchmark_contract_v1
session_tool: benchmark.run
snapshot_tool: benchmark.snapshot
submission_tool: benchmark.submit
proposal_schema_file: proposal.json
snapshot_views: [root, episode, receipt, history]
access_policy: private_control_plane
```

The selected profile's `required_tools` must contain all three operations, with
Session, Query and Action semantics respectively, on one endpoint. A Skill cannot
also declare `interaction.yaml`. The installer generates
`.paos-benchmark-install.json` after verifying the archive. Archives cannot supply
that receipt. Activation, binding freeze and execution validation check the installed
bundle and frozen ToolSpecs. Workspace Skill shadowing is rejected. Declared controlled
Skills fail closed when the extension is disabled, missing or invalid.

Full bundle hashing is intentionally retained. Large bundles incur validation cost;
a manifest-digest cache needs reliable invalidation before it can replace rehashing.

## Executor protocol

The Session starts with `task_id`, `run_id`, `binding_digest`, `plan_digest`.
The Query operation provides four views:

| View | Required response |
| --- | --- |
| `root` | Matching root scope, `start_digest`, `session_invocation_id`, `stopped` |
| `episode` | Strict `EpisodeSnapshot` from `forge/benchmark/contracts.py` |
| `receipt` | `status: found` with an immutable scoped receipt, or unresolved status |
| `history` | `complete: true` and ordered `records`; otherwise no success evidence |

Episode scope consists of `task_id`, `run_id`, `subtask_id`, `attempt_id`,
`worker_id`, `world_epoch`, `binding_digest`. Each episode snapshot also has
`observation_version`, `observation`, `state` (`ready`, `running`, `terminal`,
`unknown`), native `outcome` (`success`, `failure`, `cancelled`, `infra_error`),
`native_score`, `stopped`, and `history_digest`.

History records are full snapshots excluding `history_digest`; the final record
must exactly match the final snapshot, and the digest must cover the complete
ordered history. All observations seen by the runner must occur in that history.
Canonical hashes use sorted UTF-8 JSON and reject NaN. They prove integrity and
association inside the trusted deployment, **not** executor honesty or authentication.

The Action operation accepts host-generated tagged commands:

- `episode.start`: scope, `request_id`, frozen `case`, `lessons_digest`.
- `decision.submit`: scope, `request_id`, `step_id`, `observation_version`,
  `observation_digest`, schema-validated `proposal`.
- `episode.stop`: scope and `request_id`; only original cleanup authority may use it.

Every accepted receipt echoes the scope and `request_id`, sets `accepted: true`,
and includes `payload_digest` over the exact request. For lost-reply recovery it
also supplies `transport_invocation_id`; Gateway GET result must independently
corroborate the same receipt and report a successful Tool operation. Native task
success still comes from the final episode evidence, never the Tool receipt.

Normal starts/submissions register existing Coordinator intents/callers/invocations.
After cancellation, ordinary intent registration remains forbidden. Episode stop
has a separate persisted, bounded cleanup intent in the benchmark journal, using
the original client and a host-owned caller. Root Session stop uses Coordinator's
existing stop intent. Model JSON roles/caller IDs never confer authority.

## Parallelism and reflection

One supervisor holds an OS process lock over the benchmark store. Multiple child
runners use explicitly authorized isolated worker slots, with one in-flight decision
per child. Reusing a worker creates a fresh world epoch. Unknown execution retains
its slot; siblings on other slots can finish, but a round barrier cannot pass until
all children are settled. A second live supervisor fails startup; no hot takeover.

The root manifest and all child reservations are durable before Session start.
Each round uses an immutable lesson snapshot. The initial snapshot is empty;
global active lessons and other experiment transcripts are not loaded into decisions.
Models receive a fresh context, `tools=None`, a strict decision schema and only
resolved observation media. `ObservationArtifacts` resolves an injected map bound
to the scope, observation hash and content hash; arbitrary paths/URLs are rejected.
A finish candidate only asks to stop and inspect authoritative evidence.

Reflection runs at the barrier, at most twice per round, including the final round
for archival. The adapter provides candidate generation and independent workflow /
abstraction validation, using the existing `LessonAbstractionValidation` contract.
Candidates require at least two **distinct case keys** with authoritative failure
trajectories, scoped applicability, and reusable validation without answer literals.
Duplicate case references, retries and later rounds do not create extra independent
support. Candidates are deduplicated and sorted before the next snapshot. Reflection
failure/no eligible candidate retains the prior snapshot with `no_update`.

This evaluates lesson adaptation, not model training. There is no holdout split,
baseline arm, or claim of generalization. Each round's denominator is the frozen case
manifest, and reports separate native outcomes, successful evaluations, evaluation
coverage, native scores, DecisionSteps, model calls/provider token usage, time,
lesson hashes and exposures. Unknown token usage is reported as null, not zero.

## Verification and experience

The only accepted root criterion is `evaluation_complete` under `enforce` and the
`benchmark_evaluation` profile. Deterministic verification checks manifest coverage,
root binding, per-child scope, ordered complete trajectories, receipt digests,
Coordinator invocation facts, stopped workers and the stopped root Session.
Missing/cross-associated/incomplete evidence cannot yield root success.

A benchmark can complete with environmental failures. That is an evaluated result,
not a successful environmental workflow. One formal TaskEpisode is archived for
the root, with report references and round results. `outcome.successful` is false
for benchmark evaluation envelopes; current global candidate/lesson promotion is
skipped. Experiment-local lessons never contribute fake child-root support. Global
experience thresholds and ordinary task promotion remain unchanged. Scoped global
promotion from benchmark evidence would require a separately designed policy.

## Persistence, recovery and limits

State lives in `.paos/benchmark/benchmark.sqlite3` (schema version 1). The task DB
atomically writes a `benchmark_bound` event with root creation; the extension DB
has a frozen binding intent beforehand. Recovery does not assume cross-DB atomicity.
A missing journal fails closed. A crash before root creation can leave an orphan
binding, which grants no execution authority.

`admission_closed`, `needs_stop`, `reconciliation_required`, and `settled` represent
separate obligations. Start/submit intents are written before network dispatch.
After timeout or reply loss, only scoped queries reconcile that request; no automatic
resend. Corroborated Action GET evidence is stored separately without rewriting the
original unknown invocation. A lost root Session receipt is conservatively blocked
even after its physical stop is proved; it cannot start another Session automatically.
Gateway 404, unknown execution, unavailable original Runtime, or incomplete evidence
retain unresolved state and resources. Background queries continue while the owner
is alive; repeated cleanup requests are not blindly resent.

Restart scans unresolved runs including those associated with terminal local roots.
It reconciles and closes admission rather than resuming new episode decisions. It
never starts another attempt, transfers a run to a different Runtime, or reuses an
uncertain slot. A mid-round restart therefore need not produce a complete evaluation.
Shutdown closes scheduling, persists cleanup obligations, drains child cancellation
and cleanup, then releases ownership before transport closure.

There is no independent watchdog or durable notification delivery acknowledgement.
Notifications are at-most-once (persisted before delivery), so a crash can lose one.
Supervisor loss cannot guarantee physical stop or an alert; production rollout needs
external health monitoring, reliable executor stop, isolated workers, durable scoped
receipt/history retention, and a documented operator reconciliation procedure.
SQLite failures close in-memory admission; storage recovery and physical cleanup may
still require that external operator path. No authentication/HMAC placeholder or
boolean configuration substitutes for production deployment review.

## Validation

Run the deterministic acceptance suite without RoboTwin, Minecraft or model access:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest tests/test_benchmark_foundation.py -q
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest tests -q
```

`tests/benchmark_support.py` installs a verified Skill, freezes a real Runtime
binding and uses a three-operation fake Gateway. The tests cover two parallel cases
in two rounds, a local lesson supported by two failures affecting the next round,
independent root completion, one formal episode, cancellation, unknown delivery,
stop/404 failures, recovery, duplicate ownership, binding changes, direct-call and
diagnostic rejection, tampered evidence, lesson support and scoped media.

## Experimental real RoboTwin deployment

A host-injected private adapter is now available in `forge/benchmark/private.py`.
It retains the existing Core protocol and defaults to no episodic context. Optional
frozen context is keyed by case identity; environment-specific arm/task semantics
belong in the deployment example. The existing in-process fake suite remains available.

See [RoboTwin deployment](robotwin-benchmark-deployment.md) for a single real episode,
explicit prerequisites, manual crash reconciliation and validation boundaries.
In reports, `model_calls` counts Core decision callbacks; `provider_calls` separately
reports actual provider requests when the adapter supplies them. Unknown usage stays null.

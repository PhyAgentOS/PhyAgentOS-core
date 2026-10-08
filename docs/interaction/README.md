# Governed interaction foundation

This opt-in foundation connects a single real `AgentTask` to a Forge Session and a
bounded observation/decision loop. It is implemented against core `dev@3dcdc914`.
Minecraft operations, its Bridge, and production control-plane isolation are not
included. No Gateway API or invocation semantics change.

## Enablement and public entry points

`forge.interaction.enabled` defaults to `false`. Enabling it permits the
interaction task contract; it does **not** authorize a real network control plane.
This release accepts only an explicitly injected `InProcessInteractionDeployment`
with an `httpx.MockTransport`, trusted world budgets, and a decision callable.
There is deliberately no `private_control_plane=true` production bypass. A future
production adapter must demonstrate network/credential isolation and endpoint
scope enforcement before it is registered here.

The interaction protocol has no token or HMAC authentication layer. Its current
authority comes from trusted in-process capabilities and the fake deployment
adapter; the protocol does not establish production authentication.

The existing workflow remains:

1. Install the Skill archive using the normal verified installer and activate it.
2. Create a bound task with `verification.mode=enforce` or `recovery`.
3. Call `forge_tool_start_session` with the frozen Session tool, `ownership=task`,
   and `arguments={"world_id":"<authorized world>"}`. The trusted host supplies
   budgets, task/revision identity, start/run IDs and binding digest.
4. Read `forge_task_get`. Its original `data` is unchanged; the optional sibling
   `interaction` reports run state, stop/reconciliation blockers and model calls.
5. Cancel through `forge_task_cancel`, Session stop or `/stop`.

Only a long-lived gateway or interactive CLI may start a run. One-shot CLI/cron
contexts must not enable a long-lived supervisor. The normal CLI has no fake
world injection and cannot turn this foundation into an unapproved real game.
AgentLoop marks its running bus lifecycle as long-lived. Chat replies do not own
the monitoring task. Shutdown/restart drains bounded stop checks before clients
close; unresolved runs remain recorded. Completion/block notifications are
persistently deduplicated, with at-most-once enqueue semantics (not a guarantee
of delivery across an abrupt crash).

## Bundle and protocol contract

An optional `interaction.yaml` declares `interaction_contract_v1`:

```yaml
protocol: interaction_contract_v1
session_tool: grid.run
snapshot_tool: grid.snapshot
submission_tool: grid.submit
proposal_schema_file: proposal.json
snapshot_views: [snapshot, submission, history, run]
event_channel:
  mode: polling
  event_type: progress
  business_kind: interaction_changed
execution_policy:
  max_active_sessions_per_task: 1
  max_inflight_decisions_per_run: 1
  runner_mode: single_owner_no_hot_takeover
  completion_policy: quiesce_then_stop
access_policy: private_control_plane
```

All three tools must be in the existing `required_tools`, have Session/Query/Action
semantics respectively, and resolve to the same Endpoint. Proposal schemas are
self-contained JSON Schema Draft 2020-12 objects with unknown object fields
forbidden. No remote schema references are resolved. Tool readiness and ToolSpec
hashes continue to be checked by the original Forge binding resolver.

The normal archive manifest covers the sidecar and schema. Installation writes
`.paos-interaction-install.json` after verification and before atomic installation.
Bundles cannot supply their own receipt. Existing ordinary bundles need no
receipt; an interaction bundle without one needs a verified reinstall. Activation
rejects a same-name workspace override. Sidecar/schema edits require a new
installation and activation, not a running-contract replacement.

The business wire contract follows the reviewed prototype: integer executor
cursors, `snapshot/submission/history/run` views, distinct start/run/step/submission
and invocation IDs, and immutable acceptance/rejection receipts. The canonical
business payload hash is sorted compact UTF-8 JSON with NaN forbidden; submission
hashing excludes `runner_epoch`. Query outputs and invocation result outputs use
their respective existing Forge envelopes. `accepted` never means the physical
operation or root goal succeeded. Receipt `reason` and `execution_status` retain
their original names and meaning.

## Ownership, recovery and verification

`CoordinatorInteractionClient` uses the real Coordinator's intent and invocation
ledger. Monitoring reads validate task/run scope without requiring the task to
remain executable. Each operation pins its Runtime client, including a final
pre-dispatch identity/cancellation check. Stop/status never follow an old
invocation ID onto a replacement Gateway. The model cannot supply Runner
capabilities through JSON arguments.

The independent `.paos/interaction/interaction.sqlite3` stores versioned bindings,
runs, steps, model attempts, submissions and append-only audit events. A durable
marker in the task event ledger prevents silent fallback if the extension journal
is missing. Cross-database crash recovery uses intents, not a claim of atomic
commit across both databases. A crash-safe process lock prevents hot takeover.

Unchanged snapshots do not repeat inference. Model attempts/time/tokens and idle
progress are bounded. `ProviderDecider` uses a fresh data-only context and
`tools=None`, and rejects tool calls and self-reported usage. Registry policy
checks both advertised schemas and each actual execution, including subagents,
subsequent messages and tool calls later in the same model response. Unsafe
in-flight tool calls prevent startup. A cancelled unsafe tool coroutine retains
its local guard because cancellation alone cannot prove an external process ended.
These checks cover core model paths, not an OS/network isolation boundary.

Timeouts, lost receipts, Gateway 404, changed Runtime identity and unresolved
execution block new work. The first version performs **no automatic start or
submission retransmission**, including after `absent_confirmed`. Unknown does not
release the duty to request stop and perform bounded reconciliation. If that
cannot establish safety, the run stays blocked and retains resource ownership.
Gateway stop acknowledgement or repeated polling cannot repair a lost Gateway
control exchange. This external limitation remains a real-game admission blocker.

Finalization, verification errors, replanning and resource release all consult the
interaction gate. Cancellation closes admission durably before control I/O. A new
Session requires a new PlanRevision after the previous run is settled. Verification
reads checked initial/final observations, immutable receipts, step associations and
durable executor history, rather than robot-only evidence capture. Evidence is
written using existing artifact hashes, lineage and EvidenceBundle validation.
Use `required_kinds: [interaction_observation]`; the complete process trace is also
required by the interaction adapter, not inserted into the legacy before/after
kind requirement. One root task produces one deduplicated experience episode with
trace references. A model `finish_candidate` can still yield a failed root goal.

## Compatibility and operational limits

The Coordinator's `_append_execution` admission guard also applies to ordinary
AgentTasks, even when interaction is disabled. When a cancellation request is
persisted or a task leaves `executing`, the intent transaction refuses new Query,
Action and Session records before dispatch. This tightens ordinary-task behavior
when state changes after the initial executable check; it does not cancel calls
already dispatched or prove their remote outcome.

When two processes share an interaction journal and one still owns an unresolved
run, the second process raises `RunnerAlreadyOwnedError` during recovery. This
propagates through the Coordinator to Agent startup, so the second Agent fails to
start; the original owner continues. This startup failure path is introduced by
the interaction foundation. It is retained as the v1 single-Runner policy, with
no hot takeover or restricted-chat fallback. Operators must avoid starting a
second owner against the same workspace and must never remove the owner lock to
force takeover of a live run.

If the Supervisor is absent or its monitoring task is no longer running, persisted
cancellation alone cannot guarantee physical stop or continued reconciliation.
Unresolved runs retain their blockers and resource ownership. Notifications from
the same Supervisor do not provide independent failure detection. Before real-game
admission, add external health monitoring and an operator procedure to stop the
environment through its authorized control plane and reconcile its execution
facts. Do not manually mark the journal settled or delete it to release resources.

`verified_extension` currently hashes every installed bundle file on each check,
including repeated checks during finalization. This is acceptable for the small
fake environment but may be expensive for bundles containing large game assets.
A manifest-digest cache is follow-up work and must have reliable invalidation on
bundle changes without weakening tamper detection. v1 retains full validation.

## Reproducible acceptance

```sh
python -m pip install -e '.[dev]'
python -m pytest tests/test_interaction_foundation.py -q
python -m pytest tests -q
```

On macOS, `TMPDIR=/private/tmp` avoids the existing evidence test's unresolved
`/var` versus `/private/var` tempfile alias. Set
`LITELLM_LOCAL_MODEL_COST_MAP=True` to keep provider cost-map loading offline.

`tests/interaction_support.py` supplies a persistent SQLite fake executor behind
MockTransport, installs a real verified Skill bundle, creates a real activation
and AgentTask, and uses an independent final-position verifier. Tests cover the
normal multistep loop, actual AgentLoop reply/stop behavior, shared tool policy,
late model results, cancellation during root verification, transport uncertainty,
Runtime replacement, process ownership and recovery, disabled/missing extensions,
installation tampering, evidence gaps and a single root experience episode.

Before adding a real game: provide its authenticated Endpoint/Bridge adapter and
independent goal evidence; repair the game's offline watchdog; prove isolation
against shell/HTTP/delegation, aliases and lifecycle routes; and validate stop
failure/packet-loss behavior against the actual Gateway and game. SSE, hot
failover, cross-Runtime rebinding, automatic retransmission and Subtask DAGs are
not provided by this foundation.

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
| `agent/core/` | Seven Core components (host, runtime, loop driver, model adapter, prompt assembler, tool executor, SQLite session store); contracts in `types.py`, `events.py`, and `protocols.py` |
| `agent/composition.py` | `compose_agent`: builds the Core assembly from injected collaborators and fixes capability order |
| `agent/capabilities/` | Native Forge tool definitions and operations, Core `ToolOutcome` mapping, and `activate_skill` |
| `agent/services/skill_documents.py`, `agent/services/turn_activation.py` | Skill document discovery and per-turn activation identity |
| `agent/bootstrap.py`, `agent/services/forge_verifier.py` | Shared Forge factories and the semantic verifier; `agent_legacy/session_verifier.py` remains an alias |
| `agent/services/session_catalog.py` | Session ids, titles, per-day counters and per-session provider/model/effort selection in `core.db` |
| `agent/services/context_compaction.py` | Manual compaction summary and the recent-complete-turn projection |
| `agent/services/usage.py` | Usage ledger, next-request context estimate and turn/session footer formatting |
| `agent/services/background_runtime.py` | One shared Core assembly for gateway cron, heartbeat and channel turns, with frozen routing and bounded admission |
| `cli/commands.py` | Single Typer app, global callback, and registration of the command groups, including the one `agent` command |
| `cli/command_groups/agent_commands.py` | Core `paos agent` (default): dependency construction, per-turn assembly, `CoreAgentSession`, and process lifecycle |
| `cli/command_groups/` (other modules) | Provider, gateway, Skill, Forge Node, channel, and onboard/status command groups registered by `commands.py` |
| `cli/configuration.py` | Config loading and startup provider/evolution selection shared by the gateway and legacy paths |
| `cli/legacy/legacy_agent.py`, `cli/legacy/legacy_support.py`, `cli/legacy/legacy_gateway.py` | Legacy `paos agent --runtime legacy`, its terminal interaction, the legacy prompt-session helpers, and the legacy gateway selected by `paos gateway --runtime legacy` |
| `cli/interaction/interactive_session.py`, `cli/interaction/agent_output.py`, `cli/interaction/terminal_input.py`, `cli/interaction/terminal_display.py`, `cli/interaction/model_picker.py` | Core input scheduling and cancel, message rendering, terminal input, the live status/tool region, and provider/model/effort selectors |
| `agent_legacy/loop.py` | Existing Agent loop, general tools, dynamic MCP, Forge tool registration, context, and lifecycle |
| `agent_legacy/tools/forge_tool_api.py` | Compatibility wrappers for the ten Gateway Tool API tools |
| `agent_legacy/tools/forge_task.py` | Compatibility wrappers for the five AgentTask lifecycle tools |
| `forge/tool_client.py` | Strict asynchronous HTTP client and response validation |
| `forge/binding.py`, `forge/task.py` | Immutable Runtime/ToolSpec binding, AgentTask models/store, evidence, verification, and recovery |
| `forge/observation.py`, `forge/evidence.py` | Best-effort image/state collection and artifact writing |
| `skill_runtime/` | Manifest, catalog, safe archive, installer, Registry, state, Runtime manager, and availability |
| `agent_legacy/experience/` | Activation, episodes, assessment, Lessons, Skill candidates, and evolution |
| `verification/` | Public task, evidence, request, and verdict contracts plus verification service |

## 3. Forge Tool API client

`ForgeToolClient` accepts only JSON object envelopes with `ok=true` and object-valued `data`.
Errors preserve HTTP status, error code, retryability, and any returned invocation identity.

Routes, response codes and pending/cancel/stop semantics are defined in the [Forge Tool API contract](../forge/README.md#2-tool-discovery-and-context). Keep the client implementation aligned with that contract rather than introducing a second route definition here.

Path components are percent-encoded. PAOS supplies `caller_id` and persists it with the intent
before Action/Session admission. Admission must contain a non-empty `invocation_id`; Action also
requires `attempt_id`. A timeout leaves an unknown record and recovery never repeats the POST.

## 4. Agent-facing tools

The public Agent-facing tool catalog is maintained in the [API contract](../forge/README.md#6-agent-tools). Tool names, descriptions, JSON Schemas, and operations live in `agent/capabilities/forge_tool_api.py` and `agent/capabilities/forge_task_tools.py`; `agent_legacy/tools/` only keeps compatibility wrappers over them. Keep transport handling in `forge/tool_client.py` and coordination in `forge/task.py`.

The Core path also registers `activate_skill` from `agent/capabilities/skill_access_capability.py`. It reads a registered Skill document and returns a turn-scoped `activation_id`; it does not start a Skill runtime and is not a Forge tool.

Diagnostic Query and bound calls invoke the same HTTP methods. Every mutating or task-attributed
wrapper validates ownership and the frozen Tool binding before network access. Tool wrappers must
never invent a Gateway identity or result.

Optional `timeout_ms` is not defaulted by the Agent: when the Gateway's limit is unknown the model
omits it and the Gateway applies its own default. An HTTP client timeout is a different setting and
must not stand in for it. The Core prompt fragment reads unresolved Action/Session blockers from
the coordinator on every step, across the whole workspace, and reports task/invocation identity,
status and semantics only; a local `succeeded` never hides an older unknown, and `None` means the
Gateway identity was never confirmed. The fragment is current blocking state, not proof of
resolution.

## 5. AgentTask contracts and transactions

`AgentTaskRecord` contains a stable ID, task description, `TaskVerificationContract`, status,
append-only PlanRevisions, evidence references, verification attempts, cancellation state, and
timestamps. Each `PlanRevision` contains its own Tool records, verdict, and verification attempts.

`AgentTaskStore` uses SQLite WAL and `BEGIN IMMEDIATE`. Creation queries for any non-terminal task
inside the same transaction, enforcing one global active slot across processes. Updates write the
complete validated record and an append-only event. Callers do not modify tables directly.

The [AgentTask state contract](../forge/README.md#8-agenttask-model) defines public states. Store transitions must preserve those semantics; an `unknown` Tool record closes accounting without proving physical stop.

The Gateway now labels its own pre-dispatch request validation with the explicit
`FORGE_TOOL_REQUEST_VALIDATION_REJECTED` error code. PAOS treats that code as `failed` only
when the response is HTTP 400 and has no invocation/attempt identity. An older or currently
released Gateway that returns a bare 400 (`msg` without `error.code`) still stays `unknown`;
the updated source must actually be deployed before the rejection stops recurring, so do not
claim the running Gateway is fixed.

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

## 13. CLI entry ownership and lifecycle

`paos agent` is registered once in `cli/commands.py`. The command takes an explicit runtime
selection and never switches to the other one after a failure:

| Selection | Owner | Status |
|:----------|:------|:-------|
| `--runtime core` (default) | `cli/command_groups/agent_commands.py` | Core path; does not import the legacy package |
| `--runtime legacy` | `cli/legacy/legacy_agent.py` | Previous implementation, explicit opt-in for comparison |

`commands.py` keeps the Typer app, the global callback, and the one registration point; each
command group owns its own Typer app or `register_*` function. `register_agent` imports the legacy
runner only inside an explicit `--runtime legacy` branch; building the app or running Core never
imports `agent_legacy` or `cli.legacy`. `configuration.py` holds config
loading and startup provider/evolution selection shared by the gateway and legacy paths.
`agent_commands.py` owns only the Core path: it reads configuration, builds dependencies, builds
one assembly per turn, and closes the session; it also still holds `CoreAgentSession` and the
Core dependency assembly. `interaction/interactive_session.py` schedules Core input,
`interaction/agent_output.py` renders messages, `interaction/terminal_input.py` owns prompt_toolkit
input and terminal state, `interaction/terminal_display.py` owns the live status and tool region,
and `interaction/model_picker.py` holds the shared terminal selectors.
The Core prompt proxy runs with raw ANSI passthrough so Rich styling reaches the terminal instead
of appearing as literal `?[36m` text; untrusted model, tool and exception text is stripped of
control characters before rendering, so only Rich introduces escape sequences.

The prompt is the only writer of the live region: its bottom toolbar renders the turn phase
(preparing, thinking, calling a tool, stopping locally) with a spinner and elapsed time, plus the
last four tool calls of the current turn, the hidden count and the total. There is no separate
`Live`/`status` renderer, so the input line, animation and tool area cannot move the cursor against
each other. `prompt_toolkit`'s refresh timer animates the toolbar; state changes only come from
persisted runtime events and the session's submit/cancel callbacks. A tool call is keyed by
`(session_id, turn_id, tool_call_id)` and updated in place from `tool.intent` to
`tool.dispatched` to `tool.settled`, so repeated events do not increase the total. Turning the turn
over resets the rows, which keeps a new turn from inheriting an older turn's state. When no prompt
is attached (a one-shot `-m` run, startup, or redirected streams) the same state prints one plain
line per transition with no animation and no escape sequences. `/tools` prints a redacted record
on request; it shows the tool name, local status and a length-limited error summary, never tool
arguments, outcomes or credentials.

The session/provider/model/effort line is part of the prompt message, above the input; the bottom
toolbar is only attached while a turn is busy. While busy the `You:` marker and the renderer cursor
are hidden through the layout's `always_hide_cursor` filter, the buffer is read-only, and Enter is
blocked. Ordinary keys therefore never enter the buffer or a hidden model queue; a draft typed
before the turn is preserved, and the marker plus cursor return when the turn ends. Non-terminal
input keeps its plain line behaviour.

On a terminal, bare `/model` and `/effort` reuse the inline picker. The reader is paused (its
prompt ends and no new prompt starts) before the second prompt_toolkit application opens and is
resumed only after the menu returns; Esc or Ctrl+C closes only the menu and leaves the session and
selection unchanged. A partially typed line is carried into the next prompt instead of being
discarded. Every model command (text, menu, reset, cancelled or failed) republishes the toolbar
label from the session's actual selection. Text commands and non-terminal input skip the picker.

The legacy path keeps its existing lifecycle inside `legacy/legacy_agent.py` (build `AgentLoop`,
run one-shot or terminal flow, then stop the loop and close MCP), with prompt-session helpers in
`legacy/legacy_support.py`; the steps below describe the new Core path.

### Core process lifecycle

1. Load and validate configuration, apply `--workspace`, and reject fleet mode.
2. Resolve the provider/model/effort selection. The selection is read when a turn actually
   starts, so a queued submit picks up changes made before it runs; a running turn keeps the
   selection it started with.
3. Build Forge components: Skill catalog and runtime availability, `ForgeToolClient`,
   `AgentTaskCoordinator`, and the optional verifier.
4. Reconcile Forge unknown Action/Session records once for this coordinator, before any turn
   can be submitted. `reconcile_nonterminal()` only issues Gateway GET requests; it never
   replays an Action or Session POST and never turns an unresolved unknown into a terminal
   state. Core has no interaction supervisor, so this path does not request a robot stop. If
   recovery itself fails, the failure is reported and the session still opens; the unresolved
   unknown state continues to block new tasks.
5. Create `SkillDocumentLoader` and `TurnSkillActivationManager`, then register the activation
   manager with the coordinator so `forge_task_create` can validate `activation_id`.
6. Open the session store at `<workspace>/sessions/core.db`.
7. Build a seed assembly with `compose_agent`; `SkillAccessCapability` is first and
   `ForgeCapability` is last.
8. Run one message or the interactive loop. Each turn reads its session's selection when it
   actually starts and builds a model-facing assembly that serves that one turn; the runtime is
   stopped as soon as the turn ends (its host stays open), rather than waiting for the next
   submit. The store, Forge coordinator/client, and activation manager stay shared. Submits in
   the same session run serially in registration order; different sessions still run
   concurrently. `cancel` targets only the current assembly, so a queued turn never inherits an
   earlier cancellation.
9. Shut down once: reject new submits and let queued submits leave before they start a turn,
   stop the runtimes that may still own a turn, wait for submits already inside the session,
   notify each accepted session's capabilities, close the canonical capabilities once, then
   close the store. The Forge client and verifier are closed by the resource stack after the
   session.

`forge_task_create` refuses to create a task while any earlier Action/Session invocation still
has unknown remote state, including tasks from another session. The refusal names the blocking
`task_id`, the confirmed `invocation_id` (or explicitly `None` when the Gateway identity was
never confirmed), the record status and semantics, and the matching status tool to inspect. It
does not suggest listing tools, does not resend the old request, and does not tell the operator
to delete the store or restart to clear the blockage.

While a turn runs, Esc is the only stop entry point: the first Esc arms a two-second window and
shows a yellow hint, the second Esc requests local cancellation through the same session cancel
path used before. A timeout, any other key, an arrow key, or a new turn clears the armed state, so
a single Esc and cursor/meta keys never stop a turn. Esc in a model menu only closes that menu.
Cancellation asks Forge to cancel the session's latest task but does not prove a remote action
stopped; the status says "local" and "remote unconfirmed". Local commands (`/help`, `/tools`,
`/correct`, and rejected unknown slash commands) are answered by the reader immediately; `/stop`
only returns a hint and never cancels or submits a model turn. Non-terminal input cannot capture
Esc, so it relies on Ctrl+C. Ordinary text and model commands keep their FIFO order, model
commands still run only between turns, and programmatic/injected queued input gets a position-only
receipt and a "processing" notice. The receipt never repeats the message text.

`agent/services/forge_verifier.py` is the single verifier implementation used by both paths;
`agent_legacy/session_verifier.py` re-exports it, so existing imports and module patches still
apply.

### Session catalog and identity

The Core store keeps events and catalog metadata in the same `core.db`. `SessionCatalog`
(`agent/services/session_catalog.py`) adds two tables: `session_catalog` holds a title and the last
provider/model/effort selection per session, and `session_counters` allocates one sequence per
local day. `create_session()` inserts the counter and the catalog row inside one
`BEGIN IMMEDIATE` transaction and retries if the resulting id already exists, so concurrent
processes and restarts never receive the same `cli:YYYYMMDD-NN` id. Sessions that only exist in
`session_events` stay listed with their original id; metadata is created lazily and no id is
renamed. The title comes from `derive_title()` over the first input: whitespace is collapsed,
credential-shaped text is redacted, the value is shortened locally, and no model call is made. The
title is metadata only; the id never changes. `--session <id>` calls `ensure_session()` instead of
allocating a new id. Bare `/session` lists catalog entries (most recently updated first), and
`/session <id>` resumes one. `/new` allocates a new id in the running process. Both commands are
answered locally and never submit a model turn. Legacy JSONL sessions are neither read nor migrated.

### Context accounting and the turn footer

`UsageLedger` (`agent/services/usage.py`) is fed from durable events and is idempotent per
`model_call_id`: a model call contributes its usage once, and a repeated completion only refines
the stored snapshot. A failed or interrupted attempt that did report usage is counted as real
spend; one without usage does not make history incomplete, while a completed call without usage
does, and the summary says so. A missing field stays `None` and renders as `--`; only an explicit
provider `0` becomes zero. `cached_tokens`
and `reasoning_tokens` are informational: cache reads are already inside prompt tokens and
reasoning tokens are already inside completion tokens, so they are never added to input, output or
total. `total_tokens` uses the provider value when present and otherwise prompt plus completion.
The cache hit ratio is shown only when every counted call reports both prompt and cached tokens.
`estimate_context()` starts from the newest durable `request.prepared` snapshot, adds assistant and
tool messages after it, and marks the number `exact` only while the provider-reported prompt size
still describes the unchanged request. `ProviderService.resolve` composes the effective window:
reliable model metadata wins unless an explicit `context_window_tokens` cap is smaller; unknown
metadata uses an explicit cap when present; only when there is neither reliable metadata nor an
explicit cap does it use the documented 65,536 fallback and record source `fallback`. The raw model
value and the effective guard stay separate. Model rounds count provider calls, not internal reasoning steps,
and no reasoning step count is fabricated. The session cumulative
line is rebuilt from stored events, so it survives a reload. The footer is a terminal rendering
only; it is never inserted into the message history sent to a provider.

### Prompt projection, tool staging and startup read

Three request-only projections run before a prompt is sent; durable events are never rewritten.

- `project_skill_documents()` keys Skill results by `(skill name, exact content sha256)`. One full
  document per version stays in the request; older copies keep activation metadata, content hash,
  character count and an omission marker. A very short body stays duplicated when collapsing would
  save fewer than 128 characters. A compact summary is not a document copy: the projection only
  deduplicates full texts already in history. A later `activate_skill` re-authorizes the turn and
  returns the full body; it does not read a local file automatically after compaction. Tool-call ids
  and message order are untouched.
- `project_forge_tool_results()` only deduplicates **identical successful** Forge read results,
  keeping the newest full result and replacing older copies with a hash/reference marker. The
  marker carries no old notes. Collapse only when the serialized result saves at least 128
  characters; short results stay verbatim. Errors, unknown/running/accepted/pending results,
  non-JSON text and non-success payloads are left intact; a non-terminal `phase`/`cancel_status`
  is never treated as success for deduplication. The
  protected fields include task/invocation/action/session id, status, evidence, image/artifact
  references and unknown semantics.
- `PromptAssemblerImpl` accepts a `ToolVisibility` hook. `composition.py` wires Forge tool stages:
  before a primary Skill activation only read/cancel tools are visible; after activation task
  creation tools become visible; registration itself is unchanged. `phase`-gated hiding must never
  remove a tool from the registered catalog.
- `SQLiteSessionStore.read_startup_snapshot()` reads recovery and conversation in one SQLite read
  transaction. It falls back to `recover()` plus `read_conversation()` for stores or subclasses that
  override recovery. `SessionSnapshot` keeps both values from the same view. If four consecutive
  CAS attempts lose to a busy writer, the turn fails closed with `RuntimeStorageError`; it does not
  open a new turn or continue with a recovery history that may be stale. Without a lease, an active
  writer can still race a read; the guarantee is no incomplete history is accepted, not exclusion.
- `utils/helpers.py` caches prompt estimates by content hash in bounded maps; tokenizer reload and
  `clear_prompt_estimate_cache()` drop those entries, so an edited System/tool/message body cannot
  reuse a stale count. Cache entries are estimates, never provider billing.

### Streaming contract

`ModelAdapter.stream()` is used when the injected provider exposes a callable `chat_stream`; a
provider that does not keep the `complete` path and yields only the terminal response. Streaming
text is body text only and is persisted as step-scoped `model.response.delta` events before
`model.attempt.completed`; `reasoning_content` is never streamed. The terminal completed event
still carries the full content and the usage actually received, history stores exactly one
complete assistant message built from that terminal response, and the display layer appends
deltas without printing the completed content again. Tool calls are collected from complete
`tool_call` events, so the loop executes nothing until the arguments are fully received. Closing
the returned generator closes the provider stream on cancellation; a cancelled or disconnected
stream is never re-requested automatically. A budget error is raised before the first event so it
is recorded as a clean failed attempt. Usage on failed or interrupted attempts contains only keys
the provider actually sent.

### Manual compaction

`/compact` stores a checkpoint row in the session's append-only checkpoint projection; it never
rewrites `session_events`, and the original events stay readable. It runs only between turns; it
writes a summary of earlier history and keeps recent complete turns verbatim. Each summary call
first writes a `compaction_attempts` row and finishes it with a status and the usage the provider
actually reported. A compaction refused during planning, before any provider call, writes no
attempt and costs nothing. A failed or cancelled attempt therefore leaves the conversation
projection and the original context unchanged, while the attempt and any known usage remain
visible. Successful and failed attempts contribute the usage they actually reported to the session
cumulative total; a failed attempt without reported usage adds no tokens but stays visible in the
attempt trail. The usage copied onto a checkpoint is reference metadata and is not added a second
time. The before/after estimates for compaction and the per-request pre-check share one real token
estimator that also counts the system prompt and the visible tools, instead of estimating from
character counts; a checkpoint is written only when the next request's estimate actually shrinks.
The UI switches the checkpoint projection only at `REQUEST_PREPARED`, so a turn start
(`TURN_STARTED`) does not fall back to the old history. Compaction never dispatches a tool call and
never changes an AgentTask or Forge record, so an unresolved `unknown` after compaction still
blocks new tasks.

### Background gateway runtime

`paos gateway` builds its cron, heartbeat and channel turns from
`open_background_core()` in `agent/services/background_runtime.py`, using the same
`CoreAgentSession`, store, Forge coordinator/client and Skill activation manager as the CLI.
`TurnBinding` freezes channel, chat, reply target and thread metadata before a turn starts, and
capabilities read the binding by `session_id` (not a shared `ContextVar`), so a later turn cannot
retarget an in-flight reply. The last external chat target is persisted to
`<workspace>/.gateway_route.json` (only channel, chat and `message_thread_id`) and restored at
startup; loading and use both re-check that the channel is enabled, so a stale target is not used.
Heartbeat cycles are serialized by the heartbeat service's cycle lock, so they cannot overlap. A
session's turns stay strictly ordered; different sessions run up to the configured concurrency and
a session over its queue limit is refused instead of growing without bound. The channel dispatcher
publishes at most one final message per turn as the completed text; streamed deltas are not
forwarded to channels. It suppresses the message when the message tool already delivered the
turn's reply. The Core CLI registers `CronCapability` over the same job store but never starts the
scheduler; scheduling runs only in `paos gateway`. Shutdown stops intake, settles accepted turns
and closes Core once. Channels that are not configured are not started. `--runtime legacy` keeps
the old implementation available for comparison and imports it only when selected; building or
running the Core gateway never imports `agent_legacy` or `cli.legacy`.

Known cron store behavior: jobs live in one `jobs.json` with a sibling `.lock` file. `_store_lock()`
takes a blocking `flock` (cross-process through `fcntl` on Linux; on platforms without `fcntl` it
only serializes within the process) and is held only around the synchronous read-modify-write,
never across an awaited model or tool call. Writes go to a temporary file and are published with
`os.replace`. After a job runs, the service reads the latest file under the lock and merges only
that job's own runtime state; an external add, delete, disable or schedule change is preserved. The
store remains one JSON file: this round did not move it to SQLite or add a configuration option.
Run a single scheduler owner; this is not a multi-gateway exactly-once claim.

### Skill activation scope

`activate_skill` is a Core tool from `agent/capabilities/skill_access_capability.py`. An activation
belongs to one `session_id` and one `turn_id`; ids from an earlier turn, another session, or a
previous process are rejected. Activation records are held in memory, so a restart requires
activating again. One primary Skill and any number of supporting Skills are allowed in a turn.
Activation reads the Skill document and does not start a Skill runtime.

### What the Core path does not cover

General-purpose tools, `/correct`, memory and evolution, managed interaction Sessions, and fleet
mode are not migrated. The Forge interaction supervisor is not wired into the Core CLI, so this is
not a completed interaction migration. Existing legacy JSONL sessions are not imported; the Core
store is a separate SQLite file. Provider failures are reported with a short redacted diagnostic:
the exception type and HTTP status are kept, while response bodies and credentials are not.

### Tests

Focused suites cover document discovery, activation scope, verifier aliasing, Core CLI single-turn
and per-turn assembly wiring, interactive FIFO, cancel, rendering, queue receipts, local command
handling, and tool-row tracking. Session catalog and manual compaction have local tests for atomic
per-day ids, title derivation, event-only sessions, selection persistence, checkpoint projection,
refused/failed summaries, open turns and retained tool pairs; the Core CLI session commands are
covered by `test_cli_session_features.py`. Streaming coverage uses fake providers with delta,
tool-call, usage, done and error events, including cancel and
no-`chat_stream` fallback. A provider-level check drives the real `AsyncOpenAI` client through an
in-process mock transport and SSE stream, confirming text before finish, one POST, one history
message and usage/cache handling; no external provider is contacted. Usage display, provider usage
details, streaming cancel/display and the gateway background runtime have local suites of their
own (`test_core_usage_display.py`, `test_provider_usage_details.py`,
`test_core_streaming_cancel.py`, `test_core_streaming_display.py`,
`test_core_background_runtime.py`, `test_core_background_capabilities.py`,
`test_core_scheduler_gateway.py`). Gateway tests cover route restore, the frozen heartbeat target,
serialized heartbeat cycles, one-reply suppression and scheduler wiring against temporary stores. These are local tests: no external provider or real
channel delivery is exercised; cron and heartbeat are driven locally, and no numeric test count is
guaranteed by this manual. The terminal behaviour is also exercised with a real prompt_toolkit
pipe input and Vt100 output: the session line above the prompt, the hidden marker and cursor plus
blocked keys while busy, draft preservation, the first-Esc hint, a second Esc cancelling exactly
once, timeout/other-key/arrow disarm, menu Esc containment, resize, and a paused reader around a
menu. This is not only a mocked TTY flag. They use local fake providers, fake Forge clients, and
temporary stores; no real provider or Gateway request is part of the suite. The full gate remains
`pytest`, `ruff check PhyAgentOS tests`, and `compileall`.

## 14. Test gates

```bash
ruff check PhyAgentOS tests
python -m compileall -q PhyAgentOS tests
pytest -q
```

Tests should cover response contracts, Session ownership, pending/cancel/stop/timeout/unknown
semantics, one active task, diagnostic Query, immutable bindings, no-POST recovery, revision
recovery, evidence, version-scoped episodes, archive attacks, transactional rollback, Registry
verification, Runtime health/switching, mocked workflows, CLI runtime selection, Core per-turn
assembly, interactive FIFO/cancel, Skill document precedence, and activation scope. Concrete
hardware/simulator tests are conditional on independently installed matching artifacts and Dora
availability.

## Next reading

- [Forge Tool API Integration Contract](../forge/README.md)
- [Bundle and Tool Integration Reference](../user_development_guide/README.md)
- [Agent Experience and Skill Evolution](05-agent-experience-and-skill-evolution.md)

## Node development references

- [TrackB framework guide](../user_development_guide/track_b/README.md)
- [Piper X MuJoCo development example](../user_development_guide/PIPER_X_DEMO.md)

# Experimental RoboTwin benchmark deployment

This adapter connects the governed benchmark Core to an installed Forge/RoboTwin
runtime and a real pi0.5 policy. Start with the single-episode smoke below. It runs
one `none` case, no development episode, no reflection, no frozen experience and no
automatic retry. A valid native robot failure is a completed evaluation, not an
infrastructure failure.

The Core still owns task/binding identities, decisions, receipts, the SQLite
journal and final verification. The adapter owns the native invocation and
physical teardown. Models receive scoped images/data and a proposal schema,
without execution tools. The private transport is host-injected and has no public
HTTP listener or configuration-only authorization bypass.

## What must already exist

The code is not a complete RoboTwin distribution. Use a **Linux** host with:

- A tested RoboTwin/XPolicyLab checkout, assets, SAPIEN and a CUDA/cuRobo build
  compatible with the actual GPU/driver. Record the external repository revisions.
- An installed Forge physical Skill with a candidate-aware `joint_abs` profile:
  Gateway, RoboTwin benchmark/policy nodes, VLA bridge, head/left/right wrist image
  nodes, their virtual environments and Forge node/bundle caches. The launcher
  copies the profile into a new isolated HOME; it does not install these external
  nodes or reconstruct a missing dataflow.
- The pi0.5/OpenPI Python environment and checkpoint. The explicit policy JSON
  must match the checkpoint's training configuration and RoboTwin joint order.
- A multimodal OpenAI-compatible model endpoint in a private PAOS config file,
  under `providers.custom` (`apiKey`, `apiBase`). Keep the file outside Git.
- Free policy/simulation GPU resources and two free local ports. The example
  does not terminate other jobs or select GPUs automatically.

Install a supplied physical Skill bundle with the existing CLI if needed:

```bash
paos skill install /absolute/path/to/robotwin-skill.tar.gz --local
paos skill inspect robotwin-benchmark
```

Use the installed name as `source_skill`. Follow that bundle's setup instructions
for native dependencies, node environments, assets and runtime environment variables.
Installing Core or the bridge example alone does not provide those prerequisites.

## Install this Core revision

```bash
git clone --branch qinhan-robotwin-vlm-vla-benchmark-20261013 \
  https://github.com/PhyAgentOS/PhyAgentOS-core.git
cd PhyAgentOS-core
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
git rev-parse HEAD
```

If using an existing checkout, select this PR's tested revision without discarding
local changes. Use the same Core installation for the runner and `paos` subprocesses.
The separate policy environment runs pi0.5; do not install GPU policy dependencies
into Core solely to run the CPU tests.

## Explicit configuration

Copy `example/robotwin-rsi-benchmark/smoke.example.json` **outside the checkout** and
replace every path/resource value. The runner requires all fields and rejects
unknown fields. Paths are absolute. The template contains no credentials.

| Field | Meaning |
| --- | --- |
| `source_home` | Existing prepared HOME containing `.PhyAgentOS/skills` and Forge caches |
| `source_skill`, `profile` | Exact installed physical Skill and its joint-absolute profile |
| `xpolicy` | XPolicyLab root; the task description is read from its parent's `description/task_instruction` |
| `policy_python` | Python executable from the matching OpenPI environment |
| `policy_config` | External checkpoint/config JSON for the policy server |
| `model_config`, `vlm_model` | Private PAOS provider config path and actual multimodal model ID |
| `task`, `max_steps` | Runtime task name and its verified native step limit |
| `policy_seed` | Policy RNG seed; native benchmark request remains seed 0 / init_state_id 0 |
| `policy_gpu`, `simulation_gpu` | Explicit physical GPU indices for the two processes |
| `gateway_port`, `policy_port` | Distinct unused loopback ports |
| `lease_dir` | Persistent, shared reservation directory used by all runners on this host |

Example **policy** config (values must match the checkpoint you actually have):

```json
{
  "task_name": "press_stapler",
  "env_cfg_type": "aloha_agilex",
  "action_type": "joint",
  "model_path": "/absolute/path/to/pi05-checkpoint",
  "checkpoint_num": 29999,
  "train_config_name": "pi05_base_aloha_full_sim_arx-x5_seed_0",
  "repo_id": "RoboTwin_lerobot_v30",
  "policy_name": "Pi_05"
}
```

The historical profile name `gpt6_pi05` describes topology, not the model selected
by the runner. `vlm_model` is recorded in the manifest and actual request audit.
For historical batch entry points, `RSI_VLM_MODEL` overrides model selection and
`RSI_VLM_BASE_URL` can override the compatible endpoint. The single smoke's explicit
`vlm_model` takes priority over `RSI_VLM_MODEL`.

## Preflight and run exactly one episode

Load the physical bundle's environment variables first, if its installation
requires them. Use a new output directory outside the checkout:

```bash
CORE=/absolute/path/to/PhyAgentOS-core
SMOKE_CONFIG=/absolute/path/to/local-smoke.json
RUN_ROOT=/absolute/path/to/new-smoke-batch

"$CORE/.venv/bin/python" "$CORE/example/robotwin-rsi-benchmark/smoke.py" \
  --config "$SMOKE_CONFIG" --root "$RUN_ROOT" --check

"$CORE/.venv/bin/python" "$CORE/example/robotwin-rsi-benchmark/smoke.py" \
  --config "$SMOKE_CONFIG" --root "$RUN_ROOT" --run
```

Omitting `--run` performs only static/local preflight. It checks paths, profile,
checkpoint metadata, credentials' presence, ports and reservation tombstones,
without a model request or worker launch. It does not prove CUDA compatibility,
asset completeness or checkpoint loading. `--run` starts real GPU work and model
requests. An existing run root is rejected, including after a crash.

Execution order:

1. Reserve the two ports durably, prepare an isolated HOME and record configuration.
2. Install/freeze the small Core control bundle and start the owned policy process.
3. Resolve the live task ID, verify policy readiness, hold the bridge and start one native Session.
4. At each pending candidate, bind current camera images and previous-boundary
   evidence to a scoped observation. Validate generated JSON and semantics before submission.
5. Allow `student` segments of 1–10 steps or `edit` segments of 1–5 steps.
   A joint edit is an offset added to the candidate's absolute target, not a copy
   of `candidate.delta_from_qpos`. Existing bridge magnitude limits remain in force.
6. Validate the native result, complete the Core trajectory and prove owned executor
   shutdown. Close the owned policy process and release reservations only after cleanup is proven.

Generation repair allows at most four requests for one observation; it is not a
physical-action retry. `model_calls` retains the Core decision-callback count;
`provider_calls` records actual provider requests. `provider_total_tokens` is null
if any request lacks reliable usage. Request failures preserve the attempts already made.

## Results and interpretation

Look under `RUN_ROOT/tasks/<task>/`:

| Artifact | Interpretation |
| --- | --- |
| `SMOKE_RESULT.json` | One valid evaluation; inspect `native_outcome` independently |
| `smoke_CORE_REPORT.json` | Core state, children, AgentTask and verified benchmark report/metrics |
| `smoke_a1/benchmark.json` | Authoritative native success/failure |
| `smoke_a1/REQUEST_AUDIT.jsonl` | Actual model, images/context references, outputs, repair and usage |
| `smoke_a1/GATE_AUDIT.jsonl` | Proposals actually accepted by the bridge |
| `smoke_a1/STOP_PROOF.json` | Owned executor stop evidence |
| `CLEANUP.json` | Policy exit and executor cleanup proof; required before resource reuse |
| `SMOKE_SCENE.json`, `RUNTIME_INITIAL_REFERENCE.json` | Observed smoke scene/initial identity, not historical screening equivalence |

On invalid execution, inspect root-level `SMOKE_ABORT.json`, the Core report and
per-episode `INVALID.json`. Do not count missing/invalid episodes as robot failures.
One successful smoke establishes a working path for that configuration; it is not
an official benchmark score, a matched VLA comparison or evidence of RSI benefit.

## Cancellation and manual reconciliation

Use Ctrl-C/SIGTERM on the foreground runner. Already dispatched blocking native
operations are drained before cleanup; stopping the coroutine alone cannot stop a
native thread. Cleanup may therefore take time. Do not use SIGKILL for routine shutdown.

There is **no automatic crash takeover or replay**. `ADAPTER_OWNER.json` prevents
recreating an adapter in the same run. Persistent `lease_dir/port-<number>/owner.json`
records the original run; a crashed process does not automatically release it.
All cooperating runs must share the same host reservation directory. These are
coordination guards, not OS-level exclusion against unrelated applications.

After a crash, an operator must:

1. Preserve the run, Core SQLite journals, native invocation IDs and owner records.
2. Inspect the original isolated HOME and its Skill/runtime state; query/stop its
   native invocation if available, then stop **that owned Skill**. Never replay a lost start/decision.
3. Verify no live executor associated with that HOME and no owned policy process
   remains. Do not kill shared/external model services or an unverified reused PID.
4. Record the stop evidence and unresolved-result classification in the original run.
   Only after physical shutdown is proved may the operator remove the two matching
   reservation directories and start a new batch. Keep `ADAPTER_OWNER.json` and the old output.

When proof is unavailable, retain the reservation and resolve the original worker;
changing ports or clearing journals is not reconciliation. Historical batch runners
now also require `--lease-dir` or `RSI_WORKER_LEASE_DIR`.

## Optional frozen-experience study and historical code

`experiment.py` retains the task-local none/fixed research protocol. It uses one
development episode, a frozen reflection, paired policy seeds and phase-specific
Core roots. It needs the existing screening/checkpoint references documented in
[the historical runtime notes](robotwin-runtime-20261013.md); the smoke does not.
`deploy_dual.py`, `deploy_shards.py`, `launch*.sh`, `selected_runner.py` and
`experiments/robotwin-eslp/` preserve historical experiments and machine presets.
They are not generic setup instructions or required by `smoke.py`.

Core's optional episodic context is now keyed by immutable `case_key`; task/arm
interpretation stays in the RoboTwin example. General multi-case lesson thresholds
and the existing multi-round framework are unchanged. The private deployment does
not enable online reflection. ESLP now rejects incomplete/infra evidence before
reflection and uses a read-only unresolved-task guard instead of a missing DB-repair script.

## Validation boundary

CPU tests exercise the **real RoboTwinAdapter and Core** with only the native GPU
gateway simulated: successful/failed/invalid outcomes, scoped images, receipts,
shutdown, bounded decisions, provider accounting and restart refusal. Run:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest tests \
  example/robotwin-vla-bridge/tests -q
```

On macOS, use a canonical temporary path such as `TMPDIR=/private/tmp` to avoid
an existing `/var` versus `/private/var` test-fixture path mismatch.
The source branch includes historical real GPU deployment evidence in the runtime
notes. This integration revision has no newly executed GPU smoke when no prepared
environment is available; that limitation is separate from its CPU validation.

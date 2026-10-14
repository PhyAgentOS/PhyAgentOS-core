# PAOS benchmark core + candidate-aware RoboTwin (experimental)

For a new installation, start with the [single-episode deployment guide](../../docs/robotwin-benchmark-deployment.md)
and `smoke.py --config /absolute/local-smoke.json --root /absolute/new-run --check`.
Use `--run` for one real episode without historical references or experience generation.
The batch descriptions below are the retained 2026-10-13 research protocol; they are not the default smoke.
All batch runs now require a shared host `RSI_WORKER_LEASE_DIR` (or `experiment.py --lease-dir`).


Current runnable VLM+VLA branch snapshot and deployment prerequisites:
[2026-10-13 runtime notes](../../docs/robotwin-runtime-20261013.md).
This is not the upcoming pure-VLM+IK implementation or proof of RSI benefit.

Integrates feature/benchmark@cbf956b with candidate-aware bridge@3374092 and
bounded outcome/next-intent supervision. Not a claim of measured RSI benefit.

The real BenchmarkSupervisor/EpisodeRunner, AgentTaskCoordinator, binding validation
and SQLite journal own every episode. PrivateBenchmarkTransport is a host-injected
transport, not MockTransport, not an HTTP listener or public deployment toggle.
Its pinned adapter translates benchmark_contract_v1 to real local Forge gateway,
native RoboTwin Session and VLA bridge. The VLM receives data/original images,
not execution tools. Scoped versions, durable no-resend intents, receipts,
native results, full trajectories and stop reconciliation remain in the core.
Unknown delivery blocks admission: there is no hot takeover or action resend.

An immutable installed control bundle is separate from generated physical runtime
profiles. Each observation includes current three cameras, one pending action and
at most one previous boundary/assessment. No future action chunk or FK/IK is claimed.
Student segments are at most ten steps; edits five. Bridge safety limits stay intact.

Four tasks: place_object_scale, handover_block, press_stapler, turn_switch.
One development episode/task (seed3101), reflection from at most eight selected
decisions, then frozen task-local JSON with provenance/hash. This is episodic
experience, not generalized LessonCandidate: no fabricated two-case support or
weakened generalization threshold. Exact task-matching fixed contexts receive it;
none contexts get an empty list. Actual requests record context, image hashes,
experience digest, model ID, output, finish reason, budget and usage.

Twelve paired seeds2001–2012/task, randomized arm order:96 formal episodes.
Both arms use identical VLA+VLM; no pure-VLA arm, no online update or cross-task
memory. Infra failures are invalid, at most one retry after proven settlement;
robot failures are never retried. The first pilot pair on each host must verify durable
reflection, fresh-session exposure, clean none arm, identical initial observations
and native outcomes before other formal work. This tests frozen reuse, not continuous RSI.

67 uses Ubuntu22.04/A100, unlike screening Ubuntu20.04/RTX3090. Screening scene,
task, initial-state and instruction stay strict. Development records a new immutable
initial numerical identity per task, and reports cross-driver equality with screening.
Every formal arm must exactly match the new identity; paired arms stay on one worker.
Never merge this batch with v4 or interpret it as a cross-hardware VLA comparison.

The v10 dual-host schedule assigns turn_switch and handover_block to67, and
press_stapler and place_object_scale toliuy. Both arms and all seeds of a task
remain on its assigned host. Each host has48 formal episodes and its own
actual none/fixed pilot gate (turn_switch on67; press_stapler onliuy).
Both development workers start concurrently; formal admission waits only for
the verified local pilot, so migration problems cannot block the other host.
No pilot flag is fabricated or copied between hardware runtimes.

JSON mode is not strict server-side schema enforcement. The v10 controller
provides an explicit output template and up to four bounded generations with
precise validation-error feedback and the same original images/candidate.
Missing model judgments are never invented, extra keys and duplicate keys
remain invalid, and no physical proposal is submitted before schema and
semantic checks pass. Requests, responses and repair feedback are audited.
This is generation repair, not physical-action retry or safety relaxation.

Native benchmark stop is episode-boundary-only: a paused episode cannot finish
that boundary by itself. Cleanup requests native stop once, then shuts down
only its owned Skill, checks the runtime stopped state and absence of live
owned executor processes (excluding the separately managed policy group).
STOP_PROOF records the actual native status and teardown evidence; it never
claims a still-stopping native Session completed. Interrupted episodes are
infra-invalid, never robot failures or successful evaluations. An unproven
shutdown has no accepted receipt and still blocks root reconciliation.

deploy_dual.py prepares a new batch from explicit core/base paths; launch_dual.sh
uses the persisted local assignment after GPU and port checks, without stopping
other jobs. RSI_CORE, RSI_BASE, RSI_AGENT_PYTHON, RSI_XPOLICY, RSI_MODEL_PATH and
RSI_RUNTIME_LABEL specify the deployed runtime. monitor.py writes per-host
progress with48 planned episodes; aggregate the two hosts for the96 global total.
Earlier deploy67.py/launch67.sh are retained only as historical single-host tools.
Worker finally stops its own skill and exits its own model process group.

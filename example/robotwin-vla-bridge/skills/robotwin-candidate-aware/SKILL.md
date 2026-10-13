---
name: robotwin-benchmark
description: Run ROBOTWIN evaluation with the installed benchmark and policy Nodes.
---

# ROBOTWIN evaluation

The g05_external profile attaches to an already running, user-configured XPolicyLab G05 websocket server. It owns only the client, never starts or stops that external server. G05 model setup is a prerequisite; an unavailable server must fail startup.

1. Start the selected installed profile and require runtime status `ready`.
2. Query `robotwin.policy.describe`; require `ready=true` and the expected action dimension (14).
3. Query `robotwin.benchmark.describe`. Use its actual suite, task table and limits. Do not infer task IDs from names or invent dataset coverage.
4. Start `robotwin.benchmark.run` as a Session. Default smoke is task_ids=[0], init_state_ids=[0], num_runs=1, seed=0. Use the selected profile's suite. Ask for the desired evaluation scope when not provided.
5. Poll Session status, consume terminal events, then retrieve its result. Report completed_episodes, total_episodes, success_rate, failure_summary and result_file. Check the benchmark-owned result JSON for protocol-specific metrics and seed records.
6. To stop a batch, stop the Session and wait for its terminal result; the current episode finishes first. Do not kill an external model service.
7. A second batch may run after the first reaches a terminal state. Stop the Skill when finished; check the Gateway and owned Node processes are gone.

Treat missing weights, assets, runtime errors and partial/stopped batches as such. Unit tests or a stub policy do not establish model accuracy. A debug dataset/subset score must never be described as a full official benchmark score. For source-vs-official acceptance, align checkpoint hashes, preprocessing, tasks, episode seeds, reset and termination semantics, and preserve raw traces/results.

## Profile gpt6_pi05: GPT-6 supervising pi0.5

pi0.5 (openpi Pi_05, RoboTwin aloha-agilex joint checkpoint) proposes 14-D absolute joint targets
`[fl_joint1..6, fl_joint7(gripper), fr_joint1..6, fr_joint7(gripper)]`; you (GPT-6) supervise them.
`vla.get_status.candidate` exposes the latest observed action and its expected delta from current qpos. Treat it as
an unexecuted candidate only when `candidate.freshness=pending`; otherwise it is historical context.
`vla_bridge` is the only action writer. RoboTwin advances one step per forwarded action, so while the
bridge holds, the simulator is frozen and takes zero sim time. The profile needs an already running
Pi_05 websocket server at `G05_SERVER_URL`; the Skill never starts or stops it.

Tools: `robotwin.benchmark.describe/run`, `robotwin.policy.describe`, `vla.set_mode`, `vla.decide`,
`vla.get_status`, `vision.get_frame` (head camera), `vision.get_frame_left_wrist`,
`vision.get_frame_right_wrist`.

Run contract for one supervised episode:

1. `robotwin.policy.describe` must report `ready=true`, `action_dim=14`.
2. `robotwin.benchmark.describe`: map the protocol task name to its runtime task id by exact name match
   in the returned task table. Never guess an id. Read the task's step limit from the table.
3. `vla.set_mode(mode=stop)` first, then start `robotwin.benchmark.run` as a Session
   (`suite=robotwin`, `task_ids=[id]`, `init_state_ids=[k]`, `num_runs=1`, `seed=0`, `max_steps=<step limit>`).
   Every episode opens at checkpoint C0 (`episode.steps == 0`) and waits for your first decision.
4. At C0 do not look at the cameras (the frames on disk belong to the previous episode); decide
   `vla.decide(mode=student, steps=10)`.
5. At C1/C2: `vla.get_status` (steps, checkpoint reason, `last_joint_positions`, and candidate), look at the head
   camera and, when a grasp or placement is close, the relevant wrist camera; first check whether
   `candidate.freshness=pending`. If so, judge its dominant joint deltas and gripper targets against the visual
   task stage before execution. Then decide:
   - `student` when pi0.5 is making sensible progress;
   - `edit` with explicit `delta_joints` in radians (at most 0.1 rad per joint per segment, 0.3 rad per
     joint per episode) and/or `left_gripper`/`right_gripper` open|closed, only with a concrete visual reason;
   - never leave a checkpoint unanswered. Every decision covers at most 10 native steps.
6. When your decision budget is spent, `vla.set_mode(mode=student)` releases the rest of the episode
   to pi0.5 until RoboTwin's step limit or success; poll `vla.get_status` sparingly, then read the Session
   status/result and stop the Session.
7. Report success, steps, the episode seed from the result file and your decisions. Do not claim success
   that the benchmark result does not show.

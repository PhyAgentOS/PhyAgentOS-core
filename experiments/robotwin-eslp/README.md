# RoboTwin ESLP Attempt

This directory contains an experimental PAOS RSI implementation for RoboTwin.
It is the current **Evidence-Scoped Lesson Pack (ESLP) attempt**, preserved on
the `qinhan-eslp-attempt` branch for review. It is not a production PAOS
framework integration and should not be treated as a final RSI result.

## Purpose

ESLP converts one real failed RoboTwin episode into a bounded context carrier
for the next same-task episode. It keeps three layers:

1. A short operating principle.
2. Evidence extracted mechanically from the session trace and benchmark JSON.
3. A checkpoint action policy with correction and stop conditions.

The carrier is appended to the task skill before `activate_skill`, so the VLM
can use the prior evidence while evaluating the next pi0.5 candidate action.

## Components

- `lesson_materializer.py` extracts checkpoint, visual, reasoning, and native
  PAOS lesson evidence, then renders `eslp.md`, `dialogue.md`, and a manifest.
- `robotwin_evidence.py` parses RoboTwin session records and benchmark facts.
- `rsi_evolve.py`, `robust_analyzer.py`, and `supervision_summary.py` connect
  the benchmark outcome to PAOS experience evolution.
- `episode_scorer.py` records success, decisions, edits, evidence references,
  and the injected carrier hash for post-run analysis.
- `lesson_ab_runner.sh` runs the `none`, `native`, `eslp`, and `dialogue`
  conditions and replays failed conditions once.

## Running

The runner keeps the original server layout as its default, but accepts these
environment overrides:

```bash
export PAOS_RSI_BASE=/path/to/rsi
export LESSON_AB_ROOT=/path/to/output
export ROBOTWIN_PARALLEL_HOME=/path/to/parallel10
export PAOS_PYTHON=/path/to/python
export ROBOTWIN_PROFILE=gpt6_pi05
```

It expects a lane environment file, a task prompt, a source session, and a
benchmark result JSON. A typical invocation is:

```bash
bash experiments/robotwin-eslp/lesson_ab_runner.sh \
  L00 hanging_mug 900 1 /path/to/source-session.jsonl
```

The benchmark result JSON is the authority for success:
`success_rate == 1`. Missing results, provider failures, and infrastructure
failures must not create a lesson or advance a replay round.

## Current status

In the initial two-task RoboTwin pilot, ESLP succeeded on both tasks while the
no-lesson condition succeeded on neither. This is an encouraging engineering
signal, not a causal RSI claim: the sample is too small and the four-condition
experiment still needs the planned larger task set and controlled repeats.

Known limitations of this attempt include task-local orchestration outside the
PAOS core API, dependency on the installed RoboTwin/PAOS runtime, and a
mechanical evidence summary that still needs broader validation.

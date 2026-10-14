---
name: EXPERIMENT_SKILL
description: The configured VLM candidate-aware RoboTwin physical runtime
---

The host PAOS BenchmarkSupervisor owns this runtime through a private adapter.
Only the bridge writes actions. The configured VLM reviews pending pi0.5 actions, current
head/wrist images and one previous boundary before bounded segments.
Student segments execute at most ten native steps; corrections five.
Only official native results define success. Frozen task-local experience is
supplied externally by EpisodeRunner, never by modifying this skill between arms.
Continuous release is prohibited. Legacy profile gpt6_pi05 names topology only;
the actual model ID is recorded in the run manifest.

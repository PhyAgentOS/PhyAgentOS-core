# RoboTwin candidate-aware VLA bridge

This example connects a RoboTwin joint-absolute policy to a PAOS VLM supervisor.
The bridge captures the latest valid Pi0.5 action before it is forwarded, exposes
the candidate through `vla.get_status`, and keeps the candidate distinguishable
from historical context.

## Candidate lifecycle

- `pending`: the action is held at a checkpoint and has not reached the benchmark.
- `historical`: the action was forwarded or the bridge has advanced; it is compact context only.
- episode reset clears the candidate.

The supervisor should inspect `candidate.dominant_joints`, `candidate.gripper_targets`,
the current proprioception, and camera observations before choosing `student`, `edit`,
or `stop`. A candidate edit is clamped to the configured per-segment and cumulative
joint limits instead of being rejected outright.

## Layout

- `src/vla_bridge`: bridge node and deterministic state machine.
- `config/vla_bridge.yaml`: joint-absolute RoboTwin profile with candidate-aware checks.
- `config/gateway.yaml`: gateway tool schema exposing the candidate payload.
- `skills/robotwin-candidate-aware/SKILL.md`: supervisor protocol.
- `tests/test_candidate_lifecycle.py`: dependency-light candidate lifecycle test.

## Run

Install this package in the same PAOS/Dora environment as the RoboTwin nodes:

```bash
uv sync
uv run vla-bridge --config config/vla_bridge.yaml
```

The gateway and RoboTwin policy/benchmark nodes must already be configured for the
same endpoint IDs and action joint order. This example does not include model weights,
benchmark assets, or private server paths.

## Test

The lifecycle test needs the bridge runtime dependencies. From this directory run:

```bash
PYTHONPATH=src python tests/test_candidate_lifecycle.py
```

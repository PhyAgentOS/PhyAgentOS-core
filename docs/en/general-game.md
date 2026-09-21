# General Game

The runtime turns a task into phase goals, bounded Actor rounds and observed execution
receipts. It uses the existing provider, target adapters, SessionRunner, MemoryStore and
SkillRuntimeResult. Game services, benchmarks and task data are supplied separately.

```text
TargetSpec + SessionSpec + GeneralGameSkillRuntime
                       ↓
              preflight → SessionRunner
                       ↓
        Planner → phase → Actor (1–3 rounds)
           ↑               ↓ one ActionSpec per round
           └── receipt ← TargetSessionHandle → game adapter
                       ↓
               SkillRuntimeResult
```

## Run a session

Install Core with `pip install -e .`, start the target's game bridge, and provide:

| File | Contents |
|:-----|:---------|
| `target.yaml` | A Core TargetSpec with its registered runtime, adapter and runtime contract. Include `general_game` in `supported_skillruntimes`. |
| `session.yaml` | A SessionSpec referencing the target and `general_game`, with task description, step limit and execution timeout. |
| `actions.json` | A nonempty map of permitted primitive action types to descriptions and parameter guidance. |
| `success.json` | Observed completion conditions; all must hold. Required for native Stardew Valley and Minecraft targets. |

```yaml
session_id: game-session
target_ref: configured_game
skillruntime_ref: general_game
task_description: Complete the configured task
execution:
  max_steps: 100
timeouts:
  execute_timeout_s: 300
```

```bash
paos general-game --workspace ./workspace --target ./target.yaml --session ./session.yaml \
  --actions ./actions.json --success-checks ./success.json --model YOUR_MODEL \
  --api-base http://localhost:8000/v1
```

Set `GAME_AGENT_API_KEY` when the endpoint requires authentication. The command loads the
packaged `templates/configs/skillruntimes/general_game.yaml`, performs preflight, and prints
the Core SessionResult. A failed task returns exit code 1. It does not start a game server.

Completion conditions use dotted observation paths and equality, for example
`{"stardew.position": [1, 0]}`. A strict numeric upper bound uses
`{"stardew.time": {"$lt": 1700}}`; missing or nonnumeric values fail the check.
A model's `finish` request alone never proves success.

## Python registration

```python
from PhyAgentOS.game_agents.stardew import register_general_game

register_general_game(
    provider_factory,
    model="YOUR_MODEL",
    action_catalog={"move": {"params": {"dx": "integer", "dy": "integer"}}},
    verify=lambda observation, feedback: observation["stardew"]["position"] == [1, 0],
)
```

`provider_factory` returns a fresh LLMProvider or an async context manager owning one.
Registration creates fresh runtime state per session. Continue through the normal
preflight, SkillRuntimeRegistry and SessionRunner; native target names are unchanged.
Use a Python verifier for tasks that need more than simple observation comparisons.

## Execution and memory

Each Actor round dispatches one primitive. Session cancellation, elapsed time, step limits,
planning limits and repeated lack of progress stop the loop. Receipts contain before/after
observations and actual target feedback. A fatal bridge response terminates the session.

Planner and Actor read separate MemoryStore snapshots under
`workspace/game_agent/{planner,actor}/memory/`. With `--evolve`, completed execution can
produce evidence-linked candidates in HISTORY.md; candidates stay unverified and are not
automatically promoted to MEMORY.md. Consolidation failure does not rewrite the task result.

## Validation

Run `python -m pytest tests/game_agents/stardew`. Tests exercise the real Core session, adapters,
provider API, CLI and result writer, with simulated external game/model HTTP services.
Live gameplay requires a running game bridge and model endpoint. The loop currently consumes
structured observations rather than raw image input.

# Stardew Planner–Actor

This module hosts the existing Planner–Actor workflow developed for Stardew Valley.
It retains the `GeneralGameSkillRuntime` API and can use compatible Core game targets.

## Execution

Planner selects a phase goal. Actor executes one permitted primitive per round, for up to
three rounds, and returns observed feedback. SessionRunner and TargetSessionHandle own the
target lifecycle; step limits, deadlines, cancellation and no-progress limits bound execution.

Task completion is determined by observations or target feedback. A model finish request
alone does not prove success. Planner and Actor read separate frozen MemoryStore snapshots;
optional consolidation records unverified candidates after the episode.

## CLI

```bash
paos general-game --workspace ./workspace --target ./target.yaml --session ./session.yaml \
  --actions ./actions.json --success-checks ./success.json --model YOUR_MODEL \
  --api-base http://localhost:8000/v1
```

Start the game bridge separately. Provide Core TargetSpec/SessionSpec configuration and an
action catalog. Set `GAME_AGENT_API_KEY` if the model endpoint requires authentication.

## Python

```python
from PhyAgentOS.game_agents.stardew import GeneralGameSkillRuntime, register_general_game
```

See the [English guide](../../../docs/en/general-game.md) or
[中文接入指南](../../../docs/zh/general-game.md) for configuration and registration.
Run `python -m pytest tests/game_agents/stardew` for software integration tests.

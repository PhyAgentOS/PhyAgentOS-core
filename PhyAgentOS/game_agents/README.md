# Game Agents

Independent game-agent workflows within the Core package.

```text
game_agents/
├── stardew/     # Planner–Actor runtime, decisions, receipts and role memory
└── minecraft/   # Skill Graph, evidence store, warm-up and benchmark runner
```

| Module | Command | Documentation |
|:-------|:--------|:--------------|
| Stardew | `paos general-game` | [Module](stardew/README.md), [English](../../docs/en/general-game.md), [中文](../../docs/zh/general-game.md) |
| Minecraft | `paos minecraft warmup` / `benchmark` | [Module](minecraft/README.md), [Guide](../../docs/scenarios/game/minecraft/4_benchmark.md) |

The modules do not import each other. Stardew keeps its existing `GeneralGameSkillRuntime`
and registration API; Minecraft keeps its `AgentFn`, `WorldAdapter` and graph APIs.
Game-specific target clients and bridges remain in their existing Core locations.

Model-generated Stardew memory candidates remain unverified. Minecraft graph claims follow
its documented single-observation verification policy. These policies are independent.

Run `python -m pytest tests/game_agents` to exercise both modules without a live game server.

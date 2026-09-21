# Minecraft Skill Graph v1

This package ports the evidence-backed Skill Graph v1 protocol to the
Mineflayer tech-tree benchmark. It intentionally excludes MineStudio,
STEVE-1 log import, curriculum generation, and automatic task exploration.

## Protocol

- Warm-up always runs W01-W07 once in manifest order, with an isolated reset
  for every case.
- The backend does not expose seed control. Every artifact records
  `backend_seed_control=false`; claim verification never depends on seed
  replay.
- One non-confounded observation promotes its success or failure claim to
  `verified`; no seed replay or second validation task is scheduled.
- `warmup_frozen/` is content-hashed and read-only. `benchmark_graph/` is a
  derived writable copy.
- A benchmark episode is committed to SQLite and its JSON artifact is synced
  before the next episode starts. No exploration task is generated.

## CLI

```bash
paos minecraft warmup \
  --url http://127.0.0.1:3001 \
  --output-dir outputs/minecraft-skill-graph

paos minecraft benchmark \
  --url http://127.0.0.1:3001 \
  --graph-dir outputs/minecraft-skill-graph/benchmark_graph \
  --output-dir outputs/minecraft-benchmark \
  --tasks wooden.obtain_oak_log \
  --trials 1 \
  --run-id smoke-001
```

Use `--all` instead of `--tasks` to run the full 40-task manifest. The bundled
executor is a deterministic Mineflayer baseline; callers may inject any
`AgentFn` through the Python API. If `--run-id` is omitted, a unique ID is
generated; results are stored below `<output-dir>/<run-id>/` so repeated runs
never reuse logical trial IDs or overwrite earlier evidence.

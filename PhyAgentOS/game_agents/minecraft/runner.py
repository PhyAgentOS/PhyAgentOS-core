"""Fixed warm-up and synchronous benchmark experience accumulation."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from PhyAgentOS.benchmarks.minecraft.techtree.harness import (
    AgentFn,
    BenchmarkResult,
    WorldAdapter,
    run_task_spec,
)
from PhyAgentOS.benchmarks.minecraft.techtree.loader import (
    DEFAULT_MANIFEST_PATH,
    list_tasks,
    load_task,
)
from PhyAgentOS.benchmarks.minecraft.techtree.schema import TaskManifest, TechTreeTask

from .model import RuntimeFingerprint, canonical_hash
from .store import GraphStore, clone_frozen_graph, freeze_graph, sync_mutable_graph

WARMUP_MANIFEST_PATH = Path(__file__).with_name("warmup_manifest.json")
AgentFactory = Callable[[TechTreeTask, WorldAdapter], Any]


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def default_runtime_fingerprint() -> RuntimeFingerprint:
    manifest_hash = canonical_hash(_json(DEFAULT_MANIFEST_PATH), "manifest")
    return RuntimeFingerprint(
        {
            "skill_graph_protocol": 1,
            "minecraft_version": "1.20.4",
            "backend": "mineflayer_http",
            "backend_seed_control": False,
            "claim_verification_policy": "single_observation",
            "benchmark_manifest_hash": manifest_hash,
        }
    )


def load_warmup_tasks(
    path: str | Path = WARMUP_MANIFEST_PATH,
) -> tuple[list[TechTreeTask], list[str]]:
    data = _json(Path(path))
    order = list(data.get("case_order") or [])
    trials = list(data.get("trials") or [])
    tasks = list(
        TaskManifest.from_dict(
            {
                "version": "skill_graph_warmup_v1",
                "name": "skill_graph_warmup",
                "tasks": data.get("tasks", []),
            }
        ).tasks
    )
    if [task.id for task in tasks] != order or order != [f"W{i:02d}" for i in range(1, 8)]:
        raise ValueError("warm-up must contain W01-W07 in fixed order")
    if trials != ["trial-01"]:
        raise ValueError("warm-up must use exactly one logical trial per case")
    benchmark_targets = {task.target_item for task in list_tasks()}
    overlap = sorted(benchmark_targets & {task.target_item for task in tasks})
    if overlap:
        raise ValueError(f"warm-up final targets overlap benchmark targets: {overlap}")
    return tasks, trials


def _table_or_furnace_setup(task: TechTreeTask, block: str) -> list[dict[str, Any]]:
    x, y, z = task.setup.arena.origin
    return [
        {"type": "equip", "params": {"item": block}},
        {"type": "place", "params": {"x": x + 2, "y": y - 1, "z": z, "face": 1}},
    ]


def scripted_actions(task: TechTreeTask) -> list[dict[str, Any]]:
    """A deterministic Mineflayer baseline; it does not explore or call an LLM."""

    setup_items = [item.item for item in task.setup.inventory]
    if task.family == "dig_pickup":
        tools = [item for item in setup_items if item.endswith(("_axe", "_pickaxe", "_shovel"))]
        actions = [{"type": "equip", "params": {"item": tools[0]}}] if tools else []
        block = task.setup.blocks[0].block
        return actions + [
            {"type": "collect", "params": {"block_type": block, "count": 1, "max_distance": 8}}
        ]
    if task.family == "smelting":
        inputs = {"iron_ingot": "raw_iron", "gold_ingot": "raw_gold", "baked_potato": "potato"}
        actions = _table_or_furnace_setup(task, "furnace")
        return actions + [
            {
                "type": "smelt",
                "params": {
                    "input": inputs[task.target_item],
                    "fuel": "coal",
                    "count": task.success_criterion.count,
                },
            }
        ]
    actions = (
        _table_or_furnace_setup(task, "crafting_table") if task.family == "crafting_table" else []
    )
    return actions + [
        {
            "type": "craft",
            "params": {"recipe_id": task.target_item, "count": task.success_criterion.count},
        }
    ]


def build_scripted_agent(task: TechTreeTask, world: WorldAdapter) -> dict[str, Any]:
    actions = scripted_actions(task)
    execute = getattr(world, "execute_action", None)
    if not callable(execute):
        raise TypeError("scripted benchmark requires a world adapter with execute_action")
    results = []
    for action in actions:
        response = execute(action["type"], action.get("params", {}))
        results.append({"action": action, "response": response})
        if isinstance(response, Mapping) and response.get("ok") is False:
            break
    return {"executor": "scripted_baseline", "actions": actions, "results": results}


def _result_record(result: BenchmarkResult, *, trial_id: str) -> dict[str, Any]:
    value = result.to_dict()
    value["trial_id"] = trial_id
    value["backend_seed_control"] = False
    return value


def _benchmark_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{uuid.uuid4().hex[:8]}"


def _graph_agent(agent_fn: AgentFn, store: GraphStore) -> AgentFn:
    def run(task: TechTreeTask, world: WorldAdapter) -> Any:
        context, claim_ids = store.retrieve(task.title, world.observe())
        enriched = replace(
            task,
            raw={
                **task.raw,
                "skill_graph_context": context,
                "skill_graph_claim_ids": claim_ids,
            },
        )
        result = agent_fn(enriched, world)
        graph = {"context": context, "claim_ids": claim_ids}
        if isinstance(result, Mapping):
            return {**dict(result), "skill_graph": graph}
        return {"result": result, "skill_graph": graph}

    return run


def run_warmup(
    world_adapter: WorldAdapter,
    output_dir: str | Path,
    *,
    agent_fn: AgentFn = build_scripted_agent,
    runtime: RuntimeFingerprint | None = None,
    manifest_path: str | Path = WARMUP_MANIFEST_PATH,
) -> dict[str, Any]:
    """Run W01-W07 once, freeze the result, and derive a writable copy."""

    runtime = runtime or default_runtime_fingerprint()
    output = Path(output_dir).resolve()
    frozen = output / "warmup_frozen"
    mutable = output / "benchmark_graph"
    recovery = output / "warmup_working"
    if frozen.exists() or mutable.exists() or recovery.exists():
        raise FileExistsError("skill graph output already exists; choose an empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    working = output / f".warmup-{os.getpid()}"
    working.mkdir()
    store = GraphStore(working / "graph.sqlite")
    store.set_metadata("runtime", runtime.to_dict())
    records: list[dict[str, Any]] = []
    try:
        tasks, trials = load_warmup_tasks(manifest_path)
        for task in tasks:
            for trial_id in trials:
                logical_trial = f"{task.id}:{trial_id}"
                result = run_task_spec(
                    task,
                    _graph_agent(agent_fn, store),
                    world_adapter,
                    metadata={"phase": "warmup"},
                )
                store.record_episode(
                    task,
                    result,
                    trial_id=logical_trial,
                    source="warmup",
                    runtime=runtime,
                )
                records.append(_result_record(result, trial_id=logical_trial))
                if result.error:
                    raise RuntimeError(f"warm-up {task.id}/{trial_id} errored: {result.error}")
        _atomic_json(working / "warmup_results.json", records)
        manifest = freeze_graph(
            store,
            working,
            runtime,
            extra={
                "origin": "fixed_warmup",
                "course": "W01-W07",
                "trials_per_case": 1,
                "claim_verification_policy": "single_observation",
                "complete": True,
            },
        )
        os.replace(working, frozen)
        mutable_store = clone_frozen_graph(frozen, mutable)
        sync_mutable_graph(
            mutable_store,
            mutable,
            runtime,
            extra={"origin": "benchmark_online", "base_frozen_graph_hash": manifest["graph_hash"]},
        )
        mutable_store.close()
        return {
            "frozen_dir": str(frozen),
            "mutable_dir": str(mutable),
            "manifest": manifest,
            "episodes": len(records),
        }
    except BaseException:
        try:
            store.close()
        except Exception:
            pass
        if working.exists() and not recovery.exists():
            os.replace(working, recovery)
        raise


def run_benchmark_tasks(
    task_ids: Iterable[str],
    agent_fn: AgentFn,
    world_adapter: WorldAdapter,
    *,
    graph_dir: str | Path,
    results_dir: str | Path,
    trials: int = 1,
    runtime: RuntimeFingerprint | None = None,
    manifest_path: str | Path | None = None,
    run_id: str | None = None,
) -> list[BenchmarkResult]:
    """Run tasks serially and synchronously settle each episode into the mutable graph."""

    if trials < 1:
        raise ValueError("trials must be >= 1")
    run_id = run_id or _benchmark_run_id()
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be one path-safe component")
    runtime = runtime or default_runtime_fingerprint()
    graph_path = Path(graph_dir).resolve()
    if (
        not (graph_path / "graph.sqlite").is_file()
        or not (graph_path / "graph_manifest.json").is_file()
    ):
        raise FileNotFoundError("benchmark graph is missing; run `paos minecraft warmup` first")
    store = GraphStore(graph_path / "graph.sqlite")
    stored_runtime = store.get_metadata("runtime")
    if stored_runtime and stored_runtime.get("hash") != runtime.hash:
        store.close()
        raise RuntimeError("mutable skill graph runtime scope mismatch")
    if not store.get_metadata("base_frozen_graph_hash"):
        store.close()
        raise RuntimeError("benchmark graph is not derived from a frozen warm-up graph")
    output = Path(results_dir).resolve()
    batch_output = output / run_id
    results: list[BenchmarkResult] = []
    records: list[dict[str, Any]] = []
    try:
        for task_id in task_ids:
            task = load_task(task_id, manifest_path)
            for index in range(1, trials + 1):
                trial_id = f"{run_id}:{task.id}:trial-{index:02d}"
                result = run_task_spec(
                    task,
                    _graph_agent(agent_fn, store),
                    world_adapter,
                    metadata={"phase": "benchmark", "run_id": run_id, "trial_id": trial_id},
                )
                store.record_episode(
                    task, result, trial_id=trial_id, source="benchmark", runtime=runtime
                )
                sync_mutable_graph(
                    store,
                    graph_path,
                    runtime,
                    extra={
                        "origin": "benchmark_online",
                        "base_frozen_graph_hash": store.get_metadata("base_frozen_graph_hash"),
                    },
                )
                record = _result_record(result, trial_id=trial_id)
                _atomic_json(batch_output / task.id / f"trial-{index:02d}.json", record)
                records.append(record)
                results.append(result)
    finally:
        store.close()
    _atomic_json(batch_output / "summary.json", records)
    return results

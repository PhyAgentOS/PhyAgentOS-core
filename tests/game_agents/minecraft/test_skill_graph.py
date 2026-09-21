from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from PhyAgentOS.benchmarks.minecraft.techtree import list_tasks
from PhyAgentOS.benchmarks.minecraft.techtree.schema import WorldSetup
from PhyAgentOS.game_agents.minecraft import (
    GraphStore,
    RuntimeFingerprint,
    canonical_hash,
    load_frozen_graph,
    run_benchmark_tasks,
    run_warmup,
)
from PhyAgentOS.game_agents.minecraft.runner import (
    build_scripted_agent,
    load_warmup_tasks,
)

RUNTIME = RuntimeFingerprint(
    {
        "skill_graph_protocol": 1,
        "minecraft_version": "1.20.4",
        "backend": "mineflayer_http",
        "backend_seed_control": False,
        "claim_verification_policy": "single_observation",
        "fixture": True,
    }
)


class FakeWorld:
    def __init__(self, *, fail_reset: bool = False) -> None:
        self.inventory: dict[str, int] = {}
        self.fail_reset = fail_reset
        self.resets = 0

    def reset(self, setup: WorldSetup) -> dict[str, Any]:
        self.resets += 1
        if self.fail_reset:
            raise RuntimeError("reset unavailable")
        self.inventory = {item.item: item.count for item in setup.inventory}
        return self.observe()

    def observe(self) -> dict[str, Any]:
        return {
            "inventory_items": [
                {"name": name, "count": count} for name, count in self.inventory.items()
            ]
        }

    def execute_action(self, action_type: str, params: dict[str, Any]) -> dict[str, Any]:
        if action_type == "collect":
            drops = {
                "stone": "cobblestone",
                "iron_ore": "raw_iron",
                "gold_ore": "raw_gold",
                "redstone_ore": "redstone",
            }
            item = drops.get(params["block_type"], params["block_type"])
            self.inventory[item] = self.inventory.get(item, 0) + int(params.get("count", 1))
        elif action_type == "craft":
            item = params["recipe_id"]
            self.inventory[item] = self.inventory.get(item, 0) + int(params.get("count", 1))
        elif action_type == "smelt":
            outputs = {"raw_iron": "iron_ingot", "raw_gold": "gold_ingot", "potato": "baked_potato"}
            item = outputs[params["input"]]
            self.inventory[item] = self.inventory.get(item, 0) + int(params.get("count", 1))
        return {"ok": True}


def test_warmup_manifest_is_fixed_single_trial_and_disjoint() -> None:
    tasks, trials = load_warmup_tasks()
    assert [task.id for task in tasks] == [f"W{i:02d}" for i in range(1, 8)]
    assert trials == ["trial-01"]
    assert not ({task.target_item for task in tasks} & {task.target_item for task in list_tasks()})


def test_warmup_freezes_and_derives_mutable_graph(tmp_path: Path) -> None:
    world = FakeWorld()
    output = run_warmup(world, tmp_path / "graph", runtime=RUNTIME)

    frozen = Path(output["frozen_dir"])
    mutable = Path(output["mutable_dir"])
    assert world.resets == 7
    assert output["episodes"] == 7
    assert not frozen.stat().st_mode & stat.S_IWUSR
    assert not (frozen / "graph.sqlite").stat().st_mode & stat.S_IWUSR
    assert (frozen / "warmup_results.json").is_file()

    readonly, manifest = load_frozen_graph(frozen, RUNTIME)
    assert manifest["counts"]["evidence"] == 7
    assert manifest["counts"]["claim_statuses"] == {"verified": 7}
    with pytest.raises(RuntimeError, match="read-only"):
        readonly.set_metadata("x", 1)
    readonly.close()

    working = GraphStore(mutable / "graph.sqlite")
    assert working.get_metadata("base_frozen_graph_hash") == manifest["graph_hash"]
    working.set_metadata("writable", True)
    working.close()


def test_benchmark_synchronously_accumulates_without_touching_frozen_copy(tmp_path: Path) -> None:
    world = FakeWorld()
    output = run_warmup(world, tmp_path / "graph", runtime=RUNTIME)
    frozen_manifest_before = json.loads(
        (Path(output["frozen_dir"]) / "graph_manifest.json").read_text()
    )

    results = run_benchmark_tasks(
        ["wooden.obtain_oak_log"],
        build_scripted_agent,
        world,
        graph_dir=output["mutable_dir"],
        results_dir=tmp_path / "results",
        trials=2,
        runtime=RUNTIME,
        run_id="test-run",
    )

    assert len(results) == 2 and all(result.success for result in results)
    mutable_manifest = json.loads((Path(output["mutable_dir"]) / "graph_manifest.json").read_text())
    assert mutable_manifest["counts"]["evidence"] == 9
    assert mutable_manifest["counts"]["claim_statuses"]["verified"] == 8
    assert (
        json.loads((Path(output["frozen_dir"]) / "graph_manifest.json").read_text())
        == frozen_manifest_before
    )
    assert (tmp_path / "results" / "test-run" / "wooden.obtain_oak_log" / "trial-02.json").is_file()
    assert not list((tmp_path / "graph").rglob("*explor*"))


def test_warmup_error_keeps_recoverable_working_graph(tmp_path: Path) -> None:
    output = tmp_path / "graph"
    with pytest.raises(RuntimeError, match="reset unavailable"):
        run_warmup(FakeWorld(fail_reset=True), output, runtime=RUNTIME)
    assert (output / "warmup_working" / "graph.sqlite").is_file()
    assert not (output / "warmup_frozen").exists()


def test_benchmark_runs_use_distinct_logical_trials(tmp_path: Path) -> None:
    world = FakeWorld()
    output = run_warmup(world, tmp_path / "graph", runtime=RUNTIME)
    for run_id in ("batch-a", "batch-b"):
        run_benchmark_tasks(
            ["wooden.obtain_oak_log"],
            build_scripted_agent,
            world,
            graph_dir=output["mutable_dir"],
            results_dir=tmp_path / "results",
            runtime=RUNTIME,
            run_id=run_id,
        )
        current = GraphStore(Path(output["mutable_dir"]) / "graph.sqlite")
        assert current.counts()["claim_statuses"] == {"verified": 8}
        current.close()
    mutable = GraphStore(Path(output["mutable_dir"]) / "graph.sqlite")
    assert mutable.counts()["evidence"] == 9
    mutable.close()
    assert (tmp_path / "results" / "batch-a" / "summary.json").is_file()
    assert (tmp_path / "results" / "batch-b" / "summary.json").is_file()


def test_canonical_hash_is_order_independent() -> None:
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})

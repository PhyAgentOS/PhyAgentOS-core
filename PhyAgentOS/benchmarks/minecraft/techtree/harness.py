"""Executor-independent harness for Minecraft tech-tree tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from PhyAgentOS.benchmarks.minecraft.techtree.evaluator import EvaluationResult, evaluate_task
from PhyAgentOS.benchmarks.minecraft.techtree.loader import load_task
from PhyAgentOS.benchmarks.minecraft.techtree.schema import TechTreeTask, WorldSetup


class WorldAdapter(Protocol):
    """Minimal world interface required by the benchmark core."""

    def reset(self, setup: WorldSetup) -> Mapping[str, Any]:
        ...

    def observe(self) -> Mapping[str, Any]:
        ...


AgentFn = Callable[[TechTreeTask, WorldAdapter], Any]


@dataclass(frozen=True)
class BenchmarkResult:
    task_id: str
    started_at: str
    finished_at: str
    success: bool
    reward: float
    verdict: EvaluationResult
    initial_observation: Mapping[str, Any] | None = None
    final_observation: Mapping[str, Any] | None = None
    agent_result: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "success": self.success,
            "reward": self.reward,
            "verdict": self.verdict.to_dict(),
            "initial_observation": self.initial_observation,
            "final_observation": self.final_observation,
            "agent_result": self.agent_result,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


def run_task(
    task_id: str,
    agent_fn: AgentFn,
    world_adapter: WorldAdapter,
    *,
    manifest_path: str | Path | None = None,
) -> BenchmarkResult:
    """Run one task with an injected agent and world adapter.

    The benchmark core does not know how the agent acts.  It only sets up the
    world through ``world_adapter``, gives the structured task to ``agent_fn``,
    observes the final state, and scores that state with deterministic code.
    """

    task = load_task(task_id, manifest_path)
    return run_task_spec(task, agent_fn, world_adapter)


def run_task_spec(
    task: TechTreeTask,
    agent_fn: AgentFn,
    world_adapter: WorldAdapter,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> BenchmarkResult:
    """Run an already loaded task.

    ``run_task`` remains the stable manifest-backed API.  This companion is
    used by the fixed warm-up curriculum, whose targets deliberately do not
    appear in the benchmark manifest.
    """

    started = _utc_now()
    initial_observation: Mapping[str, Any] | None = None
    final_observation: Mapping[str, Any] | None = None
    agent_result: Any = None
    error: str | None = None

    try:
        initial_observation = world_adapter.reset(task.setup)
        agent_result = agent_fn(task, world_adapter)
    except Exception as exc:  # pragma: no cover - exercised by harness users
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            final_observation = world_adapter.observe()
        except Exception as exc:  # pragma: no cover - exercised by harness users
            if error is None:
                error = f"{type(exc).__name__}: {exc}"

    verdict = evaluate_task(task, final_observation or {})
    if error and not verdict.success:
        verdict = EvaluationResult(
            success=False,
            reward=0.0,
            reason="agent_or_world_error",
            metrics={**verdict.metrics, "error": error, "original_reason": verdict.reason},
        )

    return BenchmarkResult(
        task_id=task.id,
        started_at=started,
        finished_at=_utc_now(),
        success=verdict.success,
        reward=verdict.reward,
        verdict=verdict,
        initial_observation=initial_observation,
        final_observation=final_observation,
        agent_result=agent_result,
        error=error,
        metadata={
            "benchmark": "minecraft_techtree",
            "tier": task.tier,
            "family": task.family,
            **dict(metadata or {}),
        },
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

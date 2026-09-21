"""General game builtin skill runtime and registry integration."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PhyAgentOS.game_agents.stardew.agent import StructuredAgent
from PhyAgentOS.game_agents.stardew.loop import GameLoop, TaskVerifier
from PhyAgentOS.game_agents.stardew.memory import GameMemory
from PhyAgentOS.game_agents.stardew.models import MemoryUpdate
from PhyAgentOS.providers.base import LLMProvider
from PhyAgentOS.runtime.schemas import AdapterPlan
from PhyAgentOS.runtime.sessions.models import SkillContext, SkillRuntimeResult
from PhyAgentOS.runtime.sessions.target_session_handle import TargetSessionHandle
from PhyAgentOS.runtime.skillruntime.builtin.base import BuiltinSkillRuntime
from PhyAgentOS.runtime.watchdog.runtime_registry import register_skill_runtime

ProviderFactory = Callable[[], LLMProvider | AbstractAsyncContextManager[LLMProvider]]


class GeneralGameSkillRuntime(BuiltinSkillRuntime):
    """Bind the Planner/Actor loop to the Core session lifecycle."""

    def __init__(
        self,
        provider_factory: ProviderFactory,
        *,
        model: str,
        action_catalog: dict[str, Any],
        memory: GameMemory | None = None,
        evolve: bool = False,
        verify: TaskVerifier | None = None,
        call_timeout_s: float = 120,
        max_loops: int = 100,
        max_no_progress: int = 5,
    ) -> None:
        super().__init__()
        if not callable(provider_factory):
            raise TypeError("provider_factory must create a provider per session")
        if not action_catalog or min(call_timeout_s, max_loops, max_no_progress) <= 0:
            raise ValueError("an action catalog and positive runtime limits are required")
        self.provider_factory = provider_factory
        self.model = model
        self.action_catalog = action_catalog
        self.memory = memory
        self.evolve = evolve
        self.verify = verify
        self.call_timeout_s = call_timeout_s
        self.max_loops = max_loops
        self.max_no_progress = max_no_progress
        self._cancelled = threading.Event()
        self._status = "idle"
        self._loop: GameLoop | None = None

    def start(self, skill_ctx: SkillContext) -> None:
        self._cancelled.clear()
        self._status = "running"
        self._loop = None

    def cancel(self, skill_ctx: SkillContext | None = None, reason: str = "interrupted") -> None:
        self._cancelled.set()

    def snapshot(self, skill_ctx: SkillContext | None = None) -> dict[str, Any]:
        return {
            "status": "cancelled" if self._cancelled.is_set() else self._status,
            "steps": self._loop.steps if self._loop else 0,
        }

    def run_builtin_loop(
        self,
        skill_ctx: SkillContext,
        target_handle: TargetSessionHandle,
        adapter_plan: AdapterPlan,
    ) -> SkillRuntimeResult:
        return asyncio.run(self._run(skill_ctx, target_handle))

    async def _run(self, ctx: SkillContext, handle: TargetSessionHandle) -> SkillRuntimeResult:
        async with AsyncExitStack() as stack:
            source = self.provider_factory()
            provider = (
                source
                if isinstance(source, LLMProvider)
                else await stack.enter_async_context(source)
            )
            return await self._run_session(ctx, handle, provider)

    async def _run_session(
        self,
        ctx: SkillContext,
        handle: TargetSessionHandle,
        provider: LLMProvider,
    ) -> SkillRuntimeResult:
        agent = StructuredAgent(provider, self.model, call_timeout_s=self.call_timeout_s)
        memory = self.memory.snapshot() if self.memory else {}
        started = handle.session_state.started_at_ns or time.time_ns()
        remaining = ctx.session.timeouts.execute_timeout_s - (time.time_ns() - started) / 1e9
        if remaining <= 0:
            self._status = "timed_out"
            return SkillRuntimeResult(
                status="timed_out",
                success=False,
                error_code="execute_timeout",
                final_status={"target_step_index": handle.session_state.step_index},
            )
        loop = GameLoop(
            agent,
            task=ctx.task_description,
            actions=self.action_catalog,
            memory=memory,
            max_steps=ctx.session.execution.max_steps,
            timeout_s=remaining,
            cancelled=lambda: self._cancelled.is_set() or handle.session_state.cancelled,
            max_loops=self.max_loops,
            max_no_progress=self.max_no_progress,
        )
        self._loop = loop
        result = await loop.run(handle, self.verify)
        warnings = []
        if self.evolve and result.status in {"succeeded", "failed"} and loop.receipts:
            try:
                result.artifacts["memory_candidates"] = await self._consolidate(ctx, loop, result)
            except (Exception, asyncio.CancelledError) as error:
                # Consolidation cannot rewrite an already observed execution outcome.
                warnings.append(f"memory_update_skipped:{type(error).__name__}")
        self._status = result.status
        result.metadata.update(
            {
                "model_calls": agent.calls,
                "model_usage": agent.usage,
                "memory_mode": "evolve" if self.evolve else "frozen",
                "warnings": warnings,
            }
        )
        return result

    async def _consolidate(
        self,
        ctx: SkillContext,
        loop: GameLoop,
        result: SkillRuntimeResult,
    ) -> list[dict[str, Any]]:
        candidates = []
        round_ids = {item.id for receipt in loop.receipts for item in receipt.rounds}
        # Validate both scopes before recording either one.
        for role in ("planner", "actor"):
            evidence = [
                receipt.planner_view() if role == "planner" else asdict(receipt)
                for receipt in loop.receipts
            ]
            update = await loop.agent.request(
                "consolidator",
                {
                    "role": role,
                    "task": ctx.task_description,
                    "outcome": {
                        "status": result.status,
                        "reason": result.metadata["termination_reason"],
                    },
                    "memory": loop.memory.get(role, ""),
                    "evidence": evidence,
                },
                MemoryUpdate,
                check=loop.check,
                remaining_s=loop.remaining_s,
            )
            for candidate in update.candidates:
                if candidate.role != role or not set(candidate.evidence) <= round_ids:
                    raise ValueError("memory candidate has invalid scope or evidence")
            candidates.extend(update.candidates)
        loop.check()
        if self.memory:
            for role in ("planner", "actor"):
                self.memory.record(
                    ctx.session.session_id, role, [item for item in candidates if item.role == role]
                )
        return [item.model_dump() for item in candidates]


def register_general_game(
    provider_factory: ProviderFactory,
    *,
    model: str,
    action_catalog: dict[str, Any],
    memory_workspace: Path | None = None,
    **runtime_options: Any,
) -> None:
    """Register a factory with fresh per-session state and the existing provider."""
    memory = GameMemory(memory_workspace) if memory_workspace is not None else None
    register_skill_runtime(
        "GeneralGameSkillRuntime",
        lambda: GeneralGameSkillRuntime(
            provider_factory,
            model=model,
            action_catalog=action_catalog,
            memory=memory,
            **runtime_options,
        ),
    )

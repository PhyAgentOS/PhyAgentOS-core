"""Planner → bounded Actor loop → observed receipt → Planner."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from PhyAgentOS.game_agents.stardew.agent import Output, StructuredAgent
from PhyAgentOS.game_agents.stardew.models import (
    ActorDecision,
    LoopReceipt,
    Phase,
    PlannerDecision,
    RoundReceipt,
)
from PhyAgentOS.runtime.sessions.models import SkillRuntimeResult
from PhyAgentOS.runtime.sessions.target_session_handle import TargetSessionHandle

TaskVerifier = Callable[[dict[str, Any], dict[str, Any]], bool]


class LoopStoppedError(Exception):
    def __init__(self, status: str, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(reason)


class GameLoop:
    """Run bounded planning rounds through Core's public session handle."""

    def __init__(
        self,
        agent: StructuredAgent,
        *,
        task: str,
        actions: dict[str, Any],
        memory: dict[str, str],
        max_steps: int,
        timeout_s: float,
        cancelled: Callable[[], bool],
        max_loops: int = 100,
        max_no_progress: int = 5,
    ) -> None:
        if min(max_steps, max_loops, max_no_progress) < 1 or timeout_s <= 0:
            raise ValueError("loop limits must be positive")
        if not actions:
            raise ValueError("an explicit action catalog is required")
        self.agent = agent
        self.task = task
        self.actions = actions
        self.memory = memory
        self.max_steps = max_steps
        self.max_loops = max_loops
        self.max_no_progress = max_no_progress
        self.deadline = time.monotonic() + timeout_s
        self.cancelled = cancelled
        self.steps = 0
        self.receipts: list[LoopReceipt] = []
        self.last_feedback: dict[str, Any] = {}
        self._last_status: dict[str, Any] = {}
        self._observation: dict[str, Any] = {}

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        if self.cancelled():
            raise LoopStoppedError("cancelled", "cancelled")
        if self.remaining_s() <= 0:
            raise LoopStoppedError("timed_out", "execute_timeout")

    async def run(
        self,
        handle: TargetSessionHandle,
        verify: TaskVerifier | None = None,
    ) -> SkillRuntimeResult:
        self._handle = handle
        self._verify = verify
        status, reason = "failed", "loop_limit"
        try:
            await self._run()
        except LoopStoppedError as stop:
            status, reason = stop.status, stop.reason
        except TimeoutError:
            status, reason = "timed_out", "model_timeout"
        except (ValueError, TypeError):
            reason = "invalid_decision"
        except Exception as error:
            # Provider/target exception messages may contain credentials.
            reason = type(error).__name__
        return SkillRuntimeResult(
            status=status,
            success=status == "succeeded",
            error_code=None if status == "succeeded" else reason,
            final_status={
                **self.last_feedback,
                "target_step_index": handle.session_state.step_index,
            },
            artifacts={"loop_receipts": [asdict(receipt) for receipt in self.receipts]},
            metadata={"termination_reason": reason, "action_attempts": self.steps},
        )

    def _observe(self) -> dict[str, Any]:
        data = dict(self._handle.observe().data)
        data.pop("target_info", None)  # Target configuration is not model input.
        if "sensors" in data:
            data["sensors"] = {
                name: sensor
                for name, sensor in data["sensors"].items()
                if sensor.get("kind") != "image"
            }
        # Adapters may expose numpy values. This loop consumes structured state.
        self._observation = json.loads(
            json.dumps(
                data,
                default=lambda value: value.tolist(),
                allow_nan=False,
            )
        )
        return self._observation

    def _feedback(self) -> dict[str, Any]:
        self._last_status = {**self._last_status, **self._handle.execution_status()}
        status = self._last_status
        info = status.get("info") or {}
        error = status.get("error_code") or info.get("error_code")
        ok = status.get("ok", info.get("ok"))
        feedback = {
            "done": status.get("done") is True,
            "success": status.get("success", info.get("success")) is True,
            "ok": False if error else ok,
            "error_code": error,
            "error_message": str(info.get("result") or "")[:500] if ok is False else None,
            "reward": status.get("reward"),
        }
        if self._verify is not None:
            feedback["success"] = bool(self._verify(self._observation, feedback))
            if feedback["success"]:
                feedback["done"] = True
        return feedback

    def _terminal(self) -> None:
        if self.last_feedback.get("done") is True:
            success = self.last_feedback.get("success") is True
            raise LoopStoppedError("succeeded" if success else "failed", "target_done")

    async def _request(
        self,
        role: str,
        context: dict[str, Any],
        schema: type[Output],
    ) -> Output:
        return await self.agent.request(
            role,
            context,
            schema,
            check=self.check,
            remaining_s=self.remaining_s,
        )

    async def _run(self) -> None:
        phase = None
        phase_index = 0
        no_progress = 0
        for loop_index in range(1, self.max_loops + 1):
            self.check()
            observation = self._observe()
            self.last_feedback = self._feedback()
            self.check()
            self._terminal()
            if self.steps >= self.max_steps:
                raise LoopStoppedError("failed", "step_limit")
            decision = await self._request(
                "planner",
                {
                    "task": self.task,
                    "observation": observation,
                    "phase": asdict(phase) if phase else None,
                    "last_receipt": self.receipts[-1].planner_view() if self.receipts else None,
                    "memory": self.memory.get("planner", ""),
                    "steps_remaining": self.max_steps - self.steps,
                },
                PlannerDecision,
            )
            self.check()
            if decision.decision == "finish":
                # A model's finish claim cannot set success without target evidence.
                self._observe()
                self.last_feedback = self._feedback()
                self.check()
                self._terminal()
                raise LoopStoppedError("failed", "unverified_finish")
            if decision.decision == "continue_phase":
                if phase is None or (decision.goal and decision.goal != phase.goal):
                    raise ValueError("continue_phase requires the current, unchanged goal")
                phase = Phase(phase.id, phase.goal, decision.max_rounds)
            else:
                phase_index += 1
                phase = Phase(f"phase-{phase_index}", decision.goal, decision.max_rounds)
            receipt = LoopReceipt(f"loop-{loop_index}", phase)
            self.receipts.append(receipt)
            for round_index in range(1, phase.max_rounds + 1):
                self.check()
                if self.steps >= self.max_steps:
                    receipt.end_reason = "step_limit"
                    raise LoopStoppedError("failed", "step_limit")
                observation = self._observe()
                self.last_feedback = self._feedback()
                self.check()
                self._terminal()
                actor = await self._request(
                    "actor",
                    {
                        "phase": asdict(phase),
                        "round": round_index,
                        "observation": observation,
                        "previous_round": asdict(receipt.rounds[-1]) if receipt.rounds else None,
                        "last_receipt": self.receipts[-2].planner_view()
                        if len(self.receipts) > 1
                        else None,
                        "actions": self.actions,
                        "memory": self.memory.get("actor", ""),
                    },
                    ActorDecision,
                )
                self.check()
                if actor.decision != "execute":
                    receipt.end_reason = actor.decision
                    receipt.end_detail = actor.intent
                    no_progress += 1
                    break
                if actor.action.type not in self.actions:
                    raise ValueError("Actor selected an action outside the target catalog")
                # One primitive crosses the boundary; all parameter validation stays in OS.
                self.steps += 1
                action = actor.action.model_dump()
                chunk = {"actions": [action]}
                if "observation_id" in observation:
                    chunk["source_observation_id"] = observation["observation_id"]
                self._last_status = self._handle.action_chunk(chunk)
                after = self._observe()
                self.last_feedback = self._feedback()
                record = RoundReceipt(
                    f"{receipt.id}/round-{round_index}",
                    actor.intent,
                    action,
                    observation,
                    after,
                    deepcopy(self.last_feedback),
                )
                receipt.rounds.append(record)
                self.check()
                if self.last_feedback.get("done"):
                    receipt.end_reason = "target_done"
                self._terminal()
                changed = record.planner_view()["changes"]
                # Observation IDs/timestamps are transport metadata, not game progress.
                metadata_keys = {"observation_id", "timestamp_ns", "state_version"}
                progressed = any(key not in metadata_keys for key in changed)
                no_progress = 0 if progressed else no_progress + 1
                if no_progress >= self.max_no_progress:
                    receipt.end_reason = "no_progress"
                    raise LoopStoppedError("failed", "no_progress")
                if self.last_feedback.get("ok") is False:
                    receipt.end_reason = "action_failed"
                    break
            if no_progress >= self.max_no_progress:
                raise LoopStoppedError("failed", "no_progress")

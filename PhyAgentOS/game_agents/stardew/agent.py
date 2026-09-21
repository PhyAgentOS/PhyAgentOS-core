"""Structured Planner, Actor and consolidation calls using the OS provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from PhyAgentOS.providers.base import LLMProvider

Output = TypeVar("Output", bound=BaseModel)

PROMPTS = {
    "planner": (
        "Choose a phase goal, continue it, replan after feedback, or finish. "
        "Plan from public observations and primitive-free loop receipts. "
        "Actor yield is a request for your review, not proof of success. "
        "Finish only requests termination; the target verifies task success."
    ),
    "actor": (
        "Work on the current phase. Execute exactly one allowed primitive, yield "
        "when the phase appears complete, or request replanning when blocked. "
        "Use the latest observation and actual feedback, not predicted state. "
        "Do not reset, close, or independently access the target."
    ),
    "consolidator": (
        "Extract reusable candidate lessons from the supplied episode evidence. "
        "Use only the requested role. Cite actual round IDs for every lesson. "
        "Distinguish observed results from hypotheses; a model claim is not proof. "
        "Return an empty candidate list when evidence is insufficient."
    ),
}


class StructuredAgent:
    """Request validated decisions through the configured Core provider."""

    def __init__(self, provider: LLMProvider, model: str, *, call_timeout_s: float = 120) -> None:
        if call_timeout_s <= 0:
            raise ValueError("call_timeout_s must be positive")
        self.provider = provider
        self.model = model
        self.call_timeout_s = call_timeout_s
        self.calls = 0
        self.usage: dict[str, int] = {}

    async def request(
        self,
        role: str,
        context: dict[str, Any],
        schema: type[Output],
        *,
        check: Callable[[], None],
        remaining_s: Callable[[], float],
    ) -> Output:
        check()
        generation = self.provider.generation
        messages = [
            {"role": "system", "content": PROMPTS[role] + " Return only JSON matching the schema."},
            {
                "role": "user",
                "content": json.dumps(
                    {"context": context, "output_schema": schema.model_json_schema()},
                    ensure_ascii=False,
                ),
            },
        ]
        self.calls += 1
        task = asyncio.create_task(
            self.provider.chat(
                messages=messages,
                model=self.model,
                temperature=generation.temperature,
                max_tokens=generation.max_tokens,
                reasoning_effort=generation.reasoning_effort,
            )
        )
        try:
            async with asyncio.timeout(min(self.call_timeout_s, remaining_s())):
                while not task.done():
                    await asyncio.wait({task}, timeout=0.05)
                    if not task.done():
                        check()
                response = await task
            for key, value in response.usage.items():
                self.usage[key] = self.usage.get(key, 0) + value
            check()
            if response.finish_reason == "error" or response.has_tool_calls:
                raise ValueError("provider did not return a structured decision")
            return schema.model_validate_json(response.content or "")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

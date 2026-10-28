"""Local request budgets; no network model discovery or destructive history edits."""

from __future__ import annotations

import sys
from typing import Any

from PhyAgentOS.utils.helpers import estimate_prompt_tokens_chain


def request_budget(provider: Any, model: str, configured_window: int, *, archival: bool = False) -> tuple[int, int]:
    window = configured_window if configured_window > 0 else 65_536
    # Use only already-loaded, exact catalog entries. Deployment aliases must
    # not silently borrow capabilities from a differently routed model.
    catalog = getattr(sys.modules.get("litellm"), "model_cost", {})
    info = catalog.get(model, {})
    known_input = info.get("max_input_tokens")
    if isinstance(known_input, int) and known_input > 0:
        window = min(window, known_input)
    generation = getattr(provider, "generation", None)
    output = max(1, getattr(generation, "max_tokens", 4096))
    if archival:
        output = min(output, 4096, max(1, window // 8))
    return max(0, window - output - max(16, window // 20)), output


def fit_history(messages: list[dict], provider: Any, model: str, budget: int, tools: list[dict]) -> tuple[list[dict], bool]:
    """Drop only old complete user turns in the request copy, never persisted data.

    Keep the latest user turn and all its tool exchanges. If that alone is
    oversized, fail before calling the provider rather than hiding execution
    evidence or resubmitting physical actions with a partial transcript.
    """
    candidate = list(messages)
    while estimate_prompt_tokens_chain(provider, model, candidate, tools)[0] > budget:
        starts = [i for i, item in enumerate(candidate) if item.get("role") == "user"]
        if len(starts) < 2:
            raise ValueError(
                "Current turn or system instructions exceed the context budget. "
                "Use /new or reduce the input/tool output; the original session is preserved."
            )
        candidate = candidate[:starts[0]] + candidate[starts[1]:]
    return candidate, len(candidate) != len(messages)

#!/usr/bin/env python3
"""Mechanical aggregate of the supervision records inside one RoboTwin session.

Every number here is arithmetic over the run's own tool results (vla.decide /
vla.set_mode / vla.get_status / vision.get_frame): which checkpoint fired, at
which step, for which reason, with which mode, and what happened after the
decision budget ran out. No interpretation is added - the reflection model is
still the one that has to judge whether the pattern is workflow-related.
"""
from __future__ import annotations

from collections import Counter

from PhyAgentOS.agent.experience.contracts import LineageOutcome
from PhyAgentOS.agent.experience.redaction import redact_text

from robotwin_evidence import ToolCall, as_dict, tool_outputs

RELEASE_TOOLS = {"vla.set_mode"}


def _checkpoint_of(call: ToolCall) -> dict:
    outputs = tool_outputs(call)
    checkpoint = as_dict(outputs.get("checkpoint"))
    return checkpoint


def _decision_of(call: ToolCall) -> dict:
    outputs = tool_outputs(call)
    closed = as_dict(outputs.get("checkpoint_closed"))
    decision = as_dict(outputs.get("decision")) or as_dict(closed.get("decision"))
    return {
        "trigger": closed.get("trigger"),
        "reason": closed.get("reason"),
        "at_step": decision.get("at_step"),
        "mode": outputs.get("mode") if outputs.get("mode") is not None else decision.get("mode"),
        "steps": call.inner_arguments.get("steps") if call.inner_arguments else decision.get("steps"),
    }


def supervision_summary(calls: list[ToolCall]) -> str:
    decisions: list[dict] = []
    checkpoints: list[str] = []
    seen_checkpoints: set[tuple] = set()
    modes: list[str] = []
    last_counters: dict = {}
    last_decision_index = 0
    frame_sources: Counter[str] = Counter()
    image_calls = 0
    get_status_calls = 0

    for call in calls:
        if call.name != "forge_tool_query":
            continue
        tool = call.inner_tool or ""
        if tool == "vla.decide":
            decisions.append(_decision_of(call))
            last_decision_index = call.index
        elif tool.startswith("vla.get_status"):
            get_status_calls += 1
            checkpoint = _checkpoint_of(call)
            if not checkpoint:
                continue
            counters = as_dict(checkpoint.get("counters"))
            if counters:
                last_counters = counters
            key = (
                checkpoint.get("trigger"),
                checkpoint.get("reason"),
                checkpoint.get("step_in_episode"),
            )
            if key not in seen_checkpoints:
                seen_checkpoints.add(key)
                checkpoints.append(
                    f"{checkpoint.get('trigger')}/{checkpoint.get('reason')}"
                    f"@{checkpoint.get('step_in_episode')}"
                )
        elif tool in RELEASE_TOOLS:
            mode = tool_outputs(call).get("mode")
            modes.append((call.index, str(mode)))
        elif tool.startswith("vision.get_frame"):
            frame_sources[tool.replace("vision.get_frame", "") or "_head"] += 1
    for call in calls:
        if call.name == "image":
            image_calls += 1

    decision_steps = [item.get("at_step") for item in decisions]
    decision_modes = Counter(str(item.get("mode")) for item in decisions)
    edits = sum(1 for item in decisions if str(item.get("mode")) == "edit")
    releases_after_budget = sum(
        1 for index, mode in modes if index > last_decision_index and mode == "student"
    )
    early_modes = sum(1 for index, _ in modes if index < last_decision_index)

    parts = [
        f"supervision_decisions={len(decisions)}",
        f"decision_steps={decision_steps}",
        f"decision_modes={dict(decision_modes)}",
        f"edit_decisions={edits}",
        f"checkpoints_observed={checkpoints}",
        f"get_status_polls={get_status_calls}",
        f"release_calls_before_last_decision={early_modes}",
        f"release_calls_after_last_decision={releases_after_budget}",
        f"frames_pulled={dict(frame_sources)}",
        f"image_tool_calls={image_calls}",
    ]
    if last_counters:
        keys = (
            "c1_events",
            "c2_events",
            "decisions_applied",
            "decisions_rejected",
            "edit_actions_biased",
            "eef_actions_generated",
            "segments_started",
            "segments_completed",
        )
        counters = {key: last_counters[key] for key in keys if key in last_counters}
        parts.append(f"final_counters={counters}")
    return "; ".join(parts)


def summary_lineage(calls: list[ToolCall]) -> LineageOutcome | None:
    summary = supervision_summary(calls)
    if not summary:
        return None
    return LineageOutcome(
        session_ref="run:supervision-summary",
        action_semantics="robotwin.supervision.summary",
        input_keys=["decisions", "checkpoints", "modes", "steps"],
        execution_status="succeeded",
        reason=redact_text(summary),
    )
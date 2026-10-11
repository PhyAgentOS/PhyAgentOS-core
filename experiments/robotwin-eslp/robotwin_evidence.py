#!/usr/bin/env python3
"""Translate real RoboTwin run artifacts into PAOS experience evidence.

Everything here is copied out of artifacts the run itself produced; nothing is
invented or hand-written:

* the agent session transcript (``<workspace>/sessions/<session>.jsonl``) gives
  the ordered tool-call trace, the tool arguments and the tool results;
* the benchmark result JSON (``benchmark_execution_result_v1``) gives the
  terminal verdict of the episode.

The translation is mechanical: tool names, argument values, result fields and
benchmark facts are passed through. Free text produced by the run (VLM frame
captions) is passed on as untrusted data after redaction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PhyAgentOS.agent.experience.contracts import LineageOutcome
from PhyAgentOS.agent.experience.redaction import opaque_ref, redact_text

CAPTION_CHARS = 320
COUNTER_KEYS = (
    "c1_events",
    "c2_events",
    "checkpoints_opened",
    "checkpoints_closed",
    "decisions_applied",
    "decisions_rejected",
    "edit_actions_biased",
    "eef_actions_generated",
    "segments_started",
    "segments_completed",
    "policy_actions_forwarded",
)


@dataclass
class ToolCall:
    index: int
    name: str
    arguments: dict[str, Any]
    inner_tool: str | None = None
    inner_arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None
    payload: Any = None
    text: str = ""

    @property
    def action_semantics(self) -> str:
        return f"{self.name}.{self.inner_tool}" if self.inner_tool else self.name

    @property
    def input_keys(self) -> list[str]:
        keys = [str(key) for key in self.arguments]
        keys.extend(f"arguments.{key}" for key in self.inner_arguments)
        return sorted(dict.fromkeys(keys))

    @property
    def execution_status(self) -> str:
        if self.ok is True:
            return "succeeded"
        if self.ok is False:
            return "failed"
        return "unknown"


def parse_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def as_dict(value: Any) -> dict[str, Any]:
    value = parse_json(value)
    return value if isinstance(value, dict) else {}


def read_session_records(session_path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    text = Path(session_path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def session_task_summary(session_path: str | Path) -> str:
    """Return the first user message of the session (the turn prompt)."""
    for record in read_session_records(session_path):
        if record.get("role") == "user":
            content = record.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def load_tool_calls(session_path: str | Path) -> list[ToolCall]:
    records = read_session_records(session_path)
    calls: list[ToolCall] = []
    positions: dict[str, int] = {}
    for record in records:
        if record.get("role") != "assistant":
            continue
        for call in record.get("tool_calls") or []:
            function = as_dict(call.get("function"))
            if not function:
                function = call.get("function") or {}
            arguments = as_dict(function.get("arguments"))
            call_id = str(call.get("id") or "")
            positions[call_id] = len(calls)
            calls.append(
                ToolCall(
                    index=len(calls) + 1,
                    name=str(function.get("name") or "unknown"),
                    arguments=arguments,
                    inner_tool=str(arguments.get("tool_id") or "") or None,
                    inner_arguments=as_dict(arguments.get("arguments")),
                )
            )
    for record in records:
        if record.get("role") != "tool":
            continue
        position = positions.get(str(record.get("tool_call_id") or ""))
        if position is None:
            continue
        call = calls[position]
        call.text = str(record.get("content") or "")
        parsed = parse_json(call.text)
        if isinstance(parsed, dict):
            call.ok = parsed.get("ok")
            data = parsed.get("data")
            call.payload = data if data is not None else parsed
        else:
            call.payload = parsed
    return calls


def tool_outputs(call: ToolCall) -> dict[str, Any]:
    payload = as_dict(call.payload)
    response = as_dict(payload.get("response"))
    result = as_dict(response.get("result"))
    return as_dict(result.get("outputs"))


def _segment_facts(segment: Any) -> str:
    segment = as_dict(segment)
    if not segment:
        return ""
    decision = as_dict(segment.get("decision"))
    return (
        f"segment.index={segment.get('index')} segment.mode={segment.get('mode')} "
        f"segment.supervised={segment.get('supervised')} "
        f"segment.decision.mode={decision.get('mode')} "
        f"segment.decision.steps={decision.get('steps')} "
        f"segment.steps_budget={segment.get('steps_budget')} "
        f"segment.steps_executed={segment.get('steps_executed')} "
        f"segment.edit_applied={segment.get('edit_applied')}"
    )


def _counter_facts(counters: Any) -> str:
    counters = as_dict(counters)
    parts = [f"{key}={counters[key]}" for key in COUNTER_KEYS if counters.get(key)]
    if not parts:
        return ""
    return "counters: " + " ".join(parts)


def evidence_reason(call: ToolCall) -> str:
    """Mechanical digest of one tool call's own arguments and result."""
    outputs = tool_outputs(call)
    if call.name == "image":
        caption = " ".join(str(call.text).split())
        return f"frame_caption={redact_text(caption[:CAPTION_CHARS])}"
    if call.name == "forge_tool_query":
        tool = call.inner_tool or ""
        if tool == "vla.decide":
            closed = as_dict(outputs.get("checkpoint_closed"))
            return (
                f"accepted={outputs.get('accepted')} decision.mode={outputs.get('mode')} "
                f"decision.steps={call.inner_arguments.get('steps')} "
                f"closed.trigger={closed.get('trigger')} closed.reason={closed.get('reason')} "
                + _segment_facts(outputs.get("segment"))
            ).strip()
        if tool == "vla.set_mode":
            return (
                f"changed={outputs.get('changed')} mode={outputs.get('mode')} "
                f"policy_command={outputs.get('policy_command')}"
            )
        if tool.startswith("vla.get_status"):
            checkpoint = as_dict(outputs.get("checkpoint"))
            if not checkpoint:
                return "checkpoint=none"
            return (
                f"checkpoint.trigger={checkpoint.get('trigger')} "
                f"checkpoint.reason={checkpoint.get('reason')} "
                f"checkpoint.step_in_episode={checkpoint.get('step_in_episode')} "
                + _segment_facts(checkpoint.get("last_segment"))
                + " "
                + _counter_facts(checkpoint.get("counters"))
            ).strip()
        if tool.startswith("vision.get_frame"):
            return (
                f"frame_seq={outputs.get('frame_seq')} "
                f"frame_age_ms={outputs.get('frame_age_ms')} "
                f"image={opaque_ref(str(outputs.get('image_path')))}"
            )
        if tool == "robotwin.policy.describe":
            return (
                f"ready={outputs.get('ready')} action_dim={outputs.get('action_dim')} "
                f"backend={outputs.get('backend')}"
            )
        if tool == "robotwin.benchmark.describe":
            return f"benchmark={outputs.get('benchmark')} defaults={outputs.get('defaults')}"
        outcome = as_dict(call.payload.get("response") if call.payload else None).get("outcome")
        return f"tool={tool} outcome={outcome}"
    if call.name == "forge_tool_session_status":
        payload = as_dict(call.payload)
        return (
            f"phase={payload.get('phase')} accepted={payload.get('accepted')} "
            f"cancel_status={payload.get('cancel_status')} stop_status={payload.get('stop_status')}"
        )
    if call.name == "forge_tool_session_result":
        payload = as_dict(call.payload)
        result = as_dict(payload.get("result"))
        outputs = as_dict(result.get("outputs"))
        return (
            f"session.result.status={result.get('status')} "
            f"success_rate={outputs.get('success_rate')} "
            f"completed_episodes={outputs.get('completed_episodes')} "
            f"total_episodes={outputs.get('total_episodes')}"
        )
    if call.name == "forge_task_finalize":
        return "task finalized"
    task_id = call.arguments.get("task_id")
    if task_id:
        return f"task_id={opaque_ref(str(task_id))}"
    return call.name


def evidence_refs(call: ToolCall) -> list[str]:
    outputs = tool_outputs(call)
    refs: list[str] = []
    for key in ("image_path", "result_file"):
        value = outputs.get(key)
        if value:
            refs.append(opaque_ref(str(value)))
    return refs


def build_lineage(calls: list[ToolCall]) -> list[LineageOutcome]:
    items: list[LineageOutcome] = []
    for call in calls:
        items.append(
            LineageOutcome(
                session_ref=f"tool:{call.index:03d}",
                action_semantics=call.action_semantics[:200],
                input_keys=call.input_keys,
                execution_status=call.execution_status,
                reason=redact_text(evidence_reason(call)),
                evidence_refs=evidence_refs(call),
            )
        )
    return items


def verification_contract(calls: list[ToolCall]) -> tuple[str | None, list[str], str]:
    """Read the goal/criteria the run registered in its own forge_task_create."""
    for call in calls:
        if call.name != "forge_task_create":
            continue
        verification = as_dict(call.arguments.get("verification"))
        goal = verification.get("goal")
        criteria = verification.get("success_criteria")
        association = verification.get("minimum_association")
        return (
            str(goal) if goal else None,
            [str(item) for item in criteria] if isinstance(criteria, list) else [],
            str(association or ""),
        )
    return None, [], ""


def benchmark_succeeded(result: dict[str, Any]) -> bool:
    total = result.get("total_episodes")
    completed = result.get("completed_episodes")
    return bool(
        total
        and completed == total
        and result.get("success_rate") == 1.0
        and result.get("status") == "succeeded"
    )


def terminal_lineage(result: dict[str, Any], *, task_name: str) -> LineageOutcome:
    facts: list[str] = []
    for episode in result.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        facts.append(
            f"episode_id={episode.get('episode_id')} task_id={episode.get('task_id')} "
            f"init_state_id={episode.get('init_state_id')} run_index={episode.get('run_index')} "
            f"termination={episode.get('termination')} success={episode.get('success')} "
            f"num_steps={episode.get('num_steps')} return={episode.get('return_value')}"
        )
    reason = (
        f"benchmark={result.get('benchmark')} task_name={task_name} status={result.get('status')} "
        f"success_rate={result.get('success_rate')} "
        f"completed={result.get('completed_episodes')}/{result.get('total_episodes')} "
        f"num_steps={result.get('num_steps')} max_steps={result.get('max_steps')}"
    )
    if facts:
        reason += " | " + " | ".join(facts)
    refs: list[str] = []
    for episode in result.get("episodes") or []:
        video = as_dict(as_dict(episode).get("metrics")).get("video")
        if video:
            refs.append(opaque_ref(str(video)))
    return LineageOutcome(
        session_ref="benchmark:result",
        action_semantics="robotwin.benchmark.result",
        input_keys=["success_rate", "num_steps", "termination"],
        execution_status=str(result.get("status") or ""),
        semantic_verdict="success" if benchmark_succeeded(result) else "failure",
        reason=redact_text(reason),
        evidence_refs=refs,
    )
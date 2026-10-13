#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from robotwin_evidence import as_dict, load_tool_calls, read_session_records, tool_outputs


def recursively_find(value: Any, suffix: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith(suffix):
                found.append(item)
            found.extend(recursively_find(item, suffix))
    elif isinstance(value, list):
        for item in value:
            found.extend(recursively_find(item, suffix))
    return found


def compact(value: Any, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    return " ".join(text.split())[:limit]


def checkpoint_facts(calls: list[Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    last_status: dict[str, Any] | None = None
    image_index = 0
    for call in calls:
        if call.name == "image":
            image_index += 1
            continue
        if call.name != "forge_tool_query":
            continue
        tool = call.inner_tool or ""
        outputs = tool_outputs(call)
        if tool.startswith("vla.get_status"):
            checkpoint = as_dict(outputs.get("checkpoint"))
            if not checkpoint:
                continue
            counters = as_dict(checkpoint.get("counters"))
            details = as_dict(checkpoint.get("details"))
            joints = recursively_find(checkpoint, "joint_positions")[:1]
            last_status = {
                "step": checkpoint.get("step_in_episode"),
                "trigger": checkpoint.get("trigger"),
                "reason": checkpoint.get("reason"),
                "displacement_rad": details.get("displacement", details.get("displacement_m")),
                "threshold_rad": details.get("threshold", details.get("threshold_m")),
                "window_steps": details.get("window_steps"),
                "counters": {key: counters.get(key) for key in (
                    "c1_events", "c2_events", "decisions_applied", "decisions_rejected",
                    "edit_actions_biased", "segments_started", "segments_completed",
                    "policy_actions_forwarded",
                ) if key in counters},
                "joints": joints[0] if joints else None,
            }
        elif tool == "vla.decide":
            closed = as_dict(outputs.get("checkpoint_closed"))
            decision = as_dict(outputs.get("decision")) or as_dict(closed.get("decision"))
            fact = {
                "step": decision.get("at_step") or (last_status or {}).get("step"),
                "trigger": closed.get("trigger") or (last_status or {}).get("trigger"),
                "reason": closed.get("reason") or (last_status or {}).get("reason"),
                "mode": outputs.get("mode") or decision.get("mode"),
                "accepted": outputs.get("accepted"),
                "steps": call.inner_arguments.get("steps"),
                "delta_joints": call.inner_arguments.get("delta_joints"),
                "gripper": {
                    key: call.inner_arguments.get(key)
                    for key in ("left_gripper", "right_gripper") if key in call.inner_arguments
                },
                "last_status": last_status,
            }
            facts.append(fact)
    if not facts:
        for call in calls:
            if call.name == "forge_tool_query" and (call.inner_tool or "").startswith("vla.get_status"):
                outputs = tool_outputs(call)
                checkpoint = as_dict(outputs.get("checkpoint"))
                if checkpoint:
                    facts.append({"step": checkpoint.get("step_in_episode"), "trigger": checkpoint.get("trigger"), "reason": checkpoint.get("reason"), "mode": "observed_only"})
    return facts[:8]


def visual_facts(calls: list[Any]) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    for call in calls:
        if call.name != "image":
            continue
        parsed = as_dict(call.text)
        caption = parsed.get("caption") or parsed.get("text") or call.text
        facts.append({"caption": compact(caption, 300)})
    return facts[:5]


def reasoning_facts(session_path: str | Path) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    for record in read_session_records(session_path):
        if record.get("role") != "assistant":
            continue
        content = " ".join(str(record.get("content") or "").split())
        if len(content) < 60:
            continue
        lower = content.lower()
        if not any(term in lower for term in ("checkpoint", "no_progress", "camera", "edit", "student", "lesson")):
            continue
        facts.append({"assistant_reasoning": content[:420]})
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for fact in facts:
        key = fact["assistant_reasoning"][:120]
        if key not in seen:
            unique.append(fact)
        seen.add(key)
    return unique[:6]


def render_eslp(task: str, result: dict[str, Any], facts: list[dict[str, Any]], visuals: list[dict[str, str]], rationales: list[dict[str, str]], native: list[dict[str, Any]]) -> str:
    lines = [
        "", "", "## ACTIVE EXPERIENCE CARRIER: EVIDENCE-SCOPED LESSON PACK",
        "This carrier comes from one real failed same-task episode. Treat it as evidence, not as a guarantee.",
        "",
        "### L1 PRINCIPLE",
        "Do not repeat a bounded student release when the same checkpoint repeats with materially unchanged state.",
        "First inspect the relevant camera, compare the joint fingerprint, then make at most one bounded correction.",
        "If two bounded corrections produce less than 0.05 rad displacement, stop escalating and report a policy-capability limit.",
        "",
        "### L2 EVIDENCE",
        f"task={task}; success_rate={result.get('success_rate')}; completed_steps={result.get('num_steps')}; status={result.get('status')}",
    ]
    for index, fact in enumerate(facts, 1):
        lines.append(f"- E{index}: " + compact(fact, 1200))
    for index, visual in enumerate(visuals, 1):
        lines.append(f"- V{index}: " + compact(visual, 340))
    for index, rationale in enumerate(rationales, 1):
        lines.append(f"- R{index}: " + compact(rationale, 480))
    lines.extend(["", "### L3 ACTION POLICY"])
    lines += [
        "1. At the first repeated no-progress checkpoint, pull the head camera and the relevant wrist camera.",
        "2. Compare the current joints with the evidence. If unchanged, issue one edit segment; never exceed 0.1 rad per joint in that segment.",
        "3. If the evidence has edit_actions_biased=0, prefer a bounded joint or gripper edit before another student release.",
        "4. After one correction, inspect displacement. Correct once more only if displacement increased or the visual error changed.",
        "5. If the same joint remains frozen, explicitly declare that pi0.5 cannot recover under the current protocol and continue observation without unsafe escalation.",
    ]
    if native:
        lines.extend(["", "### PAOS NATIVE LESSON CROSS-CHECK"])
        for lesson in native[:2]:
            lines.append("- " + compact(lesson, 1200))
    lines.append("")
    return "\n".join(lines)


def render_dialogue(task: str, result: dict[str, Any], facts: list[dict[str, Any]], visuals: list[dict[str, str]], rationales: list[dict[str, str]]) -> str:
    lines = [
        "", "", "## ACTIVE EXPERIENCE CARRIER: PRIOR-EPISODE DIALOGUE CARD",
        "These are compressed excerpts from one failed same-task attempt. Do not copy its failed decision unless current evidence materially changed.",
        "",
        f"SUPERVISOR TASK CONTEXT: Run one same-seed episode of {task}. The prior episode ended with success_rate={result.get('success_rate')}.",
    ]
    for index, fact in enumerate(facts, 1):
        lines.append(f"PRIOR CHECKPOINT {index}: " + compact(fact, 1000))
    for index, visual in enumerate(visuals, 1):
        lines.append(f"PRIOR OBSERVATION {index}: " + compact(visual, 320))
    for index, rationale in enumerate(rationales, 1):
        lines.append(f"PRIOR SUPERVISOR REASONING {index}: " + compact(rationale, 480))
    lines += [
        f"PRIOR TERMINAL RESULT: {compact(result, 900)}",
        "COUNTERFACTUAL INSTRUCTION: The old action sequence failed. A different, evidence-backed checkpoint decision is required.",
        "",
    ]
    return "\n".join(lines)


def native_lessons(report_path: Path | None) -> list[dict[str, Any]]:
    if report_path is None or not report_path.is_file():
        return []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    lessons = report.get("lessons") or []
    if not lessons:
        for lesson in report.get("all_lessons") or []:
            if lesson.get("status") == "active":
                lessons.append(lesson)
    return lessons


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--native-report")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    calls = load_tool_calls(args.session)
    rationales = reasoning_facts(args.session)
    facts = checkpoint_facts(calls)
    visuals = visual_facts(calls)
    native = native_lessons(Path(args.native_report) if args.native_report else None)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    documents = {
        "eslp.md": render_eslp(args.task, result, facts, visuals, rationales, native),
        "dialogue.md": render_dialogue(args.task, result, facts, visuals, rationales),
    }
    manifest = {"task": args.task, "evidence": facts, "visuals": visuals, "assistant_reasoning": rationales, "native_lessons": native, "documents": {}}
    for name, body in documents.items():
        path = out / name
        path.write_text(body, encoding="utf-8")
        manifest["documents"][name] = {
            "bytes": len(body.encode("utf-8")),
            "approx_tokens": len(body) // 4,
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out_dir": str(out), "evidence_count": len(facts), "visual_count": len(visuals), "reasoning_count": len(rationales), "native_count": len(native), "documents": manifest["documents"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

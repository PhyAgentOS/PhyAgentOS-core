#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from robotwin_evidence import as_dict, load_tool_calls, tool_outputs


def assistant_text(calls: list[Any]) -> str:
    return " ".join(str(getattr(call, "text", "")) for call in calls).lower()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--carrier")
    parser.add_argument("--arm", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    calls = load_tool_calls(args.session)
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    decisions = []
    last_counters = {}
    for call in calls:
        if call.name != "forge_tool_query":
            continue
        tool = call.inner_tool or ""
        outputs = tool_outputs(call)
        if tool == "vla.decide":
            closed = as_dict(outputs.get("checkpoint_closed"))
            decision = as_dict(outputs.get("decision")) or as_dict(closed.get("decision"))
            decisions.append({
                "step": decision.get("at_step"),
                "mode": outputs.get("mode") or decision.get("mode"),
                "steps": call.inner_arguments.get("steps"),
                "delta_joints": call.inner_arguments.get("delta_joints"),
            })
        elif tool.startswith("vla.get_status"):
            checkpoint = as_dict(outputs.get("checkpoint"))
            if checkpoint:
                last_counters = as_dict(checkpoint.get("counters"))
    text = assistant_text(calls)
    carrier_hash = ""
    carrier_tokens = 0
    if args.carrier and Path(args.carrier).is_file():
        body = Path(args.carrier).read_text(encoding="utf-8")
        carrier_hash = hashlib.sha256(body.encode()).hexdigest()
        carrier_tokens = len(body) // 4
    score = {
        "arm": args.arm,
        "phase": args.phase,
        "success_rate": result.get("success_rate"),
        "steps_used": result.get("num_steps"),
        "decision_count": len(decisions),
        "decisions": decisions,
        "first_decision_mode": decisions[0]["mode"] if decisions else None,
        "edit_decisions": sum(1 for item in decisions if item.get("mode") == "edit"),
        "student_decisions": sum(1 for item in decisions if item.get("mode") == "student"),
        "repeated_student_tail": max((sum(1 for item in reversed(decisions[:index + 1]) if item.get("mode") == "student") for index, item in enumerate(decisions) if item.get("mode") != "student"), default=len(decisions)),
        "final_counters": last_counters,
        "mentions_lesson": any(term in text for term in ("lesson", "prior", "evidence")),
        "mentions_eslp": "evidence-scoped" in text or "l1 principle" in text or "e1:" in text,
        "mentions_dialogue": "dialogue card" in text or "prior checkpoint" in text,
        "mentions_numeric_evidence": any(term in text for term in ("0.1 rad", "0.05", "displacement", "no_progress")),
        "carrier_sha256": carrier_hash,
        "carrier_approx_tokens": carrier_tokens,
    }
    Path(args.out).write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False))


if __name__ == "__main__":
    main()

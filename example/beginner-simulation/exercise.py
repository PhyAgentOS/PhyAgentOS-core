"""Run one bounded Piper simulation exercise through the native Tool API.

Run only after starting this tutorial's Skill. Every Action must reach a
successful terminal result; an HTTP receipt or a video alone is insufficient.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from PhyAgentOS.forge.tool_client import ForgeToolClient


def find_object(value, key):
    if isinstance(value, dict):
        if key in value:
            return value
        for child in value.values():
            found = find_object(child, key)
            if found is not None:
                return found
    return None


async def exercise(gateway_url: str, destination: Path) -> None:
    destination = destination.absolute()
    if destination.exists():
        raise ValueError("Choose a new report path; previous evidence is not overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Piper simulation only; reset, small joint motion, reset",
        "gateway_url": gateway_url,
        "actions": [],
        "passed": False,
    }

    def save():
        destination.write_text(json.dumps(report, indent=2))

    save()
    try:
        async with ForgeToolClient(gateway_url) as client:
            report["discovery"] = await client.invoke_query_tool("sim.get_robot_info", {})
            info = find_object(report["discovery"], "initial_qpos")
            if not info or info.get("name") != "piper" or info.get("source") != "mjcf":
                raise ValueError("Expected this tutorial's simulated Piper descriptor")
            joints = [j for j in info["joints"] if j["kind"] == "revolute"]
            names = [j["name"] for j in joints]
            if names != [f"joint{i}" for i in range(1, 7)]:
                raise ValueError("Unexpected arm joint order; inspect the active Skill")
            positions = [info["initial_qpos"][name] for name in names]
            positions[0] += 0.2
            if any(not j["limits"][0] <= q <= j["limits"][1] for j, q in zip(joints, positions)):
                raise ValueError("Tutorial target is outside discovered limits")
            requests = [
                ("reset-before", "sim.reset", {}),
                (
                    "joint-motion",
                    "sim.set_joint_positions",
                    {
                        "group_name": "piper_arm",
                        "joint_names": names,
                        "positions": positions,
                        "velocity_scale": 0.2,
                        "acceleration_scale": 0.2,
                    },
                ),
                ("reset-after", "sim.reset", {}),
            ]
            for label, tool, arguments in requests:
                row = {
                    "label": label,
                    "tool_id": tool,
                    "arguments": arguments,
                    "status_samples": [],
                }
                report["actions"].append(row)
                row["context"] = await client.get_tool_context(tool)
                if row["context"].get("data", {}).get("ready") is not True:
                    raise RuntimeError(f"Tool is not ready: {tool}")
                row["admission"] = await client.invoke_action(tool, arguments, timeout_ms=20000)
                ident = row["admission"]["data"]["invocation_id"]
                save()
                deadline = time.monotonic() + 70
                while time.monotonic() < deadline:
                    status = await client.invocation_status(ident)
                    row["status_samples"].append(status)
                    phase = status.get("data", {}).get("phase")
                    save()
                    if phase in {
                        "completed",
                        "failed",
                        "cancelled",
                        "canceled",
                        "timed_out",
                        "unknown",
                    }:
                        row["result"] = await client.invocation_result(ident)
                        break
                    await asyncio.sleep(0.5)
                else:
                    row["cancel_request"] = await client.cancel_invocation(ident)
                    raise RuntimeError(
                        "Action did not terminate; cancellation requested, no automatic replay"
                    )
                terminal = row["result"].get("data", {}).get("result", {})
                if terminal.get("status") != "succeeded":
                    raise RuntimeError(f"{label} did not succeed; inspect the saved result")
                if tool == "sim.set_joint_positions":
                    motion = find_object(terminal, "final_position_errors")
                    if (
                        not motion
                        or motion.get("error_code") != "SUCCESS"
                        or motion.get("joint_names") != names
                    ):
                        raise RuntimeError(
                            "Motion did not supply successful ordered joint evidence"
                        )
                    errors = motion["final_position_errors"]
                    if len(errors) != len(names) or any(
                        not math.isfinite(float(e)) or abs(float(e)) > 0.03 for e in errors
                    ):
                        raise RuntimeError("Joint error exceeded 0.03 radians")
                    row["max_joint_error_rad"] = max(abs(float(e)) for e in errors)
                elif not (
                    terminal.get("outputs", {}).get("episode_reset")
                    and terminal["outputs"].get("rehomed")
                ):
                    raise RuntimeError("Reset did not confirm episode_reset and rehomed")
                row["passed"] = True
                save()
            report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        report["error_type"] = type(exc).__name__
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
        print(json.dumps({"report": str(destination), "passed": report["passed"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:19002")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(exercise(args.gateway_url, args.report))

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vla_bridge.service import BridgeState

labels = [f"joint{i}" for i in range(14)]
grippers = (6, 13)
state = BridgeState(
    policy_id="test",
    initial_mode="stop",
    policy_command_sender=lambda verb: "req",
    checkpoints_enabled=True,
    accepted_modes=("policy", "agent", "student", "edit", "stop", "eef"),
    joint_abs_labels=labels,
    gripper_indices=grippers,
)
state.observe_proprio(joint_positions=[0.0] * 14)
values = [0.0] * 14
values[2] = 0.03
values[6] = 1.0
values[9] = -0.02
values[13] = 0.0
state.observe_candidate(labels=labels, values=values, gripper_indices=grippers)
status = state.status(max_proprio_age_ms=1000)
candidate = status["candidate"]
assert candidate["freshness"] == "pending", candidate
assert candidate["pending_execution"] is True
assert candidate["dominant_joints"][0] == {"joint": "joint2", "delta_rad": 0.03}
assert candidate["gripper_targets"] == {"left_gripper": "open", "right_gripper": "closed"}
state.note_forwarded()
state.mark_candidate_forwarded()
candidate = state.status(max_proprio_age_ms=1000)["candidate"]
assert candidate["freshness"] == "historical", candidate
assert candidate["pending_execution"] is False, candidate
state.reset_episode()
assert state.status(max_proprio_age_ms=1000)["candidate"] is None
print("candidate lifecycle test OK")

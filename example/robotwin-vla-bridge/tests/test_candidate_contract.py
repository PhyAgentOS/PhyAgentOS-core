from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vla_bridge.config import load_bridge_config
from vla_bridge.service import BridgeState


ROOT = Path(__file__).resolve().parents[1]


def test_robotwin_profile_and_candidate_lifecycle():
    config = load_bridge_config(ROOT / "config" / "vla_bridge.yaml")
    assert config.is_joint_abs
    assert len(config.arm_joints) == 14
    assert config.gripper_indices == (6, 13)
    assert config.checkpoints_enabled

    state = BridgeState(
        policy_id="test",
        initial_mode="stop",
        policy_command_sender=lambda verb: "request",
        checkpoints_enabled=True,
        accepted_modes=config.accepted_modes,
        joint_abs_labels=config.arm_joints,
        gripper_indices=config.gripper_indices,
        edit_max_joint_rad=config.edit_max_joint_rad,
        max_cumulative_edit_joint_rad=config.max_cumulative_edit_joint_rad,
        hold_on_episode_start=True,
    )
    state.observe_proprio(joint_positions=[0.0] * 14)
    state.observe_candidate(
        labels=config.arm_joints,
        values=[0.2] * 14,
        gripper_indices=config.gripper_indices,
    )
    assert state.status(max_proprio_age_ms=1000)["candidate"]["freshness"] == "pending"

    decision = state.decide(
        mode="edit",
        steps=5,
        edit={"delta_joints": {config.arm_joints[0]: 1.0}},
    )
    delta = decision["decision"]["edit"]["delta_joints_vector"]
    assert delta[0] == config.edit_max_joint_rad
    assert state.cumulative_edit_joint_rad[0] == 0.0

    shaped, rewritten = state.shape_action([0.0] * 14)
    assert rewritten
    expected_first_step = config.edit_max_joint_rad / config.edit_ramp_steps
    assert abs(shaped[0] - expected_first_step) < 1e-12

    state.note_forwarded()
    assert state.status(max_proprio_age_ms=1000)["candidate"]["freshness"] == "historical"
    state.reset_episode()
    assert state.status(max_proprio_age_ms=1000)["candidate"] is None


if __name__ == "__main__":
    test_robotwin_profile_and_candidate_lifecycle()
    print("candidate contract test OK")

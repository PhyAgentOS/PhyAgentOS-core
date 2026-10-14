"""vla_bridge: the single writer between the policy loop and the robot.

Routes (dora input ids in parentheses):
- POLICY mode: lerobot_infer JointCommands (policy_action) are split into
  arm/gripper, clamped against joint limits and per-tick deltas, and written
  to mujoco; the motion chain's commands are dropped.
- AGENT mode: joint_trajectory_controller / gripper_action_controller
  commands pass straight through; policy actions are dropped.
- proprio_state is relayed to the policy with the gripper unit converted
  (mujoco meters -> policy contract, default millimeters).
- vla.set_mode / vla.get_status (gateway tools) flip the mode and translate
  it into PolicyCommand start/pause for lerobot_infer's lifecycle channel.
"""

def main(*args, **kwargs):
    """Load the Dora runtime only when the executable entry point is used."""
    from .main import main as runtime_main

    return runtime_main(*args, **kwargs)


__all__ = ["main"]

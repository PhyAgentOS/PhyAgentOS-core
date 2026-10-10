"""vla_bridge dora node: the single writer to the robot's action inputs.

两个动作空间（config.action_space 决定，见 config.py）：
- ``joint``（piper_hybrid）：关节目标 → 限位 + 单条增量限幅 + 夹爪单位换算，
  split 成 arm/gripper 两路写 mujoco；
- ``eef_delta``（libero_bench）：robosuite 原生 7 维笛卡尔增量 → **原样直通**
  （student 通道），单路 ``action`` 输出给 benchmark 节点；限幅留给 edit 层。

Event loop responsibilities:
- relay mujoco proprio_state to lerobot_infer with the gripper unit converted
  (sim meters -> policy millimeters; libero 档案 scale=1.0 即恒等中继);
- POLICY mode: eef_delta 直通 / joint 拆分限幅后写 mujoco;
- AGENT mode: 直通 agent 运动链的 joint_command（joint 档案）；eef_delta 档案下
  agent = 持有（丢动作、不发 action → benchmark 零仿真时间冻结）;
- relay the benchmark's PolicyCommand to lerobot_infer（libero 档案；
  episode 生命周期归 benchmark，bridge 只中继并在 eef 重建时插话）;
- keep lerobot_infer's lifecycle in step with the mux (vla.set_mode ->
  PolicyCommand start/pause) and expose vla.get_status.
"""

from __future__ import annotations

import argparse
import math
import sys
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow as pa
from forge_common import get_logger
from forge_msgs import JointCommand, JointState, PolicyCommand, PolicyCommandStatus

from .config import VlaBridgeConfig, load_bridge_config
from .node_runtime import TOOL_INPUT_ID, ToolEndpointRuntime
from .service import BridgeState, new_request_id
from .tools import create_bridge_binding, create_bridge_descriptor

logger = get_logger(__name__)

PROPRIO_INPUT_ID = "proprio_state"
POLICY_ACTION_INPUT_ID = "policy_action"
POLICY_STATUS_INPUT_ID = "policy_status"
AGENT_ARM_INPUT_ID = "agent_arm_command"
AGENT_GRIPPER_INPUT_ID = "agent_gripper_command"
BENCHMARK_COMMAND_INPUT_ID = "benchmark_command"
TIMER_INPUT_ID = "tick"

ARM_COMMAND_OUTPUT_ID = "arm_command"
GRIPPER_COMMAND_OUTPUT_ID = "gripper_command"
ACTION_OUTPUT_ID = "action"
POLICY_COMMAND_OUTPUT_ID = "policy_command"
POLICY_PROPRIO_OUTPUT_ID = "policy_proprio_state"


def _from_arrow(model: Any, value: object, label: str):
    if not isinstance(value, (pa.RecordBatch, pa.Table, pa.StructArray)):
        raise TypeError(f"{label} requires an in-memory Arrow carrier")
    return model.from_arrow(value)


class VlaBridgeNode:
    def __init__(self, config: VlaBridgeConfig, node: Any) -> None:
        self._config = config
        self._node = node
        # ONE process identity for the request validator (binding handler)
        # and the gateway registration (announcer): mismatched ids make every
        # dispatched query fail with FORGE_PROTOCOL_ROUTE_MISMATCH.
        self._endpoint_instance_id = f"vla_bridge-{uuid.uuid4().hex[:12]}"
        self.state = BridgeState(
            policy_id=config.policy_id,
            initial_mode=config.initial_mode,
            policy_command_sender=self._send_policy_command,
            proprio_gate=config.proprio_gate,
            max_segment_steps=config.max_segment_steps,
            max_eef_segment_steps=config.max_eef_segment_steps,
            edit_max_pos_units=config.edit_max_pos_units,
            edit_max_rot_units=config.edit_max_rot_units,
            # 同一组界的物理量表示：请求与报错说米/弧度，判据与记账说动作单位。
            edit_max_pos_m=config.edit_max_pos_m,
            edit_max_rot_rad=config.edit_max_rot_rad,
            max_cumulative_edit_pos_m=config.max_cumulative_edit_pos_m,
            edit_ramp_steps=config.edit_ramp_steps,
            # eef 档（S4c）：每步单位必须与 robosuite OSC_POSE 的 output_max 一致
            # （5 cm / 0.5 rad），到达判据与可达性粗筛见 config 的注释。
            eef_pos_unit_m=config.eef_pos_unit_m,
            eef_rot_unit_rad=config.eef_rot_unit_rad,
            eef_pos_tolerance_m=config.eef_pos_tolerance_m,
            eef_max_reach_m=config.eef_max_reach_m,
            eef_min_z_m=config.eef_min_z_m,
            rtc_merge_period_steps=config.rtc_merge_period_steps,
            max_empty_tick_streak=config.max_empty_tick_streak,
            stall_window_steps=config.stall_window_steps,
            stall_min_displacement_m=config.stall_min_displacement_m,
            starvation_timeout_ms=config.starvation_timeout_ms,
            max_invalid_actions_per_segment=config.max_invalid_actions_per_segment,
            max_cumulative_edit_pos_units=config.max_cumulative_edit_pos_units,
            checkpoints_enabled=config.checkpoints_enabled,
            # 动作账本只对 eef_delta 档案开（piper 的 get_status 契约逐字不变）
            track_action_stats=config.is_eef_delta,
            report_mode=config.report_mode,
            # set_mode 的入参词由 profile 的 mode_vocabulary 定（legacy → policy/agent）
            accepted_modes=config.accepted_modes,
            joint_abs_labels=config.arm_joints if config.is_joint_abs else None,
            gripper_indices=config.gripper_indices,
            edit_max_joint_rad=config.edit_max_joint_rad,
            max_cumulative_edit_joint_rad=config.max_cumulative_edit_joint_rad,
            hold_on_episode_start=config.hold_on_episode_start,
            detect_on_released_segments=config.detect_on_released_segments,
            c2_cooldown_steps=config.c2_cooldown_steps,
            stall_min_joint_rad=config.stall_min_joint_rad,
        )
        self._binding = create_bridge_binding(
            self.state,
            endpoint_id=config.endpoint_id,
            max_proprio_age_ms=config.max_proprio_age_ms,
            endpoint_instance_id=self._endpoint_instance_id,
        )
        self._runtime = ToolEndpointRuntime(
            node,
            self._binding,
            registration=config.registration,
            tag="vla_bridge",
            endpoint_instance_id=self._endpoint_instance_id,
        )
        # freshest proprio, name -> position (sim units: rad, m)
        self._proprio: dict[str, float] = {}
        self._warned_missing_joints: set[str] = set()
        # joint_abs（robotwin）：策略节点是**事件驱动**的（一帧观测回一条动作），不像
        # lerobot_infer 按 tick 连续出动作。检查点持有期间丢掉的那条动作若不保留，恢复
        # 放行后没有新观测就没有新动作 → benchmark 永远等动作 → 死锁。所以持有时锁存
        # 最后一条动作，放行时按同一整形路径补发（它正对应 benchmark 在等的那一帧观测）。
        self._latched_action: object | None = None

    # -- policy lifecycle -----------------------------------------------------

    def _send_policy_command(self, verb: str) -> str:
        request_id = new_request_id()
        command = PolicyCommand.from_inputs(
            policy_id=self._config.policy_id,
            command=verb,
            request_id=request_id,
        )
        self._node.send_output(POLICY_COMMAND_OUTPUT_ID, command.to_arrow())
        logger.info("[vla_bridge] policy_command %s request_id=%s", verb, request_id)
        return request_id

    # -- proprio relay ----------------------------------------------------------

    def _handle_proprio(self, value: object) -> None:
        try:
            state = _from_arrow(JointState, value, "proprio_state")
        except (TypeError, ValueError) as exc:
            logger.warning("[vla_bridge] dropped proprio_state: %s", exc)
            return
        self._proprio = dict(zip(state.name, (float(v) for v in state.position)))
        self.state.bump("proprio_relayed")

        # Gripper unit conversion for the policy side (m -> mm by default).
        names = list(state.name)
        positions = [float(v) for v in state.position]
        gripper = self._config.gripper_joint
        if gripper in names:
            positions[names.index(gripper)] *= self._config.gripper_policy_scale
        # eef_delta 档案：8 维 proprio 的语义是 eef_pos(3)+axis_angle(3)+gripper_qpos(2)
        # （位置是契约，见 libero/config.py）。前 3 位是 TCP（C2 失速判据用），
        # 3:6 是轴角（eef 段算姿态误差用）——两段都要喂，缺一段 eef 段就退化成
        # 零增量保持（见 service._shape_eef_action）。
        if self._config.is_joint_abs:
            by_name = dict(zip(names, positions))
            if all(label in by_name for label in self._config.arm_joints):
                self.state.observe_proprio(joint_positions=[by_name[l] for l in self._config.arm_joints])
            else:
                self.state.observe_proprio()
        elif self._config.is_eef_delta and len(positions) >= 8:
            self.state.observe_proprio(
                eef_pos=positions[0:3],
                eef_rotvec=positions[3:6],
                gripper_qpos=positions[-2:],
            )
        else:
            self.state.observe_proprio()
        relayed = JointState(
            name=names,
            position=positions,
            velocity=[float(v) for v in state.velocity],
            effort=[float(v) for v in state.effort],
        )
        self._node.send_output(POLICY_PROPRIO_OUTPUT_ID, relayed.to_arrow())

    def _handle_policy_status(self, value: object) -> None:
        try:
            status = _from_arrow(PolicyCommandStatus, value, "policy_command_status")
        except (TypeError, ValueError) as exc:
            logger.warning("[vla_bridge] dropped policy_command_status: %s", exc)
            return
        self.state.observe_policy_status(
            {
                "command": status.command,
                "request_id": status.request_id,
                "status": status.status,
                "message": status.message,
            }
        )

    # -- POLICY mode action path --------------------------------------------------

    def _note_invalid(self) -> None:
        self.state.bump("policy_actions_dropped_invalid")
        self.state.note_invalid_action()  # 监督段内的连续非法动作是 C2 事件

    def _handle_policy_action(self, value: object) -> None:
        self.state.bump("policy_actions_received")
        if self._config.is_joint_abs:
            self._observe_candidate(value)
        if not self.state.forwarding:  # stop/hold 一律丢（libero 侧因此零仿真时间）
            self.state.bump("policy_actions_dropped_mode")
            if self._config.is_joint_abs:
                self._latched_action = value
                self.state.bump("policy_actions_latched")
            return
        if self._config.single_action_output:
            self._handle_policy_action_delta(value)
            return
        try:
            command = _from_arrow(JointCommand, value, "policy_action")
        except (TypeError, ValueError) as exc:
            logger.warning("[vla_bridge] dropped policy action: %s", exc)
            self._note_invalid()
            return
        by_name = dict(zip(command.name, (float(v) for v in command.position)))

        missing = [j for j in self._config.arm_joints + [self._config.gripper_joint] if j not in by_name]
        if missing:
            newly = set(missing) - self._warned_missing_joints
            if newly:
                logger.warning("[vla_bridge] policy action missing joints %s; dropped", sorted(newly))
                self._warned_missing_joints |= newly
            self._note_invalid()
            return
        if any(not math.isfinite(v) for v in by_name.values()):
            self._note_invalid()
            return

        # Freshness gate: without live proprio the delta clamp has no anchor,
        # so a stale stream must not reach the robot.
        if not self.state.status(self._config.max_proprio_age_ms)["proprio_fresh"]:
            self.state.bump("policy_actions_dropped_stale")
            return

        arm_names: list[str] = []
        arm_positions: list[float] = []
        for joint in self._config.arm_joints:
            lo, hi = self._config.arm_limits[joint]
            target = by_name[joint]
            clamped = min(max(target, lo), hi)
            if clamped != target:
                self.state.bump("arm_values_clamped")
            # Rate-limit against live proprio so a misbehaving chunk cannot
            # command a jump larger than max_joint_step_rad in one message.
            current = self._proprio.get(joint)
            if current is not None:
                span = self._config.max_joint_step_rad
                bounded = min(max(clamped, current - span), current + span)
                if bounded != clamped:
                    clamped = bounded
                    self.state.bump("arm_values_clamped")
            arm_names.append(joint)
            arm_positions.append(clamped)

        lo, hi = self._config.gripper_limits
        raw = by_name[self._config.gripper_joint] / self._config.gripper_policy_scale
        gripper = min(max(raw, lo), hi)
        if gripper != raw:
            self.state.bump("gripper_values_clamped")

        self._node.send_output(
            ARM_COMMAND_OUTPUT_ID,
            JointCommand(name=arm_names, position=arm_positions).to_arrow(),
        )
        self._node.send_output(
            GRIPPER_COMMAND_OUTPUT_ID,
            JointCommand(name=[self._config.gripper_joint], position=[gripper]).to_arrow(),
        )
        self.state.bump("policy_actions_forwarded")
        self.state.note_forwarded()
        self.state.mark_candidate_forwarded()
        self._log_action_counters()

    def _observe_candidate(self, value: object) -> None:
        """Capture a valid pi0.5 joint-abs action before the hold/forward branch."""
        try:
            command = _from_arrow(JointCommand, value, "policy_action")
        except (TypeError, ValueError):
            return
        by_name = dict(zip(command.name, (float(v) for v in command.position)))
        labels = self._config.arm_joints
        if any(label not in by_name for label in labels):
            return
        values = [by_name[label] for label in labels]
        if any(not math.isfinite(value) for value in values):
            return
        self.state.observe_candidate(
            labels=labels,
            values=values,
            gripper_indices=self._config.gripper_indices,
        )

    def _log_action_counters(self) -> None:
        received = self.state.counter("policy_actions_received")
        if received % self._config.counters_log_every == 0:
            logger.info(
                "[vla_bridge] policy actions received=%d forwarded=%d",
                received,
                self.state.counter("policy_actions_forwarded"),
            )

    def _handle_policy_action_delta(self, value: object) -> None:
        """eef_delta：robosuite 原生 7 维增量，student 通道原样直通。

        这里刻意**不做任何限位/限幅/换算**：动作语义属于下游 OSC_POSE 控制器
        （1 单位 = 5 cm / 0.5 rad，控制器内部还有自己的夹取）。改写学生动作会让
        基线对照失去意义；5 cm / 0.35 rad 的上限只约束 VLM 的 edit 偏置。

        edit 偏置加在**输出侧**（下游）而不是让策略侧改候选：RTC 每个 merge 周期
        会整体替换候选队列，加在上游会被融合覆盖（S4b 的既定规则）。
        """
        try:
            command = _from_arrow(JointCommand, value, "policy_action")
        except (TypeError, ValueError) as exc:
            logger.warning("[vla_bridge] dropped policy action: %s", exc)
            self._note_invalid()
            return

        labels = self._config.arm_joints
        by_name = dict(zip(command.name, (float(v) for v in command.position)))
        missing = [label for label in labels if label not in by_name]
        if missing:
            newly = set(missing) - self._warned_missing_joints
            if newly:
                logger.warning("[vla_bridge] policy action missing dims %s; dropped", sorted(newly))
                self._warned_missing_joints |= newly
            self._note_invalid()
            return
        values = [by_name[label] for label in labels]
        if any(not math.isfinite(v) for v in values):
            self._note_invalid()
            return

        # 看门狗：本档案的判据是「收到过 proprio」（proprio_gate=seen），不是年龄。
        # libero 的观测与仿真步 1:1，环境在两条 action 之间不动，年龄不是安全信号；
        # 用年龄会连第一步都丢掉（首帧推理 600ms+ > 400ms 阈值）→ 死锁。
        if not self.state.status(self._config.max_proprio_age_ms)["proprio_fresh"]:
            self.state.bump("policy_actions_dropped_stale")
            return

        shaped, rewritten = self.state.shape_action(values)
        self._node.send_output(
            ACTION_OUTPUT_ID,
            JointCommand(name=labels, position=shaped).to_arrow(),
        )
        self.state.observe_action(shaped)  # 账本记**执行值**（含 edit 偏置）
        self.state.bump("policy_actions_forwarded")
        self.state.note_forwarded()
        if rewritten:
            # edit = 叠加学生动作；eef = **整段替换**学生动作（日志要能分辨，
            # 否则事后回放会以为 eef 段跑的还是学生流）
            logger.info(
                "[vla_bridge] %s applied: %s -> %s",
                "eef delta" if self.state.mode == "eef" else "edit bias",
                [round(v, 4) for v in values],
                [round(v, 4) for v in shaped],
            )
        self._log_action_counters()

    # -- benchmark policy_command relay (libero) ---------------------------------

    def _handle_benchmark_command(self, value: object) -> None:
        """benchmark → 策略节点的 PolicyCommand 中继（episode 生命周期归 benchmark）。

        bridge 仍是 policy_command 的**单一写者**：只有它能向策略节点发命令，
        benchmark 的命令经此中继（eef 段后的 pause→reset→resume 也走同一出口，
        顺序由事件循环天然串行保证）。
        """
        try:
            command = _from_arrow(PolicyCommand, value, "benchmark_command")
        except (TypeError, ValueError) as exc:
            logger.warning("[vla_bridge] dropped benchmark policy_command: %s", exc)
            return
        self._node.send_output(POLICY_COMMAND_OUTPUT_ID, value)
        self.state.bump("benchmark_commands_relayed")
        # episode 边界：reset_scene = 新 episode（候选队列已被策略侧清空，记账归零；
        # hold 中开 C0 检查点）；stop = 批次结束（段与检查点收尾）。
        note = ""
        if command.command in ("reset_scene", "stop", "set_instruction"):
            # 锁存动作属于上一帧观测；episode 边界后作废
            self._latched_action = None
            self.state.clear_candidate()
        if command.command == "reset_scene":
            outcome = self.state.reset_episode()
            note = (
                f" episode={outcome['episode_index']} supervised={outcome['supervised']}"
                f" checkpoint={(outcome['checkpoint'] or {}).get('checkpoint_id')}"
            )
        elif command.command == "stop":
            self.state.end_episode()
            note = " episode_ended"
        logger.info(
            "[vla_bridge] relayed benchmark policy_command=%s policy_id=%s%s",
            command.command,
            command.policy_id,
            note,
        )

    # -- AGENT mode passthrough ------------------------------------------------

    def _handle_agent_arm(self, value: object) -> None:
        if not self.state.holding:
            self.state.bump("agent_arm_dropped_mode")
            return
        self._node.send_output(ARM_COMMAND_OUTPUT_ID, value)
        self.state.bump("agent_arm_forwarded")

    def _handle_agent_gripper(self, value: object) -> None:
        if not self.state.holding:
            self.state.bump("agent_gripper_dropped_mode")
            return
        self._node.send_output(GRIPPER_COMMAND_OUTPUT_ID, value)
        self.state.bump("agent_gripper_forwarded")

    def _maybe_replay_latched(self) -> None:
        if self._latched_action is None or not self.state.forwarding:
            return
        value, self._latched_action = self._latched_action, None
        self.state.bump("policy_actions_replayed")
        self._handle_policy_action(value)

    # -- event loop ---------------------------------------------------------------

    def process_event(self, event: dict[str, Any]) -> int | None:
        event_type = event.get("type")
        if event_type == "INPUT":
            self._runtime.drive_lease()
            input_id = event.get("id")
            value = event.get("value")
            if input_id == PROPRIO_INPUT_ID:
                self._handle_proprio(value)
            elif input_id == POLICY_ACTION_INPUT_ID:
                self._handle_policy_action(value)
            elif input_id == POLICY_STATUS_INPUT_ID:
                self._handle_policy_status(value)
            elif input_id == BENCHMARK_COMMAND_INPUT_ID:
                self._handle_benchmark_command(value)
            elif input_id == AGENT_ARM_INPUT_ID:
                self._handle_agent_arm(value)
            elif input_id == AGENT_GRIPPER_INPUT_ID:
                self._handle_agent_gripper(value)
            elif input_id == TOOL_INPUT_ID:
                self._runtime.handle_tool_input(value)
                self._maybe_replay_latched()
            elif input_id == TIMER_INPUT_ID:
                # timer 只用于 C2 的策略停摆判据（步进门控下没有"空拍"可数，
                # 只能看墙钟）；piper 档案的 dataflow 里没有这个输入。
                self.state.note_tick()
                self._maybe_replay_latched()
            return None
        if event_type == "STOP":
            self._runtime.shutdown()
            return 0
        if event_type == "ERROR":
            logger.error("[vla_bridge] dora error: %s", event.get("error", "unknown"))
            self._runtime.shutdown()
            return 1
        return None

    def run(self, events: Iterable[dict[str, Any]]) -> int:
        self._runtime.drive_lease()
        for event in events:
            code = self.process_event(event)
            if code is not None:
                return code
        self._runtime.shutdown()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vla_bridge: policy/agent action mux ToolEndpoint node")
    parser.add_argument("--config", required=True, help="path to the vla_bridge YAML config")
    args = parser.parse_args(argv)

    try:
        config = load_bridge_config(Path(args.config))
    except Exception as exc:
        print(f"vla_bridge: invalid config: {exc}", file=sys.stderr)
        return 2

    logger.info(
        "[vla_bridge] started endpoint=%s action_space=%s initial_mode=%s policy_id=%s arm=%s gripper=%s "
        "checkpoints=%s mode_vocabulary=%s",
        config.endpoint_id,
        config.action_space,
        config.initial_mode,
        config.policy_id,
        ",".join(config.arm_joints),
        config.gripper_joint,
        config.checkpoints,
        config.mode_vocabulary,
    )

    from dora import Node

    node = Node()
    runner = VlaBridgeNode(config, node)
    descriptor = create_bridge_descriptor(config.endpoint_id)
    outputs = (
        f"{ACTION_OUTPUT_ID}"
        if config.single_action_output
        else f"{ARM_COMMAND_OUTPUT_ID},{GRIPPER_COMMAND_OUTPUT_ID}"
    )
    logger.info(
        "[vla_bridge] operations=%s outputs=%s,%s,%s",
        ",".join(operation.name for operation in descriptor.operations),
        outputs,
        POLICY_COMMAND_OUTPUT_ID,
        POLICY_PROPRIO_OUTPUT_ID,
    )
    return runner.run(node)


__all__ = ["VlaBridgeNode", "main"]

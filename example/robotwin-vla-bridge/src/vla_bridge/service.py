"""Mutable bridge state shared by the mux loop (main) and the tool queries.

dora runs single-threaded and the asyncio.Runner in ToolEndpointRuntime runs
query handlers on that same thread, so plain attribute access is safe.

状态机（S4a）：决策模式 student/edit/eef/stop + 段记账 + 检查点。
- 段（segment）= 一次决策授权的执行区间（`steps` 步），走完自动冻结并开检查点（C1）；
- stop = hold：不下发动作 → libero 侧零仿真时间（观测与步 1:1），策略节点同时 pause；
- C2 事件（策略停摆 / 失速 / 连续非法动作）由本模块的确定性计数器判定，命中即冻结 → 开检查点；
- 快照暴露桥接器实际观测到的最新候选动作；candidate_id / queue_depth 仍然在消息
  契约里拿不到（策略节点每 tick 只发一条无元数据的 action，节点是二进制的、不改），
  因此 `candidate_epoch_est` 仍只是步数推算，并在 observation_note 里明确标注。

检查点纪律只对 **decide() 起头的段**（supervised）生效：`set_mode` 起头的自发透传
（piper 档案、S3 学生基线）不检测、不冻结 —— 保证基线可复现。
"""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import (
    ACCEPTED_MODES_LEGACY,
    DECISION_EDIT,
    DECISION_EEF,
    DECISION_MODES,
    DECISION_STOP,
    DECISION_STUDENT,
    FORWARDING_MODES,
    MODE_ALIASES,
)

Mode = str  # "student" | "edit" | "eef" | "stop"；兼容别名 policy/agent
PolicyCommandSender = Callable[[str], str]
"""Send one PolicyCommand verb ("start"/"pause") to lerobot_infer; returns request_id."""

TRIGGER_EPISODE_START = "C0"
TRIGGER_SEGMENT_END = "C1"
TRIGGER_EVENT = "C2"
TRIGGER_AGENT = "C3"

REASON_SEGMENT_END = "segment_budget_exhausted"
REASON_POLICY_STARVED = "policy_starved"
REASON_NO_PROGRESS = "no_progress"
REASON_INVALID_ACTIONS = "invalid_actions"
REASON_EDIT_DRIFT = "edit_drift"
REASON_AGENT_STOP = "agent_stop"

# LIBERO / robosuite 夹爪动作槽：-1 = 张开，+1 = 闭合。两处源码互证：
#   robosuite models/grippers/panda_gripper.py:46  "-1 => open, 1 => closed"
#   libero adapters/libero_adapter.py:35           预热空动作末位 -1.0 = 保持张开
# （π0.5 v044 冷启动输出第 7 位 ≈ −1.006，即张开；此前的设计文档把它记成
#  「闭合约定」是误读，以源码为准。）
GRIPPER_ACTION_OPEN = -1.0
GRIPPER_ACTION_CLOSED = 1.0

_VALID_GRIPPER_HINTS = ("keep", "open", "closed")

_COUNTER_KEYS = (
    "policy_actions_received",
    "policy_actions_forwarded",
    "policy_actions_dropped_mode",
    "policy_actions_dropped_stale",
    "policy_actions_dropped_invalid",
    "arm_values_clamped",
    "gripper_values_clamped",
    "agent_arm_forwarded",
    "agent_arm_dropped_mode",
    "agent_gripper_forwarded",
    "agent_gripper_dropped_mode",
    "proprio_relayed",
    "policy_commands_sent",
    # libero 档案：benchmark 的 episode 生命周期命令经 bridge 中继的次数
    "benchmark_commands_relayed",
    # 检查点仲裁
    "segments_started",
    "segments_completed",
    "segments_aborted",
    "checkpoints_opened",
    "checkpoints_closed",
    "decisions_applied",
    "decisions_rejected",
    "c1_events",
    "c2_events",
    "episodes_seen",
    "episodes_ended",
    "edit_actions_biased",
    "eef_actions_generated",
    # eef 段（S4c）：段数 / 清候选次数。清候选 = eef 段结束后向策略节点发 reset_scene
    # （policy.reset() 清掉 sync 队列里那几条用**段前**观测算出来的陈旧候选）。
    "eef_segments_started",
    "candidate_resets",
    "candidate_actions_observed",
    "candidate_actions_forwarded",
    # eef 段里到达目标位置的段数（到达后仍按 `gripper_closed` 持续下发夹爪指令，
    # 姿态误差则继续收敛到段预算用完；见 eef_delta_to_target）
    "eef_targets_reached",
    # eef 段里**缺 proprio** 而算不出增量的步数（防御分支，正常为 0）。这几步下发
    # 零增量（保持当前位姿）而不是学生的动作 —— 见 shape_action 的说明。
    "eef_actions_without_proprio",
)

# eef 段结束后必须作废策略侧候选：那段动作绕过了策略，队列里剩下的几条是用**段前**
# 观测算出来的，接着执行 = 把机械臂往旧前提上带。
# 用 `reset_scene` 而不是 `reset`：两者在策略节点里走同一条代码路径
# （forge_policy/control.py: `_call_optional(policy, "reset")` + 清观测缓存），
# 但 `reset_scene` 是本栈**已实证可用**的那个（S1/S3 的逐 episode 复位一直走它），
# 不赌 PyInstaller 二进制里 verb 集合的版本差异。
CANDIDATE_RESET_COMMAND = "reset_scene"

# eef 的到达判据：与目标位置差小于此值即视为到位（此后发零增量 + 夹爪指令）。
DEFAULT_EEF_POS_TOLERANCE_M = 0.01
# eef 目标的可达性**粗筛**（不是 IK）：目标离当前 TCP 超过此距离直接拒。bridge 里
# 没有运动学模型（P0 §1 已判定 libero 档案不挂 motion 链），真正的工作空间校验由
# VLM 侧负责；这里只拦"明显不可能"的目标，避免生成一串朝墙撞的增量。
DEFAULT_EEF_MAX_REACH_M = 1.0
# 目标高度下限（m，world 系）：libero 的桌面在 z≈0，低于桌面 5 cm 的目标必然是错的。
DEFAULT_EEF_MIN_Z_M = -0.05


class DecisionRejected(Exception):
    """决策在**执行前**被拒（schema / 限幅 / 状态不合法）。

    S4 的分工：这里是执行前拒绝 → 工具层翻成 ToolEndpointError（可重试，agent
    可以改好再发一次）；执行期的失败才用 ToolResult(status="failed")。
    """

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details)


def resolve_mode(name: str) -> str:
    """把对外用词（含 legacy 别名）翻成内部规范模式。"""
    if name in DECISION_MODES:
        return name
    alias = MODE_ALIASES.get(name)
    if alias is None:
        raise DecisionRejected(
            "VLA_INVALID_MODE",
            f"mode must be one of {list(DECISION_MODES)} (aliases: {sorted(MODE_ALIASES)})",
            mode=name,
        )
    return alias


def _empty_action_stats() -> dict[str, Any]:
    """eef_delta 动作账本（每 episode 归零）。

    用于报告里的「动作量级 / 饱和率 / 夹爪占空比」三行：libero 档案的 student 通道
    不做任何限幅，所以**饱和只能靠观测**（|a| > 1 即越出归一化单位盒，robosuite
    OSC 会照比例放大到 5 cm/0.5 rad 之外）。
    """
    return {
        "steps": 0,
        "abs_sum": [0.0] * 7,
        "abs_max": [0.0] * 7,
        "mean_abs": [0.0] * 7,
        "overflow_steps": 0,  # 前 6 位里有任何一位 |a| > 1
        "gripper_closed_steps": 0,  # 第 7 位 > 0（robosuite: 正 = 闭合）
        "gripper_open_steps": 0,  # 第 7 位 < 0
    }


# -- eef 目标 → 增量（四元数/旋转向量，纯 math，不引 numpy） ---------------------


def _quat_from_rotvec(rotvec: list[float] | tuple[float, float, float]) -> tuple[float, float, float, float]:
    """旋转向量（Rodrigues，轴角）→ 四元数 (w, x, y, z)。"""
    theta = math.sqrt(sum(v * v for v in rotvec))
    if theta < 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    half = theta / 2.0
    scale = math.sin(half) / theta
    return (math.cos(half), rotvec[0] * scale, rotvec[1] * scale, rotvec[2] * scale)


def _quat_mul(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _quat_conj(q: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (q[0], -q[1], -q[2], -q[3])


def _rotvec_from_quat(q: tuple[float, float, float, float]) -> list[float]:
    """四元数 → 旋转向量，取**最短路径**（w < 0 时整体取反，否则会绕远路 2π−θ）。"""
    w, x, y, z = q
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm < 1e-12:
        return [0.0, 0.0, 0.0]
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    if w < 0.0:
        w, x, y, z = -w, -x, -y, -z
    s = math.sqrt(x * x + y * y + z * z)
    if s < 1e-12:
        return [0.0, 0.0, 0.0]
    scale = 2.0 * math.atan2(s, w) / s
    return [x * scale, y * scale, z * scale]


def eef_delta_to_target(
    *,
    target_position: list[float],
    target_quaternion_wxyz: list[float],
    eef_pos: list[float],
    eef_rotvec: list[float],
    pos_unit_m: float,
    rot_unit_rad: float,
    tolerance_m: float,
    gripper_closed: bool,
) -> tuple[list[float], float, bool]:
    """朝目标位姿的**一步**增量（robosuite OSC 归一化单位，逐轴 clip 到 [-1, 1]）。

    为什么是纯算术而不是 IK：libero 的 proprio 第 1 段就是 `eef_pos(3)`、第 2 段是
    `axis_angle(3)`（`libero/config.py` 的契约），动作又恰好是笛卡尔增量，所以
    「到目标的增量」= 位姿误差 ÷ 每步单位，不需要运动学（P0 §1 的结论）。

    返回 `(delta7, position_error_m, arrived)`：
      - `delta7` = [dx, dy, dz, drx, dry, drz, gripper]；到位后前 6 位为 0，
        夹爪仍按 `gripper_closed` 持续下发（夹爪是位置伺服，需要保持指令）；
      - `arrived` 只按**位置**判（`position_error_m <= tolerance_m`），姿态误差
        仍会继续收敛到段预算用完为止（姿态伺服慢，一次到位不现实）。
    """
    error = [target_position[axis] - eef_pos[axis] for axis in range(3)]
    position_error_m = math.sqrt(sum(v * v for v in error))
    arrived = position_error_m <= tolerance_m
    if arrived:
        delta_pos = [0.0, 0.0, 0.0]
    else:
        delta_pos = [max(-1.0, min(1.0, v / pos_unit_m)) for v in error]
    # 姿态误差：R_target · R_current⁻¹，再取最短路径旋转向量。
    q_target = (target_quaternion_wxyz[0], target_quaternion_wxyz[1], target_quaternion_wxyz[2], target_quaternion_wxyz[3])
    q_error = _quat_mul(q_target, _quat_conj(_quat_from_rotvec(eef_rotvec)))
    rotvec = _rotvec_from_quat(q_error)
    delta_rot = [max(-1.0, min(1.0, v / rot_unit_rad)) for v in rotvec]
    gripper = GRIPPER_ACTION_CLOSED if gripper_closed else GRIPPER_ACTION_OPEN
    return [*delta_pos, *delta_rot, gripper], position_error_m, arrived


@dataclass
class Segment:
    """一次决策授权的执行区间。"""

    index: int
    mode: str
    steps_budget: int  # 0 = 无预算（set_mode 起头的自发透传）
    decision: dict[str, Any] = field(default_factory=dict)
    supervised: bool = False  # 由 decide() 起头 → C1/C2 生效
    steps_executed: int = 0
    started_at: float = 0.0
    start_tcp: list[float] | None = None
    invalid_actions: int = 0
    edit_applied: int = 0
    # edit 段的逐步偏置（前 m 步各施加一次，累计 = 决策给的 delta）
    edit_step_bias: list[float] = field(default_factory=list)
    edit_ramp_steps: int = 0
    # edit 的夹爪提示落到动作第 7 位（keep → None，不改）
    eef_gripper: float | None = None
    # eef 段的目标（规范化后的 {position, quaternion_wxyz, gripper_closed}）；该段的
    # 动作**整段替换**策略输出（不叠加），逐步朝目标收敛。
    eef_target: dict[str, Any] | None = None
    eef_arrived: bool = False
    eef_applied: int = 0  # 本段由 bridge 生成的下发步数（accounted=动作写者）
    # 段末按最新 proprio 复核的位置误差（米）——VLM 靠它判断"还差多远、要不要再补一段"
    eef_arrival_error_m: float | None = None
    # joint_abs 的 edit：每关节目标偏移（rad，段内前 ramp 步线性爬升、之后保持到段末）
    edit_joint_delta: list[float] = field(default_factory=list)
    edit_gripper_values: dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "mode": self.mode,
            "supervised": self.supervised,
            "steps_budget": self.steps_budget,
            "steps_executed": self.steps_executed,
            "steps_remaining": max(0, self.steps_budget - self.steps_executed) if self.steps_budget else None,
            "invalid_actions": self.invalid_actions,
            "edit_applied": self.edit_applied,
            "eef_applied": self.eef_applied,
            "eef_arrived": self.eef_arrived,
            "eef_arrival_error_m": self.eef_arrival_error_m,
            "decision": dict(self.decision),
            "start_tcp": list(self.start_tcp) if self.start_tcp else None,
            "elapsed_s": round(time.monotonic() - self.started_at, 3) if self.started_at else None,
        }


class BridgeState:
    """Everything vla.get_status reports, vla.set_mode / vla.decide flips."""

    def __init__(
        self,
        *,
        policy_id: str,
        initial_mode: Mode,
        policy_command_sender: PolicyCommandSender,
        proprio_gate: str = "age",
        max_segment_steps: int = 15,
        max_eef_segment_steps: int = 5,
        edit_max_pos_units: float = 1.0,
        edit_max_rot_units: float = 0.7,
        edit_ramp_steps: int = 5,
        # 同一组界的**物理量**表示（米 / 弧度）：请求与报错都用物理量说人话，内部判据
        # 与记账走 units。两个都留着，报错里就能同时给出两套数（见 _validate_edit）。
        edit_max_pos_m: float = 0.05,
        edit_max_rot_rad: float = 0.35,
        max_cumulative_edit_pos_m: float = 0.05,
        # eef 档（S4c）：每步单位与到达判据；pos_unit/rot_unit 必须与 robosuite
        # OSC_POSE 的 output_max 一致（5 cm / 0.5 rad），否则增量换算错。
        eef_pos_unit_m: float = 0.05,
        eef_rot_unit_rad: float = 0.5,
        eef_pos_tolerance_m: float = DEFAULT_EEF_POS_TOLERANCE_M,
        eef_max_reach_m: float = DEFAULT_EEF_MAX_REACH_M,
        eef_min_z_m: float = DEFAULT_EEF_MIN_Z_M,
        rtc_merge_period_steps: int = 20,
        max_empty_tick_streak: int = 10,
        stall_window_steps: int = 40,
        stall_min_displacement_m: float = 0.01,
        starvation_timeout_ms: int = 10000,
        max_invalid_actions_per_segment: int = 5,
        max_cumulative_edit_pos_units: float = 1.0,
        checkpoints_enabled: bool = False,
        track_action_stats: bool = False,
        report_mode: Callable[[str], str] | None = None,
        accepted_modes: tuple[str, ...] = ACCEPTED_MODES_LEGACY,
        joint_abs_labels: list[str] | None = None,
        gripper_indices: tuple[int, ...] = (),
        edit_max_joint_rad: float = 0.1,
        max_cumulative_edit_joint_rad: float = 0.3,
        hold_on_episode_start: bool = False,
        detect_on_released_segments: bool = False,
        c2_cooldown_steps: int = 0,
        stall_min_joint_rad: float | None = None,
    ) -> None:
        self.hold_on_episode_start = bool(hold_on_episode_start)
        # 放行后仍然检测（默认关，保持 piper / S3 学生基线的逐字行为）：
        # `set_mode(student)` 是「把本集交给策略自己跑完」的出口，但它不该同时是
        # 「从此不再看它」——episode 长到 800~1200 步时，前 50 步的 5 次决策等于
        # 完全没监督。开启后，自发透传段仍跑 C2 判据，命中即冻结并开检查点。
        self.detect_on_released_segments = bool(detect_on_released_segments)
        # C2 冷却：停摆是持续状态，不设冷却会在「C2→decide(student)→1 步→C2」之间
        # 空转，把 VLM 的回合预算烧光。按 episode 步数（不是墙钟）计。
        self.c2_cooldown_steps = int(c2_cooldown_steps)
        self._last_c2_step = -1
        # joint_abs 的失速阈值：窗口里放的是**臂关节角（rad）**，拿米制阈值比是量纲
        # 错配。None = 沿用 stall_min_displacement_m（旧行为）。
        self.stall_min_joint_rad = None if stall_min_joint_rad is None else float(stall_min_joint_rad)
        # joint_abs（robotwin）：动作 = N 维绝对关节目标；None = 其余动作空间
        self.joint_abs_labels = list(joint_abs_labels) if joint_abs_labels else None
        self.gripper_indices = tuple(gripper_indices)
        self.edit_max_joint_rad = float(edit_max_joint_rad)
        self.max_cumulative_edit_joint_rad = float(max_cumulative_edit_joint_rad)
        self.cumulative_edit_joint_rad = [0.0] * len(self.joint_abs_labels or [])
        self.last_joint_positions: list[float] | None = None
        self._candidate: dict[str, Any] | None = None
        self.mode: Mode = resolve_mode(initial_mode)
        self.policy_id = policy_id
        # "age" = 过期即不可用（连续流仿真）；"seen" = 收到过即可用（步进门控仿真，
        # 见 config.PROPRIO_GATE_SEEN 的死锁说明）。
        self.proprio_gate = proprio_gate
        self._send_policy_command = policy_command_sender
        self._counters = {key: 0 for key in _COUNTER_KEYS}
        # lerobot_infer's last PolicyCommandStatus (phase tracking)
        self.last_policy_status: dict[str, Any] = {}
        self.last_policy_status_mono: float | None = None
        # freshest proprio snapshot (already relayed); age gates policy actions
        self.last_proprio_mono: float | None = None
        self.last_tcp_mono: float | None = None
        self.last_mode_switch_mono: float | None = None

        # -- 检查点仲裁 --------------------------------------------------------
        self.checkpoints_enabled = checkpoints_enabled
        self._report_mode = report_mode or (lambda mode: mode)
        # 该档案接受的对外模式词（默认 legacy = piper 冻结契约；libero 走 decision）。
        # 与 gateway ToolSpec 的 enum 同源，见 config.accepted_mode_words。
        self.accepted_modes = tuple(accepted_modes)
        self.max_segment_steps = max_segment_steps
        self.max_eef_segment_steps = max_eef_segment_steps
        self.edit_max_pos_units = edit_max_pos_units
        self.edit_max_rot_units = edit_max_rot_units
        self.edit_max_pos_m = edit_max_pos_m
        self.edit_max_rot_rad = edit_max_rot_rad
        self.max_cumulative_edit_pos_m = max_cumulative_edit_pos_m
        self.edit_ramp_steps = edit_ramp_steps
        self.eef_pos_unit_m = eef_pos_unit_m
        self.eef_rot_unit_rad = eef_rot_unit_rad
        self.eef_pos_tolerance_m = eef_pos_tolerance_m
        self.eef_max_reach_m = eef_max_reach_m
        self.eef_min_z_m = eef_min_z_m
        self.rtc_merge_period_steps = rtc_merge_period_steps
        self.max_empty_tick_streak = max_empty_tick_streak
        self.stall_window_steps = stall_window_steps
        self.stall_min_displacement_m = stall_min_displacement_m
        self.starvation_timeout_ms = starvation_timeout_ms
        self.max_invalid_actions_per_segment = max_invalid_actions_per_segment
        self.max_cumulative_edit_pos_units = max_cumulative_edit_pos_units

        self.segment: Segment | None = None
        self._segment_seq = 0
        self.supervised = False  # 本 episode 是否**曾**由 decide() 驱动（干预比例的分母）
        self.checkpoint: dict[str, Any] | None = None
        self._checkpoint_seq = 0
        self.checkpoint_history: list[dict[str, Any]] = []
        self.checkpoint_history_limit = 32
        self.last_decision: dict[str, Any] = {}
        # episode 记账：benchmark 的 reset_scene 归零
        self.episode_index = 0
        self.episode_steps = 0
        self.episode_started_mono: float | None = None
        self.empty_tick_streak = 0
        self.empty_ticks_since_action = 0
        self.last_action_mono: float | None = None
        self.cumulative_edit_units = [0.0, 0.0, 0.0]
        # 最近若干步的 TCP 位置（失速判据），来自 proprio 的 eef_pos(3)
        self._tcp_window: deque[tuple[float, float, float]] = deque(maxlen=stall_window_steps)
        self.last_eef_pos: list[float] | None = None
        self.last_eef_rotvec: list[float] | None = None
        self.last_gripper_qpos: list[float] | None = None
        # 动作量级账本（eef_delta 档案开启；S2 的评测口径：量级/饱和/夹爪占空比）。
        # 记的是**执行值**（edit 偏置之后的实际下发值）——bridge 是动作写者，
        # 账本要对得上真正进仿真的那串数。
        self.track_action_stats = track_action_stats
        self.action_stats: dict[str, Any] = _empty_action_stats()

    # -- counters ---------------------------------------------------------------

    def bump(self, key: str, amount: int = 1) -> None:
        self._counters[key] = self._counters.get(key, 0) + amount

    def counter(self, key: str) -> int:
        return self._counters.get(key, 0)

    @property
    def forwarding(self) -> bool:
        """当前是否放行动作（student/edit/eef）；stop 一律丢。"""
        return self.mode in FORWARDING_MODES

    @property
    def holding(self) -> bool:
        return not self.forwarding

    def observe_proprio(
        self,
        eef_pos: list[float] | None = None,
        gripper_qpos: list[float] | None = None,
        eef_rotvec: list[float] | None = None,
        joint_positions: list[float] | None = None,
    ) -> None:
        self.last_proprio_mono = time.monotonic()
        if joint_positions is not None:
            self.last_joint_positions = [float(v) for v in joint_positions]
        if eef_pos is not None:
            self.last_eef_pos = [float(v) for v in eef_pos[:3]]
            self.last_tcp_mono = self.last_proprio_mono
        if gripper_qpos is not None:
            self.last_gripper_qpos = [float(v) for v in gripper_qpos]
        # 姿态（axis_angle）：eef 档要它才能闭环收敛姿态。C2 的失速判据只看位置，
        # 所以此前没收；S4c 起必须收。
        if eef_rotvec is not None:
            self.last_eef_rotvec = [float(v) for v in eef_rotvec[:3]]

    def observe_policy_status(self, status: dict[str, Any]) -> None:
        self.last_policy_status = status
        self.last_policy_status_mono = time.monotonic()

    def observe_candidate(
        self,
        *,
        labels: list[str],
        values: list[float],
        gripper_indices: tuple[int, ...],
    ) -> None:
        """Capture the latest pi0.5 action before bridge forwarding/latching."""
        qpos = self.last_joint_positions
        delta = (
            [value - current for value, current in zip(values, qpos)]
            if qpos is not None and len(qpos) == len(values)
            else None
        )
        arm_delta = (
            [delta[i] for i in range(len(delta)) if i not in gripper_indices]
            if delta is not None
            else None
        )
        dominant_joints = (
            [
                {"joint": labels[i], "delta_rad": round(delta[i], 5)}
                for i in sorted(
                    (i for i in range(len(delta)) if i not in gripper_indices),
                    key=lambda i: abs(delta[i]),
                    reverse=True,
                )
                if abs(delta[i]) >= 1e-4
            ][:4]
            if delta is not None
            else []
        )
        gripper_targets = {
            "left_gripper": "open" if values[gripper_indices[0]] > 0.5 else "closed",
            "right_gripper": "open" if values[gripper_indices[-1]] > 0.5 else "closed",
        }
        self._candidate = {
            "available": True,
            "source": "pi0.5_policy_action",
            "action_space": "joint_abs",
            "observed_at_step": self.episode_steps,
            "joint_names": list(labels),
            "action": [round(value, 6) for value in values],
            "delta_from_qpos": None if delta is None else [round(value, 6) for value in delta],
            "arm_movement_norm_rad": (
                None if arm_delta is None else round(math.sqrt(sum(value * value for value in arm_delta)), 6)
            ),
            "dominant_joints": dominant_joints,
            "gripper_targets": gripper_targets,
            "full_future_chunk_visible": False,
            "observability_note": (
                "Only the latest observed JointCommand is captured. Read freshness to tell "
                "whether it is an unexecuted candidate or the last executed/historical action."
            ),
        }
        self.bump("candidate_actions_observed")

    def mark_candidate_forwarded(self) -> None:
        if self._candidate is not None:
            self._candidate["last_disposition"] = "forwarded"
            self.bump("candidate_actions_forwarded")

    def clear_candidate(self) -> None:
        self._candidate = None

    def _candidate_snapshot(self) -> dict[str, Any] | None:
        if self._candidate is None:
            return None
        candidate = dict(self._candidate)
        age_steps = max(0, self.episode_steps - int(candidate.get("observed_at_step", 0)))
        candidate["age_steps"] = age_steps
        candidate["last_disposition"] = candidate.get("last_disposition", "observed")
        candidate["freshness"] = "pending" if self.holding and age_steps == 0 else "historical"
        candidate["pending_execution"] = candidate["freshness"] == "pending"
        if candidate["freshness"] == "pending":
            candidate.pop("joint_names", None)
            return candidate
        return {
            "available": True,
            "action_space": candidate.get("action_space", "joint_abs"),
            "age_steps": age_steps,
            "freshness": "historical",
            "pending_execution": False,
            "arm_movement_norm_rad": candidate.get("arm_movement_norm_rad"),
            "dominant_joints": candidate.get("dominant_joints", []),
            "gripper_targets": candidate.get("gripper_targets"),
            "full_future_chunk_visible": False,
            "observability_note": "Historical compact summary only; raw action is available while pending.",
        }

    def observe_action(self, values: list[float]) -> None:
        """记一条**已下发**的 eef_delta 动作（量级/饱和/夹爪占空比）。"""
        if not self.track_action_stats:
            return
        stats = self.action_stats
        stats["steps"] += 1
        overflow = False
        for index in range(7):
            if index >= len(values):
                break
            magnitude = abs(float(values[index]))
            stats["abs_sum"][index] += magnitude
            stats["abs_max"][index] = max(stats["abs_max"][index], magnitude)
            if index < 6 and magnitude > 1.0:
                overflow = True
        if overflow:
            stats["overflow_steps"] += 1
        if len(values) >= 7:
            if float(values[6]) > 0.0:
                stats["gripper_closed_steps"] += 1
            elif float(values[6]) < 0.0:
                stats["gripper_open_steps"] += 1
        steps = stats["steps"]
        stats["mean_abs"] = [round(total / steps, 4) for total in stats["abs_sum"]]

    # -- mode / decisions ---------------------------------------------------------

    def set_mode(self, mode: Mode) -> dict[str, Any]:
        """Flip the mux; translate to a PolicyCommand lifecycle verb.

        这条路是**自发透传**（piper 档案、S3 学生基线）：不记段预算、不检测 C2。
        检查点纪律走 decide()。

        入参词按**本档案的词汇**校验（legacy 只认 policy/agent，decision 认四档 +
        两个别名）：piper 的"接受什么"与它 ToolSpec 的 enum 同源，不靠第二份白名单。
        """
        if mode not in self.accepted_modes:
            raise DecisionRejected(
                "VLA_INVALID_MODE",
                f"mode must be one of {list(self.accepted_modes)}",
                mode=mode,
            )
        canonical = resolve_mode(mode)
        if canonical == self.mode:
            request_id = self._send_policy_command(self._verb())
            return {
                "mode": self.report_mode(),
                "changed": False,
                "policy_command": self._verb(),
                "request_id": request_id,
            }
        self.mode = canonical
        self.last_mode_switch_mono = time.monotonic()
        if self.holding:
            # 冻结点：把进行中的自发段收尾，策略节点一起 pause（RTC 队列停排空）
            self._close_segment(completed=True)
        else:
            self._open_segment(mode=canonical, steps_budget=0, decision={}, supervised=False)
        # set_mode 是"放弃监督"的出口（与 decide 并列的第二种回答）：挂着的检查点必须
        # 在此收尾。否则 get_status 会一直报一个没人会回答的检查点，且下一次 C0/C1/C2
        # 会被 _open_checkpoint 的幂等分支吞掉（只 bump repeat_triggers）→ 报告陈旧快照。
        self._close_checkpoint(outcome=f"set_mode:{self.report_mode()}")
        verb = self._verb()
        request_id = self._send_policy_command(verb)
        self.bump("policy_commands_sent")
        return {
            "mode": self.report_mode(),
            "changed": True,
            "policy_command": verb,
            "request_id": request_id,
        }

    def decide(
        self,
        *,
        mode: Mode,
        steps: int | None = None,
        edit: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        reason: str = "agent",
        assessment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """检查点决策（student|edit|eef|stop）—— 唯一会开段的入口。

        执行前校验：越界/缺字段一律 DecisionRejected（工具层翻成可重试的结构化错误）。
        入参词同样按本档案的词汇校验（与 set_mode 同一白名单）：legacy 档案
        （piper_hybrid，joint 动作空间）只认 policy/agent —— 否则 `decide(eef)` 会把
        7 维笛卡尔增量写进关节通路。
        """
        if mode not in self.accepted_modes:
            raise DecisionRejected(
                "VLA_INVALID_MODE",
                f"mode must be one of {list(self.accepted_modes)}",
                mode=mode,
            )
        canonical = resolve_mode(mode)
        if canonical == DECISION_EEF and self.joint_abs_labels is not None:
            raise DecisionRejected(
                "VLA_MODE_UNSUPPORTED",
                "eef is not available for the joint_abs action space (RoboTwin proprio has no "
                "end-effector pose); use student or a joint-space edit",
                mode=canonical,
            )
        if canonical == DECISION_STOP:
            if steps is not None:
                raise DecisionRejected("VLA_INVALID_ARGUMENT", "stop takes no steps", mode=canonical)
            for name, value in (("edit", edit), ("target", target)):
                if value is not None:
                    raise DecisionRejected("VLA_INVALID_ARGUMENT", f"stop takes no {name}", mode=canonical)
            self._close_segment(completed=True)
            self.mode = DECISION_STOP
            self.last_mode_switch_mono = time.monotonic()
            verb = self._verb()
            request_id = self._send_policy_command(verb)
            self.bump("policy_commands_sent")
            self.bump("decisions_applied")
            snapshot = self._open_checkpoint(trigger=TRIGGER_AGENT, reason=REASON_AGENT_STOP)
            self.last_decision = self._decision_record(canonical, None, edit, target, reason, assessment)
            return {
                "accepted": True,
                "mode": self.report_mode(),
                "policy_command": verb,
                "request_id": request_id,
                "segment": None,
                "checkpoint": snapshot,
                "decision": dict(self.last_decision),
            }

        budget = self._validate_budget(canonical, steps)
        if canonical == DECISION_EDIT and edit is None:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "edit requires the edit block", mode=canonical)
        if canonical != DECISION_EDIT and edit is not None:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", f"{canonical} takes no edit block", mode=canonical)
        if canonical == DECISION_EEF and target is None:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "eef requires the target block", mode=canonical)
        if canonical != DECISION_EEF and target is not None:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", f"{canonical} takes no target block", mode=canonical)

        edit_norm = self._validate_edit(edit, budget) if canonical == DECISION_EDIT else None
        target_norm = self._validate_target(target) if canonical == DECISION_EEF else None

        self._close_segment(completed=True)
        self.mode = canonical
        self.last_mode_switch_mono = time.monotonic()
        self.supervised = True
        segment = self._open_segment(
            mode=canonical,
            steps_budget=budget,
            decision=self._decision_record(canonical, budget, edit_norm, target_norm, reason, assessment),
            supervised=True,
        )
        if edit_norm is not None and self.joint_abs_labels is not None:
            segment.edit_joint_delta = list(edit_norm["delta_joints_vector"])
            segment.edit_ramp_steps = int(edit_norm["ramp_steps"])
            segment.edit_gripper_values = {int(k): float(v) for k, v in edit_norm["gripper_values"].items()}
        elif edit_norm is not None:
            segment.edit_step_bias = list(edit_norm["step_bias"])
            segment.edit_ramp_steps = int(edit_norm["ramp_steps"])
            segment.eef_gripper = edit_norm["gripper_action"]
        if target_norm is not None:
            # eef 段的动作权威在 bridge（整段替换策略输出），目标随段保存；
            # 段内每一步都拿**最新** proprio 重算增量（闭环，不是预排的轨迹）。
            segment.eef_target = dict(target_norm)
            self.bump("eef_segments_started")
        verb = self._verb()
        request_id = self._send_policy_command(verb)
        self.bump("policy_commands_sent")
        self.bump("decisions_applied")
        closed = self._close_checkpoint(outcome=f"decision:{canonical}")
        return {
            "accepted": True,
            "mode": self.report_mode(),
            "policy_command": verb,
            "request_id": request_id,
            "segment": segment.to_dict(),
            "checkpoint_closed": closed,
            "checkpoint": self.checkpoint,
            "decision": dict(segment.decision),
            "edit_step_bias": list(segment.edit_step_bias),
        }

    # -- action shaping / accounting ------------------------------------------------

    def shape_action(self, values: list[float]) -> tuple[list[float], bool]:
        """按当前段改写这一步的策略动作。

        - `edit` 段：把该步的偏置叠加到前 6 位（+ 夹爪提示），**一次性偏移**在段内
          前 m 步按斜坡施加（累计 = 决策给的 delta），不是恒定偏置——robosuite OSC
          每步都吃一次增量，恒定偏置会按步累加。
        - `eef` 段：**整段替换**策略输出（不叠加）——这段的权威是 VLM 给的目标，
          用**最新** proprio 重算朝目标的增量（闭环）。
        - 其余档位（student / stop）原样返回。
        """
        segment = self.segment
        if self.joint_abs_labels is not None:
            return self._shape_joint_abs_action(segment, values)
        if segment is not None and segment.mode == DECISION_EEF and segment.eef_target is not None:
            return self._shape_eef_action(segment, values)
        if segment is None or segment.mode != DECISION_EDIT or not segment.edit_step_bias:
            return list(values), False
        index = segment.steps_executed + 1  # 本步是段内第几步（1-based）
        if index > segment.edit_ramp_steps:
            return list(values), False
        out = [float(v) for v in values]
        for axis, bias in enumerate(segment.edit_step_bias):
            if axis < len(out):
                out[axis] += bias
        if segment.eef_gripper is not None and len(out) >= 7:
            out[6] = segment.eef_gripper
        segment.edit_applied += 1
        self.bump("edit_actions_biased")
        return out, True

    def _shape_joint_abs_action(self, segment: Segment | None, values: list[float]) -> tuple[list[float], bool]:
        """joint_abs 的 edit：绝对关节目标 + 偏移。

        绝对目标下偏移**不累加**（与 eef_delta 的逐步增量不同）：第 k 步施加
        delta·min(k, ramp)/ramp，爬升后保持到段末，段内机械臂被持续推向
        「学生目标 + delta」。夹爪提示直接覆盖对应夹爪维。
        """
        if segment is None or segment.mode != DECISION_EDIT or not (
            segment.edit_joint_delta or segment.edit_gripper_values
        ):
            return list(values), False
        k = segment.steps_executed + 1
        ramp = max(1, segment.edit_ramp_steps)
        frac = min(k, ramp) / ramp
        out = [float(v) for v in values]
        for i, delta in enumerate(segment.edit_joint_delta):
            if i < len(out) and delta:
                out[i] += delta * frac
        for i, value in segment.edit_gripper_values.items():
            if i < len(out):
                out[i] = value
        segment.edit_applied += 1
        self.bump("edit_actions_biased")
        return out, True

    def _shape_eef_action(self, segment: Segment, values: list[float]) -> tuple[list[float], bool]:
        """eef 段的一步：朝目标位姿的增量（替换策略输出）。

        拿不到 proprio（从未收到 8 维观测 / 缺 axis-angle）时下发**零增量**——
        OSC 是位置伺服，零增量 = 保持当前位姿，且段预算照常走完 → C1 开检查点
        交回 VLM。**刻意不退化成学生的动作**：那会让 mode=eef 的段里实际跑的是
        student 流，干预归因（一等指标）从此不可信；也不抛异常（节点不该因一条
        动作崩掉）。这一分支计数 + 记日志，正常档案下恒为 0。
        """
        target = segment.eef_target or {}
        gripper_closed = bool(target.get("gripper_closed", False))
        if self.last_eef_pos is None or self.last_eef_rotvec is None:
            self.bump("eef_actions_without_proprio")
            out = [0.0] * len(values)
            if len(out) >= 7:
                out[6] = GRIPPER_ACTION_CLOSED if gripper_closed else GRIPPER_ACTION_OPEN
            return out, True
        delta, _error_m, arrived = eef_delta_to_target(
            target_position=list(target["position"]),
            target_quaternion_wxyz=list(target["quaternion_wxyz"]),
            eef_pos=list(self.last_eef_pos),
            eef_rotvec=list(self.last_eef_rotvec),
            pos_unit_m=self.eef_pos_unit_m,
            rot_unit_rad=self.eef_rot_unit_rad,
            tolerance_m=self.eef_pos_tolerance_m,
            gripper_closed=gripper_closed,
        )
        if arrived and not segment.eef_arrived:
            segment.eef_arrived = True
            self.bump("eef_targets_reached")
        segment.eef_applied += 1
        self.bump("eef_actions_generated")
        return delta, True

    def note_forwarded(self) -> None:
        """每成功下发一条动作调一次（= 仿真前进一步）。"""
        self.last_action_mono = time.monotonic()
        self.empty_ticks_since_action = 0
        self.empty_tick_streak = 0
        self.episode_steps += 1
        if self.joint_abs_labels is not None:
            if self.last_joint_positions is not None:
                # 关节空间失速判据：窗口首末的**臂关节**向量 L2 距离（rad），夹爪维不计
                arm = [v for i, v in enumerate(self.last_joint_positions) if i not in self.gripper_indices]
                self._tcp_window.append(tuple(arm))  # type: ignore[arg-type]
        elif self.last_eef_pos is not None:
            self._tcp_window.append(tuple(self.last_eef_pos[:3]))  # type: ignore[arg-type]

        segment = self.segment
        if segment is not None:
            segment.steps_executed += 1
            if segment.steps_budget and segment.steps_executed >= segment.steps_budget:
                self.bump("segments_completed")
                self.bump("c1_events")
                self._freeze(TRIGGER_SEGMENT_END, REASON_SEGMENT_END)
                return
        if not self._watching(segment):
            return
        if self.c2_cooldown_steps and (self.episode_steps - self._last_c2_step) < self.c2_cooldown_steps:
            return
        event = self._detect_event()
        if event is not None:
            reason, details = event
            self.bump("c2_events")
            self._freeze(TRIGGER_EVENT, reason, details=details)

    def note_invalid_action(self) -> None:
        segment = self.segment
        if segment is None or not segment.supervised:
            return
        segment.invalid_actions += 1
        if segment.invalid_actions >= self.max_invalid_actions_per_segment:
            self.bump("c2_events")
            self._freeze(
                TRIGGER_EVENT,
                REASON_INVALID_ACTIONS,
                details={"invalid_actions_in_segment": segment.invalid_actions},
            )

    def note_tick(self) -> None:
        """timer 事件（dora/timer）：策略停摆只能用墙钟判（步进门控下没有空拍可数）。

        判据挂在**当前段**上（与 note_forwarded / note_invalid_action 同口径）：
        `set_mode` 释放成自发透传后就不再停摆检测 —— episode 级的 supervised 只是
        「本 episode 曾被 decide() 驱动过」的记账（干预比例的分母），不是检测开关。
        """
        if not self.checkpoints_enabled or not self.forwarding:
            return
        segment = self.segment
        if not self._watching(segment):
            return
        if self.last_action_mono is None:
            return
        idle_ms = (time.monotonic() - self.last_action_mono) * 1000.0
        if idle_ms < self.starvation_timeout_ms:
            return
        self.empty_tick_streak += 1
        self.bump("empty_ticks")
        self.bump("c2_events")
        self._freeze(
            TRIGGER_EVENT,
            REASON_POLICY_STARVED,
            details={"idle_ms": int(idle_ms), "timeout_ms": self.starvation_timeout_ms},
        )

    # -- episode boundaries -----------------------------------------------------------

    def reset_episode(self) -> dict[str, Any]:
        """benchmark 的 reset_scene：episode 记账归零（候选队列也已被策略侧清空）。

        hold 中开 C0 检查点（新 episode 需要第一次决策）；自发透传不冻结 ——
        S3 的 500 episode 基线靠这条保持透传。
        """
        # 跨 episode 收尾的 eef 段不必再清候选：benchmark 的 reset_scene 本身就把
        # 策略队列清了（上面中继的那一条），多发一条只是噪音。
        self._close_segment(completed=True, invalidate=False)
        self.episode_index += 1
        self.episode_steps = 0
        self.episode_started_mono = time.monotonic()
        self.last_action_mono = None
        self.empty_tick_streak = 0
        self.empty_ticks_since_action = 0
        self._last_c2_step = -1
        self._tcp_window.clear()
        self.cumulative_edit_units = [0.0, 0.0, 0.0]
        self.cumulative_edit_joint_rad = [0.0] * len(self.joint_abs_labels or [])
        self.action_stats = _empty_action_stats()
        self.clear_candidate()
        self.bump("episodes_seen")
        if self.checkpoints_enabled and self.hold_on_episode_start and not self.holding:
            # 上一集末尾被 set_mode(student) 放行 —— 新一集回到持有，C0 等 VLM 首个决策
            self.mode = DECISION_STOP
            self.last_mode_switch_mono = time.monotonic()
            self._send_policy_command("pause")
            self.bump("policy_commands_sent")
        if self.checkpoints_enabled and self.holding:
            self.supervised = True
            snapshot = self._open_checkpoint(trigger=TRIGGER_EPISODE_START, reason="episode_start")
        else:
            self.supervised = False
            snapshot = None
        return {"episode_index": self.episode_index, "checkpoint": snapshot, "supervised": self.supervised}

    def end_episode(self) -> None:
        """benchmark 批次结束（PolicyCommand stop）：段收尾、检查点关闭。"""
        self._close_segment(completed=True)
        self._close_checkpoint(outcome="episode_ended")
        self.bump("episodes_ended")

    # -- tool operations ----------------------------------------------------------

    def status(self, max_proprio_age_ms: int) -> dict[str, Any]:
        now = time.monotonic()
        proprio_age_ms = (
            None if self.last_proprio_mono is None else int((now - self.last_proprio_mono) * 1000)
        )
        status_age_ms = (
            None if self.last_policy_status_mono is None else int((now - self.last_policy_status_mono) * 1000)
        )
        phase = self.last_policy_status.get("status", "unknown") if self.last_policy_status else "unknown"
        seen = self.last_proprio_mono is not None
        if self.proprio_gate == "seen":
            proprio_fresh = seen
        else:
            proprio_fresh = seen and proprio_age_ms <= max_proprio_age_ms
        payload: dict[str, Any] = {
            "mode": self.report_mode(),
            "policy_id": self.policy_id,
            "proprio_gate": self.proprio_gate,
            "policy_last_status": dict(self.last_policy_status) if self.last_policy_status else {},
            "policy_status_age_ms": status_age_ms,
            "policy_phase_hint": phase,
            "proprio_age_ms": proprio_age_ms,
            "proprio_fresh": proprio_fresh,
            "counters": dict(self._counters),
        }
        if self.track_action_stats:
            # eef_delta 档案才有：动作量级/饱和/夹爪占空比（报告口径，pid 逐字不变）
            payload["action_stats"] = dict(self.action_stats)
        # 检查点字段只在开启检查点纪律的档案里出现 —— piper_hybrid 的
        # get_status 契约（additionalProperties: false）保持逐字不变。
        if self.checkpoints_enabled:
            payload.update(
                {
                    "checkpoints_enabled": True,
                    "supervised": self.supervised,
                    "episode": self._episode_snapshot(now),
                    "segment": self.segment.to_dict() if self.segment else None,
                    "checkpoint": self.checkpoint,
                    "checkpoint_history": list(self.checkpoint_history[-5:]),
                    "cumulative_edit_units": [round(v, 4) for v in self.cumulative_edit_units],
                    "last_eef_pos": list(self.last_eef_pos) if self.last_eef_pos else None,
                    # 姿态：eef 目标要的是绝对四元数，只给位置等于逼 VLM 每次决策都
                    # 附带一个它没打算要的旋转（"往左 3 cm 但保持朝向"这种最基本的
                    # 意图就表达不出来）。两个都发：rotvec 是 proprio 的原生表示
                    # （轴角，便于人读与差分），四元数是 target 的入参表示（直接可用）。
                    "last_eef_rotvec": list(self.last_eef_rotvec) if self.last_eef_rotvec else None,
                    "last_eef_quaternion_wxyz": (
                        list(_quat_from_rotvec(self.last_eef_rotvec)) if self.last_eef_rotvec else None
                    ),
                    "last_decision": dict(self.last_decision) if self.last_decision else None,
                }
            )
            if self.joint_abs_labels is not None:
                payload["joint_names"] = list(self.joint_abs_labels)
                payload["last_joint_positions"] = (
                    [round(v, 4) for v in self.last_joint_positions] if self.last_joint_positions else None
                )
                payload["candidate"] = self._candidate_snapshot()
                payload["cumulative_edit_joint_rad"] = {
                    self.joint_abs_labels[i]: round(v, 4) for i, v in enumerate(self.cumulative_edit_joint_rad) if v
                }
        return payload

    def report_mode(self) -> str:
        return self._report_mode(self.mode)

    def _verb(self) -> str:
        return "start" if self.forwarding else "pause"

    # -- internals -----------------------------------------------------------------

    def _validate_budget(self, mode: str, steps: int | None) -> int:
        ceiling = self.max_eef_segment_steps if mode == DECISION_EEF else self.max_segment_steps
        if steps is None:
            return ceiling
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "steps must be a positive integer", steps=steps)
        if steps > ceiling:
            raise DecisionRejected(
                "VLA_DECISION_OUT_OF_RANGE",
                f"steps={steps} exceeds the {mode} segment ceiling {ceiling}",
                steps=steps,
                ceiling=ceiling,
            )
        return steps

    def _validate_edit(self, edit: dict[str, Any], budget: int) -> dict[str, Any]:
        if not isinstance(edit, dict):
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "edit must be an object")
        if self.joint_abs_labels is not None:
            return self._validate_joint_edit(edit, budget)
        allowed = {"delta_position", "delta_rotation_vector", "gripper"}
        unexpected = sorted(set(edit) - allowed)
        if unexpected:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", f"edit accepts only {sorted(allowed)}", unexpected=unexpected)
        position = self._vector(edit.get("delta_position"), 3, "edit.delta_position")
        rotation = self._vector(edit.get("delta_rotation_vector"), 3, "edit.delta_rotation_vector")
        gripper = edit.get("gripper", "keep")
        if gripper not in _VALID_GRIPPER_HINTS:
            raise DecisionRejected(
                "VLA_INVALID_ARGUMENT",
                f"edit.gripper must be one of {list(_VALID_GRIPPER_HINTS)}",
                gripper=gripper,
            )
        # 请求是**物理量**（米 / 弧度），限幅与记账走**动作空间单位**（1 单位 = 5 cm /
        # 0.5 rad，即 robosuite OSC_POSE 的单步输出上限）。转换必须在这一层完成：
        # 配置里的界（edit_max_*_m / _rad）是物理量，而动作是单位量 —— 漏掉这一步会让
        # edit 静默缩水 20 倍（位置）/ 2 倍（姿态）：VLM 以为推了 3 cm，实际推了 1.5 mm。
        position_units = [value / self.eef_pos_unit_m for value in position]
        rotation_units = [value / self.eef_rot_unit_rad for value in rotation]
        if math.sqrt(sum(v * v for v in position_units)) > self.edit_max_pos_units + 1e-9:
            raise DecisionRejected(
                "VLA_DECISION_OUT_OF_RANGE",
                f"edit.delta_position exceeds the {self.edit_max_pos_m:.3f} m "
                f"({self.edit_max_pos_units:.2f}-unit) cumulative bound",
                magnitude=math.sqrt(sum(v * v for v in position)),
                magnitude_units=math.sqrt(sum(v * v for v in position_units)),
                bound_m=self.edit_max_pos_m,
                bound_units=self.edit_max_pos_units,
            )
        for axis, value in enumerate(position_units):
            if abs(value + self.cumulative_edit_units[axis]) > self.max_cumulative_edit_pos_units + 1e-9:
                raise DecisionRejected(
                    "VLA_DECISION_OUT_OF_RANGE",
                    f"edit.delta_position axis {axis} would push the episode-cumulative bias past "
                    f"{self.max_cumulative_edit_pos_m:.3f} m "
                    f"({self.max_cumulative_edit_pos_units:.2f} units)",
                    axis=axis,
                    cumulative_units=self.cumulative_edit_units[axis],
                    requested_m=position[axis],
                    requested_units=value,
                )
        if math.sqrt(sum(v * v for v in rotation_units)) > self.edit_max_rot_units + 1e-9:
            raise DecisionRejected(
                "VLA_DECISION_OUT_OF_RANGE",
                f"edit.delta_rotation_vector exceeds the {self.edit_max_rot_rad:.3f} rad "
                f"({self.edit_max_rot_units:.2f}-unit) cumulative bound",
                magnitude=math.sqrt(sum(v * v for v in rotation)),
                magnitude_units=math.sqrt(sum(v * v for v in rotation_units)),
                bound_rad=self.edit_max_rot_rad,
                bound_units=self.edit_max_rot_units,
            )
        # 逐步偏置：前 m 步每步 delta/m，段内累计恰好 = delta。m ≤ 段预算，否则
        # 偏移在段结束前走不完（那等于没给足偏置，宁可缩短斜坡也不截断决策）。
        ramp = max(1, min(self.edit_ramp_steps, budget))
        step_bias = [value / ramp for value in position_units + rotation_units]
        gripper_action = {
            "keep": None,
            "open": GRIPPER_ACTION_OPEN,
            "closed": GRIPPER_ACTION_CLOSED,
        }[gripper]
        return {
            # 请求原样（米 / 弧度）——回显与决策记录用这套，读者看到的就是他写下的数。
            "delta_position": position,
            "delta_rotation_vector": rotation,
            # 动作空间单位 —— 限幅、逐步偏置、episode 累计记账都用这套。
            "position_units": position_units,
            "rotation_units": rotation_units,
            "gripper": gripper,
            "ramp_steps": ramp,
            "step_bias": step_bias,
            "gripper_action": gripper_action,
        }

    def _validate_joint_edit(self, edit: dict[str, Any], budget: int) -> dict[str, Any]:
        """joint_abs 的 edit：{delta_joints: {关节名: rad}, left_gripper/right_gripper: keep|open|closed}。"""
        labels = self.joint_abs_labels or []
        allowed = {"delta_joints", "left_gripper", "right_gripper"}
        unexpected = sorted(set(edit) - allowed)
        if unexpected:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", f"edit accepts only {sorted(allowed)}", unexpected=unexpected)
        raw = edit.get("delta_joints", {}) or {}
        if not isinstance(raw, dict):
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "edit.delta_joints must be an object {joint_name: radians}")
        vector = [0.0] * len(labels)
        for name, value in raw.items():
            if name not in labels:
                raise DecisionRejected("VLA_INVALID_ARGUMENT", f"unknown joint {name!r}", joints=labels)
            index = labels.index(name)
            if index in self.gripper_indices:
                raise DecisionRejected(
                    "VLA_INVALID_ARGUMENT", f"{name} is a gripper; use left_gripper/right_gripper instead"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise DecisionRejected("VLA_INVALID_ARGUMENT", f"delta for {name} must be a finite number")
            value = float(value)
            if abs(value) > self.edit_max_joint_rad:
                value = math.copysign(self.edit_max_joint_rad, value)
            remaining = self.max_cumulative_edit_joint_rad - abs(self.cumulative_edit_joint_rad[index])
            if remaining < 0.0:
                remaining = 0.0
            if abs(value) > remaining:
                value = math.copysign(remaining, value) if remaining > 0.0 else 0.0
            vector[index] = value
        gripper_values: dict[int, float] = {}
        # 夹爪维按位置顺序：第一个 = 左、最后一个 = 右（aloha-agilex：fl_joint7 / fr_joint7）
        sides = {"left_gripper": self.gripper_indices[0] if self.gripper_indices else None,
                 "right_gripper": self.gripper_indices[-1] if self.gripper_indices else None}
        hints = {}
        for key, index in sides.items():
            hint = edit.get(key, "keep")
            if hint not in _VALID_GRIPPER_HINTS:
                raise DecisionRejected("VLA_INVALID_ARGUMENT", f"edit.{key} must be one of {list(_VALID_GRIPPER_HINTS)}")
            hints[key] = hint
            if hint != "keep" and index is not None:
                gripper_values[index] = 1.0 if hint == "open" else 0.0
        if not any(vector) and not gripper_values:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "edit changes nothing; use student instead")
        ramp = max(1, min(self.edit_ramp_steps, budget))
        return {
            "delta_joints": {labels[i]: v for i, v in enumerate(vector) if v},
            "delta_joints_vector": vector,
            **hints,
            "gripper_values": {str(k): v for k, v in gripper_values.items()},
            "ramp_steps": ramp,
        }

    def _validate_target(self, target: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(target, dict):
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "target must be an object")
        allowed = {"position", "quaternion_wxyz", "gripper_closed"}
        unexpected = sorted(set(target) - allowed)
        if unexpected:
            raise DecisionRejected(
                "VLA_INVALID_ARGUMENT", f"target accepts only {sorted(allowed)}", unexpected=unexpected
            )
        position = self._vector(target.get("position"), 3, "target.position")
        quaternion = self._vector(target.get("quaternion_wxyz"), 4, "target.quaternion_wxyz")
        norm = math.sqrt(sum(v * v for v in quaternion))
        if norm < 1e-6:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "target.quaternion_wxyz must be non-zero")
        quaternion = [v / norm for v in quaternion]
        closed = target.get("gripper_closed")
        if not isinstance(closed, bool):
            raise DecisionRejected("VLA_INVALID_ARGUMENT", "target.gripper_closed must be a boolean")
        # 可达性**粗筛**（不是 IK，bridge 里没有运动学模型——P0 §1 的结论）。
        # 只拦"明显不可能"的目标，避免生成一串朝墙撞的增量；真正的可行性由 VLM 判。
        # 有 proprio 才能筛：没有当前 TCP 就没有参照点（此时放行，段内会走
        # `eef_actions_without_proprio` 的零增量路径）。
        if self.last_eef_pos is not None:
            reach = math.dist(position, list(self.last_eef_pos[:3]))
            if reach > self.eef_max_reach_m:
                raise DecisionRejected(
                    "VLA_DECISION_OUT_OF_RANGE",
                    f"target.position is {reach:.3f} m from the current TCP, beyond the "
                    f"{self.eef_max_reach_m:.2f} m reach screening bound",
                    distance_m=round(reach, 4),
                    bound_m=self.eef_max_reach_m,
                )
        if position[2] < self.eef_min_z_m:
            raise DecisionRejected(
                "VLA_DECISION_OUT_OF_RANGE",
                f"target.position z={position[2]:.3f} m is below the {self.eef_min_z_m:.3f} m floor "
                "(the LIBERO table top is z≈0)",
                z=position[2],
                floor_m=self.eef_min_z_m,
            )
        return {"position": position, "quaternion_wxyz": quaternion, "gripper_closed": closed}

    @staticmethod
    def _vector(raw: Any, length: int, label: str) -> list[float]:
        if not isinstance(raw, (list, tuple)) or len(raw) != length:
            raise DecisionRejected("VLA_INVALID_ARGUMENT", f"{label} must be a list of {length} numbers")
        values = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise DecisionRejected("VLA_INVALID_ARGUMENT", f"{label} must be finite numbers")
            values.append(float(value))
        return values

    def _decision_record(
        self,
        mode: str,
        budget: int | None,
        edit: dict[str, Any] | None,
        target: dict[str, Any] | None,
        reason: str,
        assessment: dict[str, Any] | None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "mode": mode,
            "steps": budget,
            "reason": reason,
            "at_step": self.episode_steps,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if edit is not None and "delta_joints_vector" in edit:
            record["edit"] = dict(edit)
        elif edit is not None:
            record["edit"] = {
                "delta_position": edit["delta_position"],
                "delta_rotation_vector": edit["delta_rotation_vector"],
                # 同一决策的动作空间表示（1 单位 = 5 cm / 0.5 rad）——记录里两套都给，
                # 便于回放时核对"请求的物理量"与"实际施加的单位量"逐条对应。
                "position_units": [round(v, 6) for v in edit.get("position_units") or []],
                "rotation_units": [round(v, 6) for v in edit.get("rotation_units") or []],
                "gripper": edit["gripper"],
            }
        if target is not None:
            record["target"] = target
        if assessment is not None:
            record["assessment"] = assessment
        return record

    def _open_segment(self, *, mode: str, steps_budget: int, decision: dict[str, Any], supervised: bool) -> Segment:
        self._segment_seq += 1
        segment = Segment(
            index=self._segment_seq,
            mode=mode,
            steps_budget=steps_budget,
            decision=decision,
            supervised=supervised,
            started_at=time.monotonic(),
            start_tcp=list(self.last_eef_pos) if self.last_eef_pos else None,
        )
        self.segment = segment
        self.bump("segments_started")
        # 停摆判据的时钟从**段起点**起算。上一段末尾的持有（C1/C2/C3 冻结、VLM
        # 思考的秒级回合）不是策略停摆，但墙钟判据只看 last_action_mono，持有超过
        # starvation_timeout_ms 后再 decide 就会在下一次 tick 立刻误判 policy_starved。
        # 段起点重置 = 给策略「被要求产出的那一刻起 10 s」的窗口；段内每条转发动作
        # 再由 note_forwarded 续期。
        self.last_action_mono = time.monotonic()
        self.empty_tick_streak = 0
        return segment

    def _close_segment(self, *, completed: bool, invalidate: bool = True) -> dict[str, Any] | None:
        segment = self.segment
        if segment is None:
            return None
        if segment.mode == DECISION_EDIT and segment.decision.get("edit") and self.joint_abs_labels is not None:
            for i, v in enumerate(segment.decision["edit"].get("delta_joints_vector", [])):
                self.cumulative_edit_joint_rad[i] += float(v)
        elif segment.mode == DECISION_EDIT and segment.decision.get("edit"):
            # 记账走**动作空间单位**（不是请求的米）：cumulative_edit_units 与
            # max_cumulative_edit_pos_units（= edit_drift 阈值）必须是同一套单位，
            # 否则漂移判据形同虚设。
            delta_units = segment.decision["edit"]["position_units"]
            for axis in range(3):
                self.cumulative_edit_units[axis] += float(delta_units[axis])
        if segment.mode == DECISION_EEF and segment.eef_target is not None and self.last_eef_pos is not None:
            # 到达标记必须在**段真正结束时**按段末 proprio 复核一次。段内判据用的是
            # 每一步"下发前的最新 proprio"——最后一步执行完产生的那点位移它看不见，
            # 所以段末完全可能已经在容差内而 arrived 仍是 False（实测：误差 0.59 cm
            # / 容差 1 cm，arrived=False）。VLM 是读段快照决定要不要再补一段的，
            # 于是这个标记必须表示"机械臂**现在**在不在目标上"，而不是"最后一次
            # 下发时在不在"。误差无条件记录（测量）；计数器只在段内没判过到达时补记
            # 一次，避免重复计数。
            error_m = math.dist(self.last_eef_pos, segment.eef_target["position"])
            segment.eef_arrival_error_m = error_m
            if not segment.eef_arrived and error_m <= self.eef_pos_tolerance_m:
                segment.eef_arrived = True
                self.bump("eef_targets_reached")
        if not completed:
            self.bump("segments_aborted")
        snapshot = segment.to_dict()
        snapshot["candidate_reset"] = invalidate and self._invalidate_candidate(segment)
        self.segment = None
        return snapshot

    def _invalidate_candidate(self, segment: Segment) -> bool:
        """eef 段结束 → 作废策略侧候选（pause → reset_scene）。

        为什么必须清：eef 段的动作绕过了策略，策略队列里剩下的那几条是用**段前**
        观测算出来的——接着执行等于把机械臂按旧前提带走。S3 的老档案靠 RTC merge
        把陈旧尾巴整体替换掉；**arm A 是 sync 口径、没有 merge 一轮**，所以这条
        在现档案下比老档案更必要（S4b 那条"merge 不覆盖偏置"的验收在 sync 下成了
        平凡项，而"eef 段后必须清队"反而从可选变成必需）。

        顺序 pause → reset_scene 是契约（先停推理再清队列）；冻结路径上 `_freeze`
        紧随其后还会再发一次 pause，幂等无害。零步的段没碰过候选，不清。
        """
        if segment.mode != DECISION_EEF or segment.steps_executed <= 0:
            return False
        self.bump("candidate_resets")
        # 顺序即契约：先停推理再清队列。本模块刻意不依赖 forge_common（离线可测），
        # 所以不在这里记日志 —— 事实由 `candidate_resets` 计数与段快照的
        # `candidate_reset` 字段承载，`get_status` 两条都能读到。
        self._send_policy_command("pause")
        self.bump("policy_commands_sent")
        self._send_policy_command(CANDIDATE_RESET_COMMAND)
        self.bump("policy_commands_sent")
        return True

    def _freeze(self, trigger: str, reason: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """冻结：不下发动作（benchmark 零仿真时间）+ pause 策略节点 + 开检查点。"""
        closed = self._close_segment(completed=True)
        self.mode = DECISION_STOP
        self.last_mode_switch_mono = time.monotonic()
        self._last_c2_step = self.episode_steps
        request_id = self._send_policy_command("pause")
        self.bump("policy_commands_sent")
        snapshot = self._open_checkpoint(trigger=trigger, reason=reason, details=details, last_segment=closed)
        snapshot["policy_command"] = "pause"
        snapshot["request_id"] = request_id
        return snapshot

    def _open_checkpoint(
        self,
        *,
        trigger: str,
        reason: str,
        details: dict[str, Any] | None = None,
        last_segment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.checkpoint is not None and not self.checkpoint.get("closed_at"):
            # 已经开着：不重开（幂等），只记一次重复触发
            self.checkpoint["repeat_triggers"] = self.checkpoint.get("repeat_triggers", 0) + 1
            return self.checkpoint
        self._checkpoint_seq += 1
        now = time.monotonic()
        snapshot: dict[str, Any] = {
            "checkpoint_id": f"ckpt-{self._checkpoint_seq:03d}",
            "trigger": trigger,
            "reason": reason,
            "details": dict(details or {}),
            "episode_index": self.episode_index,
            "step_in_episode": self.episode_steps,
            "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "opened_at_mono": round(now, 3),
            "segment": self.segment.to_dict() if self.segment else None,
            "last_segment": last_segment,
            "policy_phase_hint": self.last_policy_status.get("status", "unknown") if self.last_policy_status else "unknown",
            "proprio_age_ms": None if self.last_proprio_mono is None else int((now - self.last_proprio_mono) * 1000),
            "last_eef_pos": list(self.last_eef_pos) if self.last_eef_pos else None,
            "counters": dict(self._counters),
            # 推算量（**不可观测**）：策略节点每 tick 只发一条无元数据的 action，
            # candidate_id / queue_depth 拿不到。换代周期由**档案的策略口径**决定
            # （sync + n_action_steps 5 → 每 5 步整批换代；退役的 async_rtc + 队阈 30
            # → chunk 50 − 30 = 20 步），配置键 rtc_merge_period_steps 即该周期，
            # 故按本 episode 已执行步数推算代次。
            "candidate_epoch_est": self.episode_steps // max(1, self.rtc_merge_period_steps),
            "observation_note": (
                "candidate_id/queue_depth are not observable through the dora action "
                "contract (one bare JointCommand per tick, no metadata); "
                "candidate_epoch_est = steps_in_episode // rtc_merge_period_steps is a "
                "step-count estimate of the candidate generation (the period is set by "
                "the profile's inference protocol, not measured), not a measurement"
            ),
        }
        self.checkpoint = snapshot
        self.bump("checkpoints_opened")
        return snapshot

    def _close_checkpoint(self, *, outcome: str) -> dict[str, Any] | None:
        if self.checkpoint is None:
            return None
        if self.checkpoint.get("closed_at"):
            return self.checkpoint
        now = time.monotonic()
        self.checkpoint["closed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.checkpoint["closed_by"] = outcome
        self.checkpoint["hold_ms"] = int((now - float(self.checkpoint.get("opened_at_mono", now))) * 1000)
        closed = dict(self.checkpoint)
        self.checkpoint_history.append(closed)
        if len(self.checkpoint_history) > self.checkpoint_history_limit:
            del self.checkpoint_history[0 : len(self.checkpoint_history) - self.checkpoint_history_limit]
        self.checkpoint = None
        self.bump("checkpoints_closed")
        return closed

    def _watching(self, segment: Segment | None) -> bool:
        """本步是否跑 C2 判据。

        默认只判 decide() 起头的段；profile 打开 detect_on_released_segments 后，
        自发透传段（`set_mode` 起头）也判 —— 否则「放行 = 盲区」，
        800~1200 步的 episode 只在开头 50 步被看过。
        """
        if segment is None:
            return False
        if segment.supervised:
            return True
        return self.detect_on_released_segments and segment.mode in FORWARDING_MODES

    def _detect_event(self) -> tuple[str, dict[str, Any]] | None:
        """C2 判据（确定性、逐动作可复现）。"""
        window = self.stall_window_steps
        # 窗口里放的是臂关节角（rad）还是 TCP 位置（m），取决于动作空间；阈值必须同源。
        threshold = self.stall_min_displacement_m
        unit = "m"
        if self.joint_abs_labels is not None and self.stall_min_joint_rad is not None:
            threshold = self.stall_min_joint_rad
            unit = "rad"
        if len(self._tcp_window) >= window:
            first = self._tcp_window[0]
            last = self._tcp_window[-1]
            displacement = math.dist(first, last)
            if displacement < threshold:
                return REASON_NO_PROGRESS, {
                    "window_steps": window,
                    "displacement": round(displacement, 5),
                    "displacement_unit": unit,
                    "threshold": threshold,
                    "threshold_unit": unit,
                    # 旧键保留（既有读者按 _m 名取值）
                    "displacement_m": round(displacement, 5),
                    "threshold_m": threshold,
                }
        drift = max(abs(v) for v in self.cumulative_edit_units)
        if drift > self.max_cumulative_edit_pos_units + 1e-9:
            return REASON_EDIT_DRIFT, {
                "cumulative_edit_units": [round(v, 4) for v in self.cumulative_edit_units],
                "threshold_units": self.max_cumulative_edit_pos_units,
            }
        return None

    def _episode_snapshot(self, now: float) -> dict[str, Any]:
        return {
            "index": self.episode_index,
            "steps": self.episode_steps,
            "elapsed_s": (
                None if self.episode_started_mono is None else round(now - self.episode_started_mono, 3)
            ),
            "idle_ms": None if self.last_action_mono is None else int((now - self.last_action_mono) * 1000),
        }


def new_request_id() -> str:
    return f"vla-bridge-{uuid.uuid4().hex[:12]}"


__all__ = [
    "BridgeState",
    "DecisionRejected",
    "GRIPPER_ACTION_CLOSED",
    "GRIPPER_ACTION_OPEN",
    "Mode",
    "REASON_AGENT_STOP",
    "REASON_EDIT_DRIFT",
    "REASON_INVALID_ACTIONS",
    "REASON_NO_PROGRESS",
    "REASON_POLICY_STARVED",
    "REASON_SEGMENT_END",
    "Segment",
    "TRIGGER_AGENT",
    "TRIGGER_EPISODE_START",
    "TRIGGER_EVENT",
    "TRIGGER_SEGMENT_END",
    "new_request_id",
    "resolve_mode",
]

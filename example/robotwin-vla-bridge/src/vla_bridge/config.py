"""vla_bridge node configuration (vla_bridge.yaml)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import ConfigError, RegistrationConfig, load_yaml, parse_registration

# Piper joint limits, in radians, as declared by the bundled MJCF (PIPER.xml).
# Ours only when the config does not override them.
DEFAULT_ARM_LIMITS: dict[str, tuple[float, float]] = {
    "joint1": (-2.618, 2.618),
    "joint2": (0.0, 3.14),
    "joint3": (-2.697, 0.0),
    "joint4": (-1.832, 1.832),
    "joint5": (-1.22, 1.22),
    "joint6": (-3.14, 3.14),
}

# mujoco gripper actuator range, meters (gripper_action_controller parity).
DEFAULT_GRIPPER_LIMITS: tuple[float, float] = (0.0, 0.105)

# Policy-side gripper unit is millimeters (Piper feedback contract); the
# simulator joint is meters. One factor covers both directions.
DEFAULT_GRIPPER_POLICY_SCALE = 1000.0

# 动作空间（同一个节点服务两个 profile）：
#   joint     — piper_hybrid：7 维关节目标（joint1..6 + 夹爪毫米），限位 + 单条
#               增量限幅 + 夹爪单位换算都是真实存在的语义。
#   eef_delta — libero_bench：robosuite 原生 7 维笛卡尔增量（前 6 位 delta pose +
#               第 7 位夹爪 [-1,1]），**不做限位/限幅/换算**，student 直通不改写；
#               5 cm / 0.35 rad 只约束 VLM 的 edit 偏置（S4b 在输出侧叠加）。
ACTION_SPACE_JOINT = "joint"
ACTION_SPACE_EEF_DELTA = "eef_delta"
# joint_abs — robotwin_benchmark：aloha-agilex 14 维**绝对**关节目标
#               [左臂6, 左爪1, 右臂6, 右爪1]（env.take_action(action_type="qpos")）。
#               student 直通不改写；edit = 关节空间一次性偏置（弧度，段内斜坡）；
#               无 eef 档（RoboTwin qpos proprio 不含末端位姿）。
ACTION_SPACE_JOINT_ABS = "joint_abs"
ACTION_SPACES = (ACTION_SPACE_JOINT, ACTION_SPACE_EEF_DELTA, ACTION_SPACE_JOINT_ABS)
# LIBERO 动作维度：前 6 位笛卡尔增量 + 第 7 位夹爪（robosuite 原生域）。
LIBERO_ACTION_DIM = 7

# proprio 新鲜度判据（决定 vla.get_status.proprio_fresh，并在策略动作下发前把关）：
#   age  — 「最近一次 proprio 距今 ≤ max_proprio_age_ms」。适用于**连续流**仿真
#          （piper：proprio 50 Hz 常发），过期即说明上游停了。
#   seen — 「收到过 proprio 即可」。适用于**步进门控**环境（libero：proprio 与
#          仿真步一一对应，每条 action 才走一步、才发一次观测）。此时 age 不是
#          安全信号：环境在两条 action 之间根本不动，而首帧推理要 600ms+，用 age
#          会把闭环的第一步就丢掉 → 策略动作全被丢 → 没有步进 → 没有新观测 →
#          **死锁**（2026-09-20 实测：stale 1526、forwarded 0）。
#          换代残留由策略侧的 reset_scene（清观测缓存）兜住，不靠 age。
PROPRIO_GATE_AGE = "age"
PROPRIO_GATE_SEEN = "seen"
PROPRIO_GATES = (PROPRIO_GATE_AGE, PROPRIO_GATE_SEEN)
DEFAULT_PROPRIO_GATE = {ACTION_SPACE_JOINT: PROPRIO_GATE_AGE, ACTION_SPACE_EEF_DELTA: PROPRIO_GATE_SEEN, ACTION_SPACE_JOINT_ABS: PROPRIO_GATE_SEEN}

# 检查点仲裁（S4）：决策模式与段预算。
#   student — 直通策略候选前缀
#   edit    — 候选 + 一次性偏置（S4b）
#   eef     — 桥侧生成朝目标位姿的 delta（S4c）
#   stop    — 冻结（hold）：不下发动作 = 零仿真时间，同时开检查点
# 兼容旧档：policy→student（无预算，piper 语义不变）、agent→stop。
DECISION_STUDENT = "student"
DECISION_EDIT = "edit"
DECISION_EEF = "eef"
DECISION_STOP = "stop"
DECISION_MODES = (DECISION_STUDENT, DECISION_EDIT, DECISION_EEF, DECISION_STOP)
MODE_ALIASES = {"policy": DECISION_STUDENT, "agent": DECISION_STOP}
# 供动作放行的模式（hold 一律丢动作）
FORWARDING_MODES = (DECISION_STUDENT, DECISION_EDIT, DECISION_EEF)

# OSC_POSE 动作单元（robosuite）：1 动作单位 = 5 cm / 0.5 rad 每步。
# edit 的「5 cm / 0.35 rad」是**段内累计**上限，换算成动作单位就是这两个数。
OSC_POS_UNIT_M = 0.05
OSC_ROT_UNIT_RAD = 0.5
DEFAULT_EDIT_MAX_POS_M = 0.05
DEFAULT_EDIT_MAX_ROT_RAD = 0.35
# 偏置在段内前 m 步按斜坡施加（累计 m·b·5cm ≤ 5cm，即 m·b ≤ 1.0 动作单位），
# 恒定偏置会按步累加，不是「一次性偏移」。
DEFAULT_EDIT_RAMP_STEPS = 5

# 段预算上限（决策里的 steps）
DEFAULT_MAX_SEGMENT_STEPS = 15
DEFAULT_MAX_EEF_SEGMENT_STEPS = 5

# ── eef 档（S4c）：无 IK 的 delta 生成器 ─────────────────────────────────────
# 每步单位必须与 robosuite OSC_POSE 的 output_max 一致（5 cm / 0.5 rad），
# 否则"到目标的增量"换算错（P0 §2 实测值）。
DEFAULT_EEF_POS_UNIT_M = OSC_POS_UNIT_M
DEFAULT_EEF_ROT_UNIT_RAD = OSC_ROT_UNIT_RAD
# 到达判据（只按位置判；姿态误差继续收敛到段预算用完）
DEFAULT_EEF_POS_TOLERANCE_M = 0.01
# 可达性**粗筛**（不是 IK）：目标离当前 TCP 超过此距离直接拒。
DEFAULT_EEF_MAX_REACH_M = 1.0
# 目标高度下限（m，world 系）：libero 桌面在 z≈0。**可为负**，所以不走 positive 校验。
DEFAULT_EEF_MIN_Z_M = -0.05

# **候选换代周期**（步）。仅用于 candidate_epoch_est 的**估算**——真正的
# candidate_id / queue_depth 在消息契约里不可观测（策略节点每 tick 只发一条 action，
# 不带元数据；节点是二进制，不改）。取值由档案的策略口径决定，**档案必须显式覆盖**：
#   sync + n_action_steps 5（libero 现用，官方 openpi 口径）→ 5
#   async_rtc + queue_threshold 30（已退役的老口径）→ chunk 50 − 30 = 20
# 这里保留 20 作为默认只是"退役口径的历史值"（piper 档案不开检查点，此值不起作用）。
DEFAULT_RTC_MERGE_PERIOD_STEPS = 20

# C2 事件判据（bridge 侧的确定性计数器，每条 action 算一遍）
DEFAULT_MAX_EMPTY_TICK_STREAK = 10  # 策略连续多少拍没产出动作 → 队列饿死
DEFAULT_STALL_WINDOW_STEPS = 40  # 观察窗（步）
DEFAULT_STALL_MIN_DISPLACEMENT_M = 0.01  # 窗内 TCP 位移低于此值 → 失速
# 策略停摆的墙钟判据：libero 是步进门控，没有 action 就没有仿真时间，也就没有
# 「空拍」可数（策略不发消息时 bridge 收不到任何事件）——只能用 tick 计时。
DEFAULT_STARVATION_TIMEOUT_MS = 10000
# 一个段内被拒动作（缺维/NaN）达到此数 → C2（策略输出坏了，交回 VLM）
DEFAULT_MAX_INVALID_ACTIONS_PER_SEGMENT = 5
# edit 偏置的 episode 累计漂移上限（设计：「执行位姿与候选前提的偏差会累积」）
DEFAULT_MAX_CUMULATIVE_EDIT_POS_M = 0.05

# 检查点纪律总开关（默认关 = 冻结行为）：
#   关 — bridge 纯透传，不冻结、不检测、不记账段（piper_hybrid 与 S3 学生基线档案；
#        行为与 vla-grasp 逐字一致）。**S3 的 500 episode 基线必须在关的状态下跑**：
#        开着的失速检测会把一个「本来会耗尽步数预算然后继续」的失败集 episode 冻死。
#   开 — 只对 **decide() 起头的段**生效（`supervised`）：C1 段预算耗尽、C2 事件命中
#        即冻结开检查点；`set_mode` 起头的自发透传不检测（保证基线可复现）。
CHECKPOINTS_DISABLED = "disabled"
CHECKPOINTS_ENABLED = "enabled"
CHECKPOINT_MODES = (CHECKPOINTS_DISABLED, CHECKPOINTS_ENABLED)

# mode 对外用词：
#   legacy   — policy/agent（piper_hybrid 冻结口径，get_status 报这两个词）
#   decision — student/edit/eef/stop（libero 检查点口径）
# 两种词汇都接受为**输入**别名（policy→student、agent→stop），只有输出用词不同。
MODE_VOCABULARY_LEGACY = "legacy"
MODE_VOCABULARY_DECISION = "decision"
MODE_VOCABULARIES = (MODE_VOCABULARY_LEGACY, MODE_VOCABULARY_DECISION)
MODE_TO_LEGACY = {DECISION_STUDENT: "policy", DECISION_STOP: "agent"}

# 某个词汇下节点**接受**的对外模式词（vla.set_mode 的入参）。
# 与 gateway 的 ToolSpec enum 同源：两者都由 profile 的 mode_vocabulary 决定，
# 所以不会出现「规格对外宣称四档、节点只认两档」的自相矛盾（S4a 修过的那个 bug）。
#   legacy   → ("policy", "agent")                 —— piper_hybrid 冻结契约
#   decision → 四档 + policy/agent 两个别名         —— libero_bench
ACCEPTED_MODES_LEGACY = tuple(MODE_TO_LEGACY.values())
ACCEPTED_MODES_DECISION = DECISION_MODES + tuple(sorted(MODE_ALIASES))


def accepted_mode_words(mode_vocabulary: str) -> tuple[str, ...]:
    return ACCEPTED_MODES_DECISION if mode_vocabulary == MODE_VOCABULARY_DECISION else ACCEPTED_MODES_LEGACY


def _str_list(raw: Any, key: str) -> list[str]:
    if not isinstance(raw, list) or not raw or not all(isinstance(v, str) and v for v in raw):
        raise ConfigError(f"{key} must be a non-empty list of strings")
    return [str(v) for v in raw]


@dataclass(frozen=True)
class VlaBridgeConfig:
    action_space: str
    endpoint_id: str
    policy_id: str
    initial_mode: str
    # joint: 被限幅的关节名（顺序即输出顺序）；eef_delta: 动作维度标签（按序，
    # 名字只是标签，位置才是契约）。
    arm_joints: list[str]
    # joint 专用；eef_delta 下为空 dict（该档案不做关节限位）。
    arm_limits: dict[str, tuple[float, float]]
    gripper_joint: str
    gripper_limits: tuple[float, float]
    gripper_policy_scale: float
    max_proprio_age_ms: int
    max_joint_step_rad: float
    proprio_gate: str
    registration: RegistrationConfig
    counters_log_every: int = 500
    # 检查点仲裁（S4）
    max_segment_steps: int = DEFAULT_MAX_SEGMENT_STEPS
    max_eef_segment_steps: int = DEFAULT_MAX_EEF_SEGMENT_STEPS
    edit_max_pos_m: float = DEFAULT_EDIT_MAX_POS_M
    edit_max_rot_rad: float = DEFAULT_EDIT_MAX_ROT_RAD
    edit_ramp_steps: int = DEFAULT_EDIT_RAMP_STEPS
    # eef 档（S4c）
    eef_pos_unit_m: float = DEFAULT_EEF_POS_UNIT_M
    eef_rot_unit_rad: float = DEFAULT_EEF_ROT_UNIT_RAD
    eef_pos_tolerance_m: float = DEFAULT_EEF_POS_TOLERANCE_M
    eef_max_reach_m: float = DEFAULT_EEF_MAX_REACH_M
    eef_min_z_m: float = DEFAULT_EEF_MIN_Z_M
    rtc_merge_period_steps: int = DEFAULT_RTC_MERGE_PERIOD_STEPS
    max_empty_tick_streak: int = DEFAULT_MAX_EMPTY_TICK_STREAK
    stall_window_steps: int = DEFAULT_STALL_WINDOW_STEPS
    stall_min_displacement_m: float = DEFAULT_STALL_MIN_DISPLACEMENT_M
    starvation_timeout_ms: int = DEFAULT_STARVATION_TIMEOUT_MS
    max_invalid_actions_per_segment: int = DEFAULT_MAX_INVALID_ACTIONS_PER_SEGMENT
    max_cumulative_edit_pos_m: float = DEFAULT_MAX_CUMULATIVE_EDIT_POS_M
    checkpoints: str = CHECKPOINTS_DISABLED
    mode_vocabulary: str = MODE_VOCABULARY_LEGACY
    # joint_abs 专用：夹爪维度的下标（edit.gripper 落点）与 edit 每关节上限（rad）
    gripper_indices: tuple[int, ...] = ()
    edit_max_joint_rad: float = 0.1
    max_cumulative_edit_joint_rad: float = 0.3
    # 每个 episode 开始（benchmark reset_scene）都回到持有并开 C0：一个批次多集时
    # 每集都受监督（robotwin 的种子游标按批次重置，协议要求同批连续取种子）。
    hold_on_episode_start: bool = False
    # 放行后仍跑 C2 检测（robotwin 的 gpt6_pi05 需要；piper / S3 基线保持 False）
    detect_on_released_segments: bool = False
    c2_cooldown_steps: int = 0
    # joint_abs 的失速阈值（rad）；None = 沿用 stall_min_displacement_m
    stall_min_joint_rad: float | None = None

    @property
    def is_eef_delta(self) -> bool:
        return self.action_space == ACTION_SPACE_EEF_DELTA

    @property
    def is_joint_abs(self) -> bool:
        return self.action_space == ACTION_SPACE_JOINT_ABS

    @property
    def single_action_output(self) -> bool:
        """eef_delta / joint_abs 都是单一 `action` 输出（下游 benchmark 按位置取值）。"""
        return self.action_space in (ACTION_SPACE_EEF_DELTA, ACTION_SPACE_JOINT_ABS)

    @property
    def checkpoints_enabled(self) -> bool:
        return self.checkpoints == CHECKPOINTS_ENABLED

    @property
    def max_cumulative_edit_pos_units(self) -> float:
        return self.max_cumulative_edit_pos_m / OSC_POS_UNIT_M

    def report_mode(self, mode: str) -> str:
        """把内部规范模式翻译成该档案的对外用词。"""
        if self.mode_vocabulary == MODE_VOCABULARY_DECISION:
            return mode
        return MODE_TO_LEGACY.get(mode, mode)

    @property
    def accepted_modes(self) -> tuple[str, ...]:
        """vla.set_mode 在该档案下接受的对外词（见 accepted_mode_words）。"""
        return accepted_mode_words(self.mode_vocabulary)

    @property
    def edit_max_pos_units(self) -> float:
        """edit 位置偏置的段内累计上限，换算成动作单位。"""
        return self.edit_max_pos_m / OSC_POS_UNIT_M

    @property
    def edit_max_rot_units(self) -> float:
        return self.edit_max_rot_rad / OSC_ROT_UNIT_RAD


def _parse_limits(raw: Any, key: str) -> dict[str, tuple[float, float]]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{key} must be a mapping of joint -> [min, max]")
    limits: dict[str, tuple[float, float]] = {}
    for name, span in raw.items():
        if (
            not isinstance(span, (list, tuple))
            or len(span) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in span)
            or float(span[0]) >= float(span[1])
        ):
            raise ConfigError(f"{key}.{name} must be [min, max] with min < max")
        limits[str(name)] = (float(span[0]), float(span[1]))
    return limits


def load_bridge_config(path: Path | str) -> VlaBridgeConfig:
    raw = load_yaml(path)

    endpoint_id = raw.get("endpoint_id", "vla.bridge")
    if not isinstance(endpoint_id, str) or not endpoint_id:
        raise ConfigError("endpoint_id must be a non-empty string")

    policy_id = raw.get("policy_id", "default")
    if not isinstance(policy_id, str) or not policy_id:
        raise ConfigError("policy_id must be a non-empty string")

    initial_mode = raw.get("initial_mode", "agent")
    if initial_mode not in ("agent", "policy"):
        raise ConfigError("initial_mode must be 'agent' or 'policy' (start safe: agent)")

    action_space = raw.get("action_space", ACTION_SPACE_JOINT)
    if action_space not in ACTION_SPACES:
        raise ConfigError(f"action_space must be one of {list(ACTION_SPACES)}")

    gripper_indices: tuple[int, ...] = ()
    if action_space == ACTION_SPACE_EEF_DELTA:
        arm_joints, arm_limits, gripper_joint, gripper_limits = _parse_eef_delta_space(raw)
    elif action_space == ACTION_SPACE_JOINT_ABS:
        arm_joints, gripper_indices = _parse_joint_abs_space(raw)
        arm_limits, gripper_joint, gripper_limits = {}, arm_joints[gripper_indices[-1]], DEFAULT_GRIPPER_LIMITS
    else:
        arm_joints, arm_limits, gripper_joint, gripper_limits = _parse_joint_space(raw)

    scale = raw.get("gripper_policy_scale", DEFAULT_GRIPPER_POLICY_SCALE)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or float(scale) <= 0:
        raise ConfigError("gripper_policy_scale must be a positive number")

    max_proprio_age_ms = raw.get("max_proprio_age_ms", 200)
    if isinstance(max_proprio_age_ms, bool) or not isinstance(max_proprio_age_ms, int) or max_proprio_age_ms < 1:
        raise ConfigError("max_proprio_age_ms must be a positive integer")

    max_joint_step_rad = raw.get("max_joint_step_rad", 0.08)
    if (
        isinstance(max_joint_step_rad, bool)
        or not isinstance(max_joint_step_rad, (int, float))
        or float(max_joint_step_rad) <= 0
    ):
        raise ConfigError("max_joint_step_rad must be a positive number")

    counters_log_every = raw.get("counters_log_every", 500)
    if isinstance(counters_log_every, bool) or not isinstance(counters_log_every, int) or counters_log_every < 1:
        raise ConfigError("counters_log_every must be a positive integer")

    # 不写就按动作空间取默认：joint→age（piper 行为不变），eef_delta→seen（否则死锁）
    proprio_gate = raw.get("proprio_gate", DEFAULT_PROPRIO_GATE[action_space])
    if proprio_gate not in PROPRIO_GATES:
        raise ConfigError(f"proprio_gate must be one of {list(PROPRIO_GATES)}")

    def _positive_int(key: str, default: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"{key} must be a positive integer")
        return value

    def _positive_float(key: str, default: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
            raise ConfigError(f"{key} must be a positive number")
        return float(value)

    def _float(key: str, default: float) -> float:
        """可正可负的有限数（eef_min_z_m 需要，桌面以下的合法值本来就是负的）。"""
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ConfigError(f"{key} must be a finite number")
        return float(value)

    max_segment_steps = _positive_int("max_segment_steps", DEFAULT_MAX_SEGMENT_STEPS)
    max_eef_segment_steps = _positive_int("max_eef_segment_steps", DEFAULT_MAX_EEF_SEGMENT_STEPS)
    if max_eef_segment_steps > max_segment_steps:
        raise ConfigError("max_eef_segment_steps must not exceed max_segment_steps")

    checkpoints = raw.get("checkpoints", CHECKPOINTS_DISABLED)
    if checkpoints not in CHECKPOINT_MODES:
        raise ConfigError(f"checkpoints must be one of {list(CHECKPOINT_MODES)}")

    mode_vocabulary = raw.get("mode_vocabulary", MODE_VOCABULARY_LEGACY)
    if mode_vocabulary not in MODE_VOCABULARIES:
        raise ConfigError(f"mode_vocabulary must be one of {list(MODE_VOCABULARIES)}")

    return VlaBridgeConfig(
        action_space=action_space,
        endpoint_id=endpoint_id,
        policy_id=policy_id,
        initial_mode=initial_mode,
        arm_joints=arm_joints,
        arm_limits=arm_limits,
        gripper_joint=gripper_joint,
        gripper_limits=gripper_limits,
        gripper_policy_scale=float(scale),
        max_proprio_age_ms=max_proprio_age_ms,
        max_joint_step_rad=float(max_joint_step_rad),
        proprio_gate=proprio_gate,
        registration=parse_registration(raw),
        counters_log_every=counters_log_every,
        max_segment_steps=max_segment_steps,
        max_eef_segment_steps=max_eef_segment_steps,
        edit_max_pos_m=_positive_float("edit_max_pos_m", DEFAULT_EDIT_MAX_POS_M),
        edit_max_rot_rad=_positive_float("edit_max_rot_rad", DEFAULT_EDIT_MAX_ROT_RAD),
        edit_ramp_steps=_positive_int("edit_ramp_steps", DEFAULT_EDIT_RAMP_STEPS),
        eef_pos_unit_m=_positive_float("eef_pos_unit_m", DEFAULT_EEF_POS_UNIT_M),
        eef_rot_unit_rad=_positive_float("eef_rot_unit_rad", DEFAULT_EEF_ROT_UNIT_RAD),
        eef_pos_tolerance_m=_positive_float("eef_pos_tolerance_m", DEFAULT_EEF_POS_TOLERANCE_M),
        eef_max_reach_m=_positive_float("eef_max_reach_m", DEFAULT_EEF_MAX_REACH_M),
        eef_min_z_m=_float("eef_min_z_m", DEFAULT_EEF_MIN_Z_M),
        rtc_merge_period_steps=_positive_int("rtc_merge_period_steps", DEFAULT_RTC_MERGE_PERIOD_STEPS),
        max_empty_tick_streak=_positive_int("max_empty_tick_streak", DEFAULT_MAX_EMPTY_TICK_STREAK),
        stall_window_steps=_positive_int("stall_window_steps", DEFAULT_STALL_WINDOW_STEPS),
        stall_min_displacement_m=_positive_float("stall_min_displacement_m", DEFAULT_STALL_MIN_DISPLACEMENT_M),
        starvation_timeout_ms=_positive_int("starvation_timeout_ms", DEFAULT_STARVATION_TIMEOUT_MS),
        max_invalid_actions_per_segment=_positive_int(
            "max_invalid_actions_per_segment", DEFAULT_MAX_INVALID_ACTIONS_PER_SEGMENT
        ),
        max_cumulative_edit_pos_m=_positive_float(
            "max_cumulative_edit_pos_m", DEFAULT_MAX_CUMULATIVE_EDIT_POS_M
        ),
        checkpoints=checkpoints,
        mode_vocabulary=mode_vocabulary,
        gripper_indices=gripper_indices,
        edit_max_joint_rad=_positive_float("edit_max_joint_rad", 0.1),
        max_cumulative_edit_joint_rad=_positive_float("max_cumulative_edit_joint_rad", 0.3),
        hold_on_episode_start=bool(raw.get("hold_on_episode_start", False)),
        detect_on_released_segments=bool(raw.get("detect_on_released_segments", False)),
        c2_cooldown_steps=int(raw.get("c2_cooldown_steps", 0) or 0),
        stall_min_joint_rad=(
            None
            if raw.get("stall_min_joint_rad") is None
            else _positive_float("stall_min_joint_rad", DEFAULT_STALL_MIN_DISPLACEMENT_M)
        ),
    )


def _parse_joint_space(
    raw: dict[str, Any],
) -> tuple[list[str], dict[str, tuple[float, float]], str, tuple[float, float]]:
    """piper 口径：关节名必填、限位取 PIPER.xml 默认并可覆盖、夹爪单列。"""
    arm_joints = _str_list(raw.get("arm_joints"), "arm_joints")
    arm_limits: dict[str, tuple[float, float]] = dict(DEFAULT_ARM_LIMITS)
    arm_limits.update(_parse_limits(raw.get("arm_limits", {}), "arm_limits"))
    missing = [joint for joint in arm_joints if joint not in arm_limits]
    if missing:
        raise ConfigError(f"arm_limits missing entries for {missing}")

    gripper_joint = raw.get("gripper_joint", "gripper")
    if not isinstance(gripper_joint, str) or not gripper_joint:
        raise ConfigError("gripper_joint must be a non-empty string")
    gripper_raw = raw.get("gripper_limits", list(DEFAULT_GRIPPER_LIMITS))
    gripper_limits = _parse_limits({"gripper": gripper_raw}, "gripper_limits")["gripper"]
    return arm_joints, arm_limits, gripper_joint, gripper_limits


def _parse_joint_abs_space(raw: dict[str, Any]) -> tuple[list[str], tuple[int, ...]]:
    """robotwin 口径：N 维绝对关节目标，名字 = proprio/动作的关节名（按序），
    `gripper_joints` 列出其中的夹爪维（edit.gripper 的落点，值域 [0,1]，1=张开）。"""
    labels = _str_list(raw.get("action_joints"), "action_joints")
    if len(set(labels)) != len(labels):
        raise ConfigError("action_joints must be unique")
    grippers = _str_list(raw.get("gripper_joints"), "gripper_joints")
    missing = [g for g in grippers if g not in labels]
    if missing:
        raise ConfigError(f"gripper_joints not in action_joints: {missing}")
    return labels, tuple(labels.index(g) for g in grippers)


def _parse_eef_delta_space(
    raw: dict[str, Any],
) -> tuple[list[str], dict[str, tuple[float, float]], str, tuple[float, float]]:
    """libero 口径：单一 7 维笛卡尔增量向量，动作即契约，无关节限位/无换算。

    这里返回的 gripper 字段只是占位（该档案不用），语义都靠 `action_joints`：
    顺序 = 输出 JointCommand 的名字顺序，下游 benchmark 节点按位置取值
    （前 6 位 delta pose、第 7 位夹爪），所以维度必须自己写对。
    """
    labels_raw = raw.get("action_joints")
    if labels_raw is None:
        labels_raw = raw.get("arm_joints")
    labels = _str_list(labels_raw, "action_joints")
    if len(labels) != LIBERO_ACTION_DIM:
        raise ConfigError(
            f"action_joints must have exactly {LIBERO_ACTION_DIM} entries for "
            f"{ACTION_SPACE_EEF_DELTA}, got {len(labels)}"
        )
    return (
        labels,
        {},
        labels[-1],
        DEFAULT_GRIPPER_LIMITS,
    )


__all__ = [
    "ACTION_SPACE_EEF_DELTA",
    "ACTION_SPACE_JOINT",
    "ACTION_SPACE_JOINT_ABS",
    "ACTION_SPACES",
    "CHECKPOINTS_DISABLED",
    "CHECKPOINTS_ENABLED",
    "CHECKPOINT_MODES",
    "DECISION_EDIT",
    "DECISION_EEF",
    "DECISION_MODES",
    "DECISION_STOP",
    "DECISION_STUDENT",
    "DEFAULT_ARM_LIMITS",
    "DEFAULT_EDIT_MAX_POS_M",
    "DEFAULT_EDIT_MAX_ROT_RAD",
    "DEFAULT_EDIT_RAMP_STEPS",
    "DEFAULT_EEF_MAX_REACH_M",
    "DEFAULT_EEF_MIN_Z_M",
    "DEFAULT_EEF_POS_TOLERANCE_M",
    "DEFAULT_EEF_POS_UNIT_M",
    "DEFAULT_EEF_ROT_UNIT_RAD",
    "DEFAULT_GRIPPER_LIMITS",
    "DEFAULT_GRIPPER_POLICY_SCALE",
    "DEFAULT_MAX_CUMULATIVE_EDIT_POS_M",
    "DEFAULT_MAX_EEF_SEGMENT_STEPS",
    "DEFAULT_MAX_EMPTY_TICK_STREAK",
    "DEFAULT_MAX_INVALID_ACTIONS_PER_SEGMENT",
    "DEFAULT_MAX_SEGMENT_STEPS",
    "DEFAULT_PROPRIO_GATE",
    "DEFAULT_RTC_MERGE_PERIOD_STEPS",
    "DEFAULT_STALL_MIN_DISPLACEMENT_M",
    "DEFAULT_STALL_WINDOW_STEPS",
    "DEFAULT_STARVATION_TIMEOUT_MS",
    "FORWARDING_MODES",
    "LIBERO_ACTION_DIM",
    "MODE_ALIASES",
    "MODE_TO_LEGACY",
    "MODE_VOCABULARIES",
    "MODE_VOCABULARY_DECISION",
    "MODE_VOCABULARY_LEGACY",
    "OSC_POS_UNIT_M",
    "OSC_ROT_UNIT_RAD",
    "PROPRIO_GATES",
    "PROPRIO_GATE_AGE",
    "PROPRIO_GATE_SEEN",
    "VlaBridgeConfig",
    "load_bridge_config",
]

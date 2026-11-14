"""策略演示的确定性 domain-randomization 计划。

sampler 只产生不可变的 episode 计划。simulator adapter 必须在
采集之前一次性应用实际得到的 camera pose，并且必须用机器人
命令执行 grasp 计划。采样出一个值并不证明 episode 在物理上
有效；得到的 rollout 仍需通过常规的物理与观测
门禁。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import random
from typing import Mapping, Sequence
from datafarm.common import sha256_text


Vector3 = tuple[float, float, float]


@dataclass(frozen=True)
class PosePerturbationBounds:
    """围绕标定 pose 的独立对称 SE(3) 残差边界。"""

    translation_half_range_m: Vector3
    rotation_half_range_deg: Vector3

    def __post_init__(self) -> None:
        values = self.translation_half_range_m + self.rotation_half_range_deg
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("pose perturbation bounds must be finite and non-negative")


@dataclass(frozen=True)
class CameraPerturbation:
    camera_id: str
    translation_m: Vector3
    rotation_deg: Vector3
    episode_constant: bool = True


@dataclass(frozen=True)
class CameraRandomizationPolicy:
    """逐相机的、episode 恒定的外参随机化策略。"""

    bounds: Mapping[str, PosePerturbationBounds]
    intrinsics_fixed: bool = True
    per_frame_jitter: bool = False

    def __post_init__(self) -> None:
        required = {"head_camera", "left_wrist_camera", "right_wrist_camera"}
        if set(self.bounds) != required:
            raise ValueError(f"camera bounds must be exactly {sorted(required)}")
        if self.per_frame_jitter:
            raise ValueError("policy demonstrations forbid per-frame camera jitter")


@dataclass(frozen=True)
class GraspRandomizationPolicy:
    """普通 grasp 抖动与显式可恢复 miss 的边界。"""

    nominal_xy_half_range_m: float = 0.004
    nominal_z_half_range_m: float = 0.003
    nominal_yaw_half_range_deg: float = 3.0
    recoverable_miss_fraction: float = 0.25
    recoverable_miss_radius_m: tuple[float, float] = (0.018, 0.030)
    recovery_lift_m: float = 0.035
    maximum_realignments: int = 1
    lateral_realign_fraction: float = 0.25
    lateral_realign_offset_m: tuple[float, float] = (0.018, 0.030)
    lateral_realign_directions: tuple[str, ...] = ("forward", "backward", "left", "right")

    def __post_init__(self) -> None:
        scalars = (
            self.nominal_xy_half_range_m,
            self.nominal_z_half_range_m,
            self.nominal_yaw_half_range_deg,
            self.recovery_lift_m,
        )
        if any(not math.isfinite(value) or value < 0 for value in scalars):
            raise ValueError("grasp randomization magnitudes must be finite and non-negative")
        if not 0.0 <= self.recoverable_miss_fraction <= 1.0:
            raise ValueError("recoverable_miss_fraction must be in [0, 1]")
        low, high = self.recoverable_miss_radius_m
        if not (0.0 < low <= high and math.isfinite(low) and math.isfinite(high)):
            raise ValueError("recoverable miss radius must be a positive ordered range")
        if self.maximum_realignments != 1:
            raise ValueError("the production teaching trace permits exactly one realignment")
        if not 0.0 <= self.lateral_realign_fraction <= 1.0:
            raise ValueError("lateral_realign_fraction must be in [0, 1]")
        low, high = self.lateral_realign_offset_m
        if not (0.0 < low <= high and math.isfinite(low) and math.isfinite(high)):
            raise ValueError("lateral realign offset must be a positive ordered range")
        if set(self.lateral_realign_directions) != set(LATERAL_REALIGN_DIRECTIONS):
            raise ValueError(f"lateral realign must cover {LATERAL_REALIGN_DIRECTIONS}")


LATERAL_REALIGN_DIRECTIONS = ("forward", "backward", "left", "right")
LATERAL_REALIGN_UNIT: dict[str, tuple[float, float]] = {
    "forward": (1.0, 0.0), "backward": (-1.0, 0.0), "left": (0.0, 1.0), "right": (0.0, -1.0),
}


@dataclass(frozen=True)
class GraspPerturbation:
    object_id: str
    arm: str
    mode: str
    nominal_offset_xyz_m: Vector3
    yaw_offset_deg: float
    miss_offset_xy_m: tuple[float, float] | None
    recovery_lift_m: float
    maximum_realignments: int
    realign_direction: str | None = None
    realign_offset_xy_m: tuple[float, float] | None = None


@dataclass(frozen=True)
class EpisodeRandomizationPlan:
    schema_version: str
    seed: int
    cameras: tuple[CameraPerturbation, ...]
    grasps: tuple[GraspPerturbation, ...]
    intrinsics_fixed: bool
    per_frame_camera_jitter: bool
    digest: str


@dataclass(frozen=True)
class RecoveryTrace:
    """针对一次计划内可恢复 miss、由 simulator 产生的证据。"""

    object_id: str
    arm: str
    phases: tuple[str, ...]
    close_count: int
    open_count: int
    object_attached_before_verified_close: bool
    object_pose_writes: int
    final_grasp_verified: bool
    active_wrist_reacquired_object: bool
    realign_clearance_m: float | None = None
    revisited_pose: bool | None = None


@dataclass(frozen=True)
class RecoveryAudit:
    passed: bool
    checks: Mapping[str, bool]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class CameraFramePerturbation:
    camera_id: str
    frame_index: int
    translation_m: Vector3
    rotation_deg: Vector3


@dataclass(frozen=True)
class CameraRealizationAudit:
    passed: bool
    checks: Mapping[str, bool]
    failures: tuple[str, ...]


def default_camera_randomization_policy() -> CameraRandomizationPolicy:
    """围绕标定名义 pose 的保守默认配置。

    边界为 episode 恒定。head 边界比 wrist 边界更紧；任务可以在自己的 held-out sweep
    之后替换两者。
    """

    return CameraRandomizationPolicy(
        bounds={
            "head_camera": PosePerturbationBounds(
                translation_half_range_m=(0.003, 0.003, 0.003),
                rotation_half_range_deg=(0.5, 0.5, 0.5),
            ),
            "left_wrist_camera": PosePerturbationBounds(
                translation_half_range_m=(0.002, 0.002, 0.002),
                rotation_half_range_deg=(0.75, 0.75, 0.75),
            ),
            "right_wrist_camera": PosePerturbationBounds(
                translation_half_range_m=(0.002, 0.002, 0.002),
                rotation_half_range_deg=(0.75, 0.75, 0.75),
            ),
        }
    )


def _uniform_vector(rng: random.Random, half_range: Vector3) -> Vector3:
    return tuple(rng.uniform(-value, value) for value in half_range)  # type: ignore[return-value]


def _canonical_payload(
    seed: int,
    cameras: Sequence[CameraPerturbation],
    grasps: Sequence[GraspPerturbation],
    *,
    intrinsics_fixed: bool,
    per_frame_camera_jitter: bool,
) -> dict[str, object]:
    return {
        "schema_version": "datafarm.episode-randomization/1.0.0",
        "seed": seed,
        "cameras": [row.__dict__ for row in cameras],
        "grasps": [row.__dict__ for row in grasps],
        "intrinsics_fixed": intrinsics_fixed,
        "per_frame_camera_jitter": per_frame_camera_jitter,
    }


def sample_episode_randomization(
    *,
    seed: int,
    object_arm_order: Sequence[tuple[str, str]],
    camera_policy: CameraRandomizationPolicy | None = None,
    grasp_policy: GraspRandomizationPolicy | None = None,
) -> EpisodeRandomizationPlan:
    """创建确定性、按内容寻址的 episode 随机化计划。"""

    camera_policy = camera_policy or default_camera_randomization_policy()
    grasp_policy = grasp_policy or GraspRandomizationPolicy()
    rng = random.Random(seed)
    cameras = tuple(
        CameraPerturbation(
            camera_id=camera_id,
            translation_m=_uniform_vector(rng, bounds.translation_half_range_m),
            rotation_deg=_uniform_vector(rng, bounds.rotation_half_range_deg),
        )
        for camera_id, bounds in sorted(camera_policy.bounds.items())
    )
    grasps: list[GraspPerturbation] = []
    realign_cursor = seed % len(LATERAL_REALIGN_DIRECTIONS)
    for position, (object_id, arm) in enumerate(object_arm_order):
        if arm not in {"left", "right"}:
            raise ValueError(f"unknown arm {arm!r}")
        nominal = (
            rng.uniform(-grasp_policy.nominal_xy_half_range_m, grasp_policy.nominal_xy_half_range_m),
            rng.uniform(-grasp_policy.nominal_xy_half_range_m, grasp_policy.nominal_xy_half_range_m),
            rng.uniform(-grasp_policy.nominal_z_half_range_m, grasp_policy.nominal_z_half_range_m),
        )
        # 横向 realign 是不回撤的恢复方式：机械臂先带偏移
        # 到达物体上方，水平滑到暂存点再下降。不会重访
        # 任何 pose，因此同一次观测绝不会承载
        # 两种不同的未来。
        realign = rng.random() < grasp_policy.lateral_realign_fraction
        realign_direction = None
        realign_offset = None
        if realign:
            realign_direction = grasp_policy.lateral_realign_directions[
                (realign_cursor + position) % len(grasp_policy.lateral_realign_directions)
            ]
            magnitude = rng.uniform(*grasp_policy.lateral_realign_offset_m)
            unit = LATERAL_REALIGN_UNIT[realign_direction]
            realign_offset = (magnitude * unit[0], magnitude * unit[1])
        recover = (not realign) and rng.random() < grasp_policy.recoverable_miss_fraction
        miss_offset = None
        if recover:
            radius = rng.uniform(*grasp_policy.recoverable_miss_radius_m)
            theta = rng.uniform(-math.pi, math.pi)
            miss_offset = (radius * math.cos(theta), radius * math.sin(theta))
        grasps.append(
            GraspPerturbation(
                object_id=object_id,
                arm=arm,
                mode="lateral_realign" if realign else ("recoverable_miss" if recover else "nominal_jitter"),
                nominal_offset_xyz_m=nominal,
                yaw_offset_deg=rng.uniform(
                    -grasp_policy.nominal_yaw_half_range_deg,
                    grasp_policy.nominal_yaw_half_range_deg,
                ),
                miss_offset_xy_m=miss_offset,
                recovery_lift_m=grasp_policy.recovery_lift_m if recover else 0.0,
                maximum_realignments=grasp_policy.maximum_realignments if (recover or realign) else 0,
                realign_direction=realign_direction,
                realign_offset_xy_m=realign_offset,
            )
        )
    payload = _canonical_payload(
        seed,
        cameras,
        grasps,
        intrinsics_fixed=camera_policy.intrinsics_fixed,
        per_frame_camera_jitter=camera_policy.per_frame_jitter,
    )
    digest = sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return EpisodeRandomizationPlan(
        schema_version=str(payload["schema_version"]),
        seed=seed,
        cameras=cameras,
        grasps=tuple(grasps),
        intrinsics_fixed=camera_policy.intrinsics_fixed,
        per_frame_camera_jitter=camera_policy.per_frame_jitter,
        digest=digest,
    )


def audit_lateral_realign_trace(trace: RecoveryTrace, *, min_clearance_m: float = 0.0) -> RecoveryAudit:
    """除非偏移接近由一次水平滑动完成，否则 fail closed。

    与回撤再返回的恢复不同，此 trace 绝不能踏回它已经保持过的
    pose：机械臂带偏移在物体上方接近，在同一高度水平平移到
    暂存点，然后下降。``clearance_m`` 是 realign 段在物体
    上方的最低高度，``revisited_pose`` 报告是否有任何后续
    pose 重复了更早的 pose（那样一次观测就会有两种
    未来）。
    """

    required = ("offset_approach", "lateral_realign", "verified_close")
    cursor = -1
    ordered = True
    for phase in required:
        try:
            cursor = trace.phases.index(phase, cursor + 1)
        except ValueError:
            ordered = False
            break
    checks = {
        "ordered_realign_phases": ordered,
        "no_retract_phase": not any(phase in {"retract", "visual_reacquire", "recenter"} for phase in trace.phases),
        "realign_stays_above_object": trace.realign_clearance_m is not None and trace.realign_clearance_m > min_clearance_m,
        "no_revisited_pose": trace.revisited_pose is False,
        "single_close_open": trace.close_count == 1 and trace.open_count == 1,
        "no_hidden_attachment": not trace.object_attached_before_verified_close,
        "robot_only_object_motion": trace.object_pose_writes == 0,
        "active_wrist_reacquired_object": trace.active_wrist_reacquired_object,
        "final_grasp_verified": trace.final_grasp_verified,
    }
    return RecoveryAudit(
        passed=all(checks.values()),
        checks=checks,
        failures=tuple(name for name, passed in checks.items() if not passed),
    )


def audit_recovery_trace(trace: RecoveryTrace) -> RecoveryAudit:
    """除非 miss 由纯机器人动作的干净序列恢复，否则 fail closed。"""

    required = (
        "misaligned_approach",
        "retract",
        "visual_reacquire",
        "recenter",
        "verified_close",
    )
    cursor = -1
    ordered = True
    for phase in required:
        try:
            cursor = trace.phases.index(phase, cursor + 1)
        except ValueError:
            ordered = False
            break
    checks = {
        "ordered_recovery_phases": ordered,
        "single_close_open": trace.close_count == 1 and trace.open_count == 1,
        "no_hidden_attachment": not trace.object_attached_before_verified_close,
        "robot_only_object_motion": trace.object_pose_writes == 0,
        "active_wrist_reacquired_object": trace.active_wrist_reacquired_object,
        "final_grasp_verified": trace.final_grasp_verified,
    }
    return RecoveryAudit(
        passed=all(checks.values()),
        checks=checks,
        failures=tuple(name for name, passed in checks.items() if not passed),
    )


def audit_camera_realization(
    plan: EpisodeRandomizationPlan,
    frames: Sequence[CameraFramePerturbation],
    *,
    expected_frame_count: int,
    tolerance: float = 1e-9,
) -> CameraRealizationAudit:
    """校验每个实际实现的相机都停留在其计划的 episode pose。"""

    planned = {row.camera_id: row for row in plan.cameras}
    grouped = {camera_id: [] for camera_id in planned}
    unknown = False
    for row in frames:
        if row.camera_id not in grouped:
            unknown = True
            continue
        grouped[row.camera_id].append(row)
    coverage = expected_frame_count > 0 and all(
        sorted(row.frame_index for row in rows) == list(range(expected_frame_count))
        for rows in grouped.values()
    )

    def close(a: Sequence[float], b: Sequence[float]) -> bool:
        return all(abs(x - y) <= tolerance for x, y in zip(a, b))

    matches_plan = coverage and all(
        close(row.translation_m, planned[camera_id].translation_m)
        and close(row.rotation_deg, planned[camera_id].rotation_deg)
        for camera_id, rows in grouped.items()
        for row in rows
    )
    constant = coverage and all(
        all(
            close(row.translation_m, rows[0].translation_m)
            and close(row.rotation_deg, rows[0].rotation_deg)
            for row in rows[1:]
        )
        for rows in grouped.values()
    )
    checks = {
        "known_cameras_only": not unknown,
        "complete_frame_coverage": coverage,
        "realized_pose_matches_plan": matches_plan,
        "episode_constant_no_jitter": constant,
        "intrinsics_fixed_by_plan": plan.intrinsics_fixed,
        "plan_forbids_per_frame_jitter": not plan.per_frame_camera_jitter,
    }
    return CameraRealizationAudit(
        passed=all(checks.values()),
        checks=checks,
        failures=tuple(name for name, passed in checks.items() if not passed),
    )

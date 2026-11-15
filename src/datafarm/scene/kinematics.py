"""机械臂运动学：从 `robot-descriptor/2`（URDF 解析结果）做正解与数值逆解，判断目标位姿是否可达。

只依赖 numpy，不导入仿真器。逆解是带关节限位的阻尼最小二乘，多起点；
"找到解"证明可达，"没找到"只说明这些起点下局部求解未收敛，不能证明全局不可达。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np


def quat_to_matrix(q_xyzw: Sequence[float]) -> np.ndarray:
    x, y, z, w = (float(v) for v in q_xyzw)
    n = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def matrix_to_quat(rotation: np.ndarray) -> list[float]:
    m = rotation
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = 2.0 * math.sqrt(trace + 1.0)
        q = [(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = 2.0 * math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2])
        q = [0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s]
    elif m[1, 1] > m[2, 2]:
        s = 2.0 * math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2])
        q = [(m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s]
    else:
        s = 2.0 * math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1])
        q = [(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s]
    q = np.array(q)
    q /= np.linalg.norm(q)
    return (q if q[3] >= 0 else -q).tolist()


def transform(position: Sequence[float], q_xyzw: Sequence[float]) -> np.ndarray:
    out = np.eye(4)
    out[:3, :3] = quat_to_matrix(q_xyzw)
    out[:3, 3] = np.asarray(position, dtype=float)
    return out


def pose_matrix(pose: Mapping[str, Any]) -> np.ndarray:
    return transform(pose["position_m"], pose["quaternion_xyzw"])


def axis_angle(axis: Sequence[float], angle: float) -> np.ndarray:
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(angle) * k + (1 - math.cos(angle)) * (k @ k)


def rotation_error(target: np.ndarray, current: np.ndarray) -> np.ndarray:
    """旋转误差向量（世界系，轴角），target ≈ exp([e]) current。"""
    r = target @ current.T
    angle = math.acos(max(-1.0, min(1.0, (np.trace(r) - 1.0) / 2.0)))
    if angle < 1e-9:
        return np.zeros(3)
    vec = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    if math.pi - angle < 1e-6:
        # 近 180°：从对称部分取轴
        w, v = np.linalg.eigh((r + r.T) / 2.0)
        return v[:, np.argmax(w)] * angle
    return vec / (2.0 * math.sin(angle)) * angle


@dataclass(frozen=True)
class Chain:
    """从根连杆到末端连杆的一条串联链；`joints` 只含沿途关节（含 fixed）。"""

    base_link: str
    tip_link: str
    joints: tuple[Mapping[str, Any], ...]
    tip_offset: np.ndarray

    @property
    def active(self) -> list[Mapping[str, Any]]:
        return [j for j in self.joints if j["type"] != "fixed"]

    @property
    def names(self) -> list[str]:
        return [j["name"] for j in self.active]

    @property
    def limits(self) -> np.ndarray:
        return np.array([j["limits"]["position"] for j in self.active], dtype=float)

    def fk(self, q: Sequence[float], base: np.ndarray | None = None) -> np.ndarray:
        """末端位姿（4×4），默认在根连杆坐标系。"""
        return self.fk_jacobian(q, base)[0]

    def fk_jacobian(self, q: Sequence[float], base: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        """末端位姿与几何雅可比（6×n，前三行线速度、后三行角速度，世界系）。"""
        t = np.eye(4) if base is None else base.copy()
        values = iter(q)
        axes, origins, kinds = [], [], []
        for joint in self.joints:
            t = t @ pose_matrix(joint["origin"])
            if joint["type"] == "fixed":
                continue
            axis = np.asarray(joint["axis"], dtype=float)
            axis = axis / np.linalg.norm(axis)
            axes.append(t[:3, :3] @ axis)
            origins.append(t[:3, 3].copy())
            kinds.append(joint["type"])
            motion = np.eye(4)
            if joint["type"] == "prismatic":
                motion[:3, 3] = axis * float(next(values))
            else:
                motion[:3, :3] = axis_angle(axis, float(next(values)))
            t = t @ motion
        t = t @ self.tip_offset
        jac = np.zeros((6, len(axes)))
        for k, (axis, origin, kind) in enumerate(zip(axes, origins, kinds)):
            if kind == "prismatic":
                jac[:3, k] = axis
            else:
                jac[:3, k] = np.cross(axis, t[:3, 3] - origin)
                jac[3:, k] = axis
        return t, jac


def chain(descriptor: Mapping[str, Any], tip_link: str, *, tip_offset: Mapping[str, Any] | None = None,
          base_link: str | None = None) -> Chain:
    """沿 parent 关系从 `tip_link` 回溯到 `base_link`（默认根连杆）。"""
    base_link = base_link or descriptor["root_link"]
    by_child = {j["child"]: j for j in descriptor["joints"]}
    path, link = [], tip_link
    while link != base_link:
        joint = by_child.get(link)
        if joint is None:
            raise ValueError(f"{tip_link!r} is not below {base_link!r}")
        path.append(joint)
        link = joint["parent"]
    offset = np.eye(4) if tip_offset is None else pose_matrix(tip_offset)
    return Chain(base_link=base_link, tip_link=tip_link, joints=tuple(reversed(path)), tip_offset=offset)


@dataclass
class IKResult:
    ok: bool
    q: list[float]
    position_error_m: float
    rotation_error_rad: float
    iterations: int
    seed_index: int

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "q": [round(v, 6) for v in self.q],
                "position_error_m": round(self.position_error_m, 6),
                "rotation_error_rad": round(self.rotation_error_rad, 6),
                "iterations": self.iterations, "seed_index": self.seed_index}


def solve_ik(arm: Chain, target: np.ndarray, *, base: np.ndarray | None = None,
             seeds: Sequence[Sequence[float]] | None = None, random_seeds: int = 24, rng_seed: int = 0,
             position_tol_m: float = 1e-3, rotation_tol_rad: float = 1e-2, max_iterations: int = 300,
             damping: float = 0.05, limit_margin: float = 0.0, position_only: bool = False) -> IKResult:
    """多起点阻尼最小二乘；每步把关节夹回 [lower + margin, upper - margin]。"""
    lower, upper = arm.limits[:, 0] + limit_margin, arm.limits[:, 1] - limit_margin
    rng = np.random.default_rng(rng_seed)
    starts = [np.clip(np.asarray(s, dtype=float), lower, upper) for s in (seeds or [])]
    starts.append(np.clip(np.zeros(len(lower)), lower, upper))
    starts += [rng.uniform(lower, upper) for _ in range(random_seeds)]
    best: IKResult | None = None
    for index, q in enumerate(starts):
        q = q.copy()
        for iteration in range(1, max_iterations + 1):
            current, jac = arm.fk_jacobian(q, base)
            dp = target[:3, 3] - current[:3, 3]
            dr = np.zeros(3) if position_only else rotation_error(target[:3, :3], current[:3, :3])
            perr, rerr = float(np.linalg.norm(dp)), float(np.linalg.norm(dr))
            if perr <= position_tol_m and rerr <= rotation_tol_rad:
                break
            error = np.concatenate([dp, dr])
            if position_only:
                jac, error = jac[:3], error[:3]
            step = jac.T @ np.linalg.solve(jac @ jac.T + damping ** 2 * np.eye(len(error)), error)
            norm = float(np.linalg.norm(step))
            if norm > 0.3:
                step *= 0.3 / norm
            moved = np.clip(q + step, lower, upper)
            if float(np.abs(moved - q).max()) < 1e-7:
                break  # 卡在限位或奇异处，换下一个起点
            q = moved
        current = arm.fk(q, base)
        perr = float(np.linalg.norm(target[:3, 3] - current[:3, 3]))
        rerr = 0.0 if position_only else float(np.linalg.norm(rotation_error(target[:3, :3], current[:3, :3])))
        result = IKResult(perr <= position_tol_m and rerr <= rotation_tol_rad, q.tolist(), perr, rerr, iteration, index)
        if result.ok:
            return result
        if best is None or (perr + 0.1 * rerr) < (best.position_error_m + 0.1 * best.rotation_error_rad):
            best = result
    assert best is not None
    return best


def approach_frame(position: Sequence[float], approach: Sequence[float], yaw_rad: float,
                   tool_approach_axis: str = "x") -> np.ndarray:
    """目标位姿：工具的接近轴（工具系 `tool_approach_axis`）指向 `approach`，绕该轴转 `yaw_rad`。"""
    a = np.asarray(approach, dtype=float)
    a /= np.linalg.norm(a)
    helper = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(a, helper)
    u /= np.linalg.norm(u)
    v = np.cross(a, u)
    c, s = math.cos(yaw_rad), math.sin(yaw_rad)
    u, v = c * u + s * v, -s * u + c * v
    columns = {"x": (a, u, v), "y": (v, a, u), "z": (u, v, a)}[tool_approach_axis]
    out = np.eye(4)
    out[:3, :3] = np.column_stack(columns)
    out[:3, 3] = np.asarray(position, dtype=float)
    return out


def reach_target(arm: Chain, base: np.ndarray, position: Sequence[float], approach: Sequence[float], *,
                 tool_approach_axis: str, yaw_samples: int = 12, limit_margin: float = 0.05,
                 rng_seed: int = 0) -> dict[str, Any]:
    """在若干绕接近轴的转角下求逆解；任一转角有解即判定可达。"""
    attempts = []
    for k in range(yaw_samples):
        yaw = 2 * math.pi * k / yaw_samples
        target = approach_frame(position, approach, yaw, tool_approach_axis)
        result = solve_ik(arm, target, base=base, limit_margin=limit_margin, rng_seed=rng_seed + k)
        attempts.append((yaw, result))
        if result.ok:
            break
    yaw, result = min(attempts, key=lambda item: (not item[1].ok, item[1].position_error_m))
    return {"ok": result.ok, "yaw_rad": round(yaw, 6), "yaw_samples_tried": len(attempts), **result.to_dict()}

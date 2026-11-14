"""用于模拟 expert 采集的每个 episode 初始物体摆放。

两种模式，由 orchestrator 根据用户目标选择，并记录在场景的 `placement_mode` 中：
  local_robustness  每个被操作的物体位于其标称位置（由真实 episode 推导）加上均匀
                    抖动，默认 +-30 mm：用于小规模验证运行与调试。
  broad_table       每个物体在可触及的桌面区域内独立且均匀采样：用于训练
                    数据与广泛部署的结论。该区域来自 grounding（`reachable_tabletop_xy`，
                    用机器人的运动学与关节限位在桌面平面上探测得到）。
两种模式都通过对整个布局重新采样来保持物体间的最小距离（默认 80 mm）。规划是其输入与 seed 的
确定性函数，并带有 sha256 摘要；场景采样器按索引读取它。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Any

SCHEMA = "datafarm.object-placement-plan/1"
MODES = ("local_robustness", "broad_table")
DEFAULT_JITTER_M = 0.030
DEFAULT_MIN_DISTANCE_M = 0.080
MAX_TRIES = 1000


def nominal_positions(scene: dict[str, Any], key: str = "objects_initial") -> dict[str, tuple[float, float]]:
    """从 scene[key] 得到 {object: (x, y)}；条目可以是 {"xy": [x, y]}、{"x": x, "y": y} 或 [x, y, ...]。"""
    out = {}
    for name, value in (scene.get(key) or {}).items():
        if isinstance(value, dict) and "xy" in value:
            x, y = value["xy"][:2]
        elif isinstance(value, dict) and "x" in value and "y" in value:
            x, y = value["x"], value["y"]
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            x, y = value[:2]
        else:
            raise ValueError(f"scene {key}.{name}: no x/y position")
        out[name] = (float(x), float(y))
    if not out:
        raise ValueError(f"scene has no object positions under {key!r}")
    return out


def reachable_bounds(reachable: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    region = reachable.get("both_arms", reachable)
    x, y = region.get("x", region.get("x_range")), region.get("y", region.get("y_range"))
    if x is None or y is None:
        raise ValueError("reachable_tabletop_xy needs x and y ranges")
    (x0, x1), (y0, y1) = (float(x[0]), float(x[1])), (float(y[0]), float(y[1]))
    if not (x0 < x1 and y0 < y1):
        raise ValueError(f"empty reachable region x={x}, y={y}")
    return (x0, x1), (y0, y1)


def _spaced(pos: dict[str, tuple[float, float]], min_dist: float) -> bool:
    pts = list(pos.values())
    return all(math.dist(pts[i], pts[j]) >= min_dist for i in range(len(pts)) for j in range(i + 1, len(pts)))


def _inside(p: tuple[float, float], bounds) -> bool:
    (x0, x1), (y0, y1) = bounds
    return x0 <= p[0] <= x1 and y0 <= p[1] <= y1


def sample_layout(rng: random.Random, mode: str, nominals: dict, jitter: float, bounds, min_dist: float) -> dict:
    for _ in range(MAX_TRIES):
        if mode == "local_robustness":
            pos = {n: (x + rng.uniform(-jitter, jitter), y + rng.uniform(-jitter, jitter)) for n, (x, y) in nominals.items()}
            if bounds is not None and not all(_inside(p, bounds) for p in pos.values()):
                continue
        else:
            (x0, x1), (y0, y1) = bounds
            pos = {n: (rng.uniform(x0, x1), rng.uniform(y0, y1)) for n in nominals}
        if _spaced(pos, min_dist):
            return pos
    raise ValueError(f"no {mode} layout of {len(nominals)} objects with {min_dist} m spacing in {MAX_TRIES} tries")


def generate_plan(scene: dict, mode: str, count: int, seed: int, *, jitter_m: float = DEFAULT_JITTER_M,
                  reachable: dict | None = None, min_dist_m: float = DEFAULT_MIN_DISTANCE_M,
                  key: str = "objects_initial") -> dict:
    if mode not in MODES:
        raise ValueError(f"placement mode must be one of {MODES}")
    if count < 1:
        raise ValueError("count must be positive")
    nominals = nominal_positions(scene, key)
    bounds = reachable_bounds(reachable) if reachable is not None else None
    if mode == "broad_table" and bounds is None:
        raise ValueError("broad_table needs reachable_tabletop_xy (grounding)")
    if not _spaced(nominals, min_dist_m):
        raise ValueError(f"the nominal layout itself is closer than {min_dist_m} m")
    rng = random.Random(seed)
    episodes = [{"index": i, "object_xy": {n: [round(v, 6) for v in p]
                                           for n, p in sample_layout(rng, mode, nominals, jitter_m, bounds,
                                                                     min_dist_m).items()}}
                for i in range(count)]
    plan = {"schema": SCHEMA, "placement_mode": mode, "seed": seed, "episode_count": count,
            "object_names": sorted(nominals), "nominals": {n: list(p) for n, p in nominals.items()},
            "policy": {"jitter_m": jitter_m if mode == "local_robustness" else None, "min_object_distance_m": min_dist_m,
                       "reachable_tabletop_xy": reachable},
            "episodes": episodes}
    plan["coverage"] = {n: {"x_span_m": round(max(xs) - min(xs), 6), "y_span_m": round(max(ys) - min(ys), 6)}
                        for n in nominals
                        for xs, ys in [([e["object_xy"][n][0] for e in episodes],
                                        [e["object_xy"][n][1] for e in episodes])]}
    plan["digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return plan


def default_mode(target_count: int, request: str = "") -> str:
    """用户未选择时 orchestrator 的默认值：训练规模的目标用 broad，否则用 local。"""
    text = request.lower()
    if any(k in text for k in ("validation", "validate", "验证")):
        return "local_robustness"
    if target_count >= 50 or any(k in text for k in ("training", "train ", "policy", "broad", "训练")):
        return "broad_table"
    return "local_robustness"

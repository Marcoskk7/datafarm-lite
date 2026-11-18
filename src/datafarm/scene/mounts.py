"""机器人安装标定：`datafarm.robot-mounts/1`（`configs/robot-mounts.json`）。

标定是显式的、按机器人包资产名记录的人写事实：安装板下表面在基座坐标系的哪个 z、
板压住多大的矩形、以及是否有 C 型夹（夹哪个方向的支撑边、竖臂内侧在哪）。
`prepare` 拿实测的基座刚体碰撞包围盒复核这份标定，再把结果写进 Robot AssetRecord；
布局只读 AssetRecord，不读这个文件。这样 IR 自洽，标定漂移在 prepare 就被抓住。
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from datafarm.ir.common import IRError, check

SCHEMA = "datafarm.robot-mounts/1"
AXES = {"+x": (1.0, 0.0), "-x": (-1.0, 0.0), "+y": (0.0, 1.0), "-y": (0.0, -1.0)}
BBOX_TOLERANCE_M = 1e-3


def load(path: Path, package_asset: str) -> dict[str, Any]:
    """读标定文件，返回 `package_asset` 的条目；缺文件、缺资产、形状不对都报错，不给默认值。"""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    check(isinstance(document, Mapping) and document.get("schema") == SCHEMA,
          f"{path}: schema must be {SCHEMA}")
    assets = document.get("assets")
    check(isinstance(assets, Mapping), f"{path}: 'assets' must be an object")
    check(package_asset in assets, f"{path}: no mount calibration for package asset {package_asset!r}; "
                                   f"known: {sorted(assets)}")
    entry = assets[package_asset]
    validate(f"robot-mounts[{package_asset}]", entry)
    return dict(entry)


def validate(label: str, entry: Any) -> None:
    check(isinstance(entry, Mapping), f"{label} must be an object")
    z = entry.get("support_face_z_m")
    check(isinstance(z, (int, float)) and not isinstance(z, bool) and math.isfinite(float(z)),
          f"{label}.support_face_z_m must be a finite number")
    footprint = entry.get("footprint_m")
    check(isinstance(footprint, list) and len(footprint) == 2, f"{label}.footprint_m must be [min_xy, max_xy]")
    for index, corner in enumerate(footprint):
        check(isinstance(corner, list) and len(corner) == 2
              and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
                      for v in corner), f"{label}.footprint_m[{index}] must be two finite numbers")
    check(footprint[0][0] < footprint[1][0] and footprint[0][1] < footprint[1][1],
          f"{label}.footprint_m min must be below max on both axes")
    clamp = entry.get("edge_clamp")
    if clamp is None:
        return
    check(isinstance(clamp, Mapping), f"{label}.edge_clamp must be an object")
    check(clamp.get("axis") in AXES, f"{label}.edge_clamp.axis must be one of {list(AXES)}")
    plane = clamp.get("edge_plane_m")
    check(isinstance(plane, (int, float)) and not isinstance(plane, bool) and math.isfinite(float(plane)),
          f"{label}.edge_clamp.edge_plane_m must be a finite number")


def collision_fields(label: str, entry: Mapping[str, Any], measured: Mapping[str, Any]) -> dict[str, Any]:
    """把标定 + 实测基座刚体碰撞包围盒变成 AssetRecord 的 `collision` 字段，并先复核两者一致。

    `measured` 是 `usd_assets.fixed_base_collision_bbox` 的结果（含中间件，即 C 型夹也在里面）。
    复核抓的是“标定与包不匹配”：把标定抄到别的机器人、或换了包没重量，都过不去。
    """
    lo = [float(v) for v in measured["bbox_min_m"]]
    hi = [float(v) for v in measured["bbox_max_m"]]
    z = float(entry["support_face_z_m"])
    check(lo[2] <= z < hi[2],
          f"{label}: calibrated support_face_z_m {z} is not inside the measured base bbox z [{lo[2]}, {hi[2]}]; "
          "the calibration belongs to a different robot package")
    if entry.get("edge_clamp") is not None:
        check(lo[2] < z,
              f"{label}: an edge clamp needs mounting hardware below support_face_z_m {z}, "
              f"but the measured base bbox bottom is {lo[2]}")
    (fx0, fy0), (fx1, fy1) = entry["footprint_m"]
    check(fx0 >= lo[0] - BBOX_TOLERANCE_M and fy0 >= lo[1] - BBOX_TOLERANCE_M
          and fx1 <= hi[0] + BBOX_TOLERANCE_M and fy1 <= hi[1] + BBOX_TOLERANCE_M,
          f"{label}: calibrated footprint_m {entry['footprint_m']} leaves the measured base bbox xy "
          f"[[{lo[0]}, {lo[1]}], [{hi[0]}, {hi[1]}]]")
    collision = {"kind": "mesh", "base_bbox_min_m": lo, "base_bbox_max_m": hi,
                 "base_support_z_m": z,
                 "base_footprint_min_m": [float(fx0), float(fy0)],
                 "base_footprint_max_m": [float(fx1), float(fy1)]}
    clamp = entry.get("edge_clamp")
    if clamp is None:
        collision["base_mount"] = {"kind": "surface"}
        return collision
    axis = AXES[clamp["axis"]]
    plane = float(clamp["edge_plane_m"])
    index = 0 if clamp["axis"][1] == "x" else 1
    check(lo[index] - BBOX_TOLERANCE_M <= plane <= hi[index] + BBOX_TOLERANCE_M,
          f"{label}: edge_plane_m {plane} is outside the measured base bbox on {clamp['axis']} "
          f"[{lo[index]}, {hi[index]}]")
    span = (fx0, fx1) if index == 0 else (fy0, fy1)
    check(span[0] - BBOX_TOLERANCE_M <= plane <= span[1] + BBOX_TOLERANCE_M,
          f"{label}: edge_plane_m {plane} is outside the calibrated footprint on {clamp['axis']} {list(span)}; "
          "the clamp must hang under the mounting plate")
    collision["base_mount"] = {"kind": "edge_clamp", "axis": clamp["axis"], "edge_plane_m": plane,
                               "overhang_m": round(_overhang(entry), 6)}
    return collision


def _overhang(entry: Mapping[str, Any]) -> float:
    """安装板越过夹持边多少米（板比竖臂内侧再多伸出一点，这部分悬在桌外）。"""
    clamp = entry["edge_clamp"]
    axis = AXES[clamp["axis"]]
    sign = 1.0 if clamp["axis"][0] == "+" else -1.0
    plane_along = sign * float(clamp["edge_plane_m"])  # 桌沿在“朝外方向”上的坐标
    (fx0, fy0), (fx1, fy1) = entry["footprint_m"]
    corners = [(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1)]
    return max(0.0, max(x * axis[0] + y * axis[1] for x, y in corners) - plane_along)


def overhang_m(collision: Mapping[str, Any]) -> float:
    """AssetRecord `collision.base_mount.overhang_m`：允许越出支撑边的板宽，缺省 0。"""
    return float(collision.get("base_mount", {}).get("overhang_m", 0.0))


def raise_without_calibration(label: str) -> None:
    raise IRError(f"{label}: record has no collision.base_support_z_m / base_mount; "
                  "rerun `scene_assemble.py prepare` with configs/robot-mounts.json")

"""从 SimReady 拉取清单 + USD 检查结果生成 AssetRecord（`datafarm.asset-record/1`）。

物理量的 label 规则：
- 质量、摩擦等数值取自资产作者写进 USD 的值，不是我们测得的，也没有拟合依据，所以记 `assumed`，
  并给随机化区间；USD 检查报告作为证据附上，便于追溯数值来源。
- 几何尺寸（包围盒、关节行程）直接读几何，写在 `bbox_m` / `joints` 字段，不进物理量账本。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from datafarm.ir import asset_record
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.scene import mounts

FRICTION_RANGE = (0.5, 1.5)   # 相对 USD 名义值的比例
MASS_RANGE = (0.7, 1.3)
PRISMATIC_DAMPING = 20.0     # N·s/m；上游没写阻尼，Isaac Lab 侧驱动用同一个值


def _ratio_range(value: float, ratio: tuple[float, float]) -> dict[str, float]:
    return {"low": round(value * ratio[0], 6), "high": round(value * ratio[1], 6)}


def body_masses(inspection: Mapping[str, Any]) -> dict[str, float]:
    """每个刚体的质量：刚体上写了 MassAPI 就用它，否则累加其碰撞网格上的 MassAPI（SimReady 的写法）。"""
    authored = {body: float(info["mass_kg"]) for body, info in inspection["bodies"].items() if info.get("mass_kg")}
    summed: dict[str, float] = {}
    for collider in inspection["colliders"]:
        body = collider.get("body")
        if body and body not in authored and collider.get("mass_kg"):
            summed[body] = summed.get(body, 0.0) + float(collider["mass_kg"])
    return {**summed, **authored}


def build_record(*, asset_id: str, role: str, fetch: Mapping[str, Any], inspection: Mapping[str, Any],
                 usd_ref: ArtifactRef, inspection_ref: ArtifactRef, collision_ref: ArtifactRef | None,
                 collision_bbox: tuple[list[float], list[float]] | None = None, notes: str = "") -> dict[str, Any]:
    """关节名保持上游 USD 里的名字，场景 `joint_state` 与 Isaac Lab 绑定都按这个名字查。

    `collision_bbox` 是替换后碰撞几何（凸包）的包围盒；凸包会比渲染网格外扩几毫米，布局按它算支撑高度。
    """
    lo, hi = inspection["bbox_min_m"], inspection["bbox_max_m"]
    bbox = [round(h - l, 5) for l, h in zip(lo, hi)]
    joints = []
    for joint in inspection["joints"]:
        if joint["type"] not in ("prismatic", "revolute"):
            continue
        joints.append({
            "name": joint["name"],
            "type": joint["type"],
            "axis": joint["axis"],
            "limits": {"lower": round(joint["limits"]["lower"], 6), "upper": round(joint["limits"]["upper"], 6)},
            "parent": joint["parent"].rsplit("/", 1)[-1],
            "child": joint["child"].rsplit("/", 1)[-1],
        })
    evidence = [inspection_ref.to_dict()]
    quantities = []
    total_mass = sum(body_masses(inspection).values())
    if total_mass > 0:
        quantities.append({"name": "mass", "label": "assumed", "unit": "kg", "value": round(total_mass, 6),
                           "randomization": _ratio_range(total_mass, MASS_RANGE), "evidence": evidence})
    materials = [c["physics_material"] for c in inspection["colliders"] if c.get("physics_material")]
    if materials:
        static = materials[0].get("static_friction")
        dynamic = materials[0].get("dynamic_friction")
        restitution = materials[0].get("restitution")
        if static is not None:
            quantities.append({"name": "friction_static", "label": "assumed", "unit": "", "value": static,
                               "randomization": _ratio_range(static, FRICTION_RANGE), "evidence": evidence})
        if dynamic is not None:
            quantities.append({"name": "friction_dynamic", "label": "assumed", "unit": "", "value": dynamic,
                               "randomization": _ratio_range(dynamic, FRICTION_RANGE), "evidence": evidence})
        if restitution is not None:
            quantities.append({"name": "restitution", "label": "assumed", "unit": "", "value": restitution,
                               "not_randomized_reason": "not a MuJoCo contact parameter; bounce follows solref",
                               "evidence": evidence})
    for joint in joints:
        if joint["type"] == "prismatic":
            quantities.append({"name": f"{joint['name']}.damping", "label": "assumed", "unit": "N*s/m",
                               "value": PRISMATIC_DAMPING, "randomization": {"low": 5.0, "high": 50.0}})
    files = [{"artifact": usd_ref.to_dict(), "format": "usd"}]
    collision: dict[str, Any] = {"kind": "mesh"}
    if collision_ref is not None:
        collision = {"kind": "convex_decomposition", "artifact": collision_ref.to_dict()}
        if collision_bbox is not None:
            collision["bbox_min_m"], collision["bbox_max_m"] = list(collision_bbox[0]), list(collision_bbox[1])
    record = {
        "schema": asset_record.SCHEMA,
        "asset_id": asset_id,
        "role": role,
        "source": {
            "kind": "simready",
            "uri": fetch["source_uri"],
            "license": fetch["license"],
            "retrieved_at": fetch["retrieved_at"],
            "upstream_id": fetch.get("package_id") or fetch["asset"],
        },
        "files": files,
        "bbox_m": bbox,
        "collision": collision,
        "joints": joints,
        "frames": {"up_axis": inspection["up_axis"], "root_frame_id": asset_id,
                   "link_frame_ids": {j["child"]: f"{asset_id}.{j['child']}" for j in joints},
                   "bbox_min_m": lo, "bbox_max_m": hi},
        "physical_quantities": quantities,
    }
    if notes:
        record["notes"] = notes
    asset_record.validate(record)
    return record


def robot_record(*, asset_id: str, descriptor: Mapping[str, Any], urdf_ref: ArtifactRef, usd_ref: ArtifactRef,
                 license: str, retrieved_at: str, uri: str,
                 base_collision: Mapping[str, Any], mount: Mapping[str, Any]) -> dict[str, Any]:
    """机器人 AssetRecord：关节与限位直接来自 URDF 描述（robot-descriptor/2）。

    `base_collision` 是基座刚体碰撞体在基座坐标系的包围盒（`fixed_base_collision_bbox`），只作实测证据；
    摆位用 `mount` 的显式标定（`configs/robot-mounts.json`，经 `scene.mounts.collision_fields` 复核）：
    支撑面高度、占地矩形、是否靠 C 型夹夹住支撑边。
    """
    joints = []
    for joint in descriptor["joints"]:
        if joint["type"] == "fixed":
            continue
        lower, upper = joint["limits"]["position"]
        entry = {"name": joint["name"], "type": joint["type"], "axis": joint["axis"],
                 "limits": {"lower": lower, "upper": upper}, "parent": joint["parent"], "child": joint["child"]}
        if joint.get("mimic"):
            entry["mimic"] = {"joint": joint["mimic"]["joint"], "multiplier": joint["mimic"]["multiplier"],
                              "offset": joint["mimic"].get("offset", 0.0)}
        joints.append(entry)
    collision = mounts.collision_fields(asset_id, mount, base_collision)
    record = {
        "schema": asset_record.SCHEMA,
        "asset_id": asset_id,
        "role": "robot",
        "source": {"kind": "local", "uri": uri, "license": license, "retrieved_at": retrieved_at,
                   "upstream_id": descriptor["robot_name"]},
        "files": [{"artifact": urdf_ref.to_dict(), "format": "urdf"}, {"artifact": usd_ref.to_dict(), "format": "usd"}],
        "bbox_m": [0.8, 0.8, 0.8],
        "collision": collision,
        "joints": joints,
        "frames": {"up_axis": "Z", "root_frame_id": asset_id,
                   "link_frame_ids": {name: f"{asset_id}.{name}" for name in descriptor["links"]}},
        "physical_quantities": [],
        "notes": "bbox_m is the arm's workspace envelope bound, not a body AABB; dynamics come from the "
                 "validated Isaac Lab 3.0 asset package (see its MIGRATION_CHANGES.md)",
    }
    asset_record.validate(record)
    return record

"""从 SimReady 拉取清单 + USD 检查结果生成 AssetRecord（`datafarm.asset-record/1`）。

物理量的 label 规则：
- 质量、摩擦等数值取自资产作者写进 USD 的值，不是我们测得的，也没有拟合依据，所以记 `assumed`，
  并给随机化区间；USD 检查报告作为证据附上，便于追溯数值来源。
- 几何尺寸（包围盒、关节行程）直接读几何，写在 `bbox_m` / `joints` 字段，不进物理量账本。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from datafarm.ir import asset_record
from datafarm.kernel.artifacts import ArtifactRef

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


def parts(inspection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """每个刚体（连杆）的碰撞几何包围盒：分件几何，资产根系、米，记录的是静止姿态。

    `name` 是刚体 prim 路径的叶名（如 `part_01_obj_00`），`prim` 是资产根相对的刚体 prim 路径
    （`prim_ref` 形式，如 `RootNode/Geometry/part_01_obj_00`）。关节的 `child` 是关节子 prim 的叶名，
    SimReady 常是刚体下面的网格（`part_01_mesh_00`），叶名对不上，所以 `joints[].child_body` 存刚体路径，
    场景按它找移动件。同一刚体下的多个碰撞体取包围盒并集；prim 路径唯一，叶名重复时保留多条。
    """
    boxes: dict[str, dict[str, list[float]]] = {}
    for collider in inspection["colliders"]:
        body = collider.get("body")
        if not body or "bbox_min_m" not in collider or "bbox_max_m" not in collider:
            continue
        entry = boxes.setdefault(body, {"bbox_min_m": list(collider["bbox_min_m"]),
                                        "bbox_max_m": list(collider["bbox_max_m"])})
        entry["bbox_min_m"] = [min(a, b) for a, b in zip(entry["bbox_min_m"], collider["bbox_min_m"])]
        entry["bbox_max_m"] = [max(a, b) for a, b in zip(entry["bbox_max_m"], collider["bbox_max_m"])]
    return [{"name": prim.rsplit("/", 1)[-1], "prim": prim,
             "bbox_min_m": [round(v, 5) for v in box["bbox_min_m"]],
             "bbox_max_m": [round(v, 5) for v in box["bbox_max_m"]]}
            for prim, box in sorted(boxes.items())]


def owner_body(child: str | None, bodies: Sequence[str]) -> str | None:
    """关节 `child` prim 所属的刚体：`bodies` 里是它父路径（或它本身）的最长一条。

    SimReady 的关节子 prim 常是刚体下的网格（`.../part_01_obj_00/part_01_mesh_00`），刚体本身是
    `.../part_01_obj_00`；没有唯一父路径时返回 None，场景据此回退到保守扫掠，不按叶名猜。
    """
    if not child:
        return None
    owners = [body for body in bodies if child == body or child.startswith(body + "/")]
    return max(owners, key=len) if owners else None


def build_record(*, asset_id: str, role: str, fetch: Mapping[str, Any], inspection: Mapping[str, Any],
                 usd_ref: ArtifactRef, inspection_ref: ArtifactRef, collision_ref: ArtifactRef | None,
                 collision_bbox: tuple[list[float], list[float]] | None = None, notes: str = "") -> dict[str, Any]:
    """关节名保持上游 USD 里的名字，场景 `joint_state` 与 Isaac Lab 绑定都按这个名字查。

    `collision_bbox` 是替换后碰撞几何（凸包）的包围盒；凸包会比渲染网格外扩几毫米，布局按它算支撑高度。
    `parts[]`、`joints[].anchor_m` 与 `joints[].child_body` 只在 inspect 结果里有时才写：
    前者给布局按移动件扫掠，后两者是转动枢轴与移动件所在的刚体。
    """
    lo, hi = inspection["bbox_min_m"], inspection["bbox_max_m"]
    bbox = [round(h - l, 5) for l, h in zip(lo, hi)]
    part_boxes = parts(inspection)
    bodies = [part["prim"] for part in part_boxes]
    joints = []
    for joint in inspection["joints"]:
        if joint["type"] not in ("prismatic", "revolute"):
            continue
        entry = {
            "name": joint["name"],
            "type": joint["type"],
            "axis": joint["axis"],
            "limits": {"lower": round(joint["limits"]["lower"], 6), "upper": round(joint["limits"]["upper"], 6)},
            "parent": joint["parent"].rsplit("/", 1)[-1],
            "child": joint["child"].rsplit("/", 1)[-1],
        }
        if joint.get("anchor_m") is not None:
            entry["anchor_m"] = [round(float(v), 5) for v in joint["anchor_m"]]
        owner = owner_body(joint.get("child"), bodies)
        if owner is not None:
            entry["child_body"] = owner
        joints.append(entry)
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
    if part_boxes:
        record["parts"] = part_boxes
    asset_record.validate(record)
    return record


def robot_record(*, asset_id: str, descriptor: Mapping[str, Any], urdf_ref: ArtifactRef, usd_ref: ArtifactRef,
                 license: str, retrieved_at: str, uri: str,
                 base_collision: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """机器人 AssetRecord：关节与限位直接来自 URDF 描述（robot-descriptor/2）。

    `base_collision` 是基座刚体碰撞体在基座坐标系的包围盒（`fixed_base_collision_bbox`），布局据此把基座放上支撑面。
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
    record = {
        "schema": asset_record.SCHEMA,
        "asset_id": asset_id,
        "role": "robot",
        "source": {"kind": "local", "uri": uri, "license": license, "retrieved_at": retrieved_at,
                   "upstream_id": descriptor["robot_name"]},
        "files": [{"artifact": urdf_ref.to_dict(), "format": "urdf"}, {"artifact": usd_ref.to_dict(), "format": "usd"}],
        "bbox_m": [0.8, 0.8, 0.8],
        "collision": {"kind": "mesh", **({"base_bbox_min_m": list(base_collision["bbox_min_m"]),
                                          "base_bbox_max_m": list(base_collision["bbox_max_m"])}
                                         if base_collision else {})},
        "joints": joints,
        "frames": {"up_axis": "Z", "root_frame_id": asset_id,
                   "link_frame_ids": {name: f"{asset_id}.{name}" for name in descriptor["links"]}},
        "physical_quantities": [],
        "notes": "bbox_m is the arm's workspace envelope bound, not a body AABB; dynamics come from the "
                 "validated Isaac Lab 3.0 asset package (see its MIGRATION_CHANGES.md)",
    }
    asset_record.validate(record)
    return record

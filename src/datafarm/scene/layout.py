"""场景布局：从布局说明（layout spec）+ AssetRecord 解出各节点位姿、做可达性检查、拼 SceneGraph。

纯 Python + numpy，不导入仿真器；仿真检查（settle / 穿透）的结果由调用方填进 `checks`。

布局说明（`datafarm.scene-layout/1`）里节点只给水平位置、偏航角和支撑面（`on`），
高度由支撑节点的包围盒顶面与本资产包围盒底面算出，避免手填 z 造成悬空或穿模。
可达性目标写在某个节点的局部系里；带 `joint` 时目标随该关节的初值一起平移（抽屉内腔）。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from datafarm.ir import scene_graph
from datafarm.ir.common import IRError, check, identifier
from datafarm.kernel.schema import is_absolute_filesystem_path
from datafarm.scene import kinematics

LAYOUT_SCHEMA = "datafarm.scene-layout/1"
SUPPORT_CLEARANCE_M = 0.001

PRIM_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ANCHOR_BODY_EXAMPLE = "RootNode/Geometry/body_01_obj_00"
ANCHOR_BODY_RULE = (
    f"must be a prim path relative to the asset root, like {ANCHOR_BODY_EXAMPLE!r}: "
    "no leading '/', no '.', '..' or empty segments"
)


def _check_anchor_body(asset: Mapping[str, Any], index: int) -> None:
    """`anchor_body` 是 `prim_ref` 形式（USD prim 路径去掉前导 `/`，见 `adapters.sim.usd_assets.prim_ref`）。

    `prepare` 之前就拒绝宿主绝对路径和所有非 prim 名的段，避免 kernel 的可移植性检查在更晚处才报错。
    """
    label = f"assets[{index}] ({asset.get('asset_id')}).anchor_body"
    value = asset.get("anchor_body")
    if value is None:
        if asset.get("body") == "articulation":
            raise IRError(f"{label} is required when body='articulation' and {ANCHOR_BODY_RULE}")
        return
    invalid = (
        not isinstance(value, str)
        or not value
        or is_absolute_filesystem_path(value)
        or any(not PRIM_NAME_PATTERN.fullmatch(segment) for segment in value.split("/"))
    )
    if invalid:
        raise IRError(f"{label} {value!r} {ANCHOR_BODY_RULE}")


def validate_assets(spec: Mapping[str, Any]) -> None:
    """校验 `assets[]` 里每个声明了 `anchor_body` 的资产；非铰接体写了也校验，但 `prepare` 仍忽略。"""
    assets = spec.get("assets")
    if assets is None:
        return
    check(isinstance(assets, list), "layout 'assets' must be an array")
    for index, asset in enumerate(assets):
        check(isinstance(asset, Mapping), f"assets[{index}] must be an object")
        _check_anchor_body(asset, index)


def yaw_quaternion(yaw_deg: float) -> list[float]:
    half = math.radians(yaw_deg) / 2.0
    return [0.0, 0.0, round(math.sin(half), 9), round(math.cos(half), 9)]


def _record_bbox(record: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """支撑用包围盒（资产根系）：有替换碰撞几何时取它的包围盒，物理接触发生在那里。"""
    for source in (record["collision"], record["frames"]):
        if "bbox_min_m" in source and "bbox_max_m" in source:
            return np.asarray(source["bbox_min_m"], float), np.asarray(source["bbox_max_m"], float)
    size = np.asarray(record["bbox_m"], float)
    return np.array([-size[0] / 2, -size[1] / 2, 0.0]), np.array([size[0] / 2, size[1] / 2, size[2]])


def _joint(record: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    for joint in record["joints"]:
        if joint["name"] == name:
            return joint
    raise IRError(f"{record['asset_id']}: no joint {name!r}")


def resolve_nodes(spec: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 `on` 依赖顺序解出每个节点的世界位姿；返回 SceneGraph 的 `nodes`。"""
    check(spec.get("schema") == LAYOUT_SCHEMA, f"layout schema must be {LAYOUT_SCHEMA}")
    validate_assets(spec)
    entries = {identifier(n["node_id"], "layout node_id"): n for n in spec["nodes"]}
    resolved: dict[str, dict[str, Any]] = {}
    top: dict[str, float] = {}

    def solve(node_id: str, trail: tuple[str, ...] = ()) -> None:
        if node_id in resolved:
            return
        check(node_id not in trail, f"layout: support cycle through {node_id!r}")
        entry = entries.get(node_id)
        check(entry is not None, f"layout: unknown node {node_id!r}")
        record = records.get(entry["asset_id"])
        check(record is not None, f"layout: {node_id} uses unknown asset {entry['asset_id']!r}")
        support = entry.get("on", "floor")
        if support == "floor":
            base = 0.0
        else:
            solve(support, trail + (node_id,))
            base = top[support]
        lo, hi = _record_bbox(record)
        z = base + SUPPORT_CLEARANCE_M - float(lo[2]) if "z_m" not in entry else float(entry["z_m"])
        x, y = (float(v) for v in entry["xy_m"])
        for name, value in entry.get("joint_state", {}).items():
            limits = _joint(record, name)["limits"]
            check(limits["lower"] <= value <= limits["upper"], f"{node_id}: {name}={value} outside {limits}")
        node = {
            "node_id": node_id,
            "asset_id": entry["asset_id"],
            "semantic_tag": entry["semantic_tag"],
            "pose": {"frame_id": "world", "position_m": [round(x, 6), round(y, 6), round(z, 6)],
                     "quaternion_xyzw": yaw_quaternion(float(entry.get("yaw_deg", 0.0)))},
        }
        if entry.get("joint_state"):
            node["joint_state"] = dict(entry["joint_state"])
        if entry.get("randomization"):
            node["randomization"] = dict(entry["randomization"])
        resolved[node_id] = node
        top[node_id] = z + float(hi[2])

    for node_id in entries:
        solve(node_id)
    return [resolved[node_id] for node_id in entries]


def support_tops(nodes: Sequence[Mapping[str, Any]], records: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    return {n["node_id"]: n["pose"]["position_m"][2] + float(_record_bbox(records[n["asset_id"]])[1][2]) for n in nodes}


def _xy_corners(lo: Sequence[float], hi: Sequence[float], pose: Mapping[str, Any]) -> np.ndarray:
    """资产根系包围盒的四个底角投到世界 XY（逆时针）；只用于平放（只有偏航）的物体。"""
    m = kinematics.pose_matrix(pose)
    local = [(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1])]
    return (np.array([[x, y, 0.0, 1.0] for x, y in local]) @ m.T)[:, :2]


def _overlap(a: np.ndarray, b: np.ndarray) -> bool:
    """两个凸四边形是否相交（分离轴，边贴边不算）。"""
    for poly in (a, b):
        for i in range(4):
            edge = poly[(i + 1) % 4] - poly[i]
            axis = np.array([-edge[1], edge[0]])
            pa, pb = a @ axis, b @ axis
            if pa.max() <= pb.min() + 1e-9 or pb.max() <= pa.min() + 1e-9:
                return False
    return True


JOINT_SWEEP_EPS_M = 1e-6


def joint_sweep_check(spec: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]],
                      records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """非零 `joint_state` 的关节初值把占地矩形沿关节轴平移到哪儿，是否撞上别的物体。

    只做可判定的部分：`prismatic` 的移动件保守地当作整个根系包围盒沿 `R_z(yaw)·axis·v` 平移，
    再和同一支撑面（`on` 相同）上的其它节点、以及自己的支撑节点做 SAT 相交测试（边贴边不算）。
    旋转关节没有枢轴信息（AssetRecord 只有 `joints[]`，没有 `parts[]` 几何），AABB 平移无法界定转动，
    因此记为 `skipped` 而不是静默放行。这是 GPU 之前的运动学前置闸门：z 盲、整盒平移、不加余量。
    """
    entries = {n["node_id"]: n for n in spec["nodes"]}
    corners: dict[str, np.ndarray] = {}
    supports: dict[str, str] = {}
    for node in nodes:
        lo, hi = _record_bbox(records[node["asset_id"]])
        corners[node["node_id"]] = _xy_corners(lo, hi, node["pose"])
        supports[node["node_id"]] = entries[node["node_id"]].get("on", "floor")
    problems: list[str] = []
    checked: list[dict[str, Any]] = []
    skipped: list[str] = []
    for node in nodes:
        node_id = node["node_id"]
        state = entries[node_id].get("joint_state") or {}
        if not state:
            continue
        record = records[node["asset_id"]]
        rotation = kinematics.pose_matrix(node["pose"])[:2, :2]
        for name, raw in state.items():
            value = float(raw)
            if abs(value) <= JOINT_SWEEP_EPS_M:
                continue
            joint = _joint(record, name)
            kind = joint["type"]
            if kind == "revolute":
                skipped.append(f"{node_id} {name}: revolute joint has no pivot in the AssetRecord, "
                               "an AABB translation cannot bound a rotation")
                continue
            if kind != "prismatic":
                skipped.append(f"{node_id} {name}: {kind} joints cannot be swept")
                continue
            moved = corners[node_id] + rotation @ (np.asarray(joint["axis"], float)[:2] * value)
            checked.append({"node": node_id, "joint": name, "travel_m": round(abs(value), 6)})
            support = supports[node_id]
            others = [other for other, other_support in supports.items()
                      if other != node_id and (other_support == support or other == support)]
            for other in others:
                if _overlap(moved, corners[other]):
                    problems.append(f"{node_id} {name} at {value:g} m moves into {other}")
    return {"ok": not problems, "problems": problems, "checked": checked, "skipped": skipped}


def footprint_check(spec: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]],
                    records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """放在支撑面上的东西（含机器人基座）占地要落在支撑顶面内，同一支撑面上的东西互不重叠。

    占地取包围盒底面矩形（按偏航旋转，不取外包框）。这是 settle 之前的静态检查：
    越出桌沿的基座在固定根仿真里不会掉，但不是可复现的摆放。
    """
    entries = {n["node_id"]: n for n in spec["nodes"]}
    corners: dict[str, np.ndarray] = {}
    supports: dict[str, str] = {}
    for node in nodes:
        lo, hi = _record_bbox(records[node["asset_id"]])
        corners[node["node_id"]] = _xy_corners(lo, hi, node["pose"])
        supports[node["node_id"]] = entries[node["node_id"]].get("on", "floor")
    robot = spec["robot"]
    if "base_pose" not in robot:
        collision = records[robot["asset_id"]]["collision"]
        corners[robot["node_id"]] = _xy_corners(collision["base_bbox_min_m"], collision["base_bbox_max_m"],
                                                robot_base_pose(spec, nodes, records))
        supports[robot["node_id"]] = robot["on"]
    by_id = {n["node_id"]: n for n in nodes}
    problems = []
    for node_id, support in supports.items():
        if support == "floor":
            continue
        slo, shi = _record_bbox(records[by_id[support]["asset_id"]])
        inv = np.linalg.inv(kinematics.pose_matrix(by_id[support]["pose"]))
        local = (np.c_[corners[node_id], np.zeros(4), np.ones(4)] @ inv.T)[:, :2]
        if np.any(local < slo[:2] - 1e-9) or np.any(local > shi[:2] + 1e-9):
            problems.append(f"{node_id} overhangs {support}")
    placed = sorted(supports)
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if supports[a] == supports[b] and _overlap(corners[a], corners[b]):
                problems.append(f"{a} overlaps {b} on {supports[a]}")
    sweep = joint_sweep_check(spec, nodes, records)
    problems = problems + sweep["problems"]
    return {"ok": not problems,
            "problems": problems,
            "xy_corners_m": {k: np.round(v, 4).tolist() for k, v in corners.items()},
            "joint_sweep": {"ok": sweep["ok"], "checked": sweep["checked"], "skipped": sweep["skipped"]}}


def target_world(target: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]],
                 records: Mapping[str, Mapping[str, Any]]) -> np.ndarray:
    node = nodes[target["node"]]
    local = np.asarray(target["local_m"], float)
    if target.get("joint"):
        joint = _joint(records[node["asset_id"]], target["joint"])
        check(joint["type"] == "prismatic", f"reach target {target['name']}: only prismatic joints shift targets")
        local = local + np.asarray(joint["axis"], float) * float(node.get("joint_state", {}).get(target["joint"], 0.0))
    return (kinematics.pose_matrix(node["pose"]) @ np.append(local, 1.0))[:3]


def robot_base_pose(spec: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]],
                     records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """机器人基座：给 `on` 时让基座刚体碰撞体的底面贴在支撑节点顶面，否则用显式 `base_pose`。

    基座碰撞包围盒来自机器人 AssetRecord 的 `collision.base_bbox_min_m`；缺失时报错，不假设基座原点就是底面。
    """
    robot = spec["robot"]
    if "base_pose" in robot:
        return dict(robot["base_pose"])
    collision = records[robot["asset_id"]]["collision"]
    check("base_bbox_min_m" in collision,
          f"{robot['asset_id']}: record has no collision.base_bbox_min_m; give robot.base_pose explicitly")
    z = support_tops(nodes, records)[robot["on"]] + SUPPORT_CLEARANCE_M - float(collision["base_bbox_min_m"][2])
    x, y = (float(v) for v in robot["xy_m"])
    return {"frame_id": "world", "position_m": [round(x, 6), round(y, 6), round(z, 6)],
            "quaternion_xyzw": yaw_quaternion(float(robot.get("yaw_deg", 0.0)))}


def reachability(spec: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]],
                 records: Mapping[str, Mapping[str, Any]], descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """逐个目标求逆解；全部有解才 ok。返回 `{checked, ok, notes, targets}`。"""
    robot = spec["robot"]
    arm = kinematics.chain(descriptor, robot["tcp_link"], tip_offset=model_tcp(descriptor, robot["tcp_link"]))
    base = kinematics.pose_matrix(robot_base_pose(spec, nodes, records))
    margin = float(robot.get("limit_margin_rad", 0.05))
    by_id = {n["node_id"]: n for n in nodes}
    rows, failed = [], []
    for target in spec["reach_targets"]:
        world = target_world(target, by_id, records)
        result = kinematics.reach_target(arm, base, world, target.get("approach", [0, 0, -1]),
                                         tool_approach_axis=robot["tool_approach_axis"],
                                         yaw_samples=int(target.get("yaw_samples", 8)), limit_margin=margin)
        rows.append({"name": target["name"], "world_m": np.round(world, 5).tolist(), **result})
        if not result["ok"]:
            failed.append(target["name"])
    notes = (f"{len(rows) - len(failed)}/{len(rows)} targets solved by multi-start damped least-squares IK "
             f"on the {robot['tcp_link']} model TCP, approach from above, joint-limit margin {margin} rad; "
             "no collision-aware planning")
    if failed:
        notes += "; unreachable: " + ", ".join(failed)
    return {"checked": True, "ok": not failed, "notes": notes, "targets": rows}


def controlled_joints(descriptor: Mapping[str, Any]) -> list[str]:
    """B 下发的关节：非 fixed、非 mimic。"""
    return [j["name"] for j in descriptor["joints"] if j["type"] != "fixed" and not j.get("mimic")]


def model_tcp(descriptor: Mapping[str, Any], link: str) -> dict[str, Any]:
    """URDF 模型自带的 TCP（`model_semantics.tcp_candidates`）；没有对应 link 的候选就报错，不拿 link 原点顶替。"""
    for candidate in descriptor.get("model_semantics", {}).get("tcp_candidates", []):
        if candidate["link"] == link:
            return {"frame_id": link, "position_m": list(candidate["position_m"]),
                    "quaternion_xyzw": list(candidate["quaternion_xyzw"])}
    raise IRError(f"robot model has no TCP candidate on {link!r}")


def build_scene_graph(*, spec: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]],
                      records: Mapping[str, Mapping[str, Any]], record_refs: Mapping[str, Mapping[str, str]],
                      descriptor: Mapping[str, Any], asset_bundle: Mapping[str, str], ledger: Mapping[str, str],
                      runtime_profile: Mapping[str, str], reach: Mapping[str, Any], settle: Mapping[str, Any],
                      penetration: Mapping[str, Any]) -> dict[str, Any]:
    robot = spec["robot"]
    reach_summary = {"checked": reach["checked"], "ok": reach["ok"], "notes": reach["notes"]}
    tcp = model_tcp(descriptor, robot["tcp_link"])
    base_pose = robot_base_pose(spec, nodes, records)
    robot_node = {"node_id": robot["node_id"], "asset_id": robot["asset_id"], "semantic_tag": "robot",
                  "pose": dict(base_pose)}
    document = {
        "schema": scene_graph.SCHEMA,
        "scene_id": spec["scene_id"],
        "units": "m",
        "frame": "world",
        "asset_bundle": dict(asset_bundle),
        "assets": [{"asset_id": asset_id, "record": dict(ref)} for asset_id, ref in record_refs.items()],
        "nodes": [robot_node, *nodes],
        "frames": [
            {"frame_id": "world", "parent_frame_id": None,
             "pose": {"frame_id": "world", "position_m": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]}},
            {"frame_id": robot["tcp_frame_id"], "parent_frame_id": robot["node_id"], "pose": dict(tcp)},
        ],
        "cameras": list(spec.get("cameras", [])),
        "robot": {
            "asset_id": robot["asset_id"],
            "base_pose": dict(base_pose),
            "arms": list(robot["arms"]),
            "gripper": robot["gripper"],
            "controlled_joints": controlled_joints(descriptor),
            "tcp": {"frame_id": robot["tcp_frame_id"], "link": robot["tcp_link"], "T_link_tcp": dict(tcp)},
            "reachability": reach_summary,
        },
        "collision_groups": spec["collision_groups"],
        "physics_parameter_ledger": dict(ledger),
        "runtime_profile": dict(runtime_profile),
        "backend": dict(scene_graph.BACKEND),
        "checks": {"settle": dict(settle), "penetration": dict(penetration), "reachability": dict(reach_summary)},
    }
    scene_graph.validate(document)
    return document


def physics_ledger(records: Mapping[str, Mapping[str, Any]], nodes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """本次实例化使用的物理量：按节点展开 AssetRecord 里的账本（未随机化时取名义值）。"""
    rows = []
    for node in nodes:
        for quantity in records[node["asset_id"]]["physical_quantities"]:
            rows.append({"node_id": node["node_id"], "name": quantity["name"], "label": quantity["label"],
                         "unit": quantity["unit"], "value": quantity.get("value"), "sampled": False})
    return {"schema": "datafarm.physics-ledger/1", "entries": rows}

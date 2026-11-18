"""Isaac RTX 渲染规划（纯 CPU）：request 的 `render` + SceneGraph + 变体参数表 → RenderSpec，以及相机位姿换算。

渲染器只读 `state_trace`。这里决定渲染哪些视角、哪些外观组合、哪些控制步，不碰几何和运动。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from datafarm.ir import render_spec
from datafarm.ir.common import check
from datafarm.kernel.io import is_finite_number

VARIANTS_SCHEMA = "datafarm.isaac-render-variants/1"
CAMERA_PREFIX = "camera."
VIRTUAL_PREFIX = "view."
# OpenGL 相机（-z 前、y 上）到 OpenCV/ROS 光学系（z 前、y 下）：绕 x 转 180°。
_GL_TO_CV = np.diag([1.0, -1.0, -1.0])


def quat_to_matrix(q: Sequence[float]) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    n = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def matrix_to_quat(m: np.ndarray) -> list[float]:
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = 2.0 * math.sqrt(1.0 + trace)
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
    q = np.asarray(q) / np.linalg.norm(q)
    return (q if q[3] >= 0 else -q).tolist()


def pose_matrix(pose: Mapping[str, Any]) -> np.ndarray:
    out = np.eye(4)
    out[:3, :3] = quat_to_matrix(pose["quaternion_xyzw"])
    out[:3, 3] = pose["position_m"]
    return out


def matrix_pose(matrix: np.ndarray, frame_id: str = "world") -> dict[str, Any]:
    return {"frame_id": frame_id, "position_m": [round(float(v), 6) for v in matrix[:3, 3]],
            "quaternion_xyzw": [round(v, 6) for v in matrix_to_quat(matrix[:3, :3])]}


def optical_offset(camera: Mapping[str, Any]) -> np.ndarray:
    """SceneGraph 相机 `offset` 换成 OpenCV 光学系相对挂载点的 4×4。"""
    offset = pose_matrix(camera["offset"])
    if camera["optical_frame"]["convention"] == "opengl":
        offset[:3, :3] = offset[:3, :3] @ _GL_TO_CV
    return offset


def frame_world(scene_graph: Mapping[str, Any], frame_id: str) -> np.ndarray:
    """节点或命名坐标系在世界系的 4×4（沿 parent 链）。只用于静态挂载：节点位姿是布局初值。"""
    if frame_id == "world":
        return np.eye(4)
    nodes = {n["node_id"]: n for n in scene_graph["nodes"]}
    frames = {f["frame_id"]: f for f in scene_graph["frames"]}
    if frame_id in nodes:
        pose = nodes[frame_id]["pose"]
        return frame_world(scene_graph, pose["frame_id"]) @ pose_matrix(pose)
    check(frame_id in frames, f"unknown frame {frame_id!r}")
    frame = frames[frame_id]
    parent = frame["parent_frame_id"] or "world"
    return frame_world(scene_graph, parent) @ pose_matrix(frame["pose"])


def static_camera_world(scene_graph: Mapping[str, Any], camera: Mapping[str, Any]) -> np.ndarray:
    """不挂 link 的相机在世界系的光学位姿（OpenCV）。机器人基座是固定的，所以挂在机器人 node 上也算静态。"""
    check(camera.get("link") is None, f"{camera['camera_id']} moves with link {camera.get('link')}; ask the simulator")
    return frame_world(scene_graph, camera["mount"]) @ optical_offset(camera)


def look_at(eye: Sequence[float], target: Sequence[float], up: Sequence[float] = (0.0, 0.0, 1.0)) -> dict[str, Any]:
    """世界系 OpenCV 光学位姿：z 指向 target，y 朝下（相对 `up`）。"""
    eye_v, forward = np.asarray(eye, float), np.asarray(target, float) - np.asarray(eye, float)
    check(np.linalg.norm(forward) > 1e-6, "look_at: eye and target coincide")
    z = forward / np.linalg.norm(forward)
    x = np.cross(z, np.asarray(up, float))
    check(np.linalg.norm(x) > 1e-6, "look_at: view direction is parallel to up")
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    out = np.eye(4)
    out[:3, :3] = np.column_stack([x, y, z])
    out[:3, 3] = eye_v
    return matrix_pose(out)


def validate_variants(config: Mapping[str, Any]) -> None:
    check(config.get("schema") == VARIANTS_SCHEMA, f"variants config schema must be {VARIANTS_SCHEMA}")
    for key in ("materials", "material_sets", "lights", "light_sets", "virtual_views", "output"):
        check(key in config, f"variants config is missing {key!r}")
    materials = {m["material_id"] for m in config["materials"]}
    lights = {light["light_id"] for light in config["lights"]}
    for key, known in (("material_sets", materials), ("light_sets", lights)):
        sets = config[key]
        check(isinstance(sets, list) and bool(sets), f"{key} must be a non-empty list")
        check(len({s["set_id"] for s in sets}) == len(sets), f"{key}: duplicate set_id")
        for entry in sets:
            check(all(m in known for m in entry["members"]), f"{key}.{entry['set_id']} names unknown ids")
    check(config["material_sets"][0]["members"] == [],
          "material_sets[0] must keep the asset materials (request materials=1 means unchanged)")


def build_spec(*, render: Mapping[str, Any], scene_graph: Mapping[str, Any], scene_graph_ref: Mapping[str, str],
               episode_ref: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    """`render` 是 request 的 `{engine, views, materials, lights}`。

    视角 `head` / `left_wrist` / `right_wrist` 解析为 SceneGraph 的 `camera.<id>` 物理相机；其他视角必须在
    参数表 `virtual_views` 里有 eye/target/intrinsics。取参数表前 `materials` 个材质组和前 `lights` 个灯光组，
    变体 = 视角 × 材质组 × 灯光组。
    """
    validate_variants(config)
    check(render.get("engine") == render_spec.ENGINE, f"request render.engine must be {render_spec.ENGINE!r}")
    cameras = {c["camera_id"]: c for c in scene_graph["cameras"]}
    material_sets = config["material_sets"][:render["materials"]]
    light_sets = config["light_sets"][:render["lights"]]
    check(len(material_sets) == render["materials"],
          f"request asks for {render['materials']} material variants; the table has "
          f"{len(config['material_sets'])}. Pass --variants with this task's appearance table")
    check(len(light_sets) == render["lights"],
          f"request asks for {render['lights']} light variants; the table has "
          f"{len(config['light_sets'])}. Pass --variants with this task's appearance table")

    views = []
    for view_id in render["views"]:
        camera_id = CAMERA_PREFIX + view_id
        if camera_id in cameras:
            views.append({"view_id": view_id, "kind": "physical", "camera_id": camera_id,
                          "fps": cameras[camera_id]["fps"]})
            continue
        virtual = config["virtual_views"].get(view_id)
        check(virtual is not None,
              f"view {view_id!r} is neither a scene graph camera ({sorted(cameras)}) nor a configured virtual view")
        views.append({"view_id": view_id, "kind": "virtual", "fps": virtual["fps"],
                      "pose": look_at(virtual["eye_m"], virtual["target_m"]),
                      "intrinsics": dict(virtual["intrinsics"])})

    used_materials = {m for s in material_sets for m in s["members"]}
    used_lights = {light for s in light_sets for light in s["members"]}
    spec = {
        "schema": render_spec.SCHEMA,
        "episode": dict(episode_ref),
        "scene_graph": dict(scene_graph_ref),
        "engine": render_spec.ENGINE,
        "views": views,
        "materials": [dict(m) for m in config["materials"] if m["material_id"] in used_materials],
        "lights": [dict(light) for light in config["lights"] if light["light_id"] in used_lights],
        "variants": [{"variant_id": f"{view['view_id']}.{ms['set_id']}.{ls['set_id']}", "view": view["view_id"],
                      "material_set": list(ms["members"]), "light_set": list(ls["members"])}
                     for view in views for ms in material_sets for ls in light_sets],
        "output": dict(config["output"]),
    }
    render_spec.validate(spec, scene_graph)
    return spec


def stream_camera_id(spec: Mapping[str, Any], view_id: str) -> str:
    view = next(v for v in spec["views"] if v["view_id"] == view_id)
    return view["camera_id"] if view["kind"] == "physical" else VIRTUAL_PREFIX + view_id


def view_intrinsics(spec: Mapping[str, Any], scene_graph: Mapping[str, Any], view_id: str) -> dict[str, Any]:
    view = next(v for v in spec["views"] if v["view_id"] == view_id)
    if view["kind"] == "virtual":
        return dict(view["intrinsics"])
    camera = next(c for c in scene_graph["cameras"] if c["camera_id"] == view["camera_id"])
    return dict(camera["intrinsics"])


def link_mount(descriptor: Mapping[str, Any], link: str, bodies: Sequence[str]) -> tuple[str, np.ndarray]:
    """把 URDF link 换到仿真里实际存在的刚体：沿固定关节向上找第一个在 `bodies` 里的祖先。

    返回 `(body, T_body_link)`。USD 转换常把固定子 link 并入父刚体（如 `left_end_link` 并入 `left_link6`），
    所以腕部相机不能假设挂载 link 本身是刚体。途中遇到非固定关节说明 link 不随该刚体刚性运动，直接报错。
    """
    by_child = {j["child"]: j for j in descriptor["joints"]}
    offset, current = np.eye(4), link
    while current not in bodies:
        joint = by_child.get(current)
        check(joint is not None, f"link {link!r} has no ancestor among the simulated bodies")
        check(joint["type"] == "fixed", f"link {link!r} is not rigidly attached to a simulated body "
                                        f"(crosses {joint['type']} joint {joint['name']!r})")
        offset = pose_matrix(joint["origin"]) @ offset
        current = joint["parent"]
    return current, offset


def full_joint_positions(record_joints: Sequence[Mapping[str, Any]], joint_names: Sequence[str],
                         q: Sequence[float], *, simulated: Sequence[str]) -> dict[str, float]:
    """`state_trace.q`（按 `joint_names`）换成仿真关节 `simulated` 的值，缺的从 mimic 主关节补全。

    URDF 里的虚拟主关节（如 `left_g2_joint`）常不在 USD 里，但 episode 按它记录夹爪；从动关节由它推出。
    仿真关节既没记录、也不是已记录关节的 mimic 时报错，不默认为 0。
    """
    recorded = dict(zip(joint_names, (float(v) for v in q), strict=True))
    by_name = {j["name"]: j for j in record_joints}
    values = {}
    for name in simulated:
        if name in recorded:
            values[name] = recorded[name]
            continue
        mimic = (by_name.get(name) or {}).get("mimic")
        check(mimic is not None and mimic["joint"] in recorded,
              f"state_trace has no value for joint {name!r} and it is not a mimic of a recorded joint")
        values[name] = mimic["multiplier"] * recorded[mimic["joint"]] + mimic.get("offset", 0.0)
    return values


def _state_entity(state: Mapping[str, Any], node_id: str, step: int) -> Mapping[str, Any]:
    where = f"state_trace[{step}].entities"
    entities = state.get("entities")
    check(isinstance(entities, Mapping), f"{where} must be an object")
    entity = entities.get(node_id)
    check(isinstance(entity, Mapping), f"{where}.{node_id} is missing")
    check(entity.get("availability") == "available", f"{where}.{node_id} is not available")
    return entity


def state_robot(state: Mapping[str, Any], step: int) -> tuple[list[str], list[float]]:
    """当前 Episode 的机器人段：`state.robot.joint_names` 与同序 `q`，按测得顺序返回，不重排、不插值。"""
    where = f"state_trace[{step}].robot"
    robot = state.get("robot")
    check(isinstance(robot, Mapping), f"{where} must be an object")
    names = robot.get("joint_names")
    check(isinstance(names, list) and bool(names) and all(isinstance(n, str) and n for n in names),
          f"{where}.joint_names must be non-empty strings")
    check(len(set(names)) == len(names), f"{where}.joint_names must be distinct")
    q = robot.get("q")
    check(isinstance(q, list) and len(q) == len(names) and all(is_finite_number(v) for v in q),
          f"{where}.q must be {len(names)} finite numbers")
    return list(names), [float(v) for v in q]


def state_entity_pose(state: Mapping[str, Any], node_id: str, step: int) -> dict[str, Any]:
    """实体根位姿：`position_m` / `quaternion_xyzw` 在世界系。

    `links[...]` 是刚体（或 articulation 的 body）原点，不是实体根位姿——桌子的实测刚体原点比场景根低
    近 0.8 m，拿它当根位姿会把整张桌子搬走，所以这里只读根位姿。
    """
    where = f"state_trace[{step}].entities.{node_id}"
    entity = _state_entity(state, node_id, step)
    position, quaternion = entity.get("position_m"), entity.get("quaternion_xyzw")
    check(isinstance(position, list) and len(position) == 3 and all(is_finite_number(v) for v in position),
          f"{where}.position_m must be 3 finite numbers")
    check(isinstance(quaternion, list) and len(quaternion) == 4 and all(is_finite_number(v) for v in quaternion),
          f"{where}.quaternion_xyzw must be 4 finite numbers")
    check(math.isclose(math.hypot(*quaternion), 1.0, abs_tol=1e-6), f"{where}.quaternion_xyzw is not a unit quaternion")
    return {"frame_id": "world", "position_m": [float(v) for v in position],
            "quaternion_xyzw": [float(v) for v in quaternion]}


def state_entity_joints(state: Mapping[str, Any], node_id: str, step: int) -> dict[str, float]:
    """该实体自己的关节测量值 `{name: q}`；调用方再按 node_id + name 落到仿真关节，避免跨实体同名。"""
    where = f"state_trace[{step}].entities.{node_id}"
    joints = _state_entity(state, node_id, step).get("joints")
    check(isinstance(joints, list), f"{where}.joints must be a list")
    values: dict[str, float] = {}
    for index, joint in enumerate(joints):
        check(isinstance(joint, Mapping), f"{where}.joints[{index}] must be an object")
        name, q = joint.get("name"), joint.get("q")
        check(isinstance(name, str) and bool(name), f"{where}.joints[{index}].name must be a non-empty string")
        check(name not in values, f"{where}.joints has duplicate name {name!r}")
        check(is_finite_number(q), f"{where}.joints[{index}].q must be finite")
        values[name] = float(q)
    return values


def appearance_groups(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """同一外观（材质组 + 灯光组）的变体共用一遍回放，各视角在同一帧一起出图。"""
    groups: dict[tuple[tuple[str, ...], tuple[str, ...]], dict[str, Any]] = {}
    for variant in spec["variants"]:
        key = (tuple(sorted(variant["material_set"])), tuple(sorted(variant["light_set"])))
        group = groups.setdefault(key, {"material_set": list(key[0]), "light_set": list(key[1]), "variants": []})
        group["variants"].append(variant)
    return list(groups.values())


def frame_status(state: Mapping[str, Any], rgb_ok: bool, reason: str = "") -> tuple[str, str]:
    if not state.get("valid", True):
        return "missing", "state_trace marks this step invalid"
    if not rgb_ok:
        return "error", reason or "renderer returned no image"
    return "ok", ""


def manifest(*, spec_ref: Mapping[str, str], spec: Mapping[str, Any], engine: Mapping[str, str],
             streams: Sequence[Mapping[str, Any]], frames: Mapping[str, Sequence[Mapping[str, Any]]],
             playback: Mapping[str, Any]) -> dict[str, Any]:
    """`frames[variant_id]` 是 `[{frame_index, control_step, status, reason}]`；`ok` 帧的 reason 忽略，文件 ref 从 stream 抄过来。"""
    frame_map = []
    for stream in streams:
        for frame in frames[stream["variant_id"]]:
            entry = {"camera_id": stream["camera_id"], "variant_id": stream["variant_id"],
                     "frame_index": frame["frame_index"], "control_step": frame["control_step"],
                     "status": frame["status"], "intrinsics": dict(stream["intrinsics"]),
                     "extrinsics": dict(stream["extrinsics"]), "image": dict(stream["video"])}
            if frame["status"] != "ok":
                entry["reason"] = frame["reason"]
            frame_map.append(entry)
    document = {"schema": render_spec.MANIFEST_SCHEMA, "render_spec": dict(spec_ref),
                "episode": dict(spec["episode"]), "scene_graph": dict(spec["scene_graph"]),
                "engine": {"name": render_spec.ENGINE, **engine},
                "streams": [dict(s) for s in streams], "frame_map": frame_map, "state_playback": dict(playback)}
    render_spec.validate_manifest(document, spec)
    return document

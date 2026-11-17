"""SceneGraph — `datafarm.scene-graph/1`（docs/ir/scene-graph.md）。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from datafarm.ir.common import check, identifier, portable, pose, ref, require, vector
from datafarm.kernel.io import is_finite_number

SCHEMA = "datafarm.scene-graph/1"
CONTROL_HZ = 120
SEMANTIC_TAGS = ("table", "drawer", "plate", "background", "robot")
BACKEND = {"name": "isaaclab", "version": "3.0.0", "physics": "newton_mjwarp"}
REQUIRED = ("schema", "scene_id", "units", "frame", "asset_bundle", "assets", "nodes", "frames", "cameras",
            "robot", "collision_groups", "physics_parameter_ledger", "runtime_profile", "backend", "checks")


def _reachability(value: Any, label: str) -> None:
    check(isinstance(value, Mapping), f"{label} must be an object")
    require(value, ("checked", "ok", "notes"), label)
    check(isinstance(value["checked"], bool) and isinstance(value["ok"], bool), f"{label}.checked/ok must be booleans")
    check(isinstance(value["notes"], str), f"{label}.notes must be a string")
    check(value["checked"] or not value["ok"], f"{label}: ok=true requires checked=true")


def _acyclic(parents: Mapping[str, str | None], label: str) -> None:
    for start in parents:
        seen, node = set(), start
        while node is not None:
            check(node not in seen, f"{label}: parent cycle through {node!r}")
            seen.add(node)
            node = parents.get(node)


def validate(document: Any) -> None:
    check(isinstance(document, Mapping), "scene graph must be an object")
    require(document, REQUIRED, "scene graph")
    check(document["schema"] == SCHEMA, f"scene graph schema must be {SCHEMA}")
    identifier(document["scene_id"], "scene_id")
    check(document["units"] == "m", "units must be 'm'")
    check(document["frame"] == "world", "frame must be 'world'")
    portable(document, "scene graph")
    for key in ("asset_bundle", "physics_parameter_ledger", "runtime_profile"):
        ref(document[key], key)
    check(dict(document["backend"]) == BACKEND, f"backend must be {BACKEND}")

    assets = document["assets"]
    check(isinstance(assets, list) and bool(assets), "assets must be a non-empty list")
    asset_ids = set()
    for index, entry in enumerate(assets):
        check(isinstance(entry, Mapping), f"assets[{index}] must be an object")
        asset_id = identifier(entry.get("asset_id"), f"assets[{index}].asset_id")
        check(asset_id not in asset_ids, f"duplicate asset {asset_id!r}")
        asset_ids.add(asset_id)
        ref(entry.get("record"), f"assets[{index}].record")

    frames = document["frames"]
    check(isinstance(frames, list) and bool(frames), "frames must be a non-empty list")
    frame_parents: dict[str, str | None] = {}
    for index, frame in enumerate(frames):
        where = f"frames[{index}]"
        check(isinstance(frame, Mapping), f"{where} must be an object")
        require(frame, ("frame_id", "parent_frame_id", "pose"), where)
        check(frame["frame_id"] not in frame_parents, f"duplicate frame {frame['frame_id']!r}")
        frame_parents[identifier(frame["frame_id"], f"{where}.frame_id")] = frame["parent_frame_id"]
        pose(frame["pose"], f"{where}.pose")
    check(frame_parents.get("world", "missing") is None, "frames must declare 'world' with parent null")

    nodes = document["nodes"]
    check(isinstance(nodes, list) and bool(nodes), "nodes must be a non-empty list")
    node_parents: dict[str, str | None] = {}
    for index, node in enumerate(nodes):
        where = f"nodes[{index}]"
        check(isinstance(node, Mapping), f"{where} must be an object")
        require(node, ("node_id", "asset_id", "pose", "semantic_tag"), where)
        node_id = identifier(node["node_id"], f"{where}.node_id")
        check(node_id not in node_parents and node_id not in frame_parents, f"duplicate node/frame id {node_id!r}")
        node_parents[node_id] = node.get("parent")
        check(node["asset_id"] in asset_ids, f"{where}.asset_id {node['asset_id']!r} is not in assets")
        check(node["semantic_tag"] in SEMANTIC_TAGS, f"{where}.semantic_tag must be one of {list(SEMANTIC_TAGS)}")
        pose(node["pose"], f"{where}.pose")
        joint_state = node.get("joint_state", {})
        check(isinstance(joint_state, Mapping) and all(is_finite_number(v) for v in joint_state.values()),
              f"{where}.joint_state must map joint names to finite numbers")
        jitter = node.get("randomization")
        if jitter is not None:
            check(isinstance(jitter, Mapping), f"{where}.randomization must be an object")
            if "position_jitter_m" in jitter:
                check(is_finite_number(jitter["position_jitter_m"]) and jitter["position_jitter_m"] >= 0,
                      f"{where}.randomization.position_jitter_m must be >= 0")
            if "yaw_range_deg" in jitter:
                low, high = vector(jitter["yaw_range_deg"], 2, f"{where}.randomization.yaw_range_deg")
                check(-180 <= low <= high <= 180, f"{where}.randomization.yaw_range_deg must lie in [-180, 180]")
    known = set(node_parents) | set(frame_parents)
    for node_id, parent in node_parents.items():
        check(parent is None or parent in node_parents, f"node {node_id!r} has dangling parent {parent!r}")
    for frame_id, parent in frame_parents.items():
        check(parent is None or parent in known, f"frame {frame_id!r} has dangling parent {parent!r}")
    _acyclic(node_parents, "nodes")
    _acyclic(frame_parents, "frames")
    for index, node in enumerate(nodes):
        check(node["pose"]["frame_id"] in known, f"nodes[{index}].pose.frame_id is not a known frame or node")

    robot = document["robot"]
    check(isinstance(robot, Mapping), "robot must be an object")
    require(robot, ("asset_id", "base_pose", "arms", "gripper", "controlled_joints", "tcp", "reachability"), "robot")
    check(robot["asset_id"] in asset_ids, "robot.asset_id is not in assets")
    pose(robot["base_pose"], "robot.base_pose")
    check(isinstance(robot["arms"], list) and bool(robot["arms"]) and set(robot["arms"]) <= {"left", "right"}
          and len(set(robot["arms"])) == len(robot["arms"]), "robot.arms must be a non-empty subset of left/right")
    check(robot["gripper"] == "g2", "robot.gripper must be 'g2'")
    check(isinstance(robot["controlled_joints"], list) and bool(robot["controlled_joints"])
          and all(isinstance(j, str) and j for j in robot["controlled_joints"]),
          "robot.controlled_joints must be a non-empty list of joint names")
    tcp = robot["tcp"]
    check(isinstance(tcp, Mapping), "robot.tcp must be an object")
    require(tcp, ("frame_id", "link", "T_link_tcp"), "robot.tcp")
    pose(tcp["T_link_tcp"], "robot.tcp.T_link_tcp")
    check(tcp["T_link_tcp"]["frame_id"] == tcp["link"], "robot.tcp.T_link_tcp.frame_id must equal robot.tcp.link")
    _reachability(robot["reachability"], "robot.reachability")

    cameras = document["cameras"]
    check(isinstance(cameras, list), "cameras must be a list")
    camera_ids = set()
    robot_nodes = {n["node_id"] for n in nodes if n["semantic_tag"] == "robot" and n["asset_id"] == robot["asset_id"]}
    for index, camera in enumerate(cameras):
        where = f"cameras[{index}]"
        check(isinstance(camera, Mapping), f"{where} must be an object")
        require(camera, ("camera_id", "kind", "mount", "offset", "intrinsics", "optical_frame", "distortion", "fps"),
                where)
        check(camera["camera_id"] not in camera_ids, f"duplicate camera {camera['camera_id']!r}")
        camera_ids.add(identifier(camera["camera_id"], f"{where}.camera_id"))
        check(camera["kind"] in ("physical", "virtual"), f"{where}.kind must be physical or virtual")
        check(camera["mount"] in known, f"{where}.mount must be a node or frame id")
        link = camera.get("link")
        if link is not None:
            check(isinstance(link, str) and bool(link), f"{where}.link must be a robot link name")
            check(camera["mount"] in robot_nodes, f"{where}: a link-mounted camera must mount on the robot node")
        pose(camera["offset"], f"{where}.offset")
        check(camera["offset"]["frame_id"] == (link or camera["mount"]),
              f"{where}.offset.frame_id must equal link when given, else mount")
        intrinsics = camera["intrinsics"]
        check(isinstance(intrinsics, Mapping) and all(is_finite_number(intrinsics.get(k)) and intrinsics[k] > 0
                                                      for k in ("fx", "fy", "cx", "cy", "width", "height")),
              f"{where}.intrinsics must have positive fx, fy, cx, cy, width, height")
        check(isinstance(camera["optical_frame"], Mapping) and camera["optical_frame"].get("convention")
              in ("opengl", "opencv", "ros"), f"{where}.optical_frame.convention must be opengl/opencv/ros")
        distortion = camera["distortion"]
        check(isinstance(distortion, Mapping) and distortion.get("model") in ("none", "radtan")
              and isinstance(distortion.get("coefficients"), list), f"{where}.distortion is malformed")
        fps = camera["fps"]
        check(isinstance(fps, int) and fps > 0 and CONTROL_HZ % fps == 0, f"{where}.fps must divide {CONTROL_HZ}")

    groups = document["collision_groups"]
    check(isinstance(groups, Mapping), "collision_groups must be an object")
    for group_id, group in groups.items():
        check(isinstance(group, Mapping), f"collision_groups.{group_id} must be an object")
        for key in ("entities", "contact_with", "forbidden_with"):
            members = group.get(key)
            check(isinstance(members, list) and all(m in node_parents for m in members),
                  f"collision_groups.{group_id}.{key} must list node ids")
        check(not set(group["contact_with"]) & set(group["forbidden_with"]),
              f"collision_groups.{group_id}: an entity cannot be both allowed and forbidden")

    checks = document["checks"]
    check(isinstance(checks, Mapping), "checks must be an object")
    require(checks, ("settle", "penetration", "reachability"), "checks")
    settle = checks["settle"]
    check(isinstance(settle, Mapping) and isinstance(settle.get("ran"), bool) and isinstance(settle.get("stable"), bool)
          and isinstance(settle.get("steps"), int) and is_finite_number(settle.get("max_drift_m")),
          "checks.settle must be {ran, steps, max_drift_m, stable}")
    check(settle["ran"] or not settle["stable"], "checks.settle: stable=true requires ran=true")
    penetration = checks["penetration"]
    check(isinstance(penetration, Mapping) and isinstance(penetration.get("ran"), bool)
          and isinstance(penetration.get("ok"), bool) and is_finite_number(penetration.get("max_depth_m")),
          "checks.penetration must be {ran, max_depth_m, ok}")
    check(penetration["ran"] or not penetration["ok"], "checks.penetration: ok=true requires ran=true")
    _reachability(checks["reachability"], "checks.reachability")
    check(dict(checks["reachability"]) == dict(robot["reachability"]),
          "checks.reachability must equal robot.reachability")

    randomization = document.get("randomization")
    if randomization is not None:
        check(isinstance(randomization, Mapping) and isinstance(randomization.get("seed"), int)
              and isinstance(randomization.get("sampled"), Mapping), "randomization must be {seed, sampled}")

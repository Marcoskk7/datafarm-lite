"""在 Isaac Lab 3.0（Newton + MJWarp）里实例化 SceneGraph 节点，做 settle、穿透和放置探针检查。

只能在 Isaac Lab 的 Python 里、`launch_simulation` 上下文内调用。
- settle：从布局初值自由仿真 `settle_s` 秒，记录每个动态节点的位移与末速度、铰接关节漂移。
- 穿透：读取 MJWarp 的接触缓冲（`Contact.dist` < 0 即穿透），按节点对汇总最大深度。
  这是 MJWarp 碰撞几何（凸包）之间的深度，不是渲染网格之间的。
- 探针：把任务物体传送到某个节点局部坐标（随关节初值平移），自由落下后检查静止高度与接触对象，
  用来证明"抽屉内腔装得下盘子且能托住"。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

PRIM_ROOT = "/World/envs/env_0"


def prim_name(node_id: str) -> str:
    return "".join(part.capitalize() for part in node_id.replace("-", "_").replace(".", "_").split("_"))


def build_scene_cfg(*, nodes: Sequence[Mapping[str, Any]], assets: Mapping[str, Mapping[str, Any]],
                    wrappers: Mapping[str, str], robot_cfg, robot_node: Mapping[str, Any], drawer_damping: float):
    """`assets[asset_id]["body"]` ∈ kinematic / rigid / articulation；`wrappers[asset_id]` 是碰撞包装层 USD。

    铰接家具关掉自碰撞：相邻抽屉/柜体的凸包相互外扩几毫米，自碰撞会让它们在初始时就互相顶住；
    抽屉相对柜体的运动已经由棱柱关节和限位约束。
    """
    import isaaclab.sim as sim_utils
    from isaaclab.actuators import ImplicitActuatorCfg
    from isaaclab.assets import ArticulationCfg, AssetBaseCfg, RigidObjectCfg
    from isaaclab.scene import InteractiveSceneCfg
    from isaaclab.utils import configclass
    from isaaclab_newton.sim.schemas import NewtonArticulationCfg

    fields: dict[str, Any] = {
        "ground": AssetBaseCfg(prim_path="/World/ground", spawn=sim_utils.GroundPlaneCfg()),
    }
    for node in nodes:
        spec = assets[node["asset_id"]]
        pos = tuple(node["pose"]["position_m"])
        rot = tuple(node["pose"]["quaternion_xyzw"])
        path = "{ENV_REGEX_NS}/" + prim_name(node["node_id"])
        usd = wrappers[node["asset_id"]]
        if spec["body"] == "articulation":
            joint_pos = dict(node.get("joint_state", {}))
            fields[node["node_id"]] = ArticulationCfg(
                prim_path=path,
                spawn=sim_utils.UsdFileCfg(usd_path=usd,
                                           articulation_props=[NewtonArticulationCfg(self_collision_enabled=False)]),
                init_state=ArticulationCfg.InitialStateCfg(pos=pos, rot=rot, joint_pos=joint_pos or {".*": 0.0}),
                actuators={"passive": ImplicitActuatorCfg(joint_names_expr=[".*"], stiffness=0.0,
                                                          damping=drawer_damping)})
        else:
            rigid = sim_utils.RigidBodyPropertiesCfg(kinematic_enabled=spec["body"] == "kinematic")
            fields[node["node_id"]] = RigidObjectCfg(
                prim_path=path, spawn=sim_utils.UsdFileCfg(usd_path=usd, rigid_props=rigid),
                init_state=RigidObjectCfg.InitialStateCfg(pos=pos, rot=rot))
    robot_usd = wrappers.get(robot_node["asset_id"])
    fields[robot_node["node_id"]] = robot_cfg.replace(
        prim_path="{ENV_REGEX_NS}/" + prim_name(robot_node["node_id"]),
        **({"spawn": robot_cfg.spawn.replace(usd_path=robot_usd)} if robot_usd else {}),
        init_state=robot_cfg.init_state.replace(pos=tuple(robot_node["pose"]["position_m"]),
                                                rot=tuple(robot_node["pose"]["quaternion_xyzw"])))
    keyed = {key.replace(".", "__"): value for key, value in fields.items()}
    cls = configclass(type("SceneCfg", (InteractiveSceneCfg,), {"__annotations__": {k: type(v) for k, v in keyed.items()},
                                                                **keyed}))
    return cls


def entity(scene, node_id: str):
    return scene[node_id.replace(".", "__")]


def _body_owner(labels: Sequence[str], node_ids: Sequence[str]) -> list[str | None]:
    prefixes = {f"{PRIM_ROOT}/{prim_name(n)}": n for n in node_ids}
    out = []
    for label in labels:
        owner = None
        for prefix, node_id in prefixes.items():
            if label == prefix or label.startswith(prefix + "/"):
                owner = node_id
                break
        out.append(owner)
    return out


def body_ref(label: str) -> str:
    """Newton 刚体标签 → 可持久化的场景 prim 引用 `usd:/World/...`；`world` 这类非 prim 标签原样返回。

    这是合成场景里的引用，和 `usd_assets.prim_ref` 的资产内相对路径不是一个命名空间。
    """
    return f"usd:{label}" if label.startswith("/") else label


def contacts(node_ids: Sequence[str]) -> list[dict[str, Any]]:
    """当前 MJWarp 接触：每条 `{a, b, body_a, body_b, dist_m}`；a/b 是节点 id（world/ground 记 None）。

    `body_a`/`body_b` 是 `usd:/World/...` 场景引用，可以直接进可移植证据，不是磁盘路径。
    """
    return contacts_with_coverage(node_ids)["rows"]


def contacts_with_coverage(node_ids: Sequence[str]) -> dict[str, Any]:
    """接触行加覆盖度证明：``nacon <= naconmax`` 时为 complete，溢出缓冲区时为 truncated。

    只证明“本控制周期末快照里求解器检测到的接触都已列出”；不提供力/法向，也不覆盖
    子步之间出现又消失的瞬时接触。
    """
    from isaaclab_newton.physics import NewtonManager
    solver = NewtonManager._solver
    model = NewtonManager.get_model()
    data = solver.mjw_data
    detected, capacity = int(data.nacon.numpy()[0]), int(data.naconmax)
    if detected < 0 or capacity < 0:
        raise ValueError("invalid contact counters")
    coverage = "complete" if detected <= capacity else "truncated"
    count = min(detected, capacity)
    if count == 0:
        return {"rows": [], "coverage": coverage, "detected": detected, "capacity": capacity}
    dist = data.contact.dist.numpy()[:count]
    geom = data.contact.geom.numpy()[:count]
    world = data.contact.worldid.numpy()[:count]
    geom_to_shape = solver.mjc_geom_to_newton_shape.numpy()
    shape_body = model.shape_body.numpy()
    labels = list(model.body_label)
    owners = _body_owner(labels, node_ids)
    rows = []
    for k in range(count):
        ends = []
        for g in geom[k]:
            shape = int(geom_to_shape[int(world[k]), int(g)]) if g >= 0 else -1
            body = int(shape_body[shape]) if shape >= 0 else -1
            ends.append((owners[body] if body >= 0 else None,
                         body_ref(labels[body]) if body >= 0 else "world"))
        rows.append({"a": ends[0][0], "b": ends[1][0], "body_a": ends[0][1], "body_b": ends[1][1],
                     "dist_m": float(dist[k])})
    return {"rows": rows, "coverage": coverage, "detected": detected, "capacity": capacity}


def penetration_summary(rows: Sequence[Mapping[str, Any]], tolerance_m: float) -> dict[str, Any]:
    """按节点对取最大穿透深度，并记下最深那条接触的两个节点与刚体名，便于定位与机器判定。"""
    pairs: dict[str, float] = {}
    deepest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = " | ".join(sorted(str(x) for x in (row["a"], row["b"])))
        if -row["dist_m"] > pairs.get(key, -math.inf):
            pairs[key] = -row["dist_m"]
            deepest[key] = {"a": row["a"], "b": row["b"], "body_a": row["body_a"], "body_b": row["body_b"]}
    worst = max(pairs.values(), default=0.0)
    ordered = sorted(pairs, key=lambda k: -pairs[k])
    return {"ran": True, "max_depth_m": round(max(worst, 0.0), 6), "ok": worst <= tolerance_m,
            "tolerance_m": tolerance_m, "contact_count": len(rows),
            "pairs": {k: {"depth_m": round(pairs[k], 6),
                          "bodies": sorted(deepest[k][end].rsplit("/", 1)[-1] for end in ("body_a", "body_b")),
                          **{f: deepest[k][f] for f in ("a", "b", "body_a", "body_b")}}
                      for k in ordered}}


def added_contacts(initial: Mapping[str, Any], final: Mapping[str, Any]) -> dict[str, Any]:
    """`final` 里出现而 `initial` 里没有的接触对，按节点对键给最终深度（可能为负，即尚未接触）。

    `initial` / `final` 都是 `penetration_summary` 的结果。用于机器判定"静置期间是否新增了接触对"。
    """
    seen = initial.get("pairs") or {}
    return {key: dict(entry) for key, entry in (final.get("pairs") or {}).items() if key not in seen}


def _step(sim, scene, steps: int) -> None:
    dt = sim.get_physics_dt()
    for _ in range(steps):
        scene.write_data_to_sim()
        sim.step(render=False)
        scene.update(dt)


def _row(tensor) -> list[float]:
    return [round(float(v), 6) for v in tensor[0].tolist()]


def apply_initial_state(sim, scene, *, nodes: Sequence[Mapping[str, Any]],
                        assets: Mapping[str, Mapping[str, Any]]) -> dict[str, list[float]]:
    """把 SceneGraph 的 `joint_state` 写进仿真并刷新一次读数；返回每个铰接节点写入后读回的关节位置。

    Isaac Lab 3.0 + Newton 下 `scene.reset()` 只更新 `default_joint_pos`，不写回 `joint_pos`（实测抽屉读回 0），
    所以这里显式写一遍，读回值不等于目标时直接报错，避免在关着的抽屉上做检查。
    """
    import torch
    readback: dict[str, list[float]] = {}
    for node in nodes:
        if assets[node["asset_id"]]["body"] != "articulation":
            continue
        handle = entity(scene, node["node_id"])
        target = handle.data.default_joint_pos.torch.clone()
        handle.write_joint_position_to_sim_index(position=target)
        handle.write_joint_velocity_to_sim_index(velocity=torch.zeros_like(target))
        handle.set_joint_position_target_index(target=target)
    scene.write_data_to_sim()
    sim.forward()
    scene.update(sim.get_physics_dt())
    for node in nodes:
        if assets[node["asset_id"]]["body"] != "articulation":
            continue
        handle = entity(scene, node["node_id"])
        readback[node["node_id"]] = _row(handle.data.joint_pos.torch)
        for name, value in node.get("joint_state", {}).items():
            got = readback[node["node_id"]][handle.joint_names.index(name)]
            if abs(got - value) > 1e-4:
                raise RuntimeError(f"{node['node_id']}.{name}: wrote {value}, simulator reads {got}")
    return readback


def settle(sim, scene, *, nodes: Sequence[Mapping[str, Any]], assets: Mapping[str, Mapping[str, Any]],
           robot_node_id: str, seconds: float, drift_tolerance_m: float, speed_tolerance_mps: float,
           joint_tolerance_active: float, joint_tolerance_passive: float, penetration_tolerance_m: float,
           joint_limits: Mapping[str, Mapping[str, Sequence[float]]] | None = None,
           active_joints: Mapping[str, Sequence[str]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 (settle, penetration)；两者都是 SceneGraph `checks` 的超集（多出的字段是证据）。

    关节分两类。**主动关节**（`active_joints` 里列出的、以及机器人自身的关节）由驱动程序保持，
    漂移必须不超过 `joint_tolerance_active`。**被动关节**（其余关节，家具的抽屉/门）可以动：
    只要求 (a) 该节点静置期间没有新增接触对，(b) 测量值仍在关节限位内，(c) 测量值被记录
    （`settle["measured_joint_state"]`）。`joint_tolerance_passive` 只是"是否报告为动了"的阈值，
    不是接受边界。布局里的 `joint_state` 是意图，静置后的测量值才是下游的初始状态。
    """
    import numpy as np
    steps = int(round(seconds / sim.get_physics_dt()))
    node_ids = [n["node_id"] for n in nodes] + [robot_node_id]
    initial_contacts = penetration_summary(contacts(node_ids), penetration_tolerance_m)
    start: dict[str, Any] = {}
    for node in nodes:
        handle = entity(scene, node["node_id"])
        if assets[node["asset_id"]]["body"] == "articulation":
            start[node["node_id"]] = (_row(handle.data.root_pos_w.torch), _row(handle.data.joint_pos.torch))
        else:
            start[node["node_id"]] = (_row(handle.data.root_pos_w.torch), None)
    robot = entity(scene, robot_node_id)
    robot_start = _row(robot.data.joint_pos.torch)
    robot.set_joint_position_target_index(target=robot.data.joint_pos.torch.clone())
    _step(sim, scene, steps)
    per_node, worst_drift, worst_speed = {}, 0.0, 0.0
    measured: dict[str, dict[str, float]] = {}
    for node in nodes:
        handle = entity(scene, node["node_id"])
        pos = _row(handle.data.root_pos_w.torch)
        drift = float(np.linalg.norm(np.subtract(pos, start[node["node_id"]][0])))
        entry: dict[str, Any] = {"start_m": start[node["node_id"]][0], "end_m": pos, "drift_m": round(drift, 6)}
        if assets[node["asset_id"]]["body"] == "articulation":
            q = _row(handle.data.joint_pos.torch)
            entry.update(joint_names=list(handle.joint_names), joint_start=start[node["node_id"]][1], joint_end=q)
            measured[node["node_id"]] = dict(zip(handle.joint_names, q))
        elif assets[node["asset_id"]]["body"] == "rigid":
            speed = float(np.linalg.norm(handle.data.root_lin_vel_w.torch[0].tolist()))
            entry["end_speed_mps"] = round(speed, 6)
            worst_speed = max(worst_speed, speed)
        worst_drift = max(worst_drift, drift)
        per_node[node["node_id"]] = entry
    robot_end = _row(robot.data.joint_pos.torch)
    per_node[robot_node_id] = {"joint_names": list(robot.joint_names), "joint_start": robot_start,
                               "joint_end": robot_end, "joint_drift": 0.0}
    measured[robot_node_id] = dict(zip(robot.joint_names, robot_end))

    final_contacts = penetration_summary(contacts(node_ids), penetration_tolerance_m)
    new_contacts = added_contacts(initial_contacts, final_contacts)
    held = {node_id: set(names) for node_id, names in (active_joints or {}).items()}
    held.setdefault(robot_node_id, set(robot.joint_names))

    worst_active = worst_passive = 0.0
    active_ok = passive_ok = True
    for node in list(nodes) + [{"node_id": robot_node_id, "asset_id": None, "joint_state": {}}]:
        node_id = node["node_id"]
        entry = per_node.get(node_id)
        if entry is None or not entry.get("joint_names"):
            continue
        names = list(entry["joint_names"])
        started = entry.get("joint_start") or [0.0] * len(names)
        ends = entry.get("joint_end") or [0.0] * len(names)
        held_here = held.get(node_id, set())
        node_added = {key: value for key, value in new_contacts.items()
                      if node_id in (value.get("a"), value.get("b"))}
        checks = []
        for index, name in enumerate(names):
            drift = abs(ends[index] - started[index])
            declared = (node.get("joint_state") or {}).get(name)
            row: dict[str, Any] = {"name": name, "actuated": name in held_here, "declared": declared,
                                   "start": round(started[index], 6), "measured": round(ends[index], 6),
                                   "drift_m": round(drift, 6)}
            if name in held_here:
                worst_active = max(worst_active, drift)
                row.update(tolerance_m=joint_tolerance_active, ok=drift <= joint_tolerance_active)
                active_ok = active_ok and row["ok"]
            else:
                limits = (joint_limits or {}).get(node_id, {}).get(name)
                lower, upper = (float(limits[0]), float(limits[1])) if limits is not None else (None, None)
                within = (lower is not None and upper is not None
                          and lower - 1e-9 <= ends[index] <= upper + 1e-9)
                moved = drift > joint_tolerance_passive
                worst_passive = max(worst_passive, drift)
                ok = within and (not moved or not node_added)
                row.update(limits=[lower, upper], within_limits=within, moved=moved,
                           new_contacts=node_added, ok=ok)
                passive_ok = passive_ok and ok
            checks.append(row)
        entry["joint_checks"] = checks
        entry["new_contacts"] = node_added
        entry["joint_drift"] = round(max((c["drift_m"] for c in checks), default=0.0), 6)

    stable = (worst_drift <= drift_tolerance_m and worst_speed <= speed_tolerance_mps
              and active_ok and passive_ok)
    settle_report = {"ran": True, "steps": steps, "max_drift_m": round(worst_drift, 6), "stable": stable,
                     "seconds": seconds, "physics_dt_s": sim.get_physics_dt(),
                     "tolerances": {"drift_m": drift_tolerance_m, "speed_mps": speed_tolerance_mps,
                                    "joint_active": joint_tolerance_active,
                                    "joint_passive": joint_tolerance_passive},
                     "max_end_speed_mps": round(worst_speed, 6), "max_joint_drift": round(worst_active, 6),
                     "max_passive_joint_drift": round(worst_passive, 6), "nodes": per_node,
                     "measured_joint_state": measured, "new_contacts": new_contacts}
    penetration = {**final_contacts, "ok": final_contacts["ok"] and initial_contacts["ok"],
                   "max_depth_m": max(final_contacts["max_depth_m"], initial_contacts["max_depth_m"]),
                   "initial": initial_contacts}
    return settle_report, penetration


def drop_probe(sim, scene, *, node_id: str, world_position: Sequence[float], seconds: float,
               rest_z: tuple[float, float], support_node: str, all_nodes: Sequence[str],
               speed_tolerance_mps: float) -> dict[str, Any]:
    """把 `node_id` 传送到 `world_position` 落下，检查静止高度、末速度、与 `support_node` 有接触。"""
    import torch
    handle = entity(scene, node_id)
    origin = scene.env_origins[0].tolist()
    quat = handle.data.root_quat_w.torch[0].tolist() if hasattr(handle.data, "root_quat_w") else [0, 0, 0, 1]
    pose = torch.tensor([[world_position[0] + origin[0], world_position[1] + origin[1],
                          world_position[2] + origin[2], *quat]], device=sim.device, dtype=torch.float32)
    handle.write_root_pose_to_sim_index(root_pose=pose)
    handle.write_root_velocity_to_sim_index(root_velocity=torch.zeros((1, 6), device=sim.device))
    _step(sim, scene, int(round(seconds / sim.get_physics_dt())))
    pos = _row(handle.data.root_pos_w.torch)
    speed = float(torch.linalg.norm(handle.data.root_lin_vel_w.torch[0]).item())
    touching = sorted({str(r["b"] if r["a"] == node_id else r["a"]) for r in contacts(all_nodes)
                       if node_id in (r["a"], r["b"]) and r["dist_m"] < 0.002})
    ok = rest_z[0] <= pos[2] <= rest_z[1] and speed <= speed_tolerance_mps and support_node in touching
    return {"node": node_id, "released_at_m": [round(float(v), 6) for v in world_position], "rest_m": pos,
            "end_speed_mps": round(speed, 6), "touching": touching, "expected_rest_z_m": list(rest_z),
            "support_node": support_node, "ok": ok}

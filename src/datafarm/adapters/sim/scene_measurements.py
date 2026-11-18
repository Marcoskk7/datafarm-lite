"""Full-scene snapshots using A node queries, without a physics step or rounding."""
from __future__ import annotations
from copy import deepcopy
import math
from datafarm.adapters.sim import isaac_scene
from datafarm.ir.episode_state import UNITS, validate_state


def array(value):
    value = value.torch if hasattr(value, "torch") else value
    value = value.detach().cpu() if hasattr(value, "detach") else value
    return value.tolist() if hasattr(value, "tolist") else deepcopy(value)


def row(data, name):
    value = array(getattr(data, name))
    if len(value) != 1:
        raise ValueError("exactly one simulation environment required")
    return value[0]


class SceneMeasurements:
    def __init__(self, scene, graph, records, api, *, contact_query=None, rejected_sink=None):
        self.scene, self.graph, self.records, self.api = scene, deepcopy(graph), deepcopy(records), api
        self.nodes = [n["node_id"] for n in graph["nodes"]]
        # 默认查询带覆盖度证明；注入的旧式查询若只返回 list，则覆盖度记 unknown。
        self.contact_query = contact_query or isaac_scene.contacts_with_coverage
        self.rejected_sink = rejected_sink
        self.robot_node = next(n["node_id"] for n in graph["nodes"] if n["semantic_tag"] == "robot")

    def snapshot(self, step, dt):
        entities = {}
        for node in self.graph["nodes"]:
            handle = isaac_scene.entity(self.scene, node["node_id"])
            data = handle.data
            entry = {"availability": "available", "asset_id": node["asset_id"],
                     "position_m": row(data, "root_pos_w"), "quaternion_xyzw": row(data, "root_quat_w"),
                     "linear_velocity_m_s": row(data, "root_lin_vel_w"),
                     "angular_velocity_rad_s": row(data, "root_ang_vel_w"), "joints": [], "links": {}}
            names = list(getattr(handle, "joint_names", []))
            source = {j["name"]: j for j in self.records[node["asset_id"]]["joints"]}
            active = {n: j for n, j in source.items() if j["type"] != "fixed"}
            if set(names) != set(active):
                raise ValueError("source/runtime joint coverage mismatch")
            if names:
                q, dq = row(data, "joint_pos"), row(data, "joint_vel")
                if len(q) != len(names) or len(dq) != len(names):
                    raise ValueError("joint state dimension mismatch")
                entry["joints"] = [{"name": n, "type": active[n]["type"],
                    "unit": "m" if active[n]["type"] == "prismatic" else "rad", "q": q[i], "dq": dq[i]}
                    for i, n in enumerate(names)]
            body_names = list(getattr(handle, "body_names", []))
            if body_names:
                positions, rotations = row(data, "body_pos_w"), row(data, "body_quat_w")
                if len(body_names) != len(set(body_names)) or len(positions) != len(body_names) or len(rotations) != len(body_names):
                    raise ValueError("link state dimension mismatch")
                entry["links"] = {n: {"position_m": positions[i], "quaternion_xyzw": rotations[i]}
                                  for i, n in enumerate(body_names)}
            entities[node["node_id"]] = entry
        try:
            reply = self.contact_query(self.nodes)
            coverage = "unknown"
            if isinstance(reply, dict):
                rows, coverage = reply.get("rows"), reply.get("coverage")
                if coverage not in ("complete", "truncated"):
                    raise ValueError("contact coverage proof missing")
            else:
                rows = reply
            if not isinstance(rows, list):
                raise ValueError("contact query did not return rows")
            for item in rows:
                if set(item) != {"a", "b", "body_a", "body_b", "dist_m"} or not math.isfinite(item["dist_m"]):
                    raise ValueError("invalid contact row")
                if any(item[end] is not None and item[end] not in self.nodes for end in ("a", "b")):
                    raise ValueError("unknown contact endpoint")
            # complete 仅当求解器原始计数 nacon <= naconmax；缓冲区溢出记 truncated，无计数证明记 unknown。
            contact = {"availability": "available", "rows": [{**i, **{k: "usd:"+i[k] if i[k].startswith("/") else i[k] for k in ("body_a", "body_b")}} for i in rows], "coverage": coverage,
                       "force_availability": "unavailable", "normal_availability": "unavailable"}
        except Exception:
            contact = {"availability": "unavailable", "rows": None, "coverage": "unknown",
                       "force_availability": "unavailable", "normal_availability": "unavailable",
                       "reason_code": "CONTACT_QUERY_UNAVAILABLE"}
        contact.update(control_step=step, sampling="cycle_end_snapshot")
        robot = self.api.observe()
        value = {"control_step": step, "time_s": step*dt, "units": UNITS, "frame_id": "world",
                 "entities": entities, "robot": robot, "contacts": contact}
        try:
            return validate_state(value, node_ids=self.nodes, step=step, dt=dt)
        except ValueError:
            if self.rejected_sink is not None:
                self.rejected_sink(value)
            raise

    def resolve_frame(self, frame_id, step, dt):
        """Resolve named A frames using current measurements, not cached node layout."""
        from datafarm.simulation.robot_semantics import compose
        state = self.snapshot(step, dt)
        if frame_id == self.graph["robot"]["tcp"]["frame_id"]:
            if "tcp" not in state["robot"]:
                raise ValueError("TCP observation unavailable")
            return deepcopy(state["robot"]["tcp"])
        frames = {f["frame_id"]: f for f in self.graph["frames"]}
        def resolve(name, seen):
            if name == "world":
                return {"frame_id": "world", "position_m": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]}
            if name in state["entities"]:
                value = state["entities"][name]
                return {"frame_id": "world", "position_m": value["position_m"], "quaternion_xyzw": value["quaternion_xyzw"]}
            if name in seen or name not in frames:
                raise ValueError("frame not observable")
            frame = frames[name]
            return {"frame_id": "world", **compose(resolve(frame["parent_frame_id"], seen | {name}), frame["pose"])}
        return resolve(frame_id, set())

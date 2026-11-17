"""A-line scene assembly without a simulator: records, layout heights, drawer-shifted reach and SceneGraph IR."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

import numpy as np

from datafarm.ir import scene_graph
from datafarm.ir.common import IRError
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.scene import asset_records, kinematics, layout, planning
from datafarm.simulation.robot_binding import discover_urdf


def ref(kind: str, char: str) -> ArtifactRef:
    return ArtifactRef(kind=kind, sha256=char * 64, media_type="application/json")


def _joint(name, parent, child, xyz, axis, kind="revolute", limits="-3 3"):
    lower, upper = limits.split()
    return (f'<joint name="{name}" type="{kind}"><origin xyz="{xyz}"/><axis xyz="{axis}"/>'
            f'<parent link="{parent}"/><child link="{child}"/>'
            f'<limit lower="{lower}" upper="{upper}" velocity="1" effort="1"/></joint>')


# 6-DOF arm with a spherical wrist; base at the origin, 0.7 m reach along +x at zero pose.
LINKS = ["base", "l1", "l2", "l3", "l4", "l5", "l6", "end_link"]
URDF = ("<robot name=\"toy\">" + "".join(f'<link name="{n}"/>' for n in LINKS)
        + _joint("j1", "base", "l1", "0 0 0.1", "0 0 1")
        + _joint("j2", "l1", "l2", "0 0 0.1", "0 1 0", limits="-2 2")
        + _joint("j3", "l2", "l3", "0.3 0 0", "0 1 0", limits="-2.5 2.5")
        + _joint("j4", "l3", "l4", "0.3 0 0", "1 0 0")
        + _joint("j5", "l4", "l5", "0 0 0", "0 1 0", limits="-2 2")
        + _joint("j6", "l5", "l6", "0 0 0", "1 0 0")
        + '<joint name="tip" type="fixed"><origin xyz="0.1 0 0"/><parent link="l6"/><child link="end_link"/></joint>'
        + "</robot>").encode()


def fetch(asset: str) -> dict:
    return {"asset": asset, "source_uri": f"https://huggingface.co/datasets/nvidia/simready-assets/tree/{'c' * 40}/{asset}",
            "license": "CC-BY-4.0", "retrieved_at": "2026-11-15T00:00:00Z", "package_id": f"pkg.{asset}"}


def inspection(lo, hi, *, joints=(), mass=1.0, material=None) -> dict:
    return {"bbox_min_m": list(lo), "bbox_max_m": list(hi), "up_axis": "Z", "joints": list(joints),
            "bodies": {"Root/body": {"mass_kg": mass}},
            "colliders": [{"body": "Root/body", "physics_material": material}]}


DRAWER = {"name": "drawer_joint", "type": "prismatic", "axis": [1.0, 0.0, 0.0],
          "limits": {"lower": 0.0, "upper": 0.4}, "parent": "Root/body", "child": "Root/drawer"}

# SimReady 命名：分件是刚体 prim（`*_obj_00`），关节 `child` 是它下面的网格 prim（`*_mesh_00`）。
CABINET_BODY = "RootNode/Geometry/body_01_obj_00"
DRAWER_BODY = "RootNode/Geometry/part_01_obj_00"


def part(name: str, prim: str, lo, hi) -> dict:
    return {"name": name, "prim": prim, "bbox_min_m": list(lo), "bbox_max_m": list(hi)}


def records() -> dict:
    table = asset_records.build_record(
        asset_id="object.table_a", role="background", fetch=fetch("table_a"),
        inspection=inspection([-0.5, -0.4, -0.7], [0.5, 0.4, 0.05], mass=40.0,
                              material={"static_friction": 0.5, "dynamic_friction": 0.4, "restitution": 0.1}),
        usd_ref=ref("asset.usd", "1"), inspection_ref=ref("asset.usd-inspection", "2"), collision_ref=None)
    cabinet = asset_records.build_record(
        asset_id="object.cabinet_a", role="receptacle", fetch=fetch("cabinet_a"),
        inspection=inspection([-0.3, -0.3, 0.0], [0.3, 0.3, 0.6], joints=[DRAWER], mass=30.0),
        usd_ref=ref("asset.usd", "3"), inspection_ref=ref("asset.usd-inspection", "4"),
        collision_ref=ref("asset.collision-prep", "5"))
    plate = asset_records.build_record(
        asset_id="object.plate_a", role="task_object", fetch=fetch("plate_a"),
        inspection=inspection([-0.1, -0.1, 0.0], [0.1, 0.1, 0.02], mass=0.2),
        usd_ref=ref("asset.usd", "6"), inspection_ref=ref("asset.usd-inspection", "7"), collision_ref=None)
    robot = asset_records.robot_record(
        asset_id="robot.toy", descriptor=discover_urdf(URDF), urdf_ref=ref("robot.urdf", "8"),
        usd_ref=ref("robot.usd", "9"), license="unknown", retrieved_at="2026-11-15T00:00:00Z", uri="local:toy",
        base_collision={"bbox_min_m": [-0.1, -0.1, -0.02], "bbox_max_m": [0.1, 0.1, 0.1]})
    return {r["asset_id"]: r for r in (table, cabinet, plate, robot)}


SPEC = {
    "schema": layout.LAYOUT_SCHEMA,
    "scene_id": "scene.toy",
    "nodes": [
        {"node_id": "object.table", "asset_id": "object.table_a", "semantic_tag": "table", "xy_m": [0.0, 0.0],
         "on": "floor"},
        {"node_id": "object.plate", "asset_id": "object.plate_a", "semantic_tag": "plate", "xy_m": [0.3, 0.1],
         "on": "object.table"},
        {"node_id": "object.cabinet", "asset_id": "object.cabinet_a", "semantic_tag": "drawer", "xy_m": [0.8, 0.0],
         "yaw_deg": 90, "on": "floor", "joint_state": {"drawer_joint": 0.2}},
    ],
    "robot": {"node_id": "robot.arm", "asset_id": "robot.toy", "on": "object.table", "xy_m": [0.0, 0.0],
              "arms": ["left"], "gripper": "g2", "tcp_link": "end_link", "tcp_frame_id": "tcp.left",
              "tool_approach_axis": "x", "limit_margin_rad": 0.05},
    "reach_targets": [
        {"name": "plate_above", "node": "object.plate", "local_m": [0.0, 0.0, 0.08], "yaw_samples": 4},
    ],
    "collision_groups": {
        "task": {"entities": ["object.plate"], "contact_with": ["object.table", "robot.arm"], "forbidden_with": []},
    },
}


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.records = records()
        self.descriptor = discover_urdf(URDF)

    def nodes(self, spec=SPEC):
        return {n["node_id"]: n for n in layout.resolve_nodes(spec, self.records)}

    def test_nodes_rest_on_their_support_top(self):
        nodes = self.nodes()
        table_z = nodes["object.table"]["pose"]["position_m"][2]
        self.assertAlmostEqual(table_z - 0.7, layout.SUPPORT_CLEARANCE_M)  # bbox bottom just above the floor
        plate_bottom = nodes["object.plate"]["pose"]["position_m"][2]
        self.assertAlmostEqual(plate_bottom, table_z + 0.05 + layout.SUPPORT_CLEARANCE_M)
        base = layout.robot_base_pose(SPEC, list(nodes.values()), self.records)
        self.assertAlmostEqual(base["position_m"][2], table_z + 0.05 + layout.SUPPORT_CLEARANCE_M + 0.02)

    def test_robot_on_support_without_base_collision_is_an_error(self):
        self.records["robot.toy"]["collision"] = {"kind": "mesh"}
        with self.assertRaisesRegex(IRError, "base_bbox_min_m"):
            layout.robot_base_pose(SPEC, list(self.nodes().values()), self.records)

    def test_footprint_rejects_overhang_and_overlap_using_rotated_rectangles(self):
        self.assertTrue(layout.footprint_check(SPEC, layout.resolve_nodes(SPEC, self.records), self.records)["ok"])
        edge = deepcopy(SPEC)  # 0.2 m square base at x=-0.39: axis-aligned fits the 0.5 m half-width, 45° does not
        edge["robot"]["xy_m"] = [-0.39, 0.0]
        self.assertTrue(layout.footprint_check(edge, layout.resolve_nodes(edge, self.records), self.records)["ok"])
        edge["robot"]["yaw_deg"] = 45
        result = layout.footprint_check(edge, layout.resolve_nodes(edge, self.records), self.records)
        self.assertEqual(result["problems"], ["robot.arm overhangs object.table"])
        crowded = deepcopy(SPEC)
        crowded["robot"]["xy_m"] = [0.15, 0.1]
        result = layout.footprint_check(crowded, layout.resolve_nodes(crowded, self.records), self.records)
        self.assertEqual(result["problems"], ["object.plate overlaps robot.arm on object.table"])

    def test_drawer_target_moves_with_joint_state_in_world_frame(self):
        nodes = self.nodes()
        target = {"name": "in_drawer", "node": "object.cabinet", "joint": "drawer_joint", "local_m": [0.0, 0.0, 0.5]}
        world = layout.target_world(target, nodes, self.records)
        # cabinet yawed 90 deg: the drawer's +x axis points along world +y
        np.testing.assert_allclose(world[:2], [0.8, 0.2], atol=1e-9)
        closed = deepcopy(SPEC)
        closed["nodes"][2]["joint_state"] = {"drawer_joint": 0.0}
        world_closed = layout.target_world(target, self.nodes(closed), self.records)
        np.testing.assert_allclose(world - world_closed, [0.0, 0.2, 0.0], atol=1e-9)

    def test_joint_state_outside_asset_limits_is_rejected(self):
        spec = deepcopy(SPEC)
        spec["nodes"][2]["joint_state"] = {"drawer_joint": 0.5}
        with self.assertRaisesRegex(IRError, "outside"):
            layout.resolve_nodes(spec, self.records)

    def test_support_cycle_is_rejected(self):
        spec = deepcopy(SPEC)
        spec["nodes"][0]["on"] = "object.plate"
        with self.assertRaisesRegex(IRError, "cycle"):
            layout.resolve_nodes(spec, self.records)

    def test_joint_sweep_rejects_displacement_the_root_box_cannot_see(self):
        # Real failure (verification/scene-author-sandbox-20261117): a 0.35 m drawer on a yaw-90 cabinet
        # cleared the table by 146 mm at the root box but measured 61.9 mm GPU penetration.
        spec = deepcopy(SPEC)
        cabinet = spec["nodes"][2]
        cabinet["xy_m"] = [0.0, -0.75]
        cabinet["yaw_deg"] = 90
        cabinet["joint_state"] = {"drawer_joint": 0.2}
        plan = planning.run_plan(spec, self.records, self.descriptor, spec_sha256="d" * 64)
        footprint = plan["footprint"]
        expected = "object.cabinet drawer_joint at 0.2 m moves into object.table"
        self.assertFalse(plan["ok"])
        self.assertIn(expected, footprint["problems"])
        self.assertIn(expected, planning.plan_diagnostics(plan))
        self.assertFalse(footprint["joint_sweep"]["ok"])
        self.assertEqual(footprint["joint_sweep"]["checked"],
                         [{"node": "object.cabinet", "joint": "drawer_joint", "travel_m": 0.2}])
        corners = {k: np.asarray(v) for k, v in footprint["xy_corners_m"].items()}
        self.assertFalse(layout._overlap(corners["object.cabinet"], corners["object.table"]))
        # root box edge sits 50 mm clear of the table; the displaced box crosses it (old check passed here)
        self.assertAlmostEqual(corners["object.cabinet"][:, 1].max(), -0.45, places=4)
        self.assertAlmostEqual(corners["object.table"][:, 1].min(), -0.4, places=4)

    def test_joint_sweep_passes_clear_travel_and_drawer_opening_away(self):
        accepted = deepcopy(SPEC)  # the accepted 1b geometry: opening 0.4 m still clears the table (46 mm there)
        accepted["nodes"][2]["xy_m"] = [0.0, -1.15]
        accepted["nodes"][2]["joint_state"] = {"drawer_joint": 0.4}
        footprint = layout.footprint_check(accepted, layout.resolve_nodes(accepted, self.records), self.records)
        self.assertTrue(footprint["ok"], footprint["problems"])
        self.assertEqual(footprint["problems"], [])
        self.assertTrue(footprint["joint_sweep"]["checked"])

        away = deepcopy(SPEC)  # yaw 270 sends +x along world -y: the box moves off the table
        away["nodes"][2]["xy_m"] = [0.0, -0.75]
        away["nodes"][2]["yaw_deg"] = 270
        away["nodes"][2]["joint_state"] = {"drawer_joint": 0.2}
        result = layout.footprint_check(away, layout.resolve_nodes(away, self.records), self.records)
        self.assertTrue(result["ok"], result["problems"])  # symmetric inflation would fail this
        self.assertEqual(result["problems"], [])

        closed = deepcopy(SPEC)  # existing SPEC case: drawer at 0.2 m, edge-touching the table at x=0.5
        self.assertTrue(layout.footprint_check(closed, layout.resolve_nodes(closed, self.records),
                                               self.records)["ok"])

    def test_joint_sweep_is_a_conservative_bbox_gate(self):
        # Accepted cost of the fallback variant: the whole root box is translated, z-blind, so a floor-level
        # plate that the drawer passes over in 3D is flagged. Records with `parts[]` take the per-part path
        # (see the `joint_sweep` parts tests below); these records have no parts, so the output stays the old one.
        spec = deepcopy(SPEC)
        spec["nodes"].append({"node_id": "object.plate_floor", "asset_id": "object.plate_a",
                              "semantic_tag": "plate", "xy_m": [0.0, -0.55], "on": "floor"})
        spec["nodes"][2]["xy_m"] = [0.0, -1.05]
        spec["nodes"][2]["yaw_deg"] = 90
        spec["nodes"][2]["joint_state"] = {"drawer_joint": 0.4}
        footprint = layout.footprint_check(spec, layout.resolve_nodes(spec, self.records), self.records)
        self.assertFalse(footprint["ok"])
        self.assertIn("object.cabinet drawer_joint at 0.4 m moves into object.plate_floor", footprint["problems"])
        corners = {k: np.asarray(v) for k, v in footprint["xy_corners_m"].items()}
        self.assertFalse(layout._overlap(corners["object.cabinet"], corners["object.plate_floor"]))

    def test_joint_sweep_reports_revolute_joints_as_skipped(self):
        records = deepcopy(self.records)  # AssetRecord has no pivot for revolute joints, so no bounding check
        records["object.cabinet_a"]["joints"].append(
            {"name": "lid_joint", "type": "revolute", "axis": [0.0, 1.0, 0.0],
             "limits": {"lower": 0.0, "upper": 1.5}, "parent": "body", "child": "lid"})
        spec = deepcopy(SPEC)
        spec["nodes"][2]["joint_state"] = {"lid_joint": 0.7}
        footprint = layout.footprint_check(spec, layout.resolve_nodes(spec, records), records)
        self.assertTrue(footprint["ok"])
        self.assertTrue(footprint["joint_sweep"]["ok"])
        self.assertEqual(footprint["joint_sweep"]["checked"], [])
        self.assertEqual(footprint["problems"], [])
        self.assertEqual(len(footprint["joint_sweep"]["skipped"]), 1)
        self.assertIn("object.cabinet lid_joint", footprint["joint_sweep"]["skipped"][0])
        self.assertIn("revolute", footprint["joint_sweep"]["skipped"][0])

    def test_joint_sweep_parts_prismatic_sweeps_only_the_moving_part(self):
        # Per-part boxes: the drawer body sits 0.45 m inside the root box, so the conservative whole-root-box
        # translation flags the table while the real moving part clears it. This case fails on the old code.
        # Names mirror a real SimReady cabinet: the part is the rigid body `part_01_obj_00`, while the joint
        # child prim is the mesh under it (`part_01_obj_00/part_01_mesh_00`), so only `child_body` links them.
        spec = deepcopy(SPEC)
        spec["nodes"][2]["xy_m"] = [0.0, -0.75]
        spec["nodes"][2]["yaw_deg"] = 90
        spec["nodes"][2]["joint_state"] = {"drawer_joint": 0.2}
        fallback = layout.joint_sweep_check(spec, layout.resolve_nodes(spec, self.records), self.records)
        self.assertFalse(fallback["ok"])  # the root box crosses the table edge at y=-0.4

        records = deepcopy(self.records)
        drawer_joint = records["object.cabinet_a"]["joints"][0]
        drawer_joint["child"] = "part_01_mesh_00"
        drawer_joint["child_body"] = DRAWER_BODY
        records["object.cabinet_a"]["parts"] = [
            part("body_01_obj_00", CABINET_BODY, [-0.3, -0.3, 0.0], [0.3, 0.3, 0.6]),
            part("part_01_obj_00", DRAWER_BODY, [-0.28, -0.28, 0.10], [-0.10, 0.28, 0.30]),
        ]
        sweep = layout.joint_sweep_check(spec, layout.resolve_nodes(spec, records), records)
        self.assertTrue(sweep["ok"], sweep["problems"])
        self.assertEqual(sweep["checked"],
                         [{"node": "object.cabinet", "joint": "drawer_joint", "method": "parts", "travel_m": 0.2}])
        self.assertEqual(sweep["skipped"], [])

        # A drawer front that really leaves the cabinet still hits: the parts path is not a blanket pass.
        records["object.cabinet_a"]["parts"][1] = part("part_01_obj_00", DRAWER_BODY,
                                                       [0.12, -0.28, 0.10], [0.28, 0.28, 0.30])
        spec["nodes"][2]["joint_state"] = {"drawer_joint": 0.4}
        sweep = layout.joint_sweep_check(spec, layout.resolve_nodes(spec, records), records)
        self.assertFalse(sweep["ok"])
        self.assertEqual(sweep["problems"], ["object.cabinet drawer_joint at 0.4 m moves into object.table"])
        self.assertEqual(sweep["checked"][0]["method"], "parts")

        # The link is `child_body`, not the leaf name of the joint child prim: without it the joint falls back.
        records["object.cabinet_a"]["joints"][0].pop("child_body")
        sweep = layout.joint_sweep_check(spec, layout.resolve_nodes(spec, records), records)
        self.assertNotIn("method", sweep["checked"][0])
        self.assertFalse(sweep["ok"])
        # ... and a `child_body` that names no part also falls back instead of guessing.
        records["object.cabinet_a"]["joints"][0]["child_body"] = "RootNode/Geometry/missing_obj_00"
        sweep = layout.joint_sweep_check(spec, layout.resolve_nodes(spec, records), records)
        self.assertNotIn("method", sweep["checked"][0])

    def test_joint_sweep_without_parts_keeps_the_conservative_output(self):
        # Captured from the pre-`parts[]` implementation: fallback results must stay byte-identical.
        spec = deepcopy(SPEC)
        spec["nodes"][2]["xy_m"] = [0.0, -0.75]
        spec["nodes"][2]["yaw_deg"] = 90
        spec["nodes"][2]["joint_state"] = {"drawer_joint": 0.2}
        self.assertEqual(layout.joint_sweep_check(spec, layout.resolve_nodes(spec, self.records), self.records), {
            "ok": False,
            "problems": ["object.cabinet drawer_joint at 0.2 m moves into object.table"],
            "checked": [{"node": "object.cabinet", "joint": "drawer_joint", "travel_m": 0.2}],
            "skipped": [],
        })

        records = deepcopy(self.records)
        records["object.cabinet_a"]["joints"].append(
            {"name": "lid_joint", "type": "revolute", "axis": [0.0, 1.0, 0.0],
             "limits": {"lower": 0.0, "upper": 1.5}, "parent": "body", "child": "lid"})
        revolute = deepcopy(SPEC)
        revolute["nodes"][2]["joint_state"] = {"lid_joint": 0.7, "drawer_joint": 0.2}
        self.assertEqual(layout.joint_sweep_check(revolute, layout.resolve_nodes(revolute, records), records), {
            "ok": True,
            "problems": [],
            "checked": [{"node": "object.cabinet", "joint": "drawer_joint", "travel_m": 0.2}],
            "skipped": ["object.cabinet lid_joint: revolute joint has no pivot in the AssetRecord, "
                        "an AABB translation cannot bound a rotation"],
        })

    def test_joint_sweep_parts_revolute_uses_the_anchor_and_finds_a_collision(self):
        def cabinet_with_revolute(parts: list[dict], joint: dict) -> dict:
            records = deepcopy(self.records)
            records["object.cabinet_a"]["joints"].append(joint)
            records["object.cabinet_a"]["parts"] = parts
            return records

        body = part("body_01_obj_00", CABINET_BODY, [-0.3, -0.3, 0.0], [0.3, 0.3, 0.6])
        lid_joint = {"name": "lid_joint", "type": "revolute", "axis": [0.0, 1.0, 0.0],
                     "limits": {"lower": 0.0, "upper": 1.5}, "parent": "body_01_mesh_00",
                     "child": "part_02_mesh_00", "child_body": "RootNode/Geometry/part_02_obj_00",
                     "anchor_m": [0.3, 0.0, 0.6]}
        lid = part("part_02_obj_00", "RootNode/Geometry/part_02_obj_00", [-0.3, -0.3, 0.6], [0.3, 0.3, 0.65])
        spec = deepcopy(SPEC)
        spec["nodes"][2]["yaw_deg"] = 0
        spec["nodes"][2]["joint_state"] = {"lid_joint": 1.2}
        records = cabinet_with_revolute([body, lid], lid_joint)
        sweep = layout.joint_sweep_check(spec, layout.resolve_nodes(spec, records), records)
        self.assertTrue(sweep["ok"], sweep["problems"])  # lifts up and inward, away from the table
        self.assertEqual(sweep["checked"],
                         [{"node": "object.cabinet", "joint": "lid_joint", "method": "parts", "travel_rad": 1.2}])
        self.assertEqual(sweep["skipped"], [])  # the old code only reported this joint as skipped

        door_joint = {"name": "door_joint", "type": "revolute", "axis": [0.0, 0.0, 1.0],
                      "limits": {"lower": 0.0, "upper": 1.5}, "parent": "body_01_mesh_00",
                      "child": "part_03_mesh_00", "child_body": "RootNode/Geometry/part_03_obj_00",
                      "anchor_m": [0.27, -0.3, 0.0]}
        door = part("part_03_obj_00", "RootNode/Geometry/part_03_obj_00", [0.24, -0.30, 0.0], [0.30, 0.30, 0.6])
        spec["nodes"][2]["joint_state"] = {"door_joint": 1.5}
        records = cabinet_with_revolute([body, door], door_joint)
        sweep = layout.joint_sweep_check(spec, layout.resolve_nodes(spec, records), records)
        self.assertFalse(sweep["ok"])
        self.assertEqual(sweep["problems"], ["object.cabinet door_joint at 1.5 rad moves into object.table"])
        self.assertEqual(sweep["checked"][0]["method"], "parts")

        # No anchor means no pivot: the revolute joint keeps the old skipped entry, parts or not.
        records["object.cabinet_a"]["joints"][-1].pop("anchor_m")
        sweep = layout.joint_sweep_check(spec, layout.resolve_nodes(spec, records), records)
        self.assertTrue(sweep["ok"])
        self.assertEqual(sweep["checked"], [])
        self.assertEqual(sweep["skipped"], ["object.cabinet door_joint: revolute joint has no pivot in the "
                                            "AssetRecord, an AABB translation cannot bound a rotation"])

    def test_reachability_uses_model_tcp_and_reports_unreachable_targets(self):
        nodes = layout.resolve_nodes(SPEC, self.records)
        reach = layout.reachability(SPEC, nodes, self.records, self.descriptor)
        self.assertTrue(reach["ok"], reach)
        row = reach["targets"][0]
        arm = kinematics.chain(self.descriptor, "end_link")
        base = kinematics.pose_matrix(layout.robot_base_pose(SPEC, nodes, self.records))
        np.testing.assert_allclose(arm.fk(row["q"], base)[:3, 3], row["world_m"], atol=2e-3)
        far = deepcopy(SPEC)
        far["reach_targets"].append({"name": "beyond", "node": "object.table", "local_m": [3.0, 0.0, 0.1],
                                     "yaw_samples": 2})
        reach = layout.reachability(far, nodes, self.records, self.descriptor)
        self.assertFalse(reach["ok"])
        self.assertIn("unreachable: beyond", reach["notes"])

    def test_plan_ok_and_diagnostics_report_footprint_overlap(self):
        plan = planning.run_plan(SPEC, self.records, self.descriptor, spec_sha256="a" * 64)
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["spec_sha256"], "a" * 64)
        self.assertEqual(plan["nodes"], layout.resolve_nodes(SPEC, self.records))
        self.assertEqual(planning.plan_diagnostics(plan), [])

        compact = deepcopy(SPEC)
        compact["robot"]["xy_m"] = [0.15, 0.1]
        plan = planning.run_plan(compact, self.records, self.descriptor, spec_sha256="b" * 64)
        self.assertFalse(plan["ok"])
        self.assertIn("object.plate overlaps robot.arm on object.table", planning.plan_diagnostics(plan))

    def test_diagnostics_name_unreachable_targets_with_position_error(self):
        far = deepcopy(SPEC)
        far["reach_targets"].append({"name": "beyond", "node": "object.table", "local_m": [3.0, 0.0, 0.1],
                                     "yaw_samples": 2})
        plan = planning.run_plan(far, self.records, self.descriptor, spec_sha256="c" * 64)
        self.assertFalse(plan["ok"])
        lines = planning.plan_diagnostics(plan)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("reach target beyond unreachable: position error ", lines[0])
        self.assertIn(" m", lines[0])

    def test_missing_tcp_candidate_is_an_error_not_a_link_origin(self):
        with self.assertRaisesRegex(IRError, "no TCP candidate"):
            layout.model_tcp(self.descriptor, "l3")

    def test_scene_graph_validates_and_rejects_inconsistent_checks(self):
        nodes = layout.resolve_nodes(SPEC, self.records)
        reach = layout.reachability(SPEC, nodes, self.records, self.descriptor)
        refs = {k: ref("ir.asset-record", c).to_dict() for k, c in zip(self.records, "abc")}
        refs["robot.toy"] = ref("ir.asset-record", "d").to_dict()
        kwargs = dict(spec=SPEC, nodes=nodes, records=self.records, record_refs=refs, descriptor=self.descriptor,
                      asset_bundle=ref("scene.asset-bundle", "e").to_dict(),
                      ledger=ref("ir.physics-ledger", "f").to_dict(),
                      runtime_profile=ref("sim.runtime-profile", "0").to_dict(), reach=reach,
                      settle={"ran": True, "steps": 240, "max_drift_m": 0.0004, "stable": True},
                      penetration={"ran": True, "max_depth_m": 0.0, "ok": True})
        graph = layout.build_scene_graph(**kwargs)
        self.assertEqual(graph["robot"]["controlled_joints"], ["j1", "j2", "j3", "j4", "j5", "j6"])
        self.assertEqual(graph["robot"]["tcp"]["T_link_tcp"]["frame_id"], "end_link")
        self.assertEqual(graph["nodes"][0]["pose"], graph["robot"]["base_pose"])
        broken = deepcopy(graph)
        broken["checks"]["settle"] = {"ran": False, "steps": 0, "max_drift_m": 0.0, "stable": True}
        with self.assertRaisesRegex(IRError, "stable=true requires ran=true"):
            scene_graph.validate(broken)
        broken = deepcopy(graph)
        broken["checks"]["reachability"] = {**broken["checks"]["reachability"], "ok": not reach["ok"]}
        with self.assertRaisesRegex(IRError, "must equal robot.reachability"):
            scene_graph.validate(broken)

    def test_physical_quantities_are_assumed_with_ranges_and_evidence(self):
        table = self.records["object.table_a"]
        by_name = {q["name"]: q for q in table["physical_quantities"]}
        self.assertEqual(by_name["mass"]["label"], "assumed")
        self.assertEqual(by_name["mass"]["randomization"], {"low": 28.0, "high": 52.0})
        self.assertTrue(by_name["friction_static"]["evidence"])
        drawer = {q["name"]: q for q in self.records["object.cabinet_a"]["physical_quantities"]}
        self.assertEqual(drawer["drawer_joint.damping"]["value"], asset_records.PRISMATIC_DAMPING)

    def test_host_paths_cannot_enter_an_asset_record(self):
        leaky = dict(fetch("table_a"), source_uri="/opt/data/private/simready-cache/table_a")
        with self.assertRaisesRegex(IRError, "absolute filesystem path"):
            asset_records.build_record(
                asset_id="object.table_a", role="background", fetch=leaky,
                inspection=inspection([-0.5, -0.4, -0.7], [0.5, 0.4, 0.05]), usd_ref=ref("asset.usd", "1"),
                inspection_ref=ref("asset.usd-inspection", "2"), collision_ref=None)

    def test_jointless_receptacle_is_rejected(self):
        with self.assertRaisesRegex(IRError, "receptacle needs"):
            asset_records.build_record(
                asset_id="object.cabinet_b", role="receptacle", fetch=fetch("cabinet_b"),
                inspection=inspection([0, 0, 0], [1, 1, 1]), usd_ref=ref("asset.usd", "1"),
                inspection_ref=ref("asset.usd-inspection", "2"), collision_ref=None)


class AnchorBodyTests(unittest.TestCase):
    """`assets[].anchor_body` must be `prim_ref` form; rejected at `plan` start, not deep inside `prepare`."""

    VERIFICATION = Path(__file__).resolve().parents[1] / "verification/request-authoring-1b-20261117"

    def setUp(self):
        self.records = records()

    def spec(self, *assets):
        spec = deepcopy(SPEC)
        spec["assets"] = list(assets)
        return spec

    def asset(self, anchor_body=None, *, body="articulation", asset_id="object.cabinet_a"):
        entry = {"asset_id": asset_id, "role": "receptacle", "simready": "cabinet_a", "body": body}
        if anchor_body is not None:
            entry["anchor_body"] = anchor_body
        return entry

    def test_accepted_prim_ref_forms_resolve(self):
        for value in ("body", "Root/body", "RootNode/Geometry/body_01_obj_00", "_Root/a1", "a/b/c"):
            with self.subTest(value=value):
                layout.resolve_nodes(self.spec(self.asset(value)), self.records)

    def test_rejected_forms_raise_one_clear_error(self):
        rejected = {
            "": "must be a prim path relative to the asset root",
            "/RootNode/Geometry/body_01_obj_00": "must be a prim path relative to the asset root",
            "a//b": "no leading '/', no '.', '..' or empty segments",
            "a/b/": "no leading '/', no '.', '..' or empty segments",
            " a/b": "must be a prim path relative to the asset root",
            "a/b ": "must be a prim path relative to the asset root",
            "a/ b": "must be a prim path relative to the asset root",
            "a/./b": "no leading '/', no '.', '..' or empty segments",
            "a/../b": "no leading '/', no '.', '..' or empty segments",
            ".": "no leading '/', no '.', '..' or empty segments",
            "..": "no leading '/', no '.', '..' or empty segments",
            "a/b-c": "must be a prim path relative to the asset root",
            "a/b:c": "must be a prim path relative to the asset root",
            "a/b/c d": "must be a prim path relative to the asset root",
        }
        for value, pattern in rejected.items():
            with self.subTest(value=value), self.assertRaisesRegex(IRError, pattern) as raised:
                layout.resolve_nodes(self.spec(self.asset(value)), self.records)
            self.assertIn("assets[0] (object.cabinet_a).anchor_body", str(raised.exception))

    def test_articulation_needs_anchor_body_and_others_may_omit_it(self):
        with self.assertRaisesRegex(IRError, "assets\\[0\\] \\(object.cabinet_a\\)\\.anchor_body is required "
                                             "when body='articulation'"):
            layout.resolve_nodes(self.spec(self.asset(body="articulation")), self.records)
        layout.resolve_nodes(self.spec(self.asset(body="rigid", asset_id="object.plate_a")), self.records)

    def test_anchor_body_is_validated_even_when_the_body_would_ignore_it(self):
        spec = self.spec(self.asset("/Root/body", body="rigid", asset_id="object.plate_a"))
        with self.assertRaisesRegex(IRError, "assets\\[0\\] \\(object.plate_a\\)\\.anchor_body"):
            layout.resolve_nodes(spec, self.records)

    def test_real_round_one_layout_is_rejected_before_prepare(self):
        path = self.VERIFICATION / "attempts/001/scene-layout.json"
        bad = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(bad["assets"][1]["anchor_body"], "/RootNode/Geometry/body_01_obj_00")
        with self.assertRaisesRegex(
                IRError,
                r"assets\[1\] \(object\.drawer_cabinet\)\.anchor_body '/RootNode/Geometry/body_01_obj_00' "
                r"must be a prim path relative to the asset root, like 'RootNode/Geometry/body_01_obj_00': "
                r"no leading '/', no '.', '\.\.' or empty segments"):
            layout.resolve_nodes(bad, {})

    def test_real_round_two_layout_still_resolves(self):
        path = self.VERIFICATION / "attempts/002/scene-layout.json"
        good = json.loads(path.read_text(encoding="utf-8"))
        joints = [{"name": name, "type": "prismatic", "limits": {"lower": 0.0, "upper": 0.5}}
                  for name in ("joint_part_01_joint_00", "joint_part_02_joint_00")]

        def stub(asset_id, bbox, asset_joints=()):
            return {"asset_id": asset_id, "bbox_m": bbox, "collision": {}, "frames": {}, "joints": list(asset_joints)}

        stubs = {
            "object.table_stainless": stub("object.table_stainless", [1.2, 0.8, 0.05]),
            "object.drawer_cabinet": stub("object.drawer_cabinet", [0.8, 0.5, 0.9], joints),
            "object.plate": stub("object.plate", [0.2, 0.2, 0.02]),
        }
        nodes = layout.resolve_nodes(good, stubs)
        self.assertEqual([node["node_id"] for node in nodes], ["object.table", "object.cabinet", "object.plate"])
        # plate bottom sits on the table top (bbox z 0.05) plus one clearance for each of the two layers.
        self.assertAlmostEqual(nodes[-1]["pose"]["position_m"][2], 0.05 + 2 * layout.SUPPORT_CLEARANCE_M, places=6)


if __name__ == "__main__":
    unittest.main()

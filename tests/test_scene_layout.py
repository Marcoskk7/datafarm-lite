"""A-line scene assembly without a simulator: records, layout heights, drawer-shifted reach and SceneGraph IR."""
from copy import deepcopy
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


if __name__ == "__main__":
    unittest.main()

"""Request -> scene-layout conformance gate: example pair passes; each mismatch is reported by name."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from datafarm.request.schema import load_request
from datafarm.scene.conformance import check_conformance, load_role_rules

ROOT = Path(__file__).resolve().parents[1]
REQUEST = load_request(ROOT / "examples" / "request" / "plate-drawer-airbot.yaml")
LAYOUT = json.loads((ROOT / "examples" / "scene" / "plate-drawer-airbot.json").read_text(encoding="utf-8"))
RULES = load_role_rules(ROOT / "configs" / "scene-roles.json")

CABINET_ASSET = "object.cabinet_drawer"
CABINET_JOINTS = (
    {"name": "joint_part_01_joint_00", "type": "prismatic",
     "limits": {"lower": 0.0, "upper": 0.4}},
    {"name": "joint_part_02_joint_00", "type": "prismatic",
     "limits": {"lower": 0.0, "upper": 0.4}},
)
RECORDS = {CABINET_ASSET: {"joints": list(CABINET_JOINTS)}}


def node(layout, node_id):
    return next(entry for entry in layout["nodes"] if entry["node_id"] == node_id)


def asset(layout, asset_id):
    return next(entry for entry in layout["assets"] if entry["asset_id"] == asset_id)


class ConformanceTests(unittest.TestCase):
    def test_example_request_and_layout_match_with_joint_checks_skipped(self):
        report = check_conformance(REQUEST, LAYOUT, None, RULES)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(report.problems, [])
        self.assertTrue(report.skipped)
        self.assertFalse(report.complete)
        self.assertTrue(any("joint type" in line for line in report.skipped))
        self.assertEqual({n["node_id"] for n in LAYOUT["nodes"]},
                         {obj.object_id for obj in REQUEST.scene.objects})

    def test_example_passes_completely_with_asset_records(self):
        report = check_conformance(REQUEST, LAYOUT, RECORDS, RULES)
        self.assertTrue(report.ok, report.problems)
        self.assertTrue(report.complete)
        self.assertEqual(report.skipped, [])

    def test_missing_receptacle_node_is_reported(self):
        layout = deepcopy(LAYOUT)
        layout["nodes"] = [n for n in layout["nodes"] if n["node_id"] != "object.cabinet"]
        report = check_conformance(REQUEST, layout, None, RULES)
        self.assertFalse(report.ok)
        self.assertTrue(any("missing layout node for request object 'object.cabinet'" in line
                            for line in report.problems), report.problems)

    def test_receptacle_without_probe_is_reported(self):
        layout = deepcopy(LAYOUT)
        layout["probes"] = []
        report = check_conformance(REQUEST, layout, None, RULES)
        self.assertFalse(report.ok)
        self.assertTrue(any("requires at least one probe" in line for line in report.problems), report.problems)

    def test_receptacle_without_reach_targets_is_reported(self):
        layout = deepcopy(LAYOUT)
        layout["reach_targets"] = [t for t in layout["reach_targets"] if t["node"] != "object.cabinet"]
        report = check_conformance(REQUEST, layout, None, RULES)
        self.assertFalse(report.ok)
        self.assertTrue(any("needs at least 1 reach targets on this node, found 0" in line
                            for line in report.problems), report.problems)

    def test_task_object_without_randomization_is_reported(self):
        layout = deepcopy(LAYOUT)
        del node(layout, "object.plate")["randomization"]
        report = check_conformance(REQUEST, layout, None, RULES)
        self.assertFalse(report.ok)
        self.assertTrue(any("missing randomization" in line for line in report.problems), report.problems)

    def test_support_mismatch_is_reported(self):
        layout = deepcopy(LAYOUT)
        node(layout, "object.plate")["on"] = "floor"
        report = check_conformance(REQUEST, layout, None, RULES)
        self.assertFalse(report.ok)
        self.assertTrue(any("support 'floor' does not match request support 'object.table'" in line
                            for line in report.problems), report.problems)

    def test_wrong_asset_role_is_reported(self):
        layout = deepcopy(LAYOUT)
        asset(layout, "object.plate")["role"] = "background"
        report = check_conformance(REQUEST, layout, None, RULES)
        self.assertFalse(report.ok)
        self.assertTrue(any("has role 'background', request says 'task_object'" in line
                            for line in report.problems), report.problems)

    def test_open_drawer_that_layout_leaves_closed_is_reported_with_records(self):
        layout = deepcopy(LAYOUT)
        node(layout, "object.cabinet")["joint_state"] = {
            "joint_part_01_joint_00": 0.0, "joint_part_02_joint_00": 0.0}
        report = check_conformance(REQUEST, layout, RECORDS, RULES)
        self.assertFalse(report.ok)
        self.assertTrue(any("joint_state 'open' but no prismatic joint reaches 50% of its range" in line
                            for line in report.problems), report.problems)

    def test_disallowed_simready_source_is_reported(self):
        scene = REQUEST.scene.model_copy(update={"asset_sources": ["partnet_mobility"]})
        request = REQUEST.model_copy(update={"scene": scene})
        report = check_conformance(request, LAYOUT, None, RULES)
        self.assertFalse(report.ok)
        self.assertTrue(any("simready is not allowed by request asset_sources ['partnet_mobility']" in line
                            for line in report.problems), report.problems)


if __name__ == "__main__":
    unittest.main()


class CameraConformanceTests(unittest.TestCase):
    """请求要记录的物理相机必须按机器人标定事实进入布局（lab30：agent 布局 cameras=[]，渲染才失败）。"""

    def test_missing_request_camera_is_reported_with_the_facts_to_declare(self):
        layout = deepcopy(LAYOUT)
        layout["cameras"] = []
        problems = check_conformance(REQUEST, layout, None, RULES).problems
        self.assertTrue(any(p.startswith("request camera 'head' is not declared") and '"fx":260.77164' in p
                            for p in problems), problems)
        self.assertTrue(any(p.startswith("request camera 'left_wrist' is not declared") for p in problems))

    def test_camera_that_differs_from_the_calibration_is_rejected(self):
        layout = deepcopy(LAYOUT)
        layout["cameras"][0]["intrinsics"]["fx"] = 300.0
        problems = check_conformance(REQUEST, layout, None, RULES).problems
        self.assertTrue(any(p.startswith("layout camera 'camera.head' differs") for p in problems), problems)

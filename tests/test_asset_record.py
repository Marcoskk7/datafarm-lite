"""`datafarm.asset-record/1` 的可选分件字段：`parts[]`、`joints[].child_body` 与 `joints[].anchor_m`
（跨线契约，B 线确认前不改版本）。"""
from __future__ import annotations

from copy import deepcopy
import unittest

from datafarm.ir import asset_record
from datafarm.ir.common import IRError
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.scene import asset_records, layout

from tests.test_scene_layout import fetch, inspection, records


def ref(kind: str, char: str) -> ArtifactRef:
    return ArtifactRef(kind=kind, sha256=char * 64, media_type="application/json")


# SimReady 命名：分件是刚体 prim（`*_obj_00`），关节 parent/child 是刚体下面的网格 prim（`*_mesh_00`）。
BODY = "RootNode/Geometry/body_01_obj_00"
DRAWER = "RootNode/Geometry/part_01_obj_00"


def simready_inspection() -> dict:
    """`usd_assets.inspect_asset` 的字段形状：刚体 prim 路径 + 每个碰撞体的包围盒 + 关节枢轴。"""
    return {
        "bbox_min_m": [-0.3, -0.3, 0.0], "bbox_max_m": [0.3, 0.3, 0.6], "up_axis": "Z",
        "joints": [{"name": "joint_part_01_joint_00", "type": "prismatic", "axis": [1.0, 0.0, 0.0],
                    "limits": {"lower": 0.0, "upper": 0.4}, "parent": f"{BODY}/body_01_mesh_00",
                    "child": f"{DRAWER}/part_01_mesh_00", "anchor_m": [0.0, -0.05, 0.2]}],
        "bodies": {BODY: {"mass_kg": 30.0}},
        "colliders": [
            {"body": BODY, "bbox_min_m": [-0.3, -0.3, 0.0], "bbox_max_m": [0.3, 0.3, 0.6],
             "physics_material": None},
            {"body": DRAWER, "bbox_min_m": [-0.28, -0.28, 0.1], "bbox_max_m": [-0.1, 0.28, 0.3],
             "physics_material": None},
            {"body": DRAWER, "bbox_min_m": [-0.1, -0.28, 0.1], "bbox_max_m": [0.28, 0.28, 0.3],
             "physics_material": None},
        ],
    }


# 实测 `fixture_cabinet_b01`（lab30, 2026-11-18）：资产根系、米。
PART_BOX = {"bbox_min_m": [-0.30875, -0.381, 0.73427], "bbox_max_m": [0.33937, 0.381, 0.89007]}
BODY_BOX = {"bbox_min_m": [-0.33931, -0.381, 0.0007], "bbox_max_m": [0.30839, 0.381, 0.92916]}


def measured_cabinet_inspection() -> dict:
    return {
        "bbox_min_m": BODY_BOX["bbox_min_m"], "bbox_max_m": BODY_BOX["bbox_max_m"], "up_axis": "Z",
        "joints": [{"name": "joint_part_01_joint_00", "type": "prismatic", "axis": [1.0, 0.0, 0.0],
                    "limits": {"lower": 0.0, "upper": 0.46218},
                    "parent": f"{BODY}/body_01_mesh_00", "child": f"{DRAWER}/part_01_mesh_00",
                    "anchor_m": [0.33287, -0.00029, 0.80866]}],
        "bodies": {BODY: {"mass_kg": 30.0}, DRAWER: {"mass_kg": 2.0}},
        "colliders": [{"body": BODY, **BODY_BOX, "physics_material": None},
                      {"body": DRAWER, **PART_BOX, "physics_material": None}],
    }


def cabinet_record(asset_id: str = "object.cabinet_parts", source: dict | None = None) -> dict:
    return asset_records.build_record(
        asset_id=asset_id, role="receptacle", fetch=fetch(asset_id.split(".")[-1]),
        inspection=source if source is not None else simready_inspection(),
        usd_ref=ref("asset.usd", "1"), inspection_ref=ref("asset.usd-inspection", "2"), collision_ref=None)


class PartsFieldTests(unittest.TestCase):
    def test_build_record_links_parts_and_joints_by_body_prim(self) -> None:
        record = cabinet_record()
        # 同一刚体的多个碰撞体取包围盒并集；`name` 是叶名，`prim` 是刚体 prim 路径。
        self.assertEqual(record["parts"], [
            {"name": "body_01_obj_00", "prim": BODY,
             "bbox_min_m": [-0.3, -0.3, 0.0], "bbox_max_m": [0.3, 0.3, 0.6]},
            {"name": "part_01_obj_00", "prim": DRAWER,
             "bbox_min_m": [-0.28, -0.28, 0.1], "bbox_max_m": [0.28, 0.28, 0.3]},
        ])
        joint = record["joints"][0]
        self.assertEqual(joint["anchor_m"], [0.0, -0.05, 0.2])
        # 关节 `child` 是网格叶名，和分件叶名对不上；`child_body` 是它所属的刚体 prim 路径。
        self.assertEqual(joint["child"], "part_01_mesh_00")
        self.assertEqual(joint["child_body"], DRAWER)
        self.assertIn(joint["child_body"], [part["prim"] for part in record["parts"]])
        asset_record.validate(record)

    def test_build_record_without_collider_boxes_emits_no_parts(self) -> None:
        # 旧的 inspection 形状（只有 body 名、没有包围盒）保持老输出：不写 `parts`，不写 `anchor_m`/`child_body`。
        record = asset_records.build_record(
            asset_id="object.cabinet_old", role="receptacle", fetch=fetch("cabinet_old"),
            inspection=inspection([-0.3, -0.3, 0.0], [0.3, 0.3, 0.6], joints=[
                {"name": "drawer_joint", "type": "prismatic", "axis": [1.0, 0.0, 0.0],
                 "limits": {"lower": 0.0, "upper": 0.4}, "parent": "Root/body", "child": "Root/drawer"}]),
            usd_ref=ref("asset.usd", "1"), inspection_ref=ref("asset.usd-inspection", "2"), collision_ref=None)
        self.assertNotIn("parts", record)
        self.assertNotIn("anchor_m", record["joints"][0])
        self.assertNotIn("child_body", record["joints"][0])

    def test_validator_accepts_old_and_new_records(self) -> None:
        old = records()["object.cabinet_a"]
        self.assertNotIn("parts", old)
        asset_record.validate(old)

        new = cabinet_record()
        asset_record.validate(deepcopy(new))
        # 版本不变：可选字段可以缺省，旧记录照旧通过。
        stripped = deepcopy(new)
        stripped.pop("parts")
        stripped["joints"][0].pop("anchor_m")
        stripped["joints"][0].pop("child_body")
        asset_record.validate(stripped)
        # 分件可以只给叶名（手写记录），`prim` 不是必填。
        no_prim = deepcopy(new)
        for part in no_prim["parts"]:
            part.pop("prim")
        asset_record.validate(no_prim)

    def test_validator_rejects_malformed_parts_and_anchor(self) -> None:
        base = records()["object.cabinet_a"]
        bad_anchor = deepcopy(base)
        bad_anchor["joints"][0]["anchor_m"] = [0.0, 0.0]
        bad_prim = deepcopy(base)
        bad_prim["parts"] = [{"name": "body", "prim": "/RootNode/Geometry/body", "bbox_min_m": [0, 0, 0],
                              "bbox_max_m": [1, 1, 1]}]
        bad_child_body = deepcopy(base)
        bad_child_body["joints"][0]["child_body"] = "RootNode/./body"

        malformed = {
            "parts not a list": {"parts": {"name": "body"}},
            "parts empty": {"parts": []},
            "part not an object": {"parts": [["body"]]},
            "part missing bbox": {"parts": [{"name": "body", "bbox_min_m": [0, 0, 0]}]},
            "bbox too short": {"parts": [{"name": "body", "bbox_min_m": [0, 0], "bbox_max_m": [1, 1, 1]}]},
            "bbox min above max": {"parts": [{"name": "body", "bbox_min_m": [1, 0, 0], "bbox_max_m": [0, 1, 1]}]},
            "bbox not finite": {"parts": [{"name": "body", "bbox_min_m": [0, 0, 0],
                                           "bbox_max_m": [1, 1, float("nan")]}]},
            "part name empty": {"parts": [{"name": "", "bbox_min_m": [0, 0, 0], "bbox_max_m": [1, 1, 1]}]},
        }
        for label, patch in malformed.items():
            with self.subTest(label):
                document = deepcopy(base)
                document.update(patch)
                with self.assertRaises(IRError):
                    asset_record.validate(document)
        for label, document in {"anchor_m": bad_anchor, "parts[].prim": bad_prim,
                                "child_body": bad_child_body}.items():
            with self.subTest(label):
                with self.assertRaises(IRError):
                    asset_record.validate(document)


class MeasuredCabinetSweepTests(unittest.TestCase):
    """实测 `fixture_cabinet_b01` 走 build_record → `joint_sweep_check` 的分件路径（旧的叶名匹配会静默回退）。"""

    NODES = [{"node_id": "object.cabinet", "asset_id": "object.cabinet_b01", "semantic_tag": "drawer",
              "xy_m": [0.0, 0.0], "on": "floor",
              "joint_state": {"joint_part_01_joint_00": 0.35}}]

    def sweep(self, records: dict, nodes: list[dict] | None = None) -> dict:
        spec = {"schema": layout.LAYOUT_SCHEMA, "nodes": nodes or self.NODES}
        return layout.joint_sweep_check(spec, layout.resolve_nodes(spec, records), records)

    def test_measured_cabinet_takes_the_parts_path(self) -> None:
        records = {"object.cabinet_b01": cabinet_record("object.cabinet_b01", measured_cabinet_inspection())}
        record = records["object.cabinet_b01"]
        self.assertEqual(record["joints"][0]["child"], "part_01_mesh_00")
        self.assertEqual(record["joints"][0]["child_body"], DRAWER)
        self.assertEqual(record["parts"][1]["bbox_min_m"], PART_BOX["bbox_min_m"])

        sweep = self.sweep(records)
        self.assertTrue(sweep["ok"], sweep["problems"])
        self.assertEqual(sweep["checked"], [{"node": "object.cabinet", "joint": "joint_part_01_joint_00",
                                             "method": "parts", "travel_m": 0.35}])
        self.assertEqual(sweep["skipped"], [])

    def test_measured_cabinet_reaches_past_the_root_box(self) -> None:
        # 拉出 0.35 m 后分件前端在 x=0.33937+0.35=0.68937，比整根系包围盒前端 0.30839+0.35=0.65839 更远：
        # 障碍放在 [0.66, 0.68] 时保守的整盒平移漏检，分件路径能抓到。
        nodes = self.NODES + [{"node_id": "object.strip", "asset_id": "object.strip", "semantic_tag": "plate",
                               "xy_m": [0.0, 0.0], "on": "floor"}]
        strip = asset_records.build_record(
            asset_id="object.strip", role="background", fetch=fetch("strip"),
            inspection=inspection([0.66, -0.1, 0.0], [0.68, 0.1, 0.01]),
            usd_ref=ref("asset.usd", "3"), inspection_ref=ref("asset.usd-inspection", "4"), collision_ref=None)
        records = {"object.cabinet_b01": cabinet_record("object.cabinet_b01", measured_cabinet_inspection()),
                   "object.strip": strip}
        sweep = self.sweep(records, nodes)
        self.assertFalse(sweep["ok"])
        self.assertEqual(sweep["problems"],
                         ["object.cabinet joint_part_01_joint_00 at 0.35 m moves into object.strip"])

        conservative = deepcopy(records)
        conservative["object.cabinet_b01"].pop("parts")
        conservative["object.cabinet_b01"]["joints"][0].pop("child_body")
        self.assertTrue(self.sweep(conservative, nodes)["ok"])


if __name__ == "__main__":
    unittest.main()

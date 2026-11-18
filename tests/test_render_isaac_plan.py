"""Isaac RTX render planning without a simulator: RenderSpec from request + SceneGraph, frame sampling,
camera mounts, joint fill and the render manifest contract."""
from copy import deepcopy
import json
import unittest

import numpy as np

from datafarm.instructions import repository_root
from datafarm.ir import render_spec
from datafarm.ir.common import IRError
from datafarm.render import plan

REF = {"kind": "x", "sha256": "a" * 64, "media_type": "application/json"}
CONFIG = json.loads((repository_root() / "configs/render/isaac-variants.v1.json").read_text())
IDENTITY = {"frame_id": "world", "position_m": [0.0, 0.0, 0.0], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}
INTRINSICS = {"fx": 600.0, "fy": 600.0, "cx": 320.0, "cy": 240.0, "width": 640, "height": 480}


def graph() -> dict:
    head = {"camera_id": "camera.head", "kind": "physical", "mount": "robot.r",
            "offset": {**IDENTITY, "frame_id": "robot.r", "position_m": [0.0, 0.0, 1.0]},
            "intrinsics": INTRINSICS, "optical_frame": {"convention": "opencv"},
            "distortion": {"model": "none", "coefficients": []}, "fps": 10}
    wrist = {**head, "camera_id": "camera.left_wrist", "link": "end_link",
             "offset": {**IDENTITY, "frame_id": "end_link"}}
    return {"cameras": [head, wrist], "frames": [],
            "nodes": [{"node_id": "robot.r", "semantic_tag": "robot", "pose": {**IDENTITY, "position_m": [1.0, 0.0, 0.0]}},
                      {"node_id": "object.table", "semantic_tag": "table", "pose": IDENTITY},
                      {"node_id": "object.cabinet", "semantic_tag": "drawer", "pose": IDENTITY},
                      {"node_id": "object.plate", "semantic_tag": "plate", "pose": IDENTITY}]}


def spec(**render) -> dict:
    request = {"engine": "isaac_rtx", "views": ["head", "left_wrist", "third_person"], "materials": 2, "lights": 2}
    return plan.build_spec(render={**request, **render}, scene_graph=graph(), scene_graph_ref=REF, episode_ref=REF,
                           config=CONFIG)


def manifest(document: dict, steps: int = 25, *, bad: dict | None = None) -> dict:
    streams, frames = [], {}
    for variant in document["variants"]:
        sampled = render_spec.frame_steps(list(range(steps)), 10)
        streams.append({"variant_id": variant["variant_id"], "view_id": variant["view"],
                        "camera_id": plan.stream_camera_id(document, variant["view"]),
                        "video": {**REF, "media_type": "video/mp4"}, "intrinsics": REF, "extrinsics": REF,
                        "frame_count": len(sampled)})
        frames[variant["variant_id"]] = [{"frame_index": i, "control_step": s, "status": "ok", "reason": ""}
                                         for i, s in enumerate(sampled)]
    for variant_id, frame in (bad or {}).items():
        frames[variant_id][0].update(frame)
    return plan.manifest(spec_ref=REF, spec=document, engine={"isaac_sim": "6.2.0", "isaac_lab": "3.0.0"},
                         streams=streams, frames=frames,
                         playback={"status": "verified", "max_position_error_m": 0.0, "max_joint_error": 0.0,
                                   "tolerance_m": 0.01, "notes": ""})


class RenderSpecTest(unittest.TestCase):
    def test_request_views_resolve_to_scene_cameras_and_variants_cross_appearance(self):
        document = spec()
        kinds = {v["view_id"]: (v["kind"], v.get("camera_id")) for v in document["views"]}
        self.assertEqual(kinds, {"head": ("physical", "camera.head"), "left_wrist": ("physical", "camera.left_wrist"),
                                 "third_person": ("virtual", None)})
        self.assertEqual(len(document["variants"]), 3 * 2 * 2)
        base = next(v for v in document["variants"] if v["variant_id"] == "head.m0.l0")
        self.assertEqual(base["material_set"], [], "m0 keeps the asset materials")

    def test_request_cannot_ask_for_more_variants_or_unknown_views_than_exist(self):
        with self.assertRaisesRegex(IRError, "3 material variants"):
            spec(materials=3)
        with self.assertRaisesRegex(IRError, "neither a scene graph camera"):
            spec(views=["right_wrist"])

    def test_spec_rejects_geometry_and_slot_conflicts(self):
        document = spec()
        physical = deepcopy(document)
        physical["views"][0]["pose"] = IDENTITY
        with self.assertRaisesRegex(IRError, "physical views take pose"):
            render_spec.validate(physical, graph())
        clash = deepcopy(document)
        clash["materials"].append({**clash["materials"][0], "material_id": "plate.other"})
        clash["variants"][0]["material_set"] = [clash["materials"][0]["material_id"], "plate.other"]
        with self.assertRaisesRegex(IRError, "same slot"):
            render_spec.validate(clash, graph())
        no_drawer = graph()
        no_drawer["nodes"] = [n for n in no_drawer["nodes"] if n["semantic_tag"] != "drawer"]
        with self.assertRaisesRegex(IRError, "has no node in the scene graph"):
            render_spec.validate(document, no_drawer)

    def test_material_slot_takes_any_identifier_that_the_scene_has_a_node_for(self):
        document = spec()
        block = graph()
        next(n for n in block["nodes"] if n["semantic_tag"] == "plate")["semantic_tag"] = "block"
        next(m for m in document["materials"] if m["slot"] == "plate")["slot"] = "block"
        render_spec.validate(document, block)
        document["materials"][0]["slot"] = "Block"
        with self.assertRaisesRegex(IRError, "slot must match"):
            render_spec.validate(document, block)

    def test_frames_sample_control_steps_every_120_over_fps_without_interpolation(self):
        self.assertEqual(render_spec.frame_steps(list(range(241)), 10), list(range(0, 241, 12)))
        with self.assertRaisesRegex(IRError, "without gaps"):
            render_spec.frame_steps([0, 1, 3], 10)
        with self.assertRaises(IRError):
            render_spec.frame_steps(list(range(10)), 7)


class CameraTest(unittest.TestCase):
    def test_look_at_is_opencv_z_forward_y_down(self):
        pose = plan.look_at([0.0, -2.0, 1.0], [0.0, 0.0, 1.0])
        rotation = plan.quat_to_matrix(pose["quaternion_xyzw"])
        np.testing.assert_allclose(rotation[:, 2], [0.0, 1.0, 0.0], atol=1e-6)
        np.testing.assert_allclose(rotation[:, 1], [0.0, 0.0, -1.0], atol=1e-6)

    def test_static_camera_follows_its_mount_and_wrist_camera_must_ask_the_simulator(self):
        g = graph()
        np.testing.assert_allclose(plan.static_camera_world(g, g["cameras"][0])[:3, 3], [1.0, 0.0, 1.0])
        with self.assertRaisesRegex(IRError, "moves with link"):
            plan.static_camera_world(g, g["cameras"][1])

    def test_link_mount_composes_fixed_joints_and_refuses_moving_ones(self):
        lift = {"position_m": [0.0, 0.0, 0.25], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}
        descriptor = {"joints": [
            {"name": "j6", "type": "revolute", "parent": "link5", "child": "link6", "origin": lift},
            {"name": "end", "type": "fixed", "parent": "link6", "child": "end_link", "origin": lift},
            {"name": "finger", "type": "prismatic", "parent": "end_link", "child": "finger", "origin": lift}]}
        body, offset = plan.link_mount(descriptor, "end_link", ["link5", "link6"])
        self.assertEqual(body, "link6")
        np.testing.assert_allclose(offset[:3, 3], [0.0, 0.0, 0.25])
        with self.assertRaisesRegex(IRError, "prismatic joint 'finger'"):
            plan.link_mount(descriptor, "finger", ["link5", "link6"])

    def test_mimic_joints_are_filled_from_the_recorded_master_never_zero(self):
        joints = [{"name": "g2", "mimic": None},
                  {"name": "g2_left", "mimic": {"joint": "g2", "multiplier": -1.0, "offset": 0.01}}]
        values = plan.full_joint_positions(joints, ["j1", "g2"], [0.3, 0.02], simulated=["j1", "g2_left"])
        self.assertEqual(values, {"j1": 0.3, "g2_left": -0.01})
        with self.assertRaisesRegex(IRError, "no value for joint 'j2'"):
            plan.full_joint_positions(joints, ["j1", "g2"], [0.3, 0.02], simulated=["j1", "j2"])


class ManifestTest(unittest.TestCase):
    def test_manifest_covers_every_variant_at_the_view_rate(self):
        document = manifest(spec())
        render_spec.validate_manifest(document, spec())
        self.assertEqual({e["control_step"] for e in document["frame_map"]}, {0, 12, 24})
        self.assertNotIn("reason", document["frame_map"][0])

    def test_bad_frames_need_a_reason_and_streams_cannot_be_dropped_or_resampled(self):
        document = spec()
        with self.assertRaisesRegex(IRError, "needs a reason"):
            manifest(document, bad={"head.m0.l0": {"status": "error"}})
        built = manifest(document, bad={"head.m0.l0": {"status": "missing", "reason": "state invalid"}})
        self.assertEqual(built["frame_map"][0]["reason"], "state invalid")
        dropped = deepcopy(built)
        dropped["streams"] = dropped["streams"][1:]
        dropped["frame_map"] = [e for e in dropped["frame_map"] if e["variant_id"] != "head.m0.l0"]
        with self.assertRaisesRegex(IRError, "cover exactly"):
            render_spec.validate_manifest(dropped, document)
        resampled = deepcopy(built)
        for entry in resampled["frame_map"]:
            if entry["variant_id"] == "head.m0.l0":
                entry["control_step"] = entry["frame_index"] * 6
        with self.assertRaisesRegex(IRError, "must be 0, 12, 24"):
            render_spec.validate_manifest(resampled, document)


if __name__ == "__main__":
    unittest.main()

"""临时 HDF5 导出/读回/校验（tmp(B)，docs/reference/hdf5-export.md）。"""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from datafarm.expert.hdf5_export import (FRAMES_NOT_RENDERED, FRAMES_RENDERED, SCHEMA, export_episode,
                                          read_episode, validate_hdf5)
from datafarm.kernel.artifacts import ArtifactDocument, ArtifactRef, IRKind
from datafarm.kernel.store import ArtifactStore

TOOL = Path(__file__).resolve().parents[1] / "tools" / "export_episode_hdf5.py"


def _tool():
    spec = importlib.util.spec_from_file_location("export_episode_hdf5_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

DT = 1 / 120
IDENTITY = [0.0, 0.0, 0.0, 1.0]
REF = {"kind": "source.episode", "sha256": "a" * 64, "media_type": "application/json"}
EPISODE_REF = {"kind": "episode-ir", "sha256": "b" * 64, "media_type": "application/json"}
PROVENANCE = {"episode_ref": EPISODE_REF, "source_refs": {"task": "tasks/plate.json"}}
ROBOT_JOINTS = ["left_joint1", "left_g2_joint"]
NODES = ["object.cabinet", "object.table"]


def _entity(asset_id, position, joints, links=None):
    return {"availability": "available", "asset_id": asset_id, "position_m": list(position),
            "quaternion_xyzw": list(IDENTITY), "linear_velocity_m_s": [0.0, 0.0, 0.0],
            "angular_velocity_rad_s": [0.0, 0.0, 0.0], "joints": joints, "links": links or {}}


def _state(step, *, drawer, tcp_x):
    return {
        "control_step": step, "time_s": step * DT,
        "units": {"length": "m", "angle": "rad", "time": "s"}, "frame_id": "world",
        "entities": {
            "object.cabinet": _entity("object.cabinet_drawer", [0.8, 0.0, 0.0],
                                      [{"name": "joint.drawer.slide", "type": "prismatic", "unit": "m",
                                        "q": drawer, "dq": 0.0}]),
            "object.table": _entity("object.table_wood", [0.0, 0.0, 0.72], [],
                                    links={"top": {"position_m": [0.0, 0.0, 0.72],
                                                   "quaternion_xyzw": list(IDENTITY)}}),
        },
        "robot": {"joint_names": list(ROBOT_JOINTS), "q": [0.1 + 0.2, tcp_x],
                  "dq": [0.0, -1e-7],
                  "tcp": {"frame_id": "world", "position_m": [0.5, 0.0, 0.8 + tcp_x],
                          "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]},
                  "gripper": {"width_m": 0.06 + tcp_x, "width_range_m": [0.0, 0.08],
                              "state_source": "measured_master_joint", "verification": "explicit_profile",
                              "within_defined_range": True}},
        "contacts": {"control_step": step, "sampling": "cycle_end_snapshot",
                     "availability": "available", "rows": [], "coverage": "unknown"},
    }


def _action(step, *, drawer):
    return {"control_step": step,
            "requested_joint_targets": {"left_joint1": 0.1, "left_g2_joint": drawer},
            "applied_joint_targets": {"left_joint1": 0.1, "left_g2_joint": drawer},
            "application": "actuator_readback", "command": {"call_id": f"call.{step:04d}"}}


def payload():
    states = [_state(0, drawer=0.0, tcp_x=0.0),
              _state(1, drawer=5e-3, tcp_x=1e-4),
              _state(2, drawer=1.2345678901234567e-2, tcp_x=2e-4)]
    states[0]["contacts"]["rows"] = []
    states[1]["contacts"].update(availability="unavailable", rows=None, coverage="unknown",
                                 force_availability="unavailable", normal_availability="unavailable",
                                 reason_code="CONTACT_QUERY_UNAVAILABLE")
    states[2]["contacts"]["rows"] = [{"a": "robot.r", "b": "object.table", "body_a": "usd:left_finger",
                                      "body_b": "usd:table_top", "dist_m": 3.0517578125e-05}]
    actions = [_action(0, drawer=0.0), _action(1, drawer=1.0e-2)]
    actions[1]["command"] = {}
    return {
        "profile": "measured-scene-episode/1",
        "setup": {"episode_id": "episode.ep_0007", "task_id": "task.plate", "task_revision": "rev.1",
                  "attempt_id": "attempt.1", "node_ids": list(NODES),
                  "control_dt_s": DT, "control_names": ["left_g2_joint", "left_joint1"],
                  "request_ref": dict(REF), "original_request_ref": dict(REF), "task_ref": dict(REF),
                  "scene_ref": dict(REF), "runtime_ref": dict(REF), "expert_ref": dict(REF),
                  "asset_refs": [dict(REF)], "source_sha256": {"expert.py": "c" * 64}},
        "actions": actions, "state_trace": states,
        "contacts": [deepcopy(state["contacts"]) for state in states],
        "events": [{"control_step": 0, "event": "primitive_started", "call_id": "call.0000", "api": "move_tcp"}],
        "frame_map": [], "replay": {"state_playback": "unverified", "action_replay": "unverified"},
        "termination": {"status": "completed", "reason_code": "EXPERT_RETURNED", "trace_complete": True},
        "judgement": "unverified",
    }


def manifest(episode_ref=EPISODE_REF):
    steps = [0, 2]
    rows = []
    for index, step in enumerate(steps):
        rows.append({"camera_id": "camera.head", "variant_id": "base", "frame_index": index,
                     "control_step": step, "status": "ok" if index == 0 else "missing",
                     "intrinsics": dict(REF), "extrinsics": dict(REF),
                     "image": {"kind": "image.mp4", "sha256": "d" * 64, "media_type": "video/mp4"}})
    return {"schema": "datafarm.render-manifest/1", "render_spec": dict(REF), "episode": dict(episode_ref),
            "streams": [{"variant_id": "base", "view_id": "head", "camera_id": "camera.head",
                         "video": {"kind": "image.mp4", "sha256": "d" * 64, "media_type": "video/mp4"},
                         "intrinsics": dict(REF), "extrinsics": dict(REF), "frame_count": len(steps)}],
            "frame_map": rows,
            "state_playback": {"status": "verified", "max_position_error_m": 0.0,
                               "max_joint_error": 0.0, "tolerance_m": 0.01, "notes": ""}}


class EpisodeHdf5Test(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)
        self.path = self.directory / "episode.h5"

    def export(self, document=None, **kwargs):
        kwargs.setdefault("provenance", PROVENANCE)
        return export_episode(self.path, episode_document=document or payload(), **kwargs)

    def test_layout_shapes_dtypes_and_bit_exact_values(self):
        summary = self.export()
        self.assertEqual(summary["schema"], SCHEMA)
        self.assertFalse(summary["real_ready"])
        self.assertEqual((summary["states"], summary["actions"]), (3, 2))
        self.assertEqual(summary["control_names"], ["left_g2_joint", "left_joint1"])
        self.assertEqual(summary["frames"], FRAMES_NOT_RENDERED)

        with h5py.File(self.path, "r") as root:
            meta = root["meta"]
            self.assertEqual(meta.attrs["schema"], SCHEMA)
            self.assertEqual(meta.attrs["control_mode"], "joint_position_target")
            self.assertFalse(bool(meta.attrs["real_ready"]))
            self.assertEqual(meta.attrs["episode_id"], "episode.ep_0007")
            self.assertEqual(meta.attrs["frames"], FRAMES_NOT_RENDERED)
            self.assertEqual(meta.attrs["source_episode_sha256"], EPISODE_REF["sha256"])
            self.assertEqual(json.loads(str(meta.attrs["judge_result_json"])), "unverified")
            self.assertEqual([str(v) for v in meta["control_names"].asstr()[...]],
                             ["left_g2_joint", "left_joint1"])
            self.assertEqual([str(v) for v in meta["joint_names"].asstr()[...]], ROBOT_JOINTS)
            self.assertNotIn("frames", root)

            steps = root["state/control_step"]
            self.assertEqual(steps.shape, (3,))
            self.assertEqual(steps.dtype, np.int64)
            self.assertEqual(steps[()].tolist(), [0, 1, 2])

            q = root["state/robots/robot/q"]
            self.assertEqual(q.shape, (3, 2))
            self.assertEqual(q.dtype, np.float64)
            self.assertEqual(q[()].tobytes(), np.asarray([[0.1 + 0.2, 0.0],
                                                          [0.1 + 0.2, 1e-4],
                                                          [0.1 + 0.2, 2e-4]], dtype=np.float64).tobytes())
            self.assertEqual(root["state/robots/robot/tcp_pose_xyzw"].shape, (3, 7))
            self.assertEqual(root["state/robots/robot/gripper_width_m"].shape, (3, 1))

            drawer = root["state/entities/object.cabinet/joints/joint.drawer.slide/q"]
            self.assertEqual(drawer.shape, (3, 1))
            self.assertEqual(drawer[()][:, 0].tolist(),
                             [0.0, 5e-3, 1.2345678901234567e-2])
            self.assertEqual(root["state/entities/object.cabinet/joints/joint.drawer.slide"].attrs["type"],
                             "prismatic")
            self.assertEqual(root["state/entities/object.table/pose_xyzw"].shape, (3, 7))

            intervals = root["action/interval_steps"]
            self.assertEqual(intervals.dtype, np.int64)
            self.assertEqual(intervals[()].tolist(), [[0, 1], [1, 2]])
            # control_names 顺序固定为排序后的 applied 键，数组按该顺序落盘。
            self.assertEqual(root["action/robots/robot/requested"][()].tolist(),
                             [[0.0, 0.1], [1.0e-2, 0.1]])
            self.assertEqual(root["action/robots/robot/applied"][()].tobytes(),
                             root["action/robots/robot/requested"][()].tobytes())
            self.assertEqual([str(v) for v in root["action/call_id"].asstr()[...]], ["call.0000", ""])

            contacts = [json.loads(text) for text in root["contacts/records"].asstr()[...]]
            self.assertEqual(contacts[0]["rows"], [])
            self.assertIsNone(contacts[1]["rows"])
            self.assertEqual(contacts[1]["availability"], "unavailable")
            self.assertAlmostEqual(contacts[2]["rows"][0]["dist_m"], 3.0517578125e-05)
            self.assertEqual(len(root["events/records"]), 1)

        self.assertEqual(validate_hdf5(self.path, payload()), [])

    def test_read_episode_round_trips_every_array_bit_exactly(self):
        source = payload()
        self.export(source)
        document = read_episode(self.path)
        self.assertEqual(document["meta"]["schema"], SCHEMA)
        self.assertEqual(document["meta"]["joint_names"], ROBOT_JOINTS)
        self.assertEqual(document["meta"]["provenance"], PROVENANCE)
        self.assertEqual(document["state"]["control_step"].tolist(), [0, 1, 2])
        self.assertEqual(document["state"]["robots"]["robot"]["q"].tobytes(),
                         np.asarray([state["robot"]["q"] for state in source["state_trace"]]).tobytes())
        self.assertEqual(document["state"]["robots"]["robot"]["dq"].tobytes(),
                         np.asarray([state["robot"]["dq"] for state in source["state_trace"]]).tobytes())
        self.assertEqual(document["state"]["entities"]["object.cabinet"]["joints"]["joint.drawer.slide"]["q"].tobytes(),
                         np.asarray([[state["entities"]["object.cabinet"]["joints"][0]["q"]]
                                     for state in source["state_trace"]]).tobytes())
        self.assertEqual(document["action"]["interval_steps"].tolist(), [[0, 1], [1, 2]])
        self.assertEqual([record["availability"] for record in document["contacts"]],
                         ["available", "unavailable", "available"])
        self.assertIsNone(document["contacts"][1]["rows"])
        self.assertEqual(document["events"], source["events"])
        self.assertEqual(document["frames"], {})

    def test_validate_catches_a_mutated_float(self):
        self.export()
        self.assertEqual(validate_hdf5(self.path), [])
        self.assertEqual(validate_hdf5(self.path, payload()), [])
        with h5py.File(self.path, "r+") as root:
            dataset = root["state/robots/robot/q"]
            value = dataset[()].copy()
            value[1, 1] = value[1, 1] + 1e-9
            dataset[...] = value
        problems = validate_hdf5(self.path, payload())
        self.assertTrue(any("state/robots/robot/q values differ" in problem for problem in problems), problems)
        self.assertEqual(validate_hdf5(self.path), [], "布局校验不比较数值，只有给出 payload 时才逐位核对")

    def test_validate_catches_shape_dtype_and_contact_regressions(self):
        self.export()
        with h5py.File(self.path, "r+") as root:
            del root["action/interval_steps"]
        self.assertTrue(any("action/interval_steps" in problem for problem in validate_hdf5(self.path)))
        with h5py.File(self.path, "w") as root:
            root.create_group("meta")
        self.assertTrue(any("meta.control_mode" in problem for problem in validate_hdf5(self.path)))

    def test_export_refuses_inconsistent_control_channels(self):
        broken = payload()
        for key in ("requested_joint_targets", "applied_joint_targets"):
            broken["actions"][1][key]["left_wrist_joint"] = 0.0
        with self.assertRaisesRegex(ValueError, "control channel set differs"):
            self.export(broken)
        self.assertFalse(self.path.exists(), "失败时不留半个文件")

        unequal = payload()
        unequal["actions"][1]["requested_joint_targets"]["left_wrist_joint"] = 0.0
        with self.assertRaisesRegex(ValueError, "not applied actuator channels"):
            self.export(unequal)

    def test_partial_requests_are_masked_not_rejected(self):
        """lab30 s2v8：move_tcp 只请求手臂关节，move_gripper 只请求夹爪；applied 仍是全量读回。"""
        partial = payload()
        partial["actions"][1]["requested_joint_targets"].pop("left_g2_joint")
        self.export(partial)
        names = [n.decode() if isinstance(n, bytes) else n for n in h5py.File(self.path, "r")["meta/control_names"][()]]
        with h5py.File(self.path, "r") as root:
            mask = root["action/robots/robot/requested_mask"][()]
        column = names.index("left_g2_joint")
        self.assertTrue(mask[0, column])
        self.assertFalse(mask[1, column])
        self.assertEqual(validate_hdf5(self.path, partial), [])

    def test_export_refuses_a_payload_that_is_not_a_recorded_episode(self):
        broken = payload()
        broken["judgement"] = "success"
        with self.assertRaisesRegex(ValueError, "Judge"):
            self.export(broken)

    def test_joint_types_come_from_provenance_or_are_marked_unknown(self):
        self.export(provenance={**PROVENANCE,
                                "robot_joint_types": {"left_joint1": "revolute", "left_g2_joint": "prismatic"}})
        with h5py.File(self.path, "r") as root:
            self.assertEqual(root["meta"].attrs["joint_types_source"], "provenance")
            self.assertEqual([str(v) for v in root["meta/joint_types"].asstr()[...]], ["revolute", "prismatic"])
        self.export()
        with h5py.File(self.path, "r") as root:
            self.assertEqual(root["meta"].attrs["joint_types_source"], "unknown")
            self.assertEqual([str(v) for v in root["meta/joint_types"].asstr()[...]], ["unknown", "unknown"])

    def test_frames_are_refs_and_keep_frame_status(self):
        summary = self.export(render_manifest=manifest())
        self.assertEqual(summary["frames"], {"status": FRAMES_RENDERED, "streams": {"camera.head": {"base": 2}}})
        with h5py.File(self.path, "r") as root:
            self.assertEqual(root["meta"].attrs["frames"], FRAMES_RENDERED)
            group = root["frames/camera.head/base"]
            self.assertEqual(group.attrs["frame_count"], 2)
            self.assertEqual(group["control_step"][()].tolist(), [0, 2])
            self.assertEqual([str(v) for v in group["status"].asstr()[...]], ["ok", "missing"])
            self.assertEqual(json.loads(str(group["video_ref_json"].asstr()[0]))["kind"], "image.mp4")
        self.assertEqual(validate_hdf5(self.path, payload()), [])
        document = read_episode(self.path)
        self.assertEqual(document["frames"]["streams"]["camera.head"]["base"]["control_step"].tolist(), [0, 2])

    def test_render_manifest_must_bind_the_exported_episode(self):
        other = {**EPISODE_REF, "sha256": "e" * 64}
        with self.assertRaisesRegex(ValueError, "does not bind"):
            self.export(render_manifest=manifest(other))


class ExportCliTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)
        self.path = self.directory / "episode.h5"
        self.store = ArtifactStore(self.directory / "store")
        self.reference = self.store.put_document(ArtifactDocument(
            kind=IRKind.EPISODE, artifact_id="episode.ep_0007", created_at="2026-11-18T00:00:00Z",
            producer="b-scene-recorder", predecessors=(ArtifactRef.from_dict(REF),), evidence=(),
            payload=payload()))
        self.args = ["--store", str(self.store.root),
                     "--episode-ref", json.dumps({"episode_ref": self.reference.to_dict()})]

    def run_tool(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = _tool().main(list(argv))
        return code, json.loads(out.getvalue())

    def test_export_then_validate_round_trip_through_the_store(self):
        code, report = self.run_tool("export", *self.args, "--out", str(self.path))
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertFalse(report["real_ready"])
        self.assertEqual(report["source_episode_sha256"], self.reference.sha256)
        self.assertTrue(self.path.is_file())

        code, report = self.run_tool("validate", "--file", str(self.path), *self.args)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["problems"], [])

        with h5py.File(self.path, "r+") as root:
            intervals = root["action/interval_steps"][()]
            intervals[1] = [1, 3]
            root["action/interval_steps"][...] = intervals
        code, report = self.run_tool("validate", "--file", str(self.path), *self.args)
        self.assertEqual(code, 1)
        self.assertTrue(any("interval_steps" in problem for problem in report["problems"]), report)

    def test_accepts_the_reference_files_render_episode_writes(self):
        """`tools/render_episode.py` takes `{"episode": ref}` and writes `{"render_manifest": ref}`; the exporter
        must read those files as they are (lab30: KeyError 'kind' on the first real episode)."""
        args = ["--store", str(self.store.root), "--episode-ref", json.dumps({"episode": self.reference.to_dict()})]
        code, report = self.run_tool("export", *args, "--out", str(self.path))
        self.assertEqual(code, 0, report)
        code, report = self.run_tool("validate", "--file", str(self.path), *args)
        self.assertEqual(code, 0, report)

    def test_validate_reports_json_on_a_missing_file(self):
        code, report = self.run_tool("validate", "--file", str(self.directory / "absent.h5"))
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertTrue(report["problems"])


if __name__ == "__main__":
    unittest.main()

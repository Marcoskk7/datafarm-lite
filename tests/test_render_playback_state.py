"""Playback reads the current B Episode state: robot segment, entity root pose, per-entity joint values."""
import importlib.util
from pathlib import Path
import unittest

from datafarm.ir.common import IRError
from datafarm.ir.episode_state import validate_state
from datafarm.render import plan

IDENTITY = {"position_m": [0.0, 0.0, 0.0], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}
UNITS = {"length": "m", "angle": "rad", "time": "s"}
ROBOT = {"joint_names": ["j1", "g2"], "q": [0.3, 0.02], "dq": [0.0, 0.0],
         "tcp": {"frame_id": "world", "position_m": [0.0, 0.0, 0.0], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}}


def entity(**overrides) -> dict:
    entry = {"availability": "available", "asset_id": "asset.block", **IDENTITY,
             "linear_velocity_m_s": [0.0, 0.0, 0.0], "angular_velocity_rad_s": [0.0, 0.0, 0.0],
             "joints": [{"name": "slide", "type": "prismatic", "unit": "m", "q": 0.12, "dq": 0.0}],
             # links 是刚体原点的测量值，绝不能当实体根位姿用
             "links": {"body": {"position_m": [9.0, 9.0, 9.0], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}}}
    entry.update(overrides)
    return entry


def state(*, entities=None, robot=None) -> dict:
    return {"control_step": 3, "time_s": 0.025, "units": UNITS, "frame_id": "world",
            "entities": {"object.block.0": entity()} if entities is None else entities,
            "robot": ROBOT if robot is None else robot,
            "contacts": {"availability": "unavailable", "rows": None, "coverage": "unknown",
                         "sampling": "cycle_end_snapshot", "control_step": 3,
                         "force_availability": "unavailable", "normal_availability": "unavailable",
                         "reason_code": "NO_QUERY"}}


class StateRobotTest(unittest.TestCase):
    def test_returns_the_recorded_order_and_values(self):
        self.assertEqual(plan.state_robot(state(), 3), (["j1", "g2"], [0.3, 0.02]))

    def test_rejects_a_length_mismatch_with_the_step_in_the_message(self):
        with self.assertRaisesRegex(IRError, r"state_trace\[3\]\.robot\.q"):
            plan.state_robot(state(robot={"joint_names": ["j1", "g2"], "q": [0.3]}), 3)

    def test_rejects_duplicate_joint_names(self):
        with self.assertRaisesRegex(IRError, "distinct"):
            plan.state_robot(state(robot={"joint_names": ["j1", "j1"], "q": [0.0, 0.0]}), 3)

    def test_rejects_non_finite_values(self):
        with self.assertRaisesRegex(IRError, "finite"):
            plan.state_robot(state(robot={"joint_names": ["j1"], "q": [float("nan")]}), 3)


class StateEntityTest(unittest.TestCase):
    def test_root_pose_comes_from_position_and_quaternion_never_from_links(self):
        self.assertEqual(plan.state_entity_pose(state(), "object.block.0", 3),
                         {"frame_id": "world", "position_m": [0.0, 0.0, 0.0],
                          "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]})

    def test_a_missing_entity_is_rejected(self):
        with self.assertRaisesRegex(IRError, r"entities\.object\.table"):
            plan.state_entity_pose(state(), "object.table", 3)

    def test_an_unavailable_entity_is_rejected(self):
        with self.assertRaisesRegex(IRError, "not available"):
            plan.state_entity_pose(state(entities={"object.block.0": entity(availability="unavailable")}),
                                   "object.block.0", 3)

    def test_a_non_unit_quaternion_is_rejected(self):
        with self.assertRaisesRegex(IRError, "unit quaternion"):
            plan.state_entity_pose(state(entities={"object.block.0": entity(quaternion_xyzw=[0.0, 0.0, 0.0, 2.0])}),
                                   "object.block.0", 3)

    def test_joint_values_are_read_per_entity_name(self):
        self.assertEqual(plan.state_entity_joints(state(), "object.block.0", 3), {"slide": 0.12})

    def test_duplicate_joint_names_inside_one_entity_are_rejected(self):
        joints = [{"name": "slide", "type": "prismatic", "unit": "m", "q": 0.1, "dq": 0.0},
                  {"name": "slide", "type": "prismatic", "unit": "m", "q": 0.2, "dq": 0.0}]
        with self.assertRaisesRegex(IRError, "duplicate"):
            plan.state_entity_joints(state(entities={"object.block.0": entity(joints=joints)}),
                                     "object.block.0", 3)

    def test_an_entity_without_joints_returns_an_empty_mapping(self):
        self.assertEqual(plan.state_entity_joints(state(entities={"object.block.0": entity(joints=[])}),
                                                  "object.block.0", 3), {})


class RecordedMimicTest(unittest.TestCase):
    def test_a_measured_mimic_joint_wins_over_the_ideal_relation(self):
        joints = [{"name": "g2", "mimic": None},
                  {"name": "g2_left", "mimic": {"joint": "g2", "multiplier": -1.0, "offset": 0.01}}]
        # 理想 mimic 会算出 -0.01；实测是 0.019，必须用实测值
        self.assertEqual(plan.full_joint_positions(joints, ["g2", "g2_left"], [0.02, 0.019],
                                                   simulated=["g2_left"]), {"g2_left": 0.019})


class SyntheticEpisodeTest(unittest.TestCase):
    """合成 fixture 自己也得是当前 schema 的合法 Episode，否则渲染测试会绕过契约。"""

    def test_synthetic_states_validate_against_the_episode_contract(self):
        path = Path(__file__).resolve().parents[1] / "tools" / "synthetic_episode.py"
        spec = importlib.util.spec_from_file_location("synthetic_episode", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        graph = {"backend": {"name": "isaaclab", "version": "3.0.0", "physics": "newton_mjwarp"},
                 "robot": {"controlled_joints": ["left_joint1", "left_g2_joint"]},
                 "nodes": [
                     {"node_id": "object.table", "asset_id": "asset.table", "semantic_tag": "table",
                      "pose": {"frame_id": "world", **IDENTITY}},
                     {"node_id": "object.cabinet", "asset_id": "asset.cabinet", "semantic_tag": "drawer",
                      "pose": {"frame_id": "world", **IDENTITY},
                      "joint_state": {"joint.drawer.slide": 0.0}},
                 ]}
        records = {"asset.table": {"joints": []},
                   "asset.cabinet": {"joints": [{"name": "joint.drawer.slide", "type": "prismatic"}]}}
        payload = module.build(graph, {"kind": "ir.scene-graph", "sha256": "a" * 64,
                                       "media_type": "application/json"},
                               steps=4, drawer={"joint.drawer.slide": 0.2}, records=records)
        node_ids = ["object.table", "object.cabinet"]
        for step, state in enumerate(payload["state_trace"]):
            validate_state(state, node_ids=node_ids, step=step, dt=1 / 120)
        last = payload["state_trace"][-1]["entities"]["object.cabinet"]["joints"][0]
        self.assertEqual((last["name"], last["q"]), ("joint.drawer.slide", 0.2))

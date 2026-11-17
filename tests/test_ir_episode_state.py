"""Episode IR `state_trace` entry validator (docs/ir/episode.md#state_trace)."""
from copy import deepcopy
import unittest

from datafarm.ir.common import IRError
from datafarm.ir.episode_state import validate_robot, validate_state

IDENTITY = [0.0, 0.0, 0.0, 1.0]
STATE = {
    "control_step": 0, "time_s": 0.0, "units": {"length": "m", "angle": "rad", "time": "s"}, "frame_id": "world",
    "entities": {
        "object.plate": {"availability": "available", "asset_id": "object.plate_raised_rim",
                         "position_m": [0.5, -0.25, 0.78], "quaternion_xyzw": IDENTITY,
                         "linear_velocity_m_s": [0.0, 0.0, 0.0], "angular_velocity_rad_s": [0.0, 0.0, 0.0],
                         "joints": [], "links": {"base": {"position_m": [0.5, -0.25, 0.78], "quaternion_xyzw": IDENTITY}}},
        "object.cabinet": {"availability": "available", "asset_id": "object.cabinet_drawer",
                           "position_m": [0.8, 0.0, 0.0], "quaternion_xyzw": IDENTITY,
                           "linear_velocity_m_s": [0.0, 0.0, 0.0], "angular_velocity_rad_s": [0.0, 0.0, 0.0],
                           "joints": [{"name": "joint.drawer.slide", "type": "prismatic", "unit": "m", "q": 0.42, "dq": 0.0}],
                           "links": {}},
    },
    "robot": {"joint_names": ["left_joint1", "left_g2_joint"], "q": [0.0, 0.02], "dq": [0.0, 0.0],
              "tcp": {"frame_id": "world", "position_m": [0.5, 0.0, 0.8], "quaternion_xyzw": IDENTITY},
              "gripper": {"width_m": 0.06, "width_range_m": [0.0, 0.08], "state_source": "measured_master_joint",
                          "verification": "explicit_profile", "within_defined_range": True}},
    "contacts": {"control_step": 0, "sampling": "cycle_end_snapshot", "availability": "unavailable",
                 "rows": None, "coverage": "unknown", "force_availability": "unavailable",
                 "normal_availability": "unavailable", "reason_code": "CONTACT_QUERY_UNAVAILABLE"},
}


def state(**changes):
    value = deepcopy(STATE)
    value.update(changes)
    return value


class EpisodeStateTests(unittest.TestCase):
    def test_canonical_entry_round_trips_and_binds_clock_and_coverage(self):
        self.assertEqual(validate_state(STATE, node_ids=["object.plate", "object.cabinet"], step=0, dt=1 / 120), STATE)
        moved = state(control_step=3, time_s=3 / 120)
        moved["contacts"]["control_step"] = 3
        validate_state(moved, step=3, dt=1 / 120)
        with self.assertRaisesRegex(IRError, "clock gap"):
            validate_state(moved, step=2)
        with self.assertRaisesRegex(IRError, "time_s"):
            validate_state(state(time_s=0.5), dt=1 / 120)
        with self.assertRaisesRegex(IRError, "cover exactly"):
            validate_state(STATE, node_ids=["object.plate"])

    def test_old_or_renamed_fields_are_rejected(self):
        for old in ("tcp_pose", "objects", "validity", "articulations", "valid", "schema_version"):
            with self.subTest(old=old), self.assertRaises(IRError):
                validate_state(state(**{old: {}}))
        robot = deepcopy(STATE["robot"])
        robot["tcp_pose"] = robot.pop("tcp")
        with self.assertRaisesRegex(IRError, "unknown fields"):
            validate_robot(robot)
        nested = deepcopy(STATE)
        nested["entities"]["object.plate"]["pose"] = {}
        with self.assertRaisesRegex(IRError, "exactly"):
            validate_state(nested)

    def test_nonfinite_unnormalized_and_unit_mismatch_rejected(self):
        bad = deepcopy(STATE)
        bad["robot"]["q"][0] = float("nan")
        with self.assertRaisesRegex(IRError, "finite"):
            validate_state(bad)
        bad = deepcopy(STATE)
        bad["entities"]["object.plate"]["quaternion_xyzw"] = [0.0, 0.0, 0.0, 2.0]
        with self.assertRaisesRegex(IRError, "unit quaternion"):
            validate_state(bad)
        bad = deepcopy(STATE)
        bad["entities"]["object.cabinet"]["joints"][0]["unit"] = "rad"
        with self.assertRaisesRegex(IRError, "type/unit"):
            validate_state(bad)
        bad = deepcopy(STATE)
        bad["robot"]["q"] = [0.0]
        with self.assertRaisesRegex(IRError, "robot.q"):
            validate_state(bad)

    def test_unavailable_contacts_are_not_empty_contacts(self):
        bad = deepcopy(STATE)
        bad["contacts"]["rows"] = []
        with self.assertRaisesRegex(IRError, "rows=null"):
            validate_state(bad)
        bad["contacts"].update(availability="available")
        validate_state(bad)

    def test_robot_segment_optional_tcp_and_gripper(self):
        validate_robot({"joint_names": ["a"], "q": [0.1], "dq": [0.0]})
        with self.assertRaisesRegex(IRError, "world"):
            validate_robot({"joint_names": ["a"], "q": [0.1], "dq": [0.0],
                            "tcp": {"frame_id": "base", "position_m": [0, 0, 0], "quaternion_xyzw": IDENTITY}})


if __name__ == "__main__":
    unittest.main()

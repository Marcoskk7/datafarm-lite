"""Negative and ordering tests; no simulator or model needed."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from datafarm.simulation.robot_binding import (
    BindingError, bind_robot, discover_urdf, gripper_target, validate_targets,
)
from datafarm.simulation.isaac_robot import IsaacRobotAPI


SOURCE = b'''<robot name="fixture">
<link name="base"/><link name="arm"/><link name="master"/><link name="finger"/>
<joint name="arm_joint" type="revolute"><parent link="base"/><child link="arm"/>
<limit lower="-1" upper="1" velocity="2" effort="5"/></joint>
<joint name="master_joint" type="prismatic"><parent link="arm"/><child link="master"/>
<limit lower="0" upper="0.1" velocity="0.2" effort="5"/></joint>
<joint name="finger_joint" type="prismatic"><parent link="master"/><child link="finger"/>
<limit lower="0" upper="0.05" velocity="0.1" effort="5"/>
<mimic joint="master_joint" multiplier="0.5"/></joint></robot>'''
REF = dict(kind='fixture-calibration', sha256='a'*64, media_type='application/json')


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.descriptor = discover_urdf(SOURCE)
        self.profile = dict(schema_version='robot-profile/1', groups={'arm': ['arm_joint']},
                            gripper=dict(master_joint='master_joint', width_range_m=[0, .1],
                                         joint_per_m=1, joint_offset=0,
                                         calibration_ref=REF))

    def test_runtime_reordering_and_passive_exclusion(self):
        b = bind_robot(self.descriptor, self.profile,
                       ['finger_joint', 'master_joint', 'arm_joint'], ['base', 'arm', 'master', 'finger'])
        self.assertEqual(b['joint_index']['arm_joint'], 2)
        self.assertEqual(b['controllable_joints'], ['master_joint', 'arm_joint'])
        self.assertEqual(gripper_target(b, .04), {'master_joint': .04})
        self.assertEqual(b['descriptor']['joints'][1]['unit'], 'm')
        self.assertFalse(b['capabilities']['move_tcp'])

    def test_no_invented_tcp_or_gripper(self):
        b = bind_robot(self.descriptor)
        self.assertFalse(b['capabilities']['tcp_pose'])
        self.assertFalse(b['capabilities']['set_gripper'])
        self.assertFalse(b['runtime_bound'])
        with self.assertRaisesRegex(BindingError, 'CAPABILITY_UNAVAILABLE'):
            gripper_target(b, .02)

    def test_stale_profile_and_runtime_mismatch(self):
        for names in (['arm_joint'], ['arm_joint', 'master_joint', 'master_joint']):
            with self.assertRaises(BindingError):
                bind_robot(self.descriptor, joint_names=names)
        with self.assertRaises(BindingError):
            bind_robot(self.descriptor, {**self.profile, 'source_sha256': 'changed'})

    def test_invalid_targets_do_not_get_clipped(self):
        b = bind_robot(self.descriptor)
        for target in ({'finger_joint': .01}, {'unknown': 0}, {'arm_joint': 2},
                       {'arm_joint': float('nan')}, {'arm_joint': True}, {}):
            with self.assertRaises(BindingError):
                validate_targets(b, target)

    def test_invalid_profiles(self):
        profiles = [
            {**self.profile, 'groups': {'a': ['arm_joint'], 'b': ['arm_joint']}},
            {**self.profile, 'groups': {'a': ['finger_joint']}},
            {**self.profile, 'tcp': {'link': 'missing'}},
            {**self.profile, 'tcp': dict(link='arm', position_m=[0,0,0], quaternion_xyzw=[1,1,0,0])},
            {**self.profile, 'gripper': {**self.profile['gripper'], 'joint_per_m': 5}},
            {**self.profile, 'gripper': {**self.profile['gripper'], 'calibration_ref': ''}},
        ]
        for p in profiles:
            with self.subTest(profile=p), self.assertRaises(BindingError):
                bind_robot(self.descriptor, p)

    def test_binding_digest_and_snapshot(self):
        b = bind_robot(self.descriptor, self.profile)
        self.profile['groups']['arm'].clear()
        self.assertEqual(b['groups']['arm'], ['arm_joint'])
        b['joint_index']['arm_joint'] = 7
        with self.assertRaisesRegex(BindingError, 'modified'):
            validate_targets(b, {'arm_joint': 0})

    def test_invalid_graph_and_mimic(self):
        variants = [SOURCE.replace(b'name="finger"', b'name="arm"'),
                    SOURCE.replace(b'<parent link="base"/>', b'<parent link="finger"/>'),
                    SOURCE.replace(b'joint="master_joint"', b'joint="finger_joint"'),
                    SOURCE.replace(b'joint="master_joint"', b'joint="missing"'),
                    SOURCE.replace(b'lower="-1" upper="1"', b'lower="2" upper="1"')]
        for source in variants:
            with self.subTest(source=source), self.assertRaises(BindingError):
                discover_urdf(source)

    def test_mimic_chain_cycle_and_unknown_version(self):
        source = SOURCE.replace(b'<limit lower="0" upper="0.1" velocity="0.2" effort="5"/>',
                                b'<limit lower="0" upper="0.1" velocity="0.2" effort="5"/><mimic joint="finger_joint"/>')
        with self.assertRaises(BindingError):
            discover_urdf(source)
        with self.assertRaises(BindingError):
            bind_robot(self.descriptor, {'schema_version': 'robot-profile/999'})

    def test_valid_tcp_and_required_runtime_link(self):
        p = {**self.profile, 'tcp': dict(link='arm', position_m=[0,0,.02], quaternion_xyzw=[0,0,0,1], calibration_ref=REF)}
        self.assertTrue(bind_robot(self.descriptor, p)['capabilities']['tcp_pose'])
        with self.assertRaises(BindingError):
            bind_robot(self.descriptor, p, body_names=['base'])


class FakeTensor:
    def __init__(self, values):
        self.values = values
        self.shape = (len(values), len(values[0]))

    def detach(self): return self
    def cpu(self): return self
    def tolist(self): return deepcopy(self.values)


class LiveBoundaryTests(unittest.TestCase):
    def robot(self):
        return SimpleNamespace(
            joint_names=['finger_joint', 'master_joint', 'arm_joint'],
            body_names=['base', 'arm', 'master', 'finger'],
            data=SimpleNamespace(joint_pos=FakeTensor([[.01,.02,.3]]),
                                 joint_vel=FakeTensor([[0,0,0]]),
                                 joint_pos_limits=FakeTensor([[[0,.05],[0,.1],[-1,1]]])))

    def test_observe_actual_order_and_missing_capabilities(self):
        api = IsaacRobotAPI(self.robot(), discover_urdf(SOURCE))
        self.assertEqual(api.observe()['q'], [.01,.02,.3])
        self.assertEqual(api.binding['joint_index']['arm_joint'], 2)
        with self.assertRaises(BindingError): api.set_gripper(.01)
        with self.assertRaises(BindingError): api.move_tcp(None)
        self.assertFalse(api.capabilities()['joint_position_target'])
        with self.assertRaisesRegex(BindingError, 'no active position drive'):
            api.set_joint_targets({'arm_joint': 0})

    def test_runtime_drift_and_invalid_state(self):
        robot = self.robot()
        robot.data.joint_pos_limits.values[0][2] = [-2,2]
        with self.assertRaisesRegex(BindingError, 'limits differ'):
            IsaacRobotAPI(robot, discover_urdf(SOURCE))
        robot = self.robot()
        api = IsaacRobotAPI(robot, discover_urdf(SOURCE))
        robot.data.joint_pos.values[0][0] = float('nan')
        with self.assertRaisesRegex(BindingError, 'NONFINITE'):
            api.observe()

    def test_multiple_environments_refused(self):
        robot = self.robot()
        robot.data.joint_pos = FakeTensor([[0,0,0],[0,0,0]])
        with self.assertRaisesRegex(BindingError, 'one environment'):
            IsaacRobotAPI(robot, discover_urdf(SOURCE))


if __name__ == '__main__':
    unittest.main()

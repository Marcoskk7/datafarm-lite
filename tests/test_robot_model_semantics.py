"""Model-first semantics, independent of GPU and deployment assets."""
from pathlib import Path
import struct
import tempfile
import unittest
from types import SimpleNamespace
from copy import deepcopy
import xml.etree.ElementTree as ET

from datafarm.simulation.robot_binding import BindingError, bind_robot, discover_urdf, gripper_target, validate_targets, verify_binding
from datafarm.simulation.robot_semantics import file_mesh_resolver, rotate
from datafarm.simulation.isaac_robot import IsaacRobotAPI
from test_robot_binding import FakeTensor


REF = dict(kind='scene', sha256='a'*64, media_type='application/json')
SOURCE = b'''<robot name="model">
<link name="base"/><link name="arm"/><link name="flange"/><link name="tool0"/>
<link name="virtual"/>
<link name="left"><collision><geometry><box size=".01 .04 .06"/></geometry></collision></link>
<link name="right"><collision><geometry><box size=".01 .04 .06"/></geometry></collision></link>
<joint name="arm_joint" type="revolute"><parent link="base"/><child link="arm"/>
<limit lower="-1" upper="1" effort="5" velocity="2"/></joint>
<joint name="mount" type="fixed"><parent link="arm"/><child link="flange"/>
<origin xyz=".01 .02 .03" rpy="0 0 1.5707963267948966"/></joint>
<joint name="end" type="fixed"><parent link="flange"/><child link="tool0"/>
<origin xyz=".1 0 .2"/></joint>
<joint name="drive" type="prismatic"><parent link="arm"/><child link="virtual"/>
<limit lower="0" upper=".08" effort="5" velocity=".2"/></joint>
<joint name="left_slide" type="prismatic"><parent link="arm"/><child link="left"/>
<origin xyz="-.015 0 0"/><axis xyz="-1 0 0"/>
<limit lower="0" upper=".1" effort="5" velocity=".2"/><mimic joint="drive" multiplier=".25" offset=".002"/></joint>
<joint name="right_slide" type="prismatic"><parent link="arm"/><child link="right"/>
<origin xyz=".015 0 0"/><axis xyz="1 0 0"/>
<limit lower="0" upper=".1" effort="5" velocity=".2"/><mimic joint="drive" multiplier=".75" offset=".001"/></joint>
</robot>'''


class ModelSemanticsTests(unittest.TestCase):
    def test_model_tcp_fixed_chain_survives_import_merging(self):
        d = discover_urdf(SOURCE)
        b = bind_robot(d, body_names=['base', 'arm', 'virtual', 'left', 'right'])
        tcp = b['semantics']['tcp']
        self.assertEqual(tcp['link'], 'arm')
        self.assertEqual(tcp['model_frame'], 'tool0')
        for actual, expected in zip(tcp['position_m'], [.01, .12, .23]):
            self.assertAlmostEqual(actual, expected)
        rotated = rotate(tcp['quaternion_xyzw'], [1, 0, 0])
        for actual, expected in zip(rotated, [0, 1, 0]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(tcp['source_ref']['sha256'], d['source_sha256'])
        self.assertEqual(b['control_mode'], 'position')
        self.assertFalse(b['capabilities']['move_tcp'])

    def test_missing_body_does_not_cross_moving_joint(self):
        b = bind_robot(discover_urdf(SOURCE), body_names=['base'])
        self.assertFalse(b['capabilities']['tcp_pose'])
        with self.assertRaises(BindingError):
            bind_robot(discover_urdf(SOURCE), {'schema_version': 'robot-profile/2', 'model_tcp': 'tool0'}, body_names=['base'])

    def test_width_uses_geometry_mimic_offsets_and_ratios(self):
        b = bind_robot(discover_urdf(SOURCE))
        g = b['semantics']['gripper']
        self.assertAlmostEqual(g['width_range_m'][0], .023)
        self.assertAlmostEqual(g['width_range_m'][1], .103)
        self.assertAlmostEqual(gripper_target(b, .063)['drive'], .04)
        self.assertEqual(g['verification'], 'model_geometry_only')
        with self.assertRaises(BindingError):
            gripper_target(b, .001)

    def test_observed_width_uses_actual_fingers_even_when_mimic_lags(self):
        d = discover_urdf(SOURCE)
        moving = [j for j in d['joints'] if j['type'] != 'fixed']
        robot = SimpleNamespace(joint_names=[j['name'] for j in moving], body_names=['base'],
            data=SimpleNamespace(joint_pos=FakeTensor([[0, .04, .009, .025]]),
                                 joint_vel=FakeTensor([[0, 0, 0, 0]]),
                                 joint_pos_limits=FakeTensor([[j['limits']['position'] for j in moving]])))
        api = IsaacRobotAPI(robot, d)
        state = api.observe()['gripper']
        self.assertAlmostEqual(state['width_m'], .054)
        self.assertEqual(state['state_source'], 'measured_finger_joints')
        self.assertFalse(api.capabilities()['set_gripper'])

    def test_explicit_profile_overrides_are_identified_and_preserved(self):
        profile = dict(schema_version='robot-profile/2',
                       tcp=dict(link='arm', position_m=[0, 0, .1], quaternion_xyzw=[0, 0, 0, 1], calibration_ref=REF),
                       gripper=dict(master_joint='drive', width_range_m=[0, .08], joint_per_m=1,
                                    joint_offset=0, calibration_ref=REF))
        b = bind_robot(discover_urdf(SOURCE), profile)
        self.assertEqual(b['profile'], profile)
        self.assertEqual(b['semantics']['tcp']['selection_source'], 'explicit_profile')
        self.assertEqual(gripper_target(b, .04), {'drive': .04})

    def test_ambiguous_tcp_requires_selection_not_last_link(self):
        source = SOURCE.replace(b'<link name="virtual"/>', b'<link name="virtual"/><link name="tcp"/>')
        source = source.replace(b'</robot>', b'<joint name="extra" type="fixed"><parent link="arm"/><child link="tcp"/></joint></robot>')
        d = discover_urdf(source)
        self.assertFalse(bind_robot(d)['capabilities']['tcp_pose'])
        b = bind_robot(d, dict(schema_version='robot-profile/2', model_tcp='tool0'))
        self.assertTrue(b['capabilities']['tcp_pose'])
        with self.assertRaises(BindingError):
            bind_robot(d, dict(schema_version='robot-profile/2', model_tcp='unknown'))

    def test_multiple_grippers_require_explicit_master_selection(self):
        root = ET.fromstring(SOURCE)
        for node in list(root):
            if node.get('name') not in ('virtual', 'left', 'right', 'drive', 'left_slide', 'right_slide'):
                continue
            clone = deepcopy(node)
            clone.set('name', 'other_' + node.get('name'))
            for child in clone:
                if child.tag == 'child':
                    child.set('link', 'other_' + child.get('link'))
                if child.tag == 'mimic':
                    child.set('joint', 'other_' + child.get('joint'))
            root.append(clone)
        d = discover_urdf(ET.tostring(root))
        self.assertFalse(bind_robot(d)['capabilities']['set_gripper'])
        b = bind_robot(d, dict(schema_version='robot-profile/2', model_gripper='other_drive'))
        self.assertAlmostEqual(gripper_target(b, .063)['other_drive'], .04)

    def test_collision_origin_affects_effective_gap(self):
        source = SOURCE.replace(b'<link name="left"><collision>',
                                b'<link name="left"><collision><origin xyz="-.002 0 0"/>')
        g = bind_robot(discover_urdf(source))['semantics']['gripper']
        self.assertAlmostEqual(g['width_range_m'][0], .025)

    def test_unsupported_or_incomplete_geometry_does_not_enable_gripper(self):
        variants = [SOURCE.replace(b'<box size=".01 .04 .06"/>', b'<sphere radius=".01"/>'),
                    SOURCE.replace(b'<box size=".01 .04 .06"/>', b'<mesh filename="missing.stl"/>'),
                    SOURCE.replace(b'axis xyz="-1 0 0"', b'axis xyz="0 1 0"'),
                    SOURCE.replace(b'xyz=".015 0 0"', b'xyz=".015 1 0"')]
        for source in variants:
            with self.subTest(source=source):
                b = bind_robot(discover_urdf(source))
                self.assertFalse(b['capabilities']['set_gripper'])
                self.assertTrue(b['semantic_diagnostics'])

    def test_mesh_scale_origin_and_content_hash(self):
        # Binary STL with a header starting with 'solid' must still parse as binary.
        vertices = [(-.005, -.02, -.03), (.005, -.02, .03), (0, .02, 0)]
        mesh = b'solid binary'.ljust(80, b' ') + struct.pack('<I12fH', 1, 0, 0, 1,
                                                       *[v for p in vertices for v in p], 0)
        source = SOURCE.replace(b'<box size=".01 .04 .06"/>', b'<mesh filename="finger.STL" scale="2 1 1"/>')
        d = discover_urdf(source, lambda uri: mesh)
        g = bind_robot(d)['semantics']['gripper']
        self.assertAlmostEqual(g['width_range_m'][0], .013)
        self.assertEqual(len(g['geometry_refs'][0]['sha256']), 64)
        changed = discover_urdf(source, lambda uri: mesh.replace(b'solid binary', b'other binary'))
        self.assertNotEqual(bind_robot(d)['binding_sha256'], bind_robot(changed)['binding_sha256'])

    def test_invalid_origins_axes_and_old_descriptor_rejected(self):
        for source in (SOURCE.replace(b'xyz=".01 .02 .03"', b'xyz="nan 0 0"'),
                       SOURCE.replace(b'axis xyz="-1 0 0"', b'axis xyz="0 0 0"')):
            with self.assertRaises(BindingError):
                discover_urdf(source)
        d = discover_urdf(SOURCE)
        d['schema_version'] = 'robot-descriptor/1'
        with self.assertRaises(BindingError):
            bind_robot(d)
        with self.assertRaises(BindingError):
            bind_robot(discover_urdf(SOURCE), dict(schema_version='robot-profile/2', control_mode='effort'))
        with self.assertRaises(BindingError):
            bind_robot(discover_urdf(SOURCE), dict(schema_version='robot-profile/1'))
        with self.assertRaises(BindingError):
            bind_robot(discover_urdf(SOURCE), {})
        b = bind_robot(discover_urdf(SOURCE))
        b['schema_version'] = 'robot-binding/1'
        with self.assertRaisesRegex(BindingError, 'Unsupported binding version'):
            verify_binding(b)

    def test_file_resolver_confines_relative_and_package_meshes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / 'package'
            package.mkdir()
            (package / 'mesh.stl').write_bytes(b'fixture')
            read = file_mesh_resolver(package / 'robot.urdf', packages={'pkg': package})
            self.assertEqual(read('mesh.stl'), b'fixture')
            self.assertEqual(read('package://pkg/mesh.stl'), b'fixture')
            for uri in ('../outside.stl', 'package://pkg/../outside.stl', 'https://example.test/mesh', 'C:/mesh.stl'):
                with self.subTest(uri=uri), self.assertRaises(ValueError):
                    read(uri)


class ModelLimitsTests(unittest.TestCase):
    def test_follower_urdf_limits_propagate_to_master_and_width(self):
        source = SOURCE.replace(b'lower="0" upper=".1"', b'lower=".002" upper=".012"', 1)
        b = bind_robot(discover_urdf(source))
        self.assertAlmostEqual(b['effective_position_limits']['drive'][1], .04)
        self.assertAlmostEqual(b['semantics']['gripper']['width_range_m'][1], .063)
        with self.assertRaises(BindingError):
            validate_targets(b, {'drive': .05})
        with self.assertRaises(BindingError):
            gripper_target(b, .073)

    def test_incompatible_mimic_limits_fail_binding(self):
        source = SOURCE.replace(b'lower="0" upper=".1"', b'lower=".09" upper=".1"', 1)
        with self.assertRaises(BindingError):
            bind_robot(discover_urdf(source))


if __name__ == '__main__':
    unittest.main()

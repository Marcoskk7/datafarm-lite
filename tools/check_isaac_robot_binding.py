#!/usr/bin/env python3
"""Run live auto-binding in an existing Isaac Lab 3.0 environment.

This is an API integration check, not the cancelled backend-selection probe or
a grasp/task evaluation. The deployment root is explicit, not a package default.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime-root', type=Path, required=True)
parser.add_argument('--asset', choices=['airbot_single', 'airbot_dual', 'dualxarm'], required=True)
parser.add_argument('--profile', type=Path)
parser.add_argument('--asset-root', type=Path)
parser.add_argument('--exercise-model-semantics', action='store_true', help='Check model TCP FK and free-space width tracking')
parser.add_argument('--output', type=Path, required=True)
args, _ = parser.parse_known_args()
if args.output.exists():
    parser.error('Output already exists; use a new evidence filename')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(args.runtime_root.resolve()))

import warp as wp
wp.config.enable_backward = False
from isaaclab.app import AppLauncher, launch_simulation
AppLauncher.add_app_launcher_args(parser)
# Parse launcher defaults after adding its arguments; caller flags remain validated.
args = parser.parse_args()
from isaaclab.scene import InteractiveScene
import isaaclab.sim as sim_utils
from robot_profiles import asset_paths, make_simulation_cfg, make_robot_scene_cfg
from datafarm.simulation.robot_binding import BindingError, discover_urdf
from datafarm.simulation.isaac_robot import IsaacRobotAPI, tensor
from datafarm.simulation.robot_semantics import file_mesh_resolver

urdf, _ = asset_paths(args.asset)
descriptor = discover_urdf(Path(urdf).read_bytes(), file_mesh_resolver(urdf, args.asset_root))
profile = json.loads(args.profile.read_text(encoding='utf-8-sig')) if args.profile else None
cfg = make_simulation_cfg(args.asset, 'newton_mjwarp')
args.physics = None
with launch_simulation(cfg, args):
    sim = sim_utils.SimulationContext(cfg)
    scene = InteractiveScene(make_robot_scene_cfg(args.asset, 'newton_mjwarp'))
    sim.reset()
    robot = scene['robot']
    api = IsaacRobotAPI(robot, descriptor, profile)
    before = api.observe()
    target = {n: before['q'][api.binding['joint_index'][n]] for n in api.binding['controllable_joints']}
    result = api.set_joint_targets(target)
    robot.write_data_to_sim()
    sim.step(render=False)
    robot.update(sim.get_physics_dt())
    after = api.observe()
    semantic_checks = []
    if args.exercise_model_semantics:
        import numpy as np
        from scipy.spatial.transform import Rotation
        import xml.etree.ElementTree as ET
        source_tree = ET.fromstring(Path(urdf).read_bytes())
        parents = {j.find('child').get('link'): j for j in source_tree.findall('joint')}

        def check_tcp(state):
            tcp = api.binding['semantics']['tcp']
            if tcp is None or tcp['selection_source'] != 'model':
                raise BindingError('Model TCP required for FK verification')
            chain, frame = [], tcp['model_frame']
            while frame in parents:
                joint = parents[frame]
                chain.append(joint)
                frame = joint.find('parent').get('link')
            i = api.binding['body_names'].index(frame)
            transform = np.eye(4)
            transform[:3, :3] = Rotation.from_quat(tensor(robot.data.body_quat_w)[0, i].tolist()).as_matrix()
            transform[:3, 3] = tensor(robot.data.body_pos_w)[0, i].tolist()
            measured = dict(zip(state['joint_names'], state['q']))
            for joint in reversed(chain):
                o = joint.find('origin')
                xyz = [float(v) for v in o.get('xyz', '0 0 0').split()] if o is not None else [0, 0, 0]
                rpy = [float(v) for v in o.get('rpy', '0 0 0').split()] if o is not None else [0, 0, 0]
                local = np.eye(4)
                local[:3, :3] = Rotation.from_euler('xyz', rpy).as_matrix()
                local[:3, 3] = xyz
                transform = transform @ local
                if joint.get('type') != 'fixed':
                    axis_node = joint.find('axis')
                    axis = np.array([float(v) for v in axis_node.get('xyz').split()] if axis_node is not None else [1, 0, 0])
                    axis /= np.linalg.norm(axis)
                    motion = np.eye(4)
                    q = measured[joint.get('name')]
                    if joint.get('type') == 'prismatic':
                        motion[:3, 3] = axis*q
                    else:
                        motion[:3, :3] = Rotation.from_rotvec(axis*q).as_matrix()
                    transform = transform @ motion
            actual = state['tcp_pose']
            position_error = float(np.linalg.norm(transform[:3, 3] - actual['position_m']))
            angle_error = float((Rotation.from_matrix(transform[:3, :3]).inv() *
                                 Rotation.from_quat(actual['quaternion_xyzw'])).magnitude())
            return dict(position_error_m=position_error, rotation_error_rad=angle_error,
                        passed=position_error < 1e-4 and angle_error < 1e-4)

        g = api.binding['semantics']['gripper']
        if g is None or not api.capabilities()['set_gripper']:
            raise BindingError('Position-driven gripper required for model semantics check')
        for fraction in (.2, .5, .8):
            low, high = g['width_range_m']
            width = low + fraction*(high-low)
            # This low-level API stages positions; the test owns a simple ramp.
            # A step jump is not a velocity-constrained motion primitive.
            current = api.observe()['q'][api.binding['joint_index'][g['master_joint']]]
            target_q = g['joint_offset'] + g['joint_per_m']*width
            master = next(j for j in descriptor['joints'] if j['name'] == g['master_joint'])
            increment = .5*master['limits']['velocity']*sim.get_physics_dt()
            steps = max(1, int(np.ceil(abs(target_q-current)/increment)))
            for step in range(1, steps+1):
                command = api.set_joint_targets({master['name']: current+(target_q-current)*step/steps})
                robot.write_data_to_sim()
                sim.step(render=False)
                robot.update(sim.get_physics_dt())
            command = api.set_gripper(width)
            for _ in range(120):
                robot.write_data_to_sim()
                sim.step(render=False)
                robot.update(sim.get_physics_dt())
            state = api.observe()
            error = abs(state['gripper']['width_m']-width)
            semantic_checks.append(dict(requested_width_m=width, measured=state['gripper'],
                                        joint_names=state['joint_names'], q=state['q'],
                                        ramp_steps=steps,
                                        width_error_m=error, passed=error < .001,
                                        tcp_fk=check_tcp(state), command=command))
            if error >= .001:
                break
    followers = [j['name'] for j in descriptor['joints'] if j['mimic']]
    rejected = []
    for n in followers:
        try:
            api.set_joint_targets({n: 0})
        except BindingError:
            rejected.append(n)
    assert rejected == followers
    args.output.parent.mkdir(parents=True, exist_ok=True)
    api_module = Path(__file__).resolve().parents[1] / 'src/datafarm/simulation/isaac_robot.py'
    discovery_module = api_module.with_name('robot_binding.py')
    provenance = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (Path(__file__), api_module, discovery_module,
                            api_module.with_name('robot_semantics.py'))}
    args.output.write_text(json.dumps(dict(binding=api.binding, before=before, after=after,
                                          command=result, passive_commands_rejected=rejected,
                                          model_semantics_checks=semantic_checks,
                                          code_sha256=provenance,
                                          versions={n: importlib.metadata.version(n) for n in ('isaaclab', 'torch', 'newton')},
                                          scope='live API integration; optional free-space width/FK check; no grasp/task acceptance'),
                                     indent=2, allow_nan=False), encoding='utf-8')
if any(not c['passed'] or not c['tcp_fk']['passed'] for c in semantic_checks):
    raise SystemExit('MODEL SEMANTICS CHECK FAILED: evidence saved to ' + str(args.output))
print('LIVE BINDING PASSED:', args.asset)

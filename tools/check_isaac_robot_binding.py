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

urdf, _ = asset_paths(args.asset)
descriptor = discover_urdf(Path(urdf).read_bytes())
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
                  for p in (Path(__file__), api_module, discovery_module)}
    args.output.write_text(json.dumps(dict(binding=api.binding, before=before, after=after,
                                          command=result, passive_commands_rejected=rejected,
                                          code_sha256=provenance,
                                          versions={n: importlib.metadata.version(n) for n in ('isaaclab', 'torch', 'newton')},
                                          scope='one-step live API integration; no task acceptance'),
                                     indent=2, allow_nan=False), encoding='utf-8')
print('LIVE BINDING PASSED:', args.asset)

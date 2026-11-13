"""Per-run fixed scene integration artifact. No scene-generation pipeline.

Task: reach a small two-joint waypoint, then return to the reset pose. This is
non-contact robot control, NOT grasping or manipulating the reference cube.
"""
from pathlib import Path
import argparse
import json
import time
import traceback
import sys
import hashlib
import random

parser=argparse.ArgumentParser()
parser.add_argument('--runtime-root',type=Path,required=True)
parser.add_argument('--package-root',type=Path,required=True)
parser.add_argument('--interpreter-root',type=Path,required=True)
parser.add_argument('--work',type=Path,required=True)
args,_=parser.parse_known_args()
sys.path[:0]=[str(args.runtime_root),str(args.package_root/'src'),str(args.interpreter_root)]
import warp as wp
wp.config.enable_backward=False
from isaaclab.app import AppLauncher,launch_simulation
AppLauncher.add_app_launcher_args(parser);args=parser.parse_args()
import torch
import isaaclab.sim as sim_utils
from isaaclab.scene import InteractiveScene,InteractiveSceneCfg
from isaaclab.assets import RigidObjectCfg
from isaaclab.utils import configclass
from robot_profiles import make_robot_cfg,make_simulation_cfg,asset_paths
from datafarm.simulation.robot_binding import discover_urdf
from datafarm.simulation.isaac_robot import IsaacRobotAPI,tensor
from dexcap.interpreter import Interpreter
from dexcap.config import RuntimeConfig


def save(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,allow_nan=False,indent=2));tmp.replace(path)


root=args.work;root.mkdir(exist_ok=True,parents=True)
for name in ('requests','responses','episodes'): (root/name).mkdir(exist_ok=True)
cfg=make_simulation_cfg('airbot_single','newton_mjwarp',export_model=str(root/'scene.xml'))
args.physics=None
@configclass
class SceneCfg(InteractiveSceneCfg):
    robot=make_robot_cfg('airbot_single','newton_mjwarp')
    table=RigidObjectCfg(prim_path='{ENV_REGEX_NS}/Table',
        spawn=sim_utils.CuboidCfg(size=(.8,.8,.04),
            rigid_props=sim_utils.UsdPhysicsRigidBodyCfg(kinematic_enabled=True),
            collision_props=[sim_utils.UsdPhysicsCollisionCfg()],
            mass_props=sim_utils.MassCfg(mass=1.)),
        init_state=RigidObjectCfg.InitialStateCfg(pos=(.4,0,-.08),rot=(0,0,0,1)))
    cube=RigidObjectCfg(prim_path='{ENV_REGEX_NS}/ReferenceCube',
        spawn=sim_utils.CuboidCfg(size=(.04,.04,.04),
            rigid_props=sim_utils.UsdPhysicsRigidBodyCfg(kinematic_enabled=True),
            collision_props=[sim_utils.UsdPhysicsCollisionCfg()],
            mass_props=sim_utils.MassCfg(mass=.03)),
        init_state=RigidObjectCfg.InitialStateCfg(pos=(.5,.15,-.04),rot=(0,0,0,1)))

with launch_simulation(cfg,args):
    sim=sim_utils.SimulationContext(cfg)
    scene=InteractiveScene(SceneCfg(num_envs=1,env_spacing=2.))
    sim.reset();robot=scene['robot']
    urdf,_=asset_paths('airbot_single')
    api=IsaacRobotAPI(robot,discover_urdf(Path(urdf).read_bytes()))
    names=api.binding['joint_names'];index=api.binding['joint_index']
    controlled=api.binding['position_driven_joints']
    arm_names=['left_joint1','left_joint2']
    dt=sim.get_physics_dt()
    code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    save(root/'ready.json',dict(ready=True,worker_sha256=code_hash,binding=api.binding,
                               task='joint-waypoint-and-return; non-contact',control_dt=dt))
    print('READY',flush=True)
    while not (root/'stop').exists():
        pending=[p for p in sorted((root/'requests').glob('*.json')) if not (root/'responses'/p.name).exists()]
        if not pending:
            time.sleep(.25);continue
        p=pending[0];req=json.loads(p.read_text());out=root/'episodes'/p.stem
        if out.exists():
            save(root/'responses'/p.name,dict(error='Prior attempt interrupted; not rerun'));continue
        out.mkdir();save(out/'request.json',req)
        try:
            home=tensor(robot.data.default_joint_pos).clone()
            rng=random.Random(req['seed'])
            home[0,index['left_joint1']]=rng.uniform(-.025,.025)
            home[0,index['left_joint2']]=rng.uniform(-.015,.015)
            for n in names:
                if 'g2_' in n or n=='left_g2_joint':
                    home[0,index[n]]=.012 if n=='left_g2_joint' else .006
            robot.write_joint_state_to_sim_index(position=home,velocity=torch.zeros_like(home))
            robot.reset();sim.forward()
            command=home.clone()
            def advance(target):
                global command
                command=command+torch.clamp(target-command,min=-.2*dt,max=.2*dt)
                api.set_joint_targets({n:float(command[0,index[n]]) for n in controlled})
                robot.write_data_to_sim();sim.step(render=False);robot.update(dt)
                scene['table'].update(dt);scene['cube'].update(dt)
            for _ in range(120):advance(home)
            origin=api.observe()['q']
            goal={arm_names[0]:origin[index[arm_names[0]]]+.12,
                  arm_names[1]:origin[index[arm_names[1]]]+.08}
            initial_targets={n:origin[index[n]] for n in arm_names}
            steps=0;reached=False;trace=[];states=[];commands=[]
            def observe():
                state=api.observe()
                objects={}
                for key in ('table','cube'):
                    obj=scene[key]
                    objects[key]={'position_m':tensor(obj.data.root_pos_w)[0].tolist(),
                                  'quaternion_xyzw':tensor(obj.data.root_quat_w)[0].tolist()}
                tool=api.binding['body_names'].index('left_link6')
                objects['robot.tool_link']={'position_m':tensor(robot.data.body_pos_w)[0,tool].tolist(),
                                           'quaternion_xyzw':tensor(robot.data.body_quat_w)[0,tool].tolist()}
                return dict(schema_version='privileged-state/1',control_step=steps,time_s=steps*dt,
                            frame_id='world',units={'length':'m','angle':'rad','time':'s'},
                            joint_names=names,joint_types=[next(j['type'] for j in api.binding['descriptor']['joints'] if j['name']==n) for n in names],
                            q=state['q'],dq=state['dq'],objects=objects,contacts=state['contacts'],validity={'source':'live_simulator'})
            states.append(observe())
            def drive(values,count=150):
                global steps,reached
                target=command.clone()
                for n,v in values.items():
                    if n not in arm_names or type(v) not in (int,float) or abs(v-initial_targets[n])>.3:
                        raise ValueError('Invalid joint or target outside task workspace')
                    target[0,index[n]]=v
                for _ in range(count):
                    if steps>=1800:raise RuntimeError('Physics step budget exceeded')
                    advance(target);steps+=1
                    s=observe();states.append(s)
                    commands.append({n:float(command[0,index[n]]) for n in controlled})
                    if max(abs(s['q'][index[n]]-goal[n]) for n in arm_names)<.025:reached=True
                s=states[-1]
                return {'ok':max(abs(s['q'][index[n]]-v) for n,v in values.items())<.03,
                        'measured':{n:s['q'][index[n]] for n in values}}
            def move_joint(name,target):return drive({name:target})
            def return_home():return drive(initial_targets)
            def wait(count):
                if type(count) is not int or not 1<=count<=150:raise ValueError('wait steps out of bounds')
                return drive({n:float(command[0,index[n]]) for n in arm_names},count)
            def traced(name,fn):
                def call(*a,**kw):
                    if len(trace)>=20:raise RuntimeError('Primitive call budget exceeded')
                    row=dict(event='primitive',event_id=len(trace),api=name,args=a,kwargs=kw,before=observe())
                    trace.append(row)
                    try:row['result']=fn(*a,**kw);return row['result']
                    except Exception as e:row['error']=dict(type=type(e).__name__,message=str(e));raise
                    finally:row['after']=observe()
                return call
            bindings={n:traced(n,f) for n,f in dict(observe=observe,move_joint=move_joint,return_home=return_home,wait=wait).items()}
            error=None
            try:Interpreter(bindings,RuntimeConfig(max_operations=30000,max_wall_seconds=60)).run(req['code'])
            except Exception as e:error=dict(type=type(e).__name__,message=str(e))
            final=observe()
            home_error=max(abs(final['q'][index[n]]-initial_targets[n]) for n in arm_names)
            success=reached and home_error<.03 and error is None
            result=dict(episode_id=p.stem,seed=req['seed'],phase=req['phase'],success=success,
                        score=float(success),evaluation=dict(reason='success' if success else 'waypoint_or_home_not_reached',
                        waypoint_reached=reached,home_error_rad=home_error),error=error,
                        trace=trace,privileged_state=dict(availability='available',initial=states[0],final=final),
                        physical_steps=steps,worker_sha256=code_hash)
            save(out/'result.json',result);save(out/'states.json',states);save(out/'commands.json',commands)
            save(root/'responses'/p.name,result)
            print('EPISODE',p.stem,success,steps,flush=True)
        except Exception:
            save(root/'responses'/p.name,dict(error=traceback.format_exc()))

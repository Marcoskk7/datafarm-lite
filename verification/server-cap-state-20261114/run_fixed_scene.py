"""Local coordinator for this per-run fixed-scene test; no credentials uploaded."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

parser=argparse.ArgumentParser()
parser.add_argument('--gateway-module-root',type=Path,required=True)
parser.add_argument('--connection-config',type=Path,required=True)
parser.add_argument('--provider-config',type=Path,required=True)
parser.add_argument('--remote-work',required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--resume-frozen',action='store_true',help='Continue skill verification after existing final selection; never rerun search')
args=parser.parse_args()
sys.path[:0]=[str(args.gateway_module_root),str(Path(__file__).resolve().parents[2]/'src')]
from unattended_gateway import connect
from datafarm.cap.codex_provider import build_provider
from datafarm.cap.io import digest,write_json,read_json
from datafarm.cap.search import SearchRunner,SearchConfig
from datafarm.cap.library import SkillLibrary


class SceneEngine:
    def __init__(self,client):self.client=client;self.records={}
    def evaluate(self,code,seeds,phase):
        episodes=[]
        for seed in seeds:
            request=dict(code=code,seed=seed,phase=phase)
            identity=digest(request);base=args.remote_work
            with self.client.open_sftp() as sftp:
                path=base+'/requests/'+identity+'.json'
                response=base+'/responses/'+identity+'.json'
                try:sftp.stat(path)
                except FileNotFoundError:
                    with sftp.open(path+'.tmp','w') as f:f.write(json.dumps(request))
                    sftp.posix_rename(path+'.tmp',path)
                deadline=time.monotonic()+300
                while True:
                    try:
                        with sftp.open(response) as f: row=json.loads(f.read())
                        break
                    except FileNotFoundError:
                        if time.monotonic()>deadline:raise TimeoutError('Native request pending; do not resubmit '+identity)
                        time.sleep(.5)
            if 'success' not in row:raise RuntimeError('Native runtime error: '+str(row.get('error')))
            self.records[identity]=row
            write_json(args.output/'native-results'/(identity+'.json'),row)
            summary={k:v for k,v in row.items() if k!='trace'}
            summary['trace']=[{k:r.get(k) for k in ('event_id','api','result','error')} for r in row['trace']]
            episodes.append(summary)
            print('PHYSICS',phase,seed,row['success'],row['physical_steps'],flush=True)
        return {'episodes':episodes}
    def inspect(self,episode,event_ids):
        rows=[r for r in self.records[episode]['trace'] if r['event_id'] in event_ids]
        if {r['event_id'] for r in rows}!=set(event_ids):raise ValueError('Unknown event')
        return rows


args.output.mkdir(parents=True,exist_ok=args.resume_frozen)
config=read_json(args.connection_config)
if not config.get('host') or type(config.get('port')) is not int:
    raise ValueError('Explicit SSH host and port required')
with connect(config) as client:
    with client.open_sftp() as sftp:
        with sftp.open(args.remote_work+'/ready.json') as f:ready=json.loads(f.read())
    write_json(args.output/'runtime-ready.json',ready)
    task=dict(name='airbot-joint-waypoint-return-fixed-scene',
              instruction='Non-contact robot control. Observe actual reset positions. First reach left_joint1 reset+0.12 rad AND left_joint2 reset+0.08 rad at the same time, then return_home. Do not manipulate the reference cube. Read actual states after each motion. Targets are absolute radians; task success is judged from actual joint trajectories, not API ok. Each candidate resets independently; do not hardcode reset values.',
              api={'observe':'observe() -> privileged-state/1; q and joint_names have matching order',
                   'move_joint':'move_joint(name, absolute_target_rad) -> {ok, measured}; 150 physical control steps, bounded slow motion; only left_joint1 and left_joint2',
                   'return_home':'return_home() -> {ok, measured}; return both task joints to actual settled initial positions in 150 steps',
                   'wait':'wait(control_steps) -> result; integer 1..150'},
              program_contract='Restricted Python with zero-argument main(). Only top-level function definitions, local scalar/list/dict variables, indexing, arithmetic, conditions/loops and supplied API/pure functions. No imports, attributes, methods, comprehensions, exceptions or file operations. No reset or judge API. Budget: 20 primitive calls, 1800 physics steps, 30000 interpreter operations. Read q by joint_names order; joint indices supplied in joint_index. A failed primitive may have changed state. Return values are dictionaries; do not assume a boolean.',
              joint_index=ready['binding']['joint_index'],
              joint_names=ready['binding']['joint_names'])
    if args.resume_frozen:
        if read_json(args.output/'public-task.json') != task:raise ValueError('Resume task changed')
    else:write_json(args.output/'public-task.json',task)
    engine=SceneEngine(client)
    provider=build_provider(read_json(args.provider_config))
    search=SearchConfig(debug_seeds=(0,),validation_seeds=(10,),verification_seeds=(30,),
                        max_generations=1,population=2,max_model_calls=12,
                        observation_mode='privileged_state',population_schedule='generation',
                        require_skill_attestation=True)
    runner=SearchRunner(provider,engine,SkillLibrary(args.output/'skills'),search)
    if args.resume_frozen:
        r=args.output/'search'
        if (r/'report.json').exists():raise ValueError('Completed search cannot be repaired by this resume route')
        winner=read_json(r/'frozen-program.json');history=read_json(r/'history.json')
        if (r/'program.py').read_text()!=winner['code']:raise ValueError('Frozen source changed')
        for p in (args.output/'native-results').glob('*.json'):
            row=read_json(p);engine.records[row['episode_id']]=row
        runner.calls=max(int(p.name) for p in (r/'decisions').iterdir())
        skill=runner.discover(task,history,winner,r)
        report=dict(schema='datafarm.cap.search/1',resumed_after='invalid skill requires field',
                    provider_decisions=runner.calls,candidates_evaluated=len(history),
                    program_sha256=winner['sha256'],debug_success_rate=winner['debug']['success_rate'],
                    validation_success_rate=winner['validation']['success_rate'],skill_admission=skill,
                    ready_for_harness_submission=winner['debug']['success_rate']==winner['validation']['success_rate']==1)
        write_json(r/'report.json',report)
    else:
        report=runner.run(task,args.output/'search',initial_program='def main():\n    move_joint("left_joint1", 0.0)\n')
    # Evaluate only after final code frozen; never return this result to provider.
    frozen=(args.output/'search/program.py').read_text()
    heldout=engine.evaluate(frozen,(20,),'evaluation')
    write_json(args.output/'heldout.json',heldout)
    replay=engine.evaluate(frozen,(20,),'replay')
    write_json(args.output/'replay.json',replay)
    result=dict(search=report,heldout=heldout,scope='real fixed-scene non-contact AIRBOT control; not grasping',
                frozen_program_sha256=hashlib.sha256(frozen.encode()).hexdigest())
    write_json(args.output/'result.json',result)
    with client.open_sftp() as sftp:
        with sftp.open(args.remote_work+'/stop','w') as f:f.write('done')
print(json.dumps({'ready':report['ready_for_harness_submission'],
                  'heldout_success':heldout['episodes'][0]['success']}),flush=True)

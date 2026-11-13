"""Separate-process recheck of frozen raw trajectory evidence; no simulator import."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,required=True)
args=parser.parse_args()
rows=[];steps=0
for folder in sorted((args.work/'episodes').iterdir()):
    result=json.loads((folder/'result.json').read_text())
    states=json.loads((folder/'states.json').read_text())
    commands=json.loads((folder/'commands.json').read_text())
    assert len(states)==len(commands)+1
    assert [s['control_step'] for s in states]==list(range(len(states)))
    names=states[0]['joint_names'];ids=[names.index(n) for n in ('left_joint1','left_joint2')]
    home=[states[0]['q'][i] for i in ids];target=[home[0]+.12,home[1]+.08]
    reached=any(max(abs(s['q'][i]-v) for i,v in zip(ids,target))<.025 for s in states[1:])
    error=max(abs(states[-1]['q'][i]-v) for i,v in zip(ids,home))
    expected=reached and error<.03 and result['error'] is None
    assert expected is result['success'],folder.name
    assert all(math.isfinite(v) for s in states for v in s['q']+s['dq'])
    steps+=len(commands)
    rows.append(dict(episode_id=folder.name,phase=result['phase'],seed=result['seed'],
                     success=expected,physical_steps=len(commands),home_error_rad=error,
                     max_abs_joint_speed=max(abs(v) for s in states for v in s['dq']),
                     evidence_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in folder.glob('*.json')}))
out=dict(reviewer_pid=os.getpid(),source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         scope='independent process recomputation of waypoint/home and action/state consistency; no visual or contact acceptance',
         physical_steps=steps,episodes=rows,all_declared_results_match=True)
(args.work/'independent-audit.json').write_text(json.dumps(out,indent=2))
print(json.dumps({'episodes':len(rows),'physical_steps':steps,'all_declared_results_match':True}))

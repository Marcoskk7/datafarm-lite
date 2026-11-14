"""State playback and HDF5 export of the fixed-scene run; no dynamics claim."""
import argparse
import json
import os
from pathlib import Path
import subprocess
os.environ['MUJOCO_GL']='egl'
import h5py
import mujoco
import numpy as np

parser=argparse.ArgumentParser()
parser.add_argument('--work',type=Path,required=True)
parser.add_argument('--episode',required=True)
parser.add_argument('--replay-episode',required=True)
args=parser.parse_args()
out=args.work/'delivery';out.mkdir(exist_ok=False)
states=json.loads((args.work/'episodes'/args.episode/'states.json').read_text())
commands=json.loads((args.work/'episodes'/args.episode/'commands.json').read_text())
replay=json.loads((args.work/'episodes'/args.replay_episode/'states.json').read_text())
assert len(states)==len(commands)+1 and len(states)==len(replay)
error=max(np.linalg.norm(np.array(a['objects']['robot.tool_link']['position_m'])-
                         np.array(b['objects']['robot.tool_link']['position_m'])) for a,b in zip(states,replay))
report={'matched_steps':len(states),'max_tool_link_position_error_m':float(error),
        'threshold_m':.01,'passed':bool(error<=.01),
        'scope':'same-seed frozen-program re-execution; separate from state playback and full applied-action replay'}
(out/'repeatability.json').write_text(json.dumps(report,indent=2))
with h5py.File(out/'episode.h5','w') as f:
    f.attrs['scope']='fixed-scene non-contact simulation; not real-ready training data'
    f.attrs['control_dt_s']=1/120
    f.create_dataset('state/control_step',data=[s['control_step'] for s in states])
    f.create_dataset('state/q',data=[s['q'] for s in states])
    f.create_dataset('state/dq',data=[s['dq'] for s in states])
    f.create_dataset('state/tool_link_position_m',data=[s['objects']['robot.tool_link']['position_m'] for s in states])
    names=list(commands[0]);f.attrs['control_names']=json.dumps(names)
    f.attrs['joint_names']=json.dumps(states[0]['joint_names'])
    f.create_dataset('action/applied',data=[[a[n] for n in names] for a in commands])
    f.create_dataset('action/interval_steps',data=[[k,k+1] for k in range(len(commands))])
model=mujoco.MjModel.from_xml_path(str(args.work/'scene.xml'))
data=mujoco.MjData(model)
mapping={}
for n in states[0]['joint_names']:
    matches=[i for i in range(model.njnt) if mujoco.mj_id2name(model,mujoco.mjtObj.mjOBJ_JOINT,i).endswith('_'+n)]
    assert len(matches)==1,(n,matches)
    mapping[n]=int(model.jnt_qposadr[matches[0]])
model.vis.global_.offwidth=640;model.vis.global_.offheight=480
model.vis.headlight.ambient[:]=.6;model.vis.headlight.diffuse[:]=.8
cam=mujoco.MjvCamera();cam.lookat[:]=[.35,0,.15];cam.distance=1.7;cam.azimuth=135;cam.elevation=-20
renderer=mujoco.Renderer(model,height=480,width=640)
video=out/'simulation-state-playback.mp4'
cmd=['ffmpeg','-y','-v','error','-threads','1','-filter_threads','1','-f','rawvideo','-pix_fmt','rgb24','-s','640x480','-r','24','-i','-',
     '-an','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(video)]
proc=subprocess.Popen(cmd,stdin=subprocess.PIPE)
frames=0
try:
    for k in range(0,len(states),5):
        state=states[k]
        for n,q in zip(state['joint_names'],state['q']):data.qpos[mapping[n]]=q
        mujoco.mj_forward(model,data)
        renderer.update_scene(data,camera=cam)
        image=renderer.render()
        if frames==0:
            from PIL import Image
            Image.fromarray(image).save(out/'first-frame.png')
        if k+5>=len(states):
            from PIL import Image
            Image.fromarray(image).save(out/'last-frame.png')
        proc.stdin.write(image.tobytes());frames+=1
finally:
    proc.stdin.close();renderer.close()
assert proc.wait()==0
(out/'render.json').write_text(json.dumps({'frames':frames,'fps':24,'source_step_stride':5,
    'scope':'MuJoCo rendering of Newton-exported scene at recorded joint states; not a different simulation backend',
    'video':video.name},indent=2))
print(json.dumps({'frames':frames,'repeatability':report}),flush=True)

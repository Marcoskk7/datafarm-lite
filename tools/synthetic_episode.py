"""造一条合成 episode（只有 state_trace，不跑物理），用来在 B 线 episode 就绪前打通渲染链路。

关节按平滑余弦从 SceneGraph 初值走到限位内的目标，抽屉按 `--drawer` 拉开，其余实体保持布局初值。
产物标记 `setup.synthetic=true`、`judge_result.outcome=aborted`：它不是执行结果，不能进数据集。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from datafarm.kernel.artifacts import ArtifactDocument, ArtifactRef, IRKind  # noqa: E402
from datafarm.kernel.io import atomic_write_json, utc_now  # noqa: E402
from datafarm.kernel.store import ArtifactStore  # noqa: E402

# 单臂 AIRBOT 的一组目标（rad / m），都在 URDF 限位内：抬臂、转腕、张开夹爪。
ARM_TARGET = {'left_joint1': 0.6, 'left_joint2': -0.8, 'left_joint3': 1.2, 'left_joint4': 0.5,
              'left_joint5': 0.6, 'left_joint6': 1.0, 'left_g2_joint': 0.06}


def build(graph: dict, graph_ref: dict, *, steps: int, drawer: dict[str, float]) -> dict:
    joints = list(graph['robot']['controlled_joints'])
    missing = [j for j in joints if j not in ARM_TARGET]
    if missing:
        raise SystemExit(f'no synthetic target for joints {missing}')
    dynamic = [n for n in graph['nodes'] if n['semantic_tag'] == 'plate']
    start_drawer = {k: v for n in graph['nodes'] for k, v in (n.get('joint_state') or {}).items()}
    trace = []
    for k in range(steps + 1):
        s = 0.5 - 0.5 * math.cos(math.pi * k / steps)
        q = [s * ARM_TARGET[j] for j in joints]
        trace.append({
            'control_step': k, 'q': q, 'dq': [0.0] * len(joints), 'valid': True,
            'entities': {n['node_id']: {'pose': dict(n['pose']), 'linear_velocity': [0.0, 0.0, 0.0],
                                        'angular_velocity': [0.0, 0.0, 0.0]} for n in dynamic},
            'articulations': {name: start + s * (drawer.get(name, start) - start)
                              for name, start in start_drawer.items()},
            'gripper': {'width_m': q[joints.index('left_g2_joint')], 'master': q[joints.index('left_g2_joint')]},
        })
    actions = [{'call_id': 'synthetic', 'interval_steps': [k, k + 1], 'mode': 'joint_position_target',
                'control_names': joints, 'requested': trace[k + 1]['q'], 'applied': trace[k + 1]['q']}
               for k in range(steps)]
    return {
        'setup': {'synthetic': True, 'scene_graph': graph_ref, 'backend': dict(graph['backend']),
                  'control_dt_s': 1 / 120, 'joint_names': joints, 'control_names': joints, 'seed': 0,
                  'judge_result': {'outcome': 'aborted', 'success': None, 'terminated': False, 'truncated': False,
                                   'reason_code': 'SYNTHETIC_STATE_TRACE'}},
        'actions': actions, 'state_trace': trace,
        'contacts': {'availability': 'not_requested', 'records': []},
        'events': [], 'frame_map': [],
        'replay': {'state_playback': 'unverified', 'action_replay': 'unverified'},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, required=True, help='scene_assemble work dir (store/, check.json)')
    parser.add_argument('--steps', type=int, default=240, help='control steps (120 Hz)')
    parser.add_argument('--drawer', default='{"joint_part_01_joint_00": 0.0}',
                        help='JSON {joint: final value}; other articulation joints stay at their layout value')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    store = ArtifactStore(args.work / 'store')
    graph_ref = json.loads((args.work / 'check.json').read_text())['scene_graph']
    graph = store.load(ArtifactRef.from_dict(graph_ref))
    payload = build(graph, graph_ref, steps=args.steps, drawer=json.loads(args.drawer))
    document = ArtifactDocument(kind=IRKind.EPISODE, artifact_id='episode.synthetic_0000', created_at=utc_now(),
                                producer='a.synthetic-episode', predecessors=(ArtifactRef.from_dict(graph_ref),),
                                evidence=(), payload=payload)
    ref = store.put_document(document)
    atomic_write_json(args.out, {'episode': ref.to_dict()})
    print(json.dumps({'episode': ref.to_dict(), 'steps': args.steps}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

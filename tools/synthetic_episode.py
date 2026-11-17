"""造一条合成 episode（只有 state_trace，不跑物理），用来在 B 线 episode 就绪前打通渲染链路。

关节按平滑余弦从 0 走到限位内的目标，抽屉按 `--drawer` 拉开，其余实体保持布局初值。
每条 state 都是 docs/ir/episode.md#state_trace 的规范条目（`datafarm.ir.episode_state` 校验），但只填回放需要的量：
速度为 0、无 link 位姿、无 TCP/夹爪读数、接触 unavailable，机器人实体的 `joints` 为空（关节在 `robot` 段）。
产物标记 `setup.synthetic=true`、`termination.status=aborted`：它不是执行结果，不能进数据集。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from datafarm.ir.episode_state import UNITS, validate_state  # noqa: E402
from datafarm.kernel.artifacts import ArtifactDocument, ArtifactRef, IRKind  # noqa: E402
from datafarm.kernel.io import atomic_write_json, utc_now  # noqa: E402
from datafarm.kernel.store import ArtifactStore  # noqa: E402

# 单臂 AIRBOT 的一组目标（rad / m），都在 URDF 限位内：抬臂、转腕、张开夹爪。
ARM_TARGET = {'left_joint1': 0.6, 'left_joint2': -0.8, 'left_joint3': 1.2, 'left_joint4': 0.5,
              'left_joint5': 0.6, 'left_joint6': 1.0, 'left_g2_joint': 0.06}
DT_S = 1 / 120


def build(graph: dict, graph_ref: dict, records: dict[str, dict], *, steps: int, drawer: dict[str, float]) -> dict:
    """`records[asset_id]` 是 AssetRecord；非机器人铰接体的关节类型/单位从这里取。"""
    joints = list(graph['robot']['controlled_joints'])
    missing = [j for j in joints if j not in ARM_TARGET]
    if missing:
        raise SystemExit(f'no synthetic target for joints {missing}')
    node_ids = [n['node_id'] for n in graph['nodes']]
    trace, actions = [], []
    for k in range(steps + 1):
        s = 0.5 - 0.5 * math.cos(math.pi * k / steps)
        q = [s * ARM_TARGET[j] for j in joints]
        entities = {}
        for node in graph['nodes']:
            entry = {'availability': 'available', 'asset_id': node['asset_id'],
                     'position_m': list(node['pose']['position_m']),
                     'quaternion_xyzw': list(node['pose']['quaternion_xyzw']),
                     'linear_velocity_m_s': [0.0, 0.0, 0.0], 'angular_velocity_rad_s': [0.0, 0.0, 0.0],
                     'joints': [], 'links': {}}
            if node['semantic_tag'] != 'robot':
                start = node.get('joint_state') or {}
                for joint in records[node['asset_id']]['joints']:
                    if joint['type'] == 'fixed':
                        continue
                    begin = start.get(joint['name'], 0.0)
                    entry['joints'].append({'name': joint['name'], 'type': joint['type'],
                                            'unit': 'm' if joint['type'] == 'prismatic' else 'rad',
                                            'q': begin + s * (drawer.get(joint['name'], begin) - begin), 'dq': 0.0})
            entities[node['node_id']] = entry
        trace.append(validate_state({
            'control_step': k, 'time_s': k * DT_S, 'units': dict(UNITS), 'frame_id': 'world',
            'entities': entities, 'robot': {'joint_names': joints, 'q': q, 'dq': [0.0] * len(joints)},
            'contacts': {'control_step': k, 'sampling': 'cycle_end_snapshot', 'availability': 'unavailable',
                         'rows': None, 'coverage': 'unknown', 'reason_code': 'SYNTHETIC_STATE_TRACE'},
        }, node_ids=node_ids, step=k, dt=DT_S))
        if k:
            target = dict(zip(joints, q))
            actions.append({'control_step': k - 1, 'requested_joint_targets': target,
                            'applied_joint_targets': dict(target), 'application': 'synthetic'})
    return {
        'setup': {'synthetic': True, 'scene_graph': graph_ref, 'backend': dict(graph['backend']),
                  'control_dt_s': DT_S, 'node_ids': node_ids, 'seed': 0},
        'actions': actions, 'state_trace': trace, 'contacts': [s['contacts'] for s in trace],
        'events': [], 'frame_map': [],
        'replay': {'state_playback': 'unverified', 'action_replay': 'unverified'},
        'termination': {'status': 'aborted', 'reason_code': 'SYNTHETIC_STATE_TRACE', 'trace_complete': True},
        'judgement': 'unverified',
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
    records = {a['asset_id']: store.load(ArtifactRef.from_dict(a['record'])) for a in graph['assets']}
    payload = build(graph, graph_ref, records, steps=args.steps, drawer=json.loads(args.drawer))
    document = ArtifactDocument(kind=IRKind.EPISODE, artifact_id='episode.synthetic_0000', created_at=utc_now(),
                                producer='a.synthetic-episode', predecessors=(ArtifactRef.from_dict(graph_ref),),
                                evidence=(), payload=payload)
    ref = store.put_document(document)
    atomic_write_json(args.out, {'episode': ref.to_dict()})
    print(json.dumps({'episode': ref.to_dict(), 'steps': args.steps}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

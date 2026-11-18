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
CONTROL_HZ = 120
UNITS = {'length': 'm', 'angle': 'rad', 'time': 's'}


def _joint_types(graph: dict, records: dict) -> dict[tuple[str, str], str]:
    """(node_id, 关节名) -> 类型，取自该节点的 AssetRecord；SceneGraph 的 joint_state 只是声明意图。"""
    types = {}
    for node in graph['nodes']:
        if node['semantic_tag'] == 'robot':
            continue
        source = {j['name']: j for j in records[node['asset_id']]['joints']}
        for name in node.get('joint_state') or {}:
            joint = source.get(name)
            if joint is None:
                raise SystemExit(f"{node['node_id']}: joint {name!r} is not in {node['asset_id']}")
            types[(node['node_id'], name)] = joint['type']
    return types


def build(graph: dict, graph_ref: dict, *, steps: int, drawer: dict[str, float], records: dict) -> dict:
    joints = list(graph['robot']['controlled_joints'])
    missing = [j for j in joints if j not in ARM_TARGET]
    if missing:
        raise SystemExit(f'no synthetic target for joints {missing}')
    nodes = [n for n in graph['nodes'] if n['semantic_tag'] != 'robot']
    types = _joint_types(graph, records)
    trace = []
    for k in range(steps + 1):
        s = 0.5 - 0.5 * math.cos(math.pi * k / steps)
        q = [s * ARM_TARGET[j] for j in joints]
        entities = {}
        for node in nodes:
            pose = node['pose']
            if pose['frame_id'] != 'world':
                raise SystemExit(f"{node['node_id']}: synthetic entities need world-frame poses")
            entities[node['node_id']] = {
                'availability': 'available', 'asset_id': node['asset_id'],
                'position_m': list(pose['position_m']), 'quaternion_xyzw': list(pose['quaternion_xyzw']),
                'linear_velocity_m_s': [0.0, 0.0, 0.0], 'angular_velocity_rad_s': [0.0, 0.0, 0.0],
                'joints': [{'name': name, 'type': types[(node['node_id'], name)],
                            'unit': 'm' if types[(node['node_id'], name)] == 'prismatic' else 'rad',
                            'q': start + s * (drawer.get(name, start) - start), 'dq': 0.0}
                           for name, start in (node.get('joint_state') or {}).items()],
                'links': {},
            }
        trace.append({
            'control_step': k, 'time_s': k / CONTROL_HZ, 'units': dict(UNITS), 'frame_id': 'world',
            'entities': entities,
            'robot': {'joint_names': joints, 'q': q, 'dq': [0.0] * len(q)},
            'contacts': {'availability': 'unavailable', 'rows': None, 'coverage': 'unknown',
                         'sampling': 'cycle_end_snapshot', 'control_step': k,
                         'force_availability': 'unavailable', 'normal_availability': 'unavailable',
                         'reason_code': 'SYNTHETIC_STATE_TRACE'},
        })
    actions = [{'call_id': 'synthetic', 'interval_steps': [k, k + 1], 'mode': 'joint_position_target',
                'control_names': joints, 'requested': trace[k + 1]['robot']['q'],
                'applied': trace[k + 1]['robot']['q']}
               for k in range(steps)]
    return {
        'setup': {'synthetic': True, 'scene_graph': graph_ref, 'backend': dict(graph['backend']),
                  'control_dt_s': 1 / CONTROL_HZ, 'joint_names': joints, 'control_names': joints, 'seed': 0,
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
    records = {entry['asset_id']: store.load(ArtifactRef.from_dict(entry['record'])) for entry in graph['assets']}
    payload = build(graph, graph_ref, steps=args.steps, drawer=json.loads(args.drawer), records=records)
    document = ArtifactDocument(kind=IRKind.EPISODE, artifact_id='episode.synthetic_0000', created_at=utc_now(),
                                producer='a.synthetic-episode', predecessors=(ArtifactRef.from_dict(graph_ref),),
                                evidence=(), payload=payload)
    ref = store.put_document(document)
    atomic_write_json(args.out, {'episode': ref.to_dict()})
    print(json.dumps({'episode': ref.to_dict(), 'steps': args.steps}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""A-line end-to-end verification: asset retrieval, scene assembly, Isaac Lab checks, SceneGraph.

    python tools/verify_a_line.py --spec examples/scene/plate-drawer-airbot.json \
        --cache /data/simready-cache --robot-package /data/robot_assets_isaaclab3_validated_20261113 \
        --work WORK [--offline] [--skip-tests]

Runs every gate in order and stops at the first failure that later gates depend on:

    env       interpreter has numpy; records whether pxr/coacd/isaaclab are present
    cache     every SimReady asset in the spec is fetched and re-hashes clean against its own manifest
    index     online: the dataset index answers, and `probe` reports each asset's joints and license
    prepare   AssetRecords, collision wrappers, robot base bbox
    plan      layout, footprint, IK reachability (numpy only; no simulator)
    check     Isaac Lab 3.0: initial joint state, settle, penetration, drawer probe, SceneGraph
    graph     the SceneGraph passes the IR validator in src/datafarm/ir/scene_graph.py
    tests     CPU test suite

`--offline` skips the network gate only; everything else is local. Writes `verify-a-line.json`
into WORK next to the stage outputs. Exit status is 0 when every gate that ran passed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from datafarm.kernel.artifacts import ArtifactRef  # noqa: E402
from datafarm.kernel.io import atomic_write_json, read_json, sha256_file  # noqa: E402
from datafarm.kernel.store import ArtifactStore  # noqa: E402

class Report:
    def __init__(self) -> None:
        self.gates: list[dict] = []

    def add(self, name: str, status: str, detail: str, evidence: dict | None = None) -> None:
        self.gates.append({'gate': name, 'status': status, 'detail': detail, 'evidence': evidence or {}})

    def step(self, name: str) -> None:
        print(f'  .. {name}', flush=True)

    def failed(self) -> list[str]:
        return [g['gate'] for g in self.gates if g['status'] == 'FAIL']

    def print_table(self) -> None:
        print()
        for gate in self.gates:
            print(f'{gate["status"]:<4} {gate["gate"]:<9} {gate["detail"]}')
        ok = not self.failed()
        print('\nA-line verification:', 'PASS' if ok else 'FAIL', f'({len(self.gates)} gates)')
        if self.failed():
            print('failed gates:', ', '.join(self.failed()))


def stage(cmd: list[str], *, env: dict[str, str], log: Path) -> tuple[int, str]:
    started = time.time()
    with log.open('w', encoding='utf-8') as handle:
        handle.write('$ ' + ' '.join(cmd) + '\n\n')
        handle.flush()
        done = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    tail = [line for line in log.read_text(encoding='utf-8').splitlines()[1:] if line.strip()][-6:]
    return done.returncode, f'{time.time() - started:.0f}s; ' + ' | '.join(tail)


def gate_env(report: Report) -> bool:
    detail = f'python {sys.version.split()[0]} at {sys.executable}'
    try:
        import numpy  # noqa: F401
    except ImportError as error:
        report.add('env', 'FAIL', f'{detail}: numpy missing ({error})')
        return False
    present = {module: importlib.util.find_spec(module) is not None for module in ('pxr', 'coacd', 'isaaclab')}
    missing = [module for module, found in present.items() if not found]
    detail = (f'python {sys.version.split()[0]} at {sys.executable}; '
              + ' '.join(f'{module}={found}' for module, found in present.items()))
    if missing:
        report.add('env', 'FAIL', f'{detail}; missing {", ".join(missing)} (use the Isaac Lab 3.0 venv)', present)
        return False
    report.add('env', 'PASS', detail, present)
    return True


def _rehash(report: Report, name: str, spec: dict, cache: Path) -> bool:
    from datafarm.adapters.assets.simready import SimReadyError, verify_fetch
    rows, ok = [], True
    for asset in spec['assets']:
        root = cache / asset['simready']
        try:
            manifest = verify_fetch(root)
        except (OSError, SimReadyError) as error:
            rows.append({'asset': asset['simready'], 'ok': False, 'error': str(error)})
            ok = False
            continue
        rows.append({'asset': asset['simready'], 'ok': True, 'files': len(manifest['files']),
                     'license': manifest['license'], 'revision': manifest['revision'],
                     'retrieved_at': manifest['retrieved_at'], 'package_id': manifest['package_id']})
    detail = '; '.join(f'{r["asset"]} {"ok" if r["ok"] else "FAIL"}' for r in rows)
    report.add(name, 'PASS' if ok else 'FAIL', detail, {'assets': rows})
    return ok


def gate_cache(report: Report, spec: dict, cache: Path) -> bool:
    return _rehash(report, 'cache', spec, cache)


def gate_source(report: Report, spec: dict, cache: Path) -> bool:
    """同样的重算，在 prepare/plan/check 之后再跑一次：源资产必须是原始字节。"""
    return _rehash(report, 'source', spec, cache)


def gate_index(report: Report, spec: dict, endpoint: str) -> bool:
    from datafarm.adapters.assets.simready import SimReadyClient
    client = SimReadyClient(endpoint=endpoint)
    try:
        revision = client.pin()
        rows = []
        for asset in spec['assets']:
            candidate = client.probe(asset['simready'])
            rows.append({'asset': asset['simready'], 'license': candidate.license,
                         'collection': candidate.collection, 'joints': len(candidate.joints)})
    except Exception as error:  # network and dataset errors are both gate failures
        report.add('index', 'FAIL', f'{endpoint}: {error}')
        return False
    drawer = next(r for r in rows if r['joints'])
    detail = f'revision {revision[:12]}; ' + '; '.join(f'{r["asset"]} {r["joints"]}j {r["license"]}' for r in rows)
    report.add('index', 'PASS', detail, {'endpoint': endpoint, 'revision': revision, 'assets': rows})
    return drawer['joints'] >= 1


def gate_prepare(report: Report, work: Path) -> bool:
    index = work / 'prepared.json'
    if not index.is_file():
        report.add('prepare', 'FAIL', 'prepared.json missing after stage')
        return False
    prepared = read_json(index)
    store = ArtifactStore(work / 'store')
    bodies = {k: v['body'] for k, v in prepared['assets'].items()}
    robot = next(k for k, v in prepared['assets'].items() if v['body'] == 'robot')
    ok = True
    deltas = {}
    for asset_id, asset in prepared['assets'].items():
        try:
            record = store.load(ArtifactRef.from_dict(asset['record']))
            deltas[asset_id] = {'joints': len(record.get('joints', [])),
                                'collision': 'base_bbox_min_m' in record['collision']}
        except Exception as error:  # a record that cannot be reloaded is a prepare failure
            deltas[asset_id] = {'error': f'{type(error).__name__}: {error}'}
            ok = False
    detail = ', '.join(f'{k}={v}' for k, v in bodies.items())
    report.add('prepare', 'PASS' if ok else 'FAIL',
               f'{detail}; records reload from store; {len(prepared["bundle_files"])} bundle files', {
                   'bodies': bodies, 'robot': robot, 'records': deltas,
                   'spec_sha256': prepared['spec_sha256']})
    return ok


def gate_plan(report: Report, work: Path) -> bool:
    plan = read_json(work / "plan.json")
    reach, footprint = plan['reachability'], plan['footprint']
    solved = sum(1 for row in reach['targets'] if row['ok'])
    ok = reach['ok'] and footprint['ok']
    report.add('plan', 'PASS' if ok else 'FAIL',
               f'{solved}/{len(reach["targets"])} targets reachable, footprint {"ok" if footprint["ok"] else footprint["problems"]}',
               {'base_pose': plan['robot_base_pose'], 'targets': reach['targets'], 'footprint': footprint})
    return ok


def gate_check(report: Report, work: Path) -> bool:
    check = read_json(work / "check.json")
    settle, penetration = check['settle'], check['penetration']
    ok = settle['stable'] and penetration['ok'] and check['probes_ok']
    detail = (f'drift {settle["max_drift_m"]} m, penetration {penetration["max_depth_m"]} m, '
              f'probes {"ok" if check["probes_ok"] else "FAIL"}')
    report.add('check', 'PASS' if ok else 'FAIL', detail,
               {'settle': settle, 'penetration': penetration, 'probes': check['probes'],
                'initial_joint_state': check.get('initial_joint_state')})
    return ok


def gate_graph(report: Report, work: Path) -> bool:
    from datafarm.ir import scene_graph
    graph = read_json(work / "scene-graph.json")
    try:
        scene_graph.validate(graph)
    except Exception as error:
        report.add('graph', 'FAIL', f'{type(error).__name__}: {error}')
        return False
    checks = graph['checks']
    report.add('graph', 'PASS',
               f'{graph["schema"]} {len(graph["nodes"])} nodes, backend {graph["backend"]["name"]} '
               f'{graph["backend"]["version"]}',
               {'checks': checks, 'sha256': sha256_file(work / 'scene-graph.json')})
    return bool(checks['settle']['stable'] and checks['penetration']['ok'] and checks['reachability']['ok'])


def gate_tests(report: Report, env: dict[str, str], python: str) -> bool:
    log = ROOT / 'verification' / 'a-line' / 'pytest-a-line.txt'
    log.parent.mkdir(parents=True, exist_ok=True)
    code, tail = stage([python, '-m', 'pytest', '-q',
                        'tests/test_scene_layout.py', 'tests/test_simready_fetch.py'],
                       env=env, log=log)
    report.add('tests', 'PASS' if code == 0 else 'FAIL', f'{python}: {tail}')
    return code == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--spec', type=Path, default=ROOT / 'examples/scene/plate-drawer-airbot.json')
    parser.add_argument('--cache', type=Path, required=True, help='SimReady fetch cache (one directory per asset)')
    parser.add_argument('--robot-package', type=Path, required=True, help='validated AIRBOT asset package')
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--endpoint', default=os.environ.get('HF_ENDPOINT') or 'https://hf-mirror.com')
    parser.add_argument('--offline', action='store_true', help='skip the dataset-index gate')
    parser.add_argument('--skip-tests', action='store_true')
    parser.add_argument('--tests-python', default=sys.executable,
                        help='interpreter that has pytest and numpy for the CPU test gate; '
                             'the Isaac venv may not have pytest')
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text(encoding='utf-8'))
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'CUDA_VISIBLE_DEVICES': ''}
    report = Report()

    print(f'A-line verification: {args.spec} -> {work}', flush=True)
    report.step('env')
    if not gate_env(report):
        report.print_table()
        return 1
    report.step('cache')
    if not gate_cache(report, spec, args.cache):
        report.print_table()
        return 1
    if args.offline:
        report.add('index', 'SKIP', '--offline')
    else:
        report.step('index')
        if not gate_index(report, spec, args.endpoint):
            report.print_table()
            return 1

    assemble = [sys.executable, str(ROOT / 'tools/scene_assemble.py')]
    spec_args = ['--spec', str(args.spec), '--work', str(work)]
    prepare_cmd = assemble + ['prepare', *spec_args, '--cache', str(args.cache),
                              '--robot-package', str(args.robot_package)]
    report.step('prepare')
    code, tail = stage(prepare_cmd, env=env, log=work / 'prepare.log')
    if code != 0:
        report.add('prepare', 'FAIL', f'exit {code}; {tail}')
        report.print_table()
        return 1
    if not gate_prepare(report, work):
        report.print_table()
        return 1

    report.step('plan')
    code, tail = stage(assemble + ['plan', *spec_args], env=env, log=work / 'plan.log')
    if code != 0:
        report.add('plan', 'FAIL', f'exit {code}; {tail}')
        report.print_table()
        return 1
    if not gate_plan(report, work):
        report.print_table()
        return 1

    # Isaac needs a real GPU; do not inherit the CPU-only setting used for the first two stages.
    check_env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    report.step('check')
    code, tail = stage(assemble + ['check', *spec_args, '--headless'], env=check_env, log=work / 'check.log')
    if code != 0:
        report.add('check', 'FAIL', f'exit {code}; {tail}')
        report.print_table()
        return 1
    if not gate_check(report, work):
        report.print_table()
        return 1
    report.step('graph')
    if not gate_graph(report, work):
        report.print_table()
        return 1
    report.step('source')
    if not gate_source(report, spec, args.cache):
        report.print_table()
        return 1

    if args.skip_tests:
        report.add('tests', 'SKIP', '--skip-tests')
    else:
        report.step('tests')
        gate_tests(report, {**env, 'PYTHONPATH': f'{ROOT / "src"}:{ROOT / "tests"}'}, args.tests_python)

    report.print_table()
    atomic_write_json(work / 'verify-a-line.json', {
        'schema': 'datafarm.a-line-verification/1', 'spec': str(args.spec),
        'spec_sha256': sha256_file(args.spec), 'work': str(work), 'cache': str(args.cache),
        'endpoint': args.endpoint, 'offline': args.offline, 'passed': not report.failed(),
        'gates': report.gates})
    return 0 if not report.failed() else 1


if __name__ == '__main__':
    sys.exit(main())

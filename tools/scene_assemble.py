#!/usr/bin/env python3
"""A-line scene assembly: SimReady assets + layout spec -> AssetRecords, reachability, Isaac checks, SceneGraph.

    # 1. pxr (+coacd) Python, e.g. the Isaac Lab venv: inspect assets, build collision wrappers, AssetRecords
    python tools/scene_assemble.py prepare --spec examples/scene/plate-drawer-airbot.json \
        --cache /data/simready-cache --robot-package /data/robot_assets_isaaclab3_validated_20261113 --work WORK
    # 2. any Python with numpy: resolve layout and check arm reachability (fast; iterate on the spec here)
    python tools/scene_assemble.py plan --spec ... --work WORK
    # 3. Isaac Lab 3.0 Python: settle, penetration and placement probes, then write the SceneGraph
    python tools/scene_assemble.py check --spec ... --work WORK [--headless]

WORK holds `store/` (content-addressed artifacts) and small JSON indexes (`prepared.json`, `plan.json`,
`check.json`, `scene-graph.json`). Host paths live only in `prepared.json`, never in IR documents.
Exit status is 0 only when every check that ran passed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from datafarm.kernel.io import atomic_write_json, sha256_file  # noqa: E402
from datafarm.kernel.store import ArtifactStore  # noqa: E402
from datafarm.entry.scene_files import load_prepared as _records, robot_descriptor  # noqa: E402

USD_MEDIA = 'model/vnd.usd'
USDA_MEDIA = 'model/vnd.usda'


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def prepare(args) -> int:
    from datafarm.adapters.sim import usd_assets
    from datafarm.scene import asset_records
    spec = load_json(args.spec)
    work = Path(args.work)
    store = ArtifactStore(work / 'store')
    wrappers_dir = work / 'collision'
    prepared = {'spec_sha256': sha256_file(args.spec), 'assets': {}, 'bundle_files': []}
    for asset in spec['assets']:
        root = Path(args.cache) / asset['simready']
        fetch = load_json(root / usd_assets.FETCH_MANIFEST)
        inspection = usd_assets.inspect_asset(root)
        report = usd_assets.prepare_collision(root, wrappers_dir, Path(args.cache) / '.hull-cache')
        wrapper = wrappers_dir / report['wrapper']
        if asset['body'] == 'articulation':
            usd_assets.anchor_to_world(wrapper, asset['anchor_body'])
            report['world_anchor_body'] = asset['anchor_body']
        root_usd = root / inspection['root_usd']
        usd_ref = store.put_bytes('asset.usd', root_usd.read_bytes(), media_type=USD_MEDIA)
        wrapper_ref = store.put_bytes('asset.collision-wrapper', wrapper.read_bytes(), media_type=USDA_MEDIA)
        inspection_ref = store.put_json('asset.usd-inspection', inspection)
        report['wrapper_sha256'] = wrapper_ref.sha256
        collision_ref = store.put_json('asset.collision-prep', report)
        record = asset_records.build_record(
            asset_id=asset['asset_id'], role=asset['role'], fetch=fetch, inspection=inspection, usd_ref=usd_ref,
            inspection_ref=inspection_ref, collision_ref=collision_ref,
            collision_bbox=(report['hull_bbox_min_m'], report['hull_bbox_max_m']),
            notes=f"collision: CoACD convex hulls {report['coacd']} replace the upstream meshes for MuJoCo Warp")
        record_ref = store.put_json('ir.asset-record', record)
        prepared['assets'][asset['asset_id']] = {
            'record': record_ref.to_dict(), 'body': asset['body'], 'wrapper': str(wrapper.resolve()),
            'fetch_bom_sha256': fetch['bom_sha256'], 'revision': fetch['revision']}
        prepared['bundle_files'] += [
            {'asset_id': asset['asset_id'], 'role': 'wrapper', 'sha256': wrapper_ref.sha256},
            *({'asset_id': asset['asset_id'], 'role': 'simready', 'member': f['relative_path'],
               'sha256': f['sha256']} for f in fetch['files'])]
        print(f"{asset['asset_id']}: record {record_ref.sha256[:12]} bodies "
              f"{ {k.rsplit('/', 1)[-1]: v['mass_kg'] for k, v in report['bodies'].items()} }", flush=True)
    robot = spec['robot']
    package = Path(args.robot_package)
    descriptor, urdf = robot_descriptor(package, robot['package_asset'])
    delivery = load_json(package / 'delivery_manifest.json')
    usd = package / delivery['assets'][robot['package_asset']]['usd']
    urdf_ref = store.put_bytes('robot.urdf', urdf.read_bytes(), media_type='application/urdf+xml')
    usd_ref = store.put_bytes('robot.usd', usd.read_bytes(), media_type=USDA_MEDIA)
    base_collision = usd_assets.fixed_base_collision_bbox(usd)
    print(f"{robot['asset_id']}: base collision bbox {base_collision['bbox_min_m']} {base_collision['bbox_max_m']}",
          flush=True)
    record = asset_records.robot_record(
        asset_id=robot['asset_id'], descriptor=descriptor, urdf_ref=urdf_ref, usd_ref=usd_ref, license='unknown',
        retrieved_at=args.robot_retrieved_at, uri=f"local:robot_assets_isaaclab3_validated_20261113/{robot['package_asset']}",
        base_collision=base_collision)
    record_ref = store.put_json('ir.asset-record', record)
    prepared['assets'][robot['asset_id']] = {'record': record_ref.to_dict(), 'body': 'robot',
                                             'package': str(package.resolve()), 'package_asset': robot['package_asset']}
    for layer, digest in delivery.get('layer_sha256', {}).items():
        if f"/{robot['package_asset']}/" in layer:
            prepared['bundle_files'].append({'asset_id': robot['asset_id'], 'role': 'robot_layer',
                                             'member': layer, 'sha256': digest})
    atomic_write_json(work / 'prepared.json', prepared)
    print(f"prepared {len(prepared['assets'])} assets -> {work / 'prepared.json'}")
    return 0


def plan(args) -> int:
    from datafarm.scene import planning
    spec = load_json(args.spec)
    work = Path(args.work)
    _, prepared, records = _records(work)
    spec_sha256 = sha256_file(args.spec)
    if prepared['spec_sha256'] != spec_sha256:
        print('note: spec changed since prepare; asset records are reused', file=sys.stderr)
    descriptor, _ = robot_descriptor(Path(prepared['assets'][spec['robot']['asset_id']]['package']),
                                     spec['robot']['package_asset'])
    out = planning.run_plan(spec, records, descriptor, spec_sha256=spec_sha256)
    atomic_write_json(work / 'plan.json', out)
    reach = out['reachability']
    footprint = out['footprint']
    for row in reach['targets']:
        print(f"  {'ok ' if row['ok'] else 'NO '} {row['name']:<28} {row['world_m']} err {row['position_error_m']}")
    print(reach['notes'])
    print('footprint ok' if footprint['ok'] else 'footprint: ' + '; '.join(footprint['problems']))
    return 0 if out['ok'] else 1


def check(args) -> int:
    import time
    work = Path(args.work)
    spec = load_json(args.spec)
    store, prepared, records = _records(work)
    planned = load_json(work / 'plan.json')
    if planned['spec_sha256'] != sha256_file(args.spec):
        raise SystemExit('plan.json is stale; rerun `plan` for this spec')
    robot_spec = spec['robot']
    package = Path(prepared['assets'][robot_spec['asset_id']]['package'])
    sys.path.insert(0, str(package))

    import warp as wp
    wp.config.enable_backward = False
    from isaaclab.app import add_launcher_args, launch_simulation
    parser = argparse.ArgumentParser()
    add_launcher_args(parser)
    launcher_args, _ = parser.parse_known_args(args.launcher)
    import isaaclab.sim as sim_utils
    from isaaclab.scene import InteractiveScene
    from robot_profiles import make_robot_cfg, make_simulation_cfg
    from datafarm.adapters.sim import isaac_scene
    from datafarm.scene import asset_records, layout

    nodes = planned['nodes']
    robot_node = {'node_id': robot_spec['node_id'], 'pose': planned['robot_base_pose']}
    sim_cfg = make_simulation_cfg(robot_spec['package_asset'], 'newton_mjwarp')
    sim_cfg.physics.solver_cfg.nconmax = args.nconmax
    sim_cfg.physics.solver_cfg.njmax = args.njmax
    launcher_args.physics = None
    by_asset = {k: v for k, v in prepared['assets'].items() if v['body'] != 'robot'}
    wrappers = {k: v['wrapper'] for k, v in by_asset.items()}
    with launch_simulation(sim_cfg, launcher_args):
        sim = sim_utils.SimulationContext(sim_cfg)
        cfg_cls = isaac_scene.build_scene_cfg(nodes=nodes, assets=by_asset, wrappers=wrappers,
                                              robot_cfg=make_robot_cfg(robot_spec['package_asset'], 'newton_mjwarp'),
                                              robot_node=robot_node, drawer_damping=asset_records.PRISMATIC_DAMPING)
        started = time.time()
        scene = InteractiveScene(cfg_cls(num_envs=1, env_spacing=4.0))
        sim.reset()
        scene.reset()
        print(f'scene loaded in {time.time() - started:.1f}s', flush=True)
        initial_joints = isaac_scene.apply_initial_state(sim, scene, nodes=nodes, assets=by_asset)
        print(f'initial joint state (declared intent) {initial_joints}', flush=True)
        joint_limits = {}
        for node in nodes:
            record = records.get(node['asset_id']) or {}
            joint_limits[node['node_id']] = {j['name']: (j['limits']['lower'], j['limits']['upper'])
                                             for j in record.get('joints', [])}
        settle, penetration = isaac_scene.settle(
            sim, scene, nodes=nodes, assets=by_asset, robot_node_id=robot_spec['node_id'], seconds=args.settle_s,
            drift_tolerance_m=args.drift_tol, speed_tolerance_mps=args.speed_tol,
            joint_tolerance_active=args.joint_tol_active, joint_tolerance_passive=args.joint_tol_passive,
            penetration_tolerance_m=args.penetration_tol, joint_limits=joint_limits)
        all_nodes = [n['node_id'] for n in nodes] + [robot_spec['node_id']]
        probes = []
        by_id = {n['node_id']: n for n in nodes}
        for probe in spec.get('probes', []):
            target = layout.target_world(probe, by_id, records)
            moved = next(n['node_id'] for n in nodes if n['asset_id'] == probe['asset_id'])
            result = isaac_scene.drop_probe(
                sim, scene, node_id=moved, world_position=target.tolist(), seconds=args.probe_s,
                rest_z=(probe['expect_rest_z_min_m'], probe['expect_rest_z_max_m']), support_node=probe['node'],
                all_nodes=all_nodes, speed_tolerance_mps=args.speed_tol)
            probes.append({'name': probe['name'], **result})
            print(f"probe {probe['name']}: ok={result['ok']} rest {result['rest_m']} touching {result['touching']}",
                  flush=True)
        runtime_profile = {
            'schema': 'datafarm.sim-runtime-profile/1', 'backend': 'newton_mjwarp',
            'physics_dt_s': sim.get_physics_dt(), 'control_hz': 120,
            'solver': {'integrator': sim_cfg.physics.solver_cfg.integrator, 'nconmax': args.nconmax,
                       'njmax': args.njmax, 'num_substeps': sim_cfg.physics.num_substeps,
                       'use_mujoco_contacts': sim_cfg.physics.solver_cfg.use_mujoco_contacts},
            'articulated_fixture_drive': {'stiffness': 0.0, 'damping': asset_records.PRISMATIC_DAMPING},
            'robot_profile': robot_spec['package_asset']}
    evidence = {'initial_joint_state': initial_joints, 'settle': settle, 'penetration': penetration, 'probes': probes}
    evidence_ref = store.put_json('scene.check-evidence', evidence)
    profile_ref = store.put_json('sim.runtime-profile', runtime_profile)
    ledger_ref = store.put_json('ir.physics-ledger', layout.physics_ledger(records, nodes))
    bundle_ref = store.put_json('scene.asset-bundle', {'schema': 'datafarm.asset-bundle/1',
                                                       'files': prepared['bundle_files']})
    probes_ok = all(p['ok'] for p in probes)
    settle_check = {k: settle[k] for k in ('ran', 'steps', 'max_drift_m', 'stable')}
    penetration_check = {k: penetration[k] for k in ('ran', 'max_depth_m', 'ok')}
    graph = layout.build_scene_graph(
        spec=spec, nodes=nodes, records=records,
        record_refs={k: v['record'] for k, v in prepared['assets'].items()},
        descriptor=robot_descriptor(package, robot_spec['package_asset'])[0],
        asset_bundle=bundle_ref.to_dict(), ledger=ledger_ref.to_dict(), runtime_profile=profile_ref.to_dict(),
        reach=planned['reachability'], settle=settle_check, penetration=penetration_check)
    graph_ref = store.put_json('ir.scene-graph', graph)
    atomic_write_json(work / 'scene-graph.json', graph)
    atomic_write_json(work / 'check.json', {'scene_graph': graph_ref.to_dict(), 'evidence': evidence_ref.to_dict(),
                                            'probes_ok': probes_ok, **evidence})
    ok = (settle['stable'] and penetration['ok'] and probes_ok and planned['reachability']['ok']
          and planned['footprint']['ok'])
    print(json.dumps({'settle': settle_check, 'penetration': penetration_check, 'probes_ok': probes_ok,
                      'new_contacts': sorted(settle['new_contacts']),
                      'measured_joint_state': settle['measured_joint_state'],
                      'reachability_ok': planned['reachability']['ok'], 'footprint_ok': planned['footprint']['ok'],
                      'scene_graph': graph_ref.sha256}, indent=1))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'plan', 'check'):
        p = sub.add_parser(name)
        p.add_argument('--spec', type=Path, required=True)
        p.add_argument('--work', type=Path, required=True)
        if name == 'prepare':
            p.add_argument('--cache', type=Path, required=True, help='SimReady fetch cache (tools/simready.py fetch)')
            p.add_argument('--robot-package', type=Path, required=True)
            p.add_argument('--robot-retrieved-at', default='2026-11-13T00:00:00Z')
        if name == 'check':
            p.add_argument('--settle-s', type=float, default=2.0)
            p.add_argument('--probe-s', type=float, default=2.0)
            p.add_argument('--drift-tol', type=float, default=0.005)
            p.add_argument('--speed-tol', type=float, default=0.01)
            # One source of truth: --joint-tol is an alias for --joint-tol-active (strict, held/actuated joints).
            p.add_argument('--joint-tol-active', '--joint-tol', dest='joint_tol_active', type=float, default=0.005,
                           help='max drift of actuated/held joints (alias: --joint-tol)')
            p.add_argument('--joint-tol-passive', type=float, default=0.005,
                           help='drift above which a passive joint is reported as moved (not an acceptance bound; '
                                'passive joints are gated by limits and new contacts)')
            p.add_argument('--penetration-tol', type=float, default=0.003)
            p.add_argument('--nconmax', type=int, default=4096)
            p.add_argument('--njmax', type=int, default=8192)
    args, rest = parser.parse_known_args()
    args.launcher = rest
    return {'prepare': prepare, 'plan': plan, 'check': check}[args.command](args)


if __name__ == '__main__':
    raise SystemExit(main())

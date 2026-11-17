"""用 Isaac RTX 渲染一条 episode：request 的 `render` 段 + SceneGraph → RenderSpec → 回放 state_trace 出图。

    python tools/render_episode.py spec   --work W --episode EP.json [--render JSON] --out spec.json
    python tools/render_episode.py render --work W --spec spec.json --out manifest.json [--headless]

`W` 是 `tools/scene_assemble.py` 的工作目录：`store/`、`prepared.json`（主机路径索引）、`check.json`（只取其中的
SceneGraph 引用）。`render` 经共享的 `scene_runtime.load_scene` 按 SceneGraph 加载场景，与 B 录制 episode 时是同一份，
不读 `plan.json`。`EP.json` 是 `{"episode": ArtifactRef}`。`spec` 纯 CPU；`render` 需要 Isaac Lab 3.0 的 Python，
加载同一场景但不步进物理。
产物（RenderSpec、mp4、内外参、RenderManifest）都进 `W/store`，`--out` 只写引用。
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))

from datafarm.kernel.artifacts import ArtifactRef  # noqa: E402
from datafarm.kernel.io import atomic_write_json, sha256_file  # noqa: E402
from datafarm.kernel.store import ArtifactStore  # noqa: E402

DEFAULT_RENDER = '{"engine": "isaac_rtx", "views": ["head", "left_wrist", "third_person"], "materials": 2, "lights": 2}'
DEFAULT_VARIANTS = ROOT / 'configs/render/isaac-variants.v1.json'


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _graph(store: ArtifactStore, work: Path) -> tuple[dict, dict]:
    ref = load_json(work / 'check.json')['scene_graph']
    return store.load(ArtifactRef.from_dict(ref)), ref


def spec(args) -> int:
    from datafarm.render import plan
    store = ArtifactStore(args.work / 'store')
    graph, graph_ref = _graph(store, args.work)
    episode_ref = load_json(args.episode)['episode']
    store.load(ArtifactRef.from_dict(episode_ref))
    document = plan.build_spec(render=json.loads(args.render), scene_graph=graph, scene_graph_ref=graph_ref,
                               episode_ref=episode_ref, config=load_json(args.variants))
    ref = store.put_json('ir.render-spec', document)
    atomic_write_json(args.out, {'render_spec': ref.to_dict()})
    print(json.dumps({'render_spec': ref.to_dict(), 'variants': [v['variant_id'] for v in document['variants']]}))
    return 0


def render(args) -> int:
    import numpy as np
    from datafarm.ir import render_spec
    from datafarm.render import plan
    from scene_assemble import robot_descriptor
    from datafarm.adapters.sim import scene_runtime

    work = args.work
    store = ArtifactStore(work / 'store')
    spec_ref = load_json(args.spec)['render_spec']
    spec_doc = store.load(ArtifactRef.from_dict(spec_ref))
    prepared = load_json(work / 'prepared.json')
    resolved = scene_runtime.load_scene(store, spec_doc['scene_graph'], prepared,
                                        asset_roots=scene_runtime.asset_roots(prepared))
    graph = resolved['graph']
    render_spec.validate(spec_doc, graph)
    episode = store.load(ArtifactRef.from_dict(spec_doc['episode']))['payload']
    trace = episode['state_trace']
    joint_names = episode['setup']['joint_names']
    package = Path(prepared['assets'][graph['robot']['asset_id']]['package'])
    runtime = store.load(ArtifactRef.from_dict(graph['runtime_profile']))
    profile = runtime['robot_profile']
    descriptor = robot_descriptor(package, profile)[0]
    sys.path.insert(0, str(package))
    steps_by_view = {v['view_id']: render_spec.frame_steps([s['control_step'] for s in trace], v['fps'])
                     for v in spec_doc['views']}

    import warp as wp
    wp.config.enable_backward = False
    from isaaclab.app import add_launcher_args, launch_simulation
    parser = argparse.ArgumentParser()
    add_launcher_args(parser)
    launcher_args, _ = parser.parse_known_args(args.launcher)
    # robot_profiles / isaaclab.sim 会加载 pxr；必须先启动 Kit，否则 RTX 插件与已加载的 USD 库符号冲突。
    launcher_args.physics = None
    launcher_args.require_kit = True
    launcher_args.enable_cameras = True
    drawer_damping = runtime['articulated_fixture_drive']['damping']
    out_dir = work / 'render' / spec_ref['sha256'][:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    with launch_simulation(None, launcher_args):
        import isaaclab.sim as sim_utils
        from isaaclab.scene import InteractiveScene
        from robot_profiles import make_robot_cfg, make_simulation_cfg
        from datafarm.adapters.render.isaac.playback import Playback
        from datafarm.adapters.render.isaac.video import Encoder

        sim = sim_utils.SimulationContext(make_simulation_cfg(profile, 'newton_mjwarp'))
        cfg_cls = scene_runtime.scene_config(resolved, make_robot_cfg(profile, 'newton_mjwarp'),
                                             drawer_damping=drawer_damping)
        scene = InteractiveScene(cfg_cls(num_envs=1, env_spacing=4.0))
        player = Playback(sim, scene, scene_graph=graph, spec=spec_doc, descriptor=descriptor)
        player.add_cameras()
        sim.reset()
        scene.reset()
        player.after_reset()

        streams, frames = [], {}
        worst = {'position_m': 0.0, 'joint': 0.0, 'camera_m': 0.0}
        views = {v['view_id']: v for v in spec_doc['views']}
        for group in plan.appearance_groups(spec_doc):
            started = time.time()
            player.apply_appearance(group['material_set'], group['light_set'])
            variants = group['variants']
            encoders, poses = {}, {}
            for variant in variants:
                k = plan.view_intrinsics(spec_doc, graph, variant['view'])
                encoders[variant['variant_id']] = Encoder(
                    out_dir / f"{variant['variant_id']}.mp4", width=int(k['width']), height=int(k['height']),
                    fps=views[variant['view']]['fps'], crf=spec_doc['output']['crf'])
                frames[variant['variant_id']], poses[variant['variant_id']] = [], []
            wanted = sorted({s for v in variants for s in steps_by_view[v['view']]})
            for index, step in enumerate(wanted):
                state = trace[step]
                if state.get('valid', True):
                    player.write_state(state, joint_names)
                    position_error, joint_error = player.readback_error()
                    worst['position_m'] = max(worst['position_m'], position_error)
                    worst['joint'] = max(worst['joint'], joint_error)
                    targets = player.place_cameras()
                    player.render(args.warmup if index == 0 else args.passes)
                for variant in variants:
                    if step not in steps_by_view[variant['view']]:
                        continue
                    k = plan.view_intrinsics(spec_doc, graph, variant['view'])
                    blank = np.zeros((int(k['height']), int(k['width']), 3), np.uint8)
                    image, actual, reason = None, None, ''
                    if state.get('valid', True):
                        image, actual = player.capture(variant['view'])
                        camera_error = float(np.linalg.norm(actual[:3, 3] - targets[variant['view']][:3, 3]))
                        worst['camera_m'] = max(worst['camera_m'], camera_error)
                        if camera_error > args.camera_tol:
                            image, reason = None, f'camera placed {camera_error:.4f} m from its target'
                    status, reason = plan.frame_status(state, image is not None, reason)
                    # 坏帧写黑图占位，保持帧号与控制步对齐；状态由 frame_map 标出，不复制上一帧。
                    encoders[variant['variant_id']].write(image if image is not None else blank)
                    entry = {'frame_index': len(frames[variant['variant_id']]), 'control_step': step,
                             'status': status, 'reason': reason}
                    frames[variant['variant_id']].append(entry)
                    poses[variant['variant_id']].append(
                        {'frame_index': entry['frame_index'], 'control_step': step,
                         'T_world_camera': plan.matrix_pose(actual) if actual is not None else None})
            for variant in variants:
                variant_id = variant['variant_id']
                video = store.put_bytes('render.video', encoders[variant_id].close(), media_type='video/mp4')
                intrinsics = store.put_json('camera.intrinsics', plan.view_intrinsics(spec_doc, graph,
                                                                                      variant['view']))
                extrinsics = store.put_json('camera.extrinsics', {'convention': 'opencv', 'frames': poses[variant_id]})
                streams.append({'variant_id': variant_id, 'view_id': variant['view'],
                                'camera_id': plan.stream_camera_id(spec_doc, variant['view']),
                                'video': video.to_dict(), 'intrinsics': intrinsics.to_dict(),
                                'extrinsics': extrinsics.to_dict(), 'frame_count': len(frames[variant_id])})
            print(f"group m={group['material_set']} l={group['light_set']}: {len(wanted)} steps, "
                  f"{len(variants)} variants in {time.time() - started:.1f}s", flush=True)
        # Kit 关闭时会直接结束进程，所以清单必须在 launch_simulation 上下文里写完。
        return _finish(args, store=store, spec_ref=spec_ref, spec_doc=spec_doc, streams=streams, frames=frames,
                       worst=worst, out_dir=out_dir)


def _finish(args, *, store, spec_ref, spec_doc, streams, frames, worst, out_dir) -> int:
    from datafarm.ir import render_spec
    from datafarm.render import plan
    ok = worst['position_m'] <= args.position_tol and worst['joint'] <= args.joint_tol
    playback = {'status': 'verified' if ok else 'failed', 'max_position_error_m': worst['position_m'],
                'max_joint_error': worst['joint'], 'tolerance_m': args.position_tol,
                'notes': (f"readback after write, no physics step; joint tolerance {args.joint_tol}; "
                          f"max camera placement error {worst['camera_m']:.6f} m")}
    engine = {'isaac_sim': importlib.metadata.version('isaacsim'),
              'isaac_lab': importlib.metadata.version('isaaclab')}
    document = plan.manifest(spec_ref=spec_ref, spec=spec_doc, engine=engine, streams=streams, frames=frames,
                             playback=playback)
    manifest_ref = store.put_json('ir.render-manifest', document)
    atomic_write_json(args.out, {'render_manifest': manifest_ref.to_dict(), 'videos': str(out_dir),
                                 'spec_file_sha256': sha256_file(args.spec)})
    counts = {s: sum(e['status'] == s for e in document['frame_map']) for s in render_spec.FRAME_STATUS}
    print(json.dumps({'render_manifest': manifest_ref.to_dict(), 'state_playback': playback, 'frames': counts}),
          flush=True)
    return 0 if ok and counts['error'] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('spec')
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--episode', type=Path, required=True, help='JSON {"episode": ArtifactRef}')
    p.add_argument('--render', default=DEFAULT_RENDER, help="request render section as JSON")
    p.add_argument('--variants', type=Path, default=DEFAULT_VARIANTS)
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('render')
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--spec', type=Path, required=True, help='JSON {"render_spec": ArtifactRef} from `spec`')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--warmup', type=int, default=60, help='RTX passes on the first frame after an appearance change')
    p.add_argument('--passes', type=int, default=8, help='RTX passes per later frame')
    p.add_argument('--position-tol', type=float, default=0.01)
    p.add_argument('--joint-tol', type=float, default=1e-3)
    p.add_argument('--camera-tol', type=float, default=1e-3)
    args, rest = parser.parse_known_args()
    args.launcher = rest
    return {'spec': spec, 'render': render}[args.command](args)


if __name__ == '__main__':
    raise SystemExit(main())

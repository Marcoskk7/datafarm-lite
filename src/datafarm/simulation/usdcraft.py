"""USDCraft（https://github.com/NVlabs/USDCraft）CLI 桥接。制定计划时无需上游导入或凭据。"""
from __future__ import annotations
import fcntl
import json
import math
import os
from datafarm.common import atomic_write_json, sha256_file
from datafarm.runtime import expand, load_bindings, source_root
from pathlib import Path
import re
import signal
import subprocess
import time
import xml.etree.ElementTree as ET


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    atomic_write_json(path, value, sort_keys=False, mode=0o644, fsync=False)


def evidence(path):
    p = Path(path).expanduser().resolve(strict=True)
    if not p.is_file():
        raise ValueError(f'Not a file: {p}')
    return {'path': str(p), 'sha256': sha256_file(p)}


def upstream_version(root, source):
    """记录检出目录的 commit。不同 commit 也许仍能工作，因此只报告而不拒绝。"""
    assets = read(source / 'configs/external-assets.json')
    tested = next(r['upstream_commit'] for r in assets['repositories'] if r.get('binding') == 'USDCRAFT')
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    commit = result.stdout.strip() if result.returncode == 0 else None
    return {'commit':commit, 'tested_commit':tested, 'tested':commit == tested}


def plan(config, observation, output):
    cfg, obs = read(config), read(observation)
    source = source_root()
    root = Path(expand(cfg['checkout'], load_bindings(source))).expanduser().resolve(strict=True)
    if not (root / 'usdcraft/cli.py').is_file():
        raise ValueError('checkout must be a USDCraft source tree')
    model, reasoning = cfg['model'], cfg['reasoning']
    if not all(isinstance(x, str) and x for x in (model, reasoning)):
        raise ValueError('model and reasoning must both be explicit')
    launcher = cfg.get('launcher', ['uv', 'run', '--frozen', 'usdcraft'])
    if not isinstance(launcher, list) or not launcher or not all(isinstance(x, str) and x for x in launcher):
        raise ValueError('launcher must be a nonempty argv array')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', obs['object_id']):
        raise ValueError('object_id must be a lowercase slug')
    if not isinstance(obs.get('description'), str) or not obs['description'].strip():
        raise ValueError('description must state observed identity, function and uncertainties')
    refs = [evidence(obs['image'])]
    mode = 'reconstruction' if obs.get('mesh') else 'generation'
    argv = launcher + [mode + '-codex']
    if mode == 'reconstruction':
        refs.append(evidence(obs['mesh']))
        argv += [refs[-1]['path'], '--prompt', obs['description']]
        for key in ('up_axis', 'front_axis', 'meters_per_unit'):
            if key in obs:
                if key == 'meters_per_unit' and (not math.isfinite(float(obs[key])) or float(obs[key]) <= 0):
                    raise ValueError('meters_per_unit must be finite and positive')
                if key != 'meters_per_unit' and obs[key] not in ('+X','-X','+Y','-Y','+Z','-Z'):
                    raise ValueError('invalid source axis')
                argv += ['--' + key.replace('_', '-') + '=' + str(obs[key])]
    else:
        argv += [obs['description']]
    argv += ['--image', refs[0]['path'], '--model', model, '--reasoning', reasoning,
             '--label', obs['object_id'], '--repo-root', str(root), '--keep-workspace', '--json', '--direct']
    # 对全部额外证据做哈希，但不要假装单图像的上游 CLI 消费了它。
    refs += [evidence(p) for p in obs.get('context_files', [])]
    refs += [evidence(config), evidence(observation), evidence(root / 'usdcraft/cli.py'), evidence(root / 'pyproject.toml')]
    result = {'schema':'datafarm.usdcraft-plan/1', 'object_id':obs['object_id'], 'mode':mode,
              'cwd':str(root), 'argv':argv, 'inputs':refs, 'observation':obs,
              'timeout_seconds':cfg.get('timeout_seconds'), 'upstream':upstream_version(root, source),
              'qualification':'unverified', 'cost':{'basis':'unknown','amount_usd':None},
              'image_semantics':'inferred-geometry' if mode == 'generation' else 'mesh-and-photo-reference'}
    timeout = result['timeout_seconds']
    if timeout is not None and (not isinstance(timeout, (int,float)) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('timeout_seconds must be null or positive finite seconds')
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    write(out / 'plan.json', result)
    return result


def check_inputs(p):
    for ref in p['inputs']:
        if evidence(ref['path']) != ref:
            raise ValueError('Input changed; create a new versioned plan: ' + ref['path'])


def collect(p, payload):
    """绑定导出字节，不执行 model.py，也不声称已通过仿真器资格验证。"""
    if payload.get('status') != p['mode'] + '_ready' or payload.get('provider') != 'codex-cli':
        raise ValueError('USDCraft did not return a ready native Codex record')
    rid = payload['record_id']
    if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',rid):
        raise ValueError('invalid record ID')
    root = Path(p['cwd']).resolve()
    record = (root/'data/records'/rid).resolve()
    material = (root/'data/cache/record_materialization'/rid).resolve()
    if not record.is_relative_to(root/'data/records') or not material.is_relative_to(root/'data/cache/record_materialization'):
        raise ValueError('record escapes data root')
    model = Path(payload['model_path'])
    # 上游报告的路径相对于它自己的 data 根，而不是相对于本进程。
    model = (model if model.is_absolute() else root / model).resolve(strict=True)
    if not model.is_relative_to(record):
        raise ValueError('model outside canonical record')
    urdf = material/'model.urdf'
    tree = ET.parse(urdf).getroot()
    if tree.tag != 'robot' or not tree.findall('link'):
        raise ValueError('missing URDF robot links')
    artifacts = [evidence(model)]
    # 保留所有导出依赖，而不仅是带相对网格的 URDF。
    for base in (record, material):
        for f in sorted(base.rglob('*')):
            if f.is_file():
                if not f.resolve().is_relative_to(base):
                    raise ValueError('external symlink in export closure')
                artifacts.append(evidence(f))
    for mesh in tree.iter('mesh'):
        name = mesh.get('filename','')
        if '://' in name or Path(name).is_absolute():
            raise ValueError('URDF mesh URI needs explicit simulator resolver: ' + name)
        resolved = (material/name).resolve(strict=True)
        if not resolved.is_relative_to(material):
            raise ValueError('URDF mesh outside materialization')
    joints = [{'name':j.get('name'), 'type':j.get('type'),
               'axis':j.find('axis').attrib if j.find('axis') is not None else None,
               'limit':j.find('limit').attrib if j.find('limit') is not None else None}
              for j in tree.findall('joint')]
    return {'schema':'datafarm.object-asset/1','object_id':p['object_id'],'backend':'usdcraft',
            'record_id':rid,'urdf':str(urdf),'artifacts':artifacts,'joints':joints,
            'inputs':p['inputs'],'mode':p['mode'],'qualification':'unverified',
            'required_next_step':'Target simulator import, observed geometry/articulation comparison and task contact probes',
            'cost':{'basis':'unknown','amount_usd':None}}


class BuildLocked(BlockingIOError):
    """另一个进程正持有该资产构建；未提交或结算任何内容。"""


def build_lock(out):
    lock = (out/'.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise BuildLocked('asset build already running in ' + str(out)) from None
    return lock


def run(directory):
    out = Path(directory).resolve(strict=True)
    with build_lock(out):
        return _run_locked(out)


def _run_locked(out):
    # worker 已死或状态未知，并不意味着可以创建重复资产或产生第二笔费用。
    if (out/'execution.json').exists():
        return read(out/'execution.json')
    p = read(out/'plan.json');check_inputs(p)
    state = {'status':'starting','started':time.time(),'cost':{'amount_usd':None,'basis':'unknown'}}
    write(out/'execution.json',state)
    with (out/'stdout.json').open('w') as stdout, (out/'stderr.log').open('w') as stderr:
        proc = None
        try:
            proc = subprocess.Popen(p['argv'], cwd=p['cwd'], stdout=stdout, stderr=stderr, start_new_session=True)
            state.update(status='running',pid=proc.pid,pgid=proc.pid);write(out/'execution.json',state)
            code = proc.wait(timeout=p['timeout_seconds'])
            state.update(status='returned' if code == 0 else 'failed',exit_code=code)
        except BaseException as exc:
            if proc and proc.poll() is None:
                os.killpg(proc.pid,signal.SIGTERM)
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            state.update(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',error=type(exc).__name__)
            write(out/'execution.json',state)
            raise
        finally:
            state['ended']=time.time();write(out/'execution.json',state)
    if state['status']=='returned':
        try:
            asset = collect(p,read(out/'stdout.json'))
            write(out/'asset.json',asset)
            state['status']='exported-unqualified'
        except (ValueError,KeyError,OSError,ET.ParseError) as exc:
            state.update(status='invalid-export',error=str(exc))
        write(out/'execution.json',state)
    return state


def run_for_task(directory, task_work):
    """把一次资产执行挂接到已有的任务预算/作业/审查生命周期上。

    导出的资产保持未资格验证（unqualified）状态。作者会排队提交仿真器资格
    验证证据，供独立审查，之后才会在任务场景中采用该资产。
    """
    from datafarm.agent.cost_budget import budget_for_work
    work = Path(task_work).resolve(strict=True)
    out = Path(directory).resolve(strict=True)
    if not out.is_relative_to(work):
        raise ValueError('task asset build must live inside its task workspace')
    # 构建锁覆盖作业登记、预算预留与状态写入，因此并发的第二次调用会在它能够
    # 覆盖状态或结算费用之前失败。
    with build_lock(out):
        return _run_for_task_locked(out, work, budget_for_work)


def _run_for_task_locked(out, work, budget_for_work):
    identifier = 'asset-' + sha256_file((out / 'plan.json'))[:24]
    # 已存在的尝试只做观察，绝不再提交，也不以新 ID 计费。
    if (out / 'execution.json').exists():
        return read(out / 'execution.json')
    budget = budget_for_work(work)
    if budget and budget.enforced:
        raise ValueError('native USDCraft has unknown descendant costs and cannot meet enforced budget caps')
    state_path = work / 'state.json'
    state = read(state_path)
    jobs = state.setdefault('external_jobs', [])
    prior = [job for job in jobs if job.get('id') == identifier]
    if prior:
        raise ValueError('existing asset job ID without execution receipt; reconcile before resubmission')
    if budget:
        budget.reserve(identifier, 0)
    jobs.append({'id':identifier,'status':'queued','kind':'observed-object',
        'status_file':str(out/'job-status.json'),'plan':str(out/'plan.json'),
        'qualification':'unverified','cost_usd':None})
    write(state_path, state)
    write(out/'job-status.json',{'id':identifier,'status':'running','qualification':'unverified'})
    try:
        result = _run_locked(out)
    except BaseException as exc:
        write(out/'job-status.json',{'id':identifier,'status':'failed','error':type(exc).__name__,
              'qualification':'unverified','cost_usd':None})
        raise
    else:
        write(out/'job-status.json',{'id':identifier,
              'status':result['status'] if result['status']=='exported-unqualified' else 'failed',
              'asset_status':result['status'],
              'exit_code':result.get('exit_code'),'qualification':'unverified','cost_usd':None})
        return result
    finally:
        if budget:
            budget.settle(identifier,{'schema':'datafarm.external-usage/1','cost_usd':None,
                'evidence':str(out/'execution.json'),'includes_descendants':False,
                'reason':'USDCraft native observer/author costs are unknown; no hardcap asserted'})

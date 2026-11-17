"""Broker-compatible scene job with a pinned trusted deployment resource factory."""
import argparse
import importlib.util
import json
import math
from copy import deepcopy
import time
from pathlib import Path

from datafarm.adapters.agent_harness.scene_execution import run_expert
from datafarm.entry.scene_rollout import record_scene_episode
from datafarm.kernel.io import sha256_file, atomic_write_json
from datafarm.kernel.artifacts import ArtifactRef


def execute_job(config, factory, expert_file, seed, attempt_id, output, class_name=None):
    """Factory supplies A-bound initialized resources; no fixture/default task factory."""
    from datafarm.kernel.identifiers import logical_id
    if not isinstance(config, dict):
        raise ValueError('explicit scene configuration required')
    logical_id(attempt_id, label='attempt_id')
    logical_id(config.get('family'), label='family')
    if type(seed) is not int or seed < 0 or not callable(factory):
        raise ValueError('explicit seed and trusted resource factory required')
    expert_file, output = Path(expert_file), Path(output)
    if output.exists():
        raise ValueError('fresh job output required')
    resources = dict(factory(deepcopy(config), seed=seed))
    expected = resources['store'].load_bytes(ArtifactRef.from_dict(resources['expert_ref']))
    if expert_file.read_bytes() != expected:
        raise ValueError('snapshot expert differs from pinned source')
    budget = resources.pop('expert_budget')
    if (not isinstance(budget,dict) or set(budget) != {'timeout_s','max_calls'} or
            type(budget['timeout_s']) not in (int,float) or not math.isfinite(budget['timeout_s']) or
            not 0 < budget['timeout_s'] <= 3600 or type(budget['max_calls']) is not int or
            not 1 <= budget['max_calls'] <= 100000):
        raise ValueError('explicit expert wall/call budgets required')
    upstream_cancel = resources.get('cancelled')
    if upstream_cancel is not None and not callable(upstream_cancel):
        raise ValueError('cancellation probe must be callable')
    deadline = time.monotonic()+budget['timeout_s']
    def cancelled():
        value = upstream_cancel() if upstream_cancel is not None else False
        if type(value) is not bool:
            raise ValueError('cancellation probe must return bool')
        return time.monotonic() >= deadline or value
    resources['cancelled'] = cancelled
    def executor(actions, source):
        if source != expert_file.read_bytes():
            raise ValueError('snapshot changed before execution')
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('expert wall budget exhausted')
        run_expert(expert_file, actions, timeout_s=remaining, max_calls=budget['max_calls'], class_name=class_name)
    result = record_scene_episode(**resources, expert=None, expert_executor=executor, attempt_id=attempt_id)
    output.mkdir(parents=True)
    atomic_write_json(output/'episode-ref.json',result)
    atomic_write_json(output/'initial-state.json',{'valid':result.get('episode_ref') is not None,'family':config['family']})
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','factory','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--expert', required=True, help='snapshotted file:class or function source file')
    parser.add_argument('--factory-sha256',required=True)
    parser.add_argument('--seed',type=int,required=True)
    parser.add_argument('--attempt-id',required=True)
    args=parser.parse_args(argv)
    if sha256_file(args.factory)!=args.factory_sha256:
        parser.error('trusted factory source changed')
    spec=importlib.util.spec_from_file_location('deployment_scene_factory',args.factory)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    expert, sep, class_name = args.expert.rpartition(':')
    expert_file = Path(expert if sep else args.expert)
    value=execute_job(json.loads(args.config.read_text(encoding='utf-8')),module.create_resources,expert_file,args.seed,args.attempt_id,args.output,class_name if sep else None)
    return 0 if value['episode_ref'] is not None else 2




def bind_scene_adapter(raw, *, factory, factory_sha256, config, store_root, contract_ref_document, bundle, anchor):
    """Build a new adapter/1 deployment config; never mutate the input adapter."""
    from copy import deepcopy
    import sys
    from datafarm.kernel.identifiers import logical_id, require_identity_profile
    from datafarm.judge.compiler import verify_bundle
    if raw.get('schema') != 'datafarm.expert-harness.adapter/1':
        raise ValueError('adapter/1 required')
    require_identity_profile(raw);logical_id(raw.get('task_id'))
    contract=verify_bundle(Path(bundle),anchor)
    from datafarm.kernel.store import ArtifactStore
    from datafarm.judge.contract_binding import verify_contract
    store = ArtifactStore(store_root)
    portable = json.loads(Path(contract_ref_document).read_text(encoding='utf-8'))
    authority = verify_contract(store, portable, bundle=bundle, anchor=anchor)
    if contract.get('schema_version') != 'task-contract/1' or contract['task_id'] != raw['task_id']:
        raise ValueError('matching measured Judge freeze/1 required')
    if authority['task_id'] != raw['task_id']:
        raise ValueError('portable contract task differs')
    if raw.get('task_ir') is not None:
        # The private source-bound Task IR, not a public summary, is the host authority.
        document = store.document(ArtifactRef.from_dict(authority['task_ref']))
        if raw['task_ir'] != document.payload:
            raise ValueError('adapter Task IR differs from frozen source')
    if sha256_file(factory)!=factory_sha256:
        raise ValueError('trusted factory source differs')
    result=deepcopy(raw)
    config_value=json.loads(Path(config).read_text(encoding='utf-8'))
    logical_id(config_value.get('family'), label='family')
    result['task_ir']=dict(store.document(ArtifactRef.from_dict(authority['task_ref'])).payload)
    result['verifier'].update(version=contract['freeze_id'],criterion_ids=[c['id'] for c in contract['conditions']],
        criteria=[c['description'] for c in contract['conditions']],criteria_tokens=[c['description'] for c in contract['conditions']],
        judge_bundle={'directory':str(Path(bundle).resolve()),'sha256':anchor})
    result['rollout']['sim']={'argv':[sys.executable,'-m','datafarm.entry.scene_job',
        '--factory',str(Path(factory).resolve()),'--factory-sha256',factory_sha256,
        '--config',str(Path(config).resolve()),'--expert','{run_dir}/expert_snapshot.py:{expert_class}',
        '--seed','{seed}','--attempt-id','{attempt_id}','--output','{run_dir}/native-parent/native'],
        'cwd':'{run_dir}','log':'sim.log','requires_output':'native-parent/native/episode-ref.json',
        'writable_run_subdirs':['native-parent'], 'timeout_s':3600}
    # prepare_run_dir creates only the parent; the entry owns a fresh native directory.
    result['rollout']['judge']=[{'name':'generated-judge','argv':[sys.executable,'-m','datafarm.entry.judge_cli',
        'evaluate-recorded','--store',str(Path(store_root).resolve()),'--bundle',str(Path(bundle).resolve()),
        '--freeze-sha256',anchor,'--episode-ref','{run_dir}/native-parent/native/episode-ref.json',
        '--contract-ref',str(Path(contract_ref_document).resolve()),'--output','{run_dir}/verdict.json'],
        'outputs':['verdict.json']}]
    result['rollout']['verdict']={'schema':'judge-result/1','file':'verdict.json','success':'success',
        'fields':{'reason_code':'reason_code','predicate_results':'predicate_results','metrics':'metrics'}}
    result['rollout']['init_state']={'file':'native-parent/native/initial-state.json','valid':'valid','family':'family'}
    result.setdefault('manifest',{}).setdefault('files',[]).extend([str(Path(factory).resolve()),str(Path(config).resolve()),str(Path(contract_ref_document).resolve())])
    return result


if __name__=='__main__':raise SystemExit(main())

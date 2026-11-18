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
    runtime = config.get('expert_runtime')
    runtime_metadata = None
    if runtime is not None:
        from datafarm.adapters.sandbox.scene_proot import runtime_record
        runtime_metadata = runtime_record(runtime)
    resources = dict(factory(deepcopy(config), seed=seed))
    store = resources['store']
    portable = resources.get('task_contract_ref')
    frozen = config.get('judge_freeze')
    if runtime is not None:
        if (not isinstance(frozen,dict) or set(frozen) != {'directory','sha256'} or portable is None):
            raise ValueError('PRoot scene job requires an external live Judge freeze and contract')
    def verify_authority():
        if runtime_metadata is not None:
            from datafarm.adapters.sandbox.scene_proot import runtime_record
            if runtime_record(runtime) != runtime_metadata:
                raise ValueError('PRoot execution runtime changed during job')
        if frozen is not None:
            from datafarm.judge.contract_binding import verify_contract
            verify_contract(store, portable, task_ref=resources['task_ref'], scene_ref=resources['scene_ref'],
                            bundle=frozen['directory'], anchor=frozen['sha256'])
    verify_authority()
    expected = resources['store'].load_bytes(ArtifactRef.from_dict(resources['expert_ref']))
    if expert_file.read_bytes() != expected:
        raise ValueError('snapshot expert differs from pinned source')
    budget = resources.pop('expert_budget')
    if (not isinstance(budget,dict) or set(budget) != {'timeout_s','max_calls'} or
            type(budget['timeout_s']) not in (int,float) or not math.isfinite(budget['timeout_s']) or
            not 0 < budget['timeout_s'] <= 3600 or type(budget['max_calls']) is not int or
            not 1 <= budget['max_calls'] <= 100000):
        raise ValueError('explicit expert wall/call budgets required')
    if runtime is not None and budget['timeout_s'] > 900:
        raise ValueError('PRoot expert timeout exceeds 900 seconds')
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
        verify_authority()
        try:
            run_expert(expert_file, actions, timeout_s=remaining, max_calls=budget['max_calls'],
                       class_name=class_name, runtime=runtime, expected_source=expected)
        finally:
            if expert_file.read_bytes() != expected:
                raise ValueError('pinned expert source changed during execution')
            verify_authority()
    if runtime_metadata is not None:
        resources['execution_runtime'] = runtime_metadata
    result = record_scene_episode(**resources, expert=None, expert_executor=executor, attempt_id=attempt_id)
    # Detect changed authority even when the recorder returns a partial/error Episode.
    verify_authority()
    if expert_file.read_bytes() != expected:
        raise ValueError('pinned expert source changed during recording')
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
    parser.add_argument('--config-sha256')
    parser.add_argument('--seed',type=int,required=True)
    parser.add_argument('--attempt-id',required=True)
    args=parser.parse_args(argv)
    if sha256_file(args.factory)!=args.factory_sha256:
        parser.error('trusted factory source changed')
    config_value = json.loads(args.config.read_text(encoding='utf-8'))
    if (config_value.get('expert_runtime') is not None and args.config_sha256 is None
            or args.config_sha256 is not None and sha256_file(args.config) != args.config_sha256):
        parser.error('PRoot deployment config requires its exact external SHA-256')
    spec=importlib.util.spec_from_file_location('deployment_scene_factory',args.factory)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    expert, sep, class_name = args.expert.rpartition(':')
    expert_file = Path(expert if sep else args.expert)
    value=execute_job(config_value,module.create_resources,expert_file,args.seed,args.attempt_id,args.output,class_name if sep else None)
    return 0 if value['episode_ref'] is not None else 2




SCENE_INTERFACE_FACT = (
    "Requests are plain JSON objects. move_tcp takes tcp-motion-request/1 (target_pose in a registered frame, "
    "speeds, tolerances, settle_steps, max_control_steps, max_wall_time_s); move_gripper takes "
    "gripper-motion-request/1 {schema_version, width_m, speed_m_s, tolerance_m, settle_steps, max_control_steps}. "
    "Results report status completed|rejected|failed|timeout|aborted|error (gripper also blocked) and reason_code. "
    "There is no wait/reset/step primitive; the host owns physics, measurement and persistence.")


def _bind_scene_brief(result):
    """Scene experts use the worker protocol, not the legacy next_plan/execute_joint_plan port."""
    from datafarm.adapters.agent_harness.scene_execution import SCENE_EXPERT_POLICY
    expert = result.setdefault('expert', {})
    expert['allowed_imports'] = list(SCENE_EXPERT_POLICY['allowed_imports'])
    expert['max_bytes'] = min(int(expert.get('max_bytes', SCENE_EXPERT_POLICY['max_bytes'])), SCENE_EXPERT_POLICY['max_bytes'])
    brief = result.setdefault('brief', {})
    facts = brief.setdefault('facts', {})
    facts['allowed_imports'] = '`math` only'
    facts['scene_interface'] = SCENE_INTERFACE_FACT
    template = Path(__file__).resolve().parents[3] / 'configs' / 'expert-harness' / 'template-scene-brief.md'
    if Path(str(brief.get('template', ''))).name == 'template-brief.md' and template.is_file():
        brief['template'] = str(template)


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
    runtime = config_value.get('expert_runtime')
    if runtime is not None:
        from datafarm.adapters.sandbox.scene_proot import validate_runtime
        validate_runtime(runtime)
        if config_value.get('judge_freeze') != {'directory':str(Path(bundle).resolve()),'sha256':anchor}:
            raise ValueError('PRoot config must pin the same live Judge freeze')
        result.setdefault('isolation', {})['rollout'] = {'mode':'proot', 'acknowledge_weak_isolation':True}
        result['rollout']['trusted_scene'] = {'entry':'datafarm.entry.scene_job',
            'configuration_sha256':sha256_file(config), 'factory_sha256':factory_sha256}
    else:
        if result.get('isolation', {}).get('rollout', {}).get('mode') == 'proot':
            raise ValueError('PRoot rollout requires matching expert_runtime configuration')
    result['task_ir']=dict(store.document(ArtifactRef.from_dict(authority['task_ref'])).payload)
    _bind_scene_brief(result)
    result['verifier'].update(version=contract['freeze_id'],criterion_ids=[c['id'] for c in contract['conditions']],
        criteria=[c['description'] for c in contract['conditions']],criteria_tokens=[c['description'] for c in contract['conditions']],
        judge_bundle={'directory':str(Path(bundle).resolve()),'sha256':anchor})
    result['rollout']['sim']={'argv':[sys.executable,'-m','datafarm.entry.scene_job',
        '--factory',str(Path(factory).resolve()),'--factory-sha256',factory_sha256,
        '--config',str(Path(config).resolve()),'--expert','{run_dir}/expert_snapshot.py:{expert_class}',
        '--seed','{seed}','--attempt-id','{attempt_id}','--output','{run_dir}/native-parent/native'],
        'cwd':'{run_dir}','log':'sim.log','requires_output':'native-parent/native/episode-ref.json',
        'writable_run_subdirs':['native-parent'], 'timeout_s':3600}
    if runtime is not None:
        result['rollout']['sim']['argv'] += ['--config-sha256',sha256_file(config)]
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

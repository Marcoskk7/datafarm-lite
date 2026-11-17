"""Portable source-bound task authority and immutable full-Episode Judge receipts."""
from copy import deepcopy
import json
from pathlib import Path
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.schema import canonical_json_bytes, validate_ref
from datafarm.task.request_profile import validate_task_payload
from datafarm.judge.compiler import verify_bundle, evaluate_bundle
from datafarm.judge.episode_input import project_episode, evidence_path

PROFILE = "datafarm.judge-episode-contract/1"


def source_binding(store, task_ref, scene_ref):
    validate_ref(task_ref, "task_ref")
    validate_ref(scene_ref, "scene_ref")
    if task_ref['kind'] != 'task-ir' or scene_ref['kind'] != 'ir.scene-graph':
        raise ValueError('typed task and scene references required')
    task = store.document(ArtifactRef.from_dict(task_ref))
    payload = validate_task_payload(task.payload)
    graph = store.load(ArtifactRef.from_dict(scene_ref))
    from datafarm.ir.scene_graph import validate
    validate(graph)
    if scene_ref not in [v.to_dict() for v in task.evidence] or payload['scene_id'] != graph['scene_id'] or task.artifact_id != payload['task_revision']:
        raise ValueError('task revision/scene binding differs')
    if len(task.predecessors) != 1 or task.predecessors[0].kind != 'task-request':
        raise ValueError('one normalized request predecessor required')
    request_ref = task.predecessors[0].to_dict()
    from datafarm.task.request_profile import validate_request
    request = validate_request(store.load(task.predecessors[0]))
    if payload['request_id'] != request['request_id']:
        raise ValueError('request identity differs')
    store.load_bytes(ArtifactRef.from_dict(request['original_request_ref']))
    nodes = [n['node_id'] for n in graph['nodes']]
    return {'node_ids':nodes, 'task_id': payload['task_id'], 'task_revision': payload['task_revision'], 'task_ref':deepcopy(task_ref),
            'scene_ref':deepcopy(scene_ref), 'request_ref':request_ref,
            'original_request_ref':deepcopy(request['original_request_ref'])}


def observation_spec(payload, public_task, binding):
    from datafarm.cap.public_task import validate_public_task
    public = validate_public_task(public_task)
    if public['task_id'] != binding['task_id'] or public['task_revision'] != binding['task_revision']:
        raise ValueError('public task binding differs')
    episode = project_episode(payload)
    if any(episode[k] != binding[k] for k in ('task_id', 'task_revision', 'scene_ref', 'node_ids')):
        raise ValueError('measurement exemplar binding differs')
    fields = []
    def walk(value, parts):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, parts+[k])
        elif isinstance(value, list):
            if value and all(isinstance(v, dict) for v in value):
                # Only expose fields present/non-null for all samples.
                common = set(value[0])
                for item in value:
                    common &= set(item)
                for k in sorted(common):
                    walk([v[k] for v in value], parts+[k])
            elif value and all(isinstance(v, (int, float, str, bool)) for v in value):
                fields.append(evidence_path(parts))
            elif value and all(isinstance(v, list) for v in value):
                fields.append(evidence_path(parts))
        elif value is not None:
            fields.append(evidence_path(parts))
    # Measurement channels only; no termination labels/targets as score authority.
    for key in ('states', 'contacts'):
        walk(episode[key], [key])
    from datafarm.judge.episode_input import path_exists
    fields = sorted({f for f in fields if path_exists(episode, f)})
    spec = {'profile':'judge-episode/1', 'source_binding':deepcopy(binding), 'public_task':public,
            'available_fields':fields, 'capabilities':[o['id'] for o in public['observations'] if o['availability']=='available']}
    from .episode_input import validate_observation_spec
    return validate_observation_spec(spec)


def bind_contract(store, task_ref, scene_ref, bundle, anchor, *, allow_fixture=False):
    bundle = Path(bundle)
    binding = source_binding(store, task_ref, scene_ref)
    contract = verify_bundle(bundle, anchor, allow_fixture=allow_fixture)
    if contract.get('schema_version') != 'task-contract/1' or contract.get('source_binding') != binding:
        raise ValueError('freeze belongs to a different task/source revision')
    # Freeze is deployment-local; store its immutable bytes, not manifest locators.
    freeze_ref = store.put_bytes('judge-freeze-anchor', (bundle / 'freeze.json').read_bytes(), media_type='application/vnd.datafarm.freeze+json')
    source_ref = store.put_bytes('judge-source', (bundle / 'judge.py').read_bytes(), media_type='text/x-python')
    frozen_contract_ref = store.put_bytes('judge-frozen-contract', (bundle / 'task-contract.json').read_bytes(), media_type='application/vnd.datafarm.judge-contract+json')
    authority = {'schema':PROFILE, **binding, 'freeze_sha256':anchor, 'freeze_id':contract['freeze_id'],
                 'freeze_ref':freeze_ref.to_dict(), 'frozen_contract_ref':frozen_contract_ref.to_dict(), 'judge_source_ref':source_ref.to_dict(),
                 'conditions':deepcopy(contract['conditions']), 'thresholds':deepcopy(contract['thresholds']),
                 'required_capabilities':deepcopy(contract['required_capabilities'])}
    canonical_json_bytes(authority)
    return store.put_json('judge-episode-contract', authority)


def verify_contract(store, reference, *, task_ref=None, scene_ref=None, bundle=None, anchor=None, allow_fixture=False):
    validate_ref(reference, 'judge_contract_ref')
    if reference['kind'] != 'judge-episode-contract':
        raise ValueError('Judge Episode contract ref required')
    value = store.load(ArtifactRef.from_dict(reference))
    if value.get('schema') != PROFILE or source_binding(store, value['task_ref'], value['scene_ref']) != {k:value[k] for k in ('node_ids','task_id','task_revision','task_ref','scene_ref','request_ref','original_request_ref')}:
        raise ValueError('invalid portable Judge contract binding')
    if task_ref is not None and task_ref != value['task_ref'] or scene_ref is not None and scene_ref != value['scene_ref']:
        raise ValueError('contract task/scene differs')
    if value['freeze_ref']['sha256'] != value['freeze_sha256']:
        raise ValueError('freeze anchor differs')
    frozen_bytes = store.load_bytes(ArtifactRef.from_dict(value['freeze_ref']))
    frozen = json.loads(frozen_bytes)
    source_bytes = store.load_bytes(ArtifactRef.from_dict(value['judge_source_ref']))
    if frozen.get('schema_version') != 'judge-freeze/1' or frozen.get('freeze_id') != value['freeze_id'] or (frozen.get('ready_for_cap') is not True and not allow_fixture):
        raise ValueError('invalid frozen production authority')
    # Verify content retained inside the opaque freeze without using local locators.
    entries = frozen['manifest']['entries']
    contract_digest = frozen['task_contract_ref']['sha256']
    if value['frozen_contract_ref']['sha256'] != contract_digest:
        raise ValueError('frozen contract reference differs')
    original = json.loads(store.load_bytes(ArtifactRef.from_dict(value['frozen_contract_ref'])))
    binding = {k:value[k] for k in ('node_ids','task_id','task_revision','task_ref','scene_ref','request_ref','original_request_ref')}
    if original.get('schema_version') != 'task-contract/1' or original.get('source_binding') != binding or original.get('judge_ref',{}).get('sha256') != value['judge_source_ref']['sha256']:
        raise ValueError('frozen source contract differs')
    if any(original.get(k) != value[k] for k in ('freeze_id','conditions','thresholds','required_capabilities')):
        raise ValueError('portable authority differs from frozen contract')
    if not any(v.get('sha256') == contract_digest for v in entries.values()):
        raise ValueError('frozen TaskContract digest missing from manifest')
    from datafarm.kernel.io import sha256_bytes
    if not any(v.get('sha256') == value['judge_source_ref']['sha256'] for v in entries.values()):
        raise ValueError('Judge source digest missing from freeze manifest')
    if sha256_bytes(source_bytes) != value['judge_source_ref']['sha256']:
        raise ValueError('Judge source bytes differ')
    if bundle is not None:
        if anchor != value['freeze_sha256']:
            raise ValueError('deployment freeze anchor differs')
        current = verify_bundle(bundle, anchor, allow_fixture=allow_fixture)
        if current['freeze_id'] != value['freeze_id'] or current['judge_ref']['sha256'] != value['judge_source_ref']['sha256'] or current['conditions'] != value['conditions'] or current['thresholds'] != value['thresholds']:
            raise ValueError('deployment freeze content differs')
    return value


def evaluate_episode(store, episode_ref, contract_ref, bundle, anchor, *, allow_fixture=False):
    validate_ref(episode_ref, 'episode_ref')
    reference = ArtifactRef.from_dict(episode_ref)
    if reference.kind != 'episode-ir':
        raise ValueError('Episode IR reference required')
    document = store.document(reference)
    payload = document.payload
    setup = payload['setup']
    authority = verify_contract(store, contract_ref, task_ref=setup['task_ref'], scene_ref=setup['scene_ref'], bundle=bundle, anchor=anchor, allow_fixture=allow_fixture)
    evidence = [r.to_dict() for r in document.evidence]
    if setup.get('task_contract_ref') != contract_ref or contract_ref not in evidence or any(setup[k] not in evidence for k in ('task_ref','scene_ref','request_ref','original_request_ref','expert_ref','runtime_ref')):
        raise ValueError('Episode has no exact contract/expert evidence binding')
    binding = {k:authority[k] for k in ('node_ids','task_id','task_revision','task_ref','scene_ref','request_ref','original_request_ref')}
    if any(setup[k] != v for k,v in binding.items()):
        raise ValueError('Episode binding differs from contract')
    graph = store.load(ArtifactRef.from_dict(setup['scene_ref']))
    runtime = store.load(ArtifactRef.from_dict(setup['runtime_ref']))
    if runtime != store.load(ArtifactRef.from_dict(graph['runtime_profile'])) or runtime.get('physics_dt_s') != setup['control_dt_s']:
        raise ValueError('Episode clock/runtime differs from source scene')
    store.load_bytes(ArtifactRef.from_dict(setup['expert_ref']))
    result = evaluate_bundle(bundle, project_episode(payload), anchor, allow_fixture=allow_fixture)
    receipt = {'schema':'datafarm.judge-episode-receipt/1', 'episode_ref':reference.to_dict(),
               'contract_ref':deepcopy(contract_ref), 'freeze_sha256':anchor, 'expert_ref':deepcopy(setup['expert_ref']),
               'execution_status':payload['termination']['status'], 'qualification':'software_fixture' if allow_fixture else 'production_evaluation', 'result':result}
    return receipt, store.put_json('judge-episode-receipt', receipt)

"""Portable source-bound task authority and immutable full-Episode Judge receipts."""
from copy import deepcopy
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.schema import canonical_json_bytes, validate_ref
from datafarm.task.request_profile import validate_task_payload
from datafarm.judge.compiler import verify_bundle, evaluate_bundle
from datafarm.judge.episode_input import project_episode, evidence_path

PROFILE = "datafarm.judge-episode-contract/2"


def source_binding(store, task_ref, scene_ref):
    validate_ref(task_ref, "task_ref")
    validate_ref(scene_ref, "scene_ref")
    if task_ref['kind'] != 'task-ir' or scene_ref['kind'] != 'ir.scene-graph':
        raise ValueError('typed task and scene references required')
    task = store.document(ArtifactRef.from_dict(task_ref))
    payload = validate_task_payload(task.payload)
    graph = store.load(ArtifactRef.from_dict(scene_ref))
    if scene_ref not in [v.to_dict() for v in task.evidence] or payload['scene_id'] != graph['scene_id'] or task.artifact_id != payload['task_revision']:
        raise ValueError('task revision/scene binding differs')
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
    return {'profile':'judge-episode/2', 'source_binding':deepcopy(binding), 'public_task':public,
            'available_fields':fields, 'capabilities':[o['id'] for o in public['observations'] if o['availability']=='available']}


def bind_contract(store, task_ref, scene_ref, bundle, anchor):
    binding = source_binding(store, task_ref, scene_ref)
    contract = verify_bundle(bundle, anchor)
    if contract.get('schema_version') != 'task-contract/2' or contract.get('source_binding') != binding:
        raise ValueError('freeze belongs to a different task/source revision')
    # Freeze is deployment-local; store its immutable bytes, not manifest locators.
    freeze_ref = store.put_bytes('judge-freeze-anchor', (bundle / 'freeze.json').read_bytes(), media_type='application/vnd.datafarm.freeze+json')
    source_ref = store.put_bytes('judge-source', (bundle / 'judge.py').read_bytes(), media_type='text/x-python')
    authority = {'schema':PROFILE, **binding, 'freeze_sha256':anchor, 'freeze_id':contract['freeze_id'],
                 'freeze_ref':freeze_ref.to_dict(), 'judge_source_ref':source_ref.to_dict(),
                 'conditions':deepcopy(contract['conditions']), 'thresholds':deepcopy(contract['thresholds']),
                 'required_capabilities':deepcopy(contract['required_capabilities'])}
    canonical_json_bytes(authority)
    return store.put_json('judge-episode-contract', authority)


def verify_contract(store, reference, *, task_ref=None, scene_ref=None, bundle=None, anchor=None):
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
    store.load_bytes(ArtifactRef.from_dict(value['freeze_ref']))
    store.load_bytes(ArtifactRef.from_dict(value['judge_source_ref']))
    if bundle is not None:
        if anchor != value['freeze_sha256']:
            raise ValueError('deployment freeze anchor differs')
        current = verify_bundle(bundle, anchor)
        if current['freeze_id'] != value['freeze_id'] or current['judge_ref']['sha256'] != value['judge_source_ref']['sha256'] or current['conditions'] != value['conditions'] or current['thresholds'] != value['thresholds']:
            raise ValueError('deployment freeze content differs')
    return value


def evaluate_episode(store, episode_ref, contract_ref, bundle, anchor):
    reference = ArtifactRef.from_dict(episode_ref)
    if reference.kind != 'episode-ir':
        raise ValueError('Episode IR reference required')
    document = store.document(reference)
    payload = document.payload
    setup = payload['setup']
    authority = verify_contract(store, contract_ref, task_ref=setup['task_ref'], scene_ref=setup['scene_ref'], bundle=bundle, anchor=anchor)
    if setup.get('task_contract_ref') != contract_ref or setup['expert_ref'] not in [r.to_dict() for r in document.evidence]:
        raise ValueError('Episode has no exact contract/expert evidence binding')
    binding = {k:authority[k] for k in ('node_ids','task_id','task_revision','task_ref','scene_ref','request_ref','original_request_ref')}
    if any(setup[k] != v for k,v in binding.items()):
        raise ValueError('Episode binding differs from contract')
    result = evaluate_bundle(bundle, project_episode(payload), anchor)
    receipt = {'schema':'datafarm.judge-episode-receipt/2', 'episode_ref':reference.to_dict(),
               'contract_ref':deepcopy(contract_ref), 'freeze_sha256':anchor, 'expert_ref':deepcopy(setup['expert_ref']),
               'execution_status':payload['termination']['status'], 'result':result}
    return receipt, store.put_json('judge-episode-receipt', receipt)

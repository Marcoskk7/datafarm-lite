"""Judge 构建与结果的确定性校验；任务语义由模型生成并独立审查。"""
from copy import deepcopy
import hashlib
import json
import math
import re

BUILD_SCHEMA = 'judge-build-input/1'
CONTRACT_SCHEMA = 'task-contract/1'
RESULT_SCHEMA = 'judge-result/1'
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}')


class JudgeError(ValueError):
    """输入、生成产物或冻结内容不满足契约。"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition, message):
    if not condition:
        raise JudgeError(message)


def identifier(value):
    check(isinstance(value, str) and ID.fullmatch(value) is not None, 'Invalid identifier')
    return value


def text(value):
    check(isinstance(value, str) and bool(value.strip()), 'Expected nonempty text')
    return value


def finite_tree(value):
    """所有来源和结果必须可表示为有限 JSON，不接受代码对象或非有限物理读数。"""
    try:
        canonical(value)
    except (ValueError, TypeError) as exc:
        raise JudgeError('Expected finite JSON data') from exc


def private_fields(value):
    forbidden = {'hidden_seeds', 'heldout', 'evaluation_seeds', 'password', 'api_key', 'credentials', 'judge_source'}
    if isinstance(value, dict):
        check(not set(value) & forbidden, 'Private evaluation or credentials in build input')
        for child in value.values():
            private_fields(child)
    elif isinstance(value, list):
        for child in value:
            private_fields(child)


def normalize_task(raw):
    """Current /1 consumes request-task/1 directly; no legacy ID inference."""
    from datafarm.task.request_profile import validate_task_payload
    from datafarm.kernel.identifiers import logical_id
    check(isinstance(raw, dict), 'Task must be an object')
    payload = validate_task_payload(raw.get('payload', raw))
    logical_id(payload['task_id'])
    requirements = deepcopy(payload['predicates'] + payload['forbidden_shortcuts'])
    ids = [r['id'] for r in requirements]
    check(len(ids) == len(set(ids)) and set(ids) == set(payload['requirement_ids']), 'Task requirement coverage differs')
    return dict(task_id=payload['task_id'], task_version=payload['task_revision'],
                requirements=requirements, provided_thresholds={p['id']:deepcopy(p) for p in payload['parameters']},
                original=deepcopy(raw))


def build_input(task, scene, observations, cases):
    from .episode_input import validate_observation_spec
    value = dict(schema_version=BUILD_SCHEMA, task=normalize_task(task), scene=deepcopy(scene),
                 observations=deepcopy(observations), development_cases=deepcopy(cases))
    finite_tree(value)
    private_fields(value)
    validate_observation_spec(observations)
    binding = observations['source_binding']
    check(binding['task_id'] == value['task']['task_id'] and binding['task_revision'] == value['task']['task_version'],
          'Judge task binding differs')
    from datafarm.kernel.io import sha256_json
    check(sha256_json(task) == binding['task_ref']['sha256'], 'Task IR content differs from its source reference')
    public = observations['public_task']
    payload = task.get('payload', task)
    check(isinstance(scene, dict) and scene.get('scene_id') == public['scene_id'] and sha256_json(scene) == binding['scene_ref']['sha256'],
          'Scene binding differs')
    check(binding['node_ids'] == [n['node_id'] for n in scene['nodes']], 'Source scene nodes differ')
    check(public['roles'] == payload['roles'] and public['instruction'] == payload['demand'] and public['request_id'] == payload['request_id'],
          'Public role/request semantics differ')
    check(public['goals'] + public['constraints'] == payload['predicates'] and public['forbidden_shortcuts'] == payload['forbidden_shortcuts'],
          'Public requirements differ')
    check(public['parameters'] == [{k:deepcopy(v) for k,v in p.items() if k != 'evidence_ref'} for p in payload['parameters']],
          'Public thresholds differ')
    validate_cases(cases)
    return value


def validate_cases(cases):
    check(isinstance(cases, list), 'Cases must be a list')
    ids = []
    for case in cases:
        ids.append(identifier(case.get('id')))
        check(case.get('expected_outcome') in ('success', 'failure', 'error', 'aborted'), 'Invalid case label')
        check(isinstance(case.get('episode'), dict), 'Case episode missing')
        check(isinstance(case.get('violates', []), list), 'Case violates must be a list')
    check(len(set(ids)) == len(ids), 'Duplicate case IDs')
    finite_tree(cases)


def validate_candidate(candidate, inputs):
    """机械校验覆盖关系和不可改阈值；物理充分性由独立审查及正反例核验。"""
    check(isinstance(candidate, dict), 'Judge candidate must be an object')
    check(set(candidate) == {'source', 'conditions', 'thresholds', 'required_capabilities', 'missing_inputs'},
          'Unexpected candidate fields')
    finite_tree(candidate)
    text(candidate['source'])
    check(len(candidate['source'].encode()) <= 64000, 'Judge source too large')
    conditions, thresholds = candidate['conditions'], candidate['thresholds']
    check(isinstance(conditions, list) and bool(conditions), 'Condition list missing')
    check(isinstance(thresholds, list), 'Threshold list missing')
    requirements = {r['id'] for r in inputs['task']['requirements']}
    covered, condition_ids, threshold_ids = set(), [], []
    for spec in thresholds:
        threshold_ids.append(identifier(spec.get('id')))
        check(type(spec.get('value')) in (int, float) and math.isfinite(spec['value']), 'Invalid threshold value')
        text(spec.get('unit'))
        text(spec.get('basis'))
        check(spec.get('origin') in ('provided', 'derived'), 'Invalid threshold origin')
        check(isinstance(spec.get('requirement_ids'), list) and bool(spec['requirement_ids'])
              and set(spec['requirement_ids']) <= requirements, 'Unbound threshold requirements')
    check(len(set(threshold_ids)) == len(threshold_ids), 'Duplicate threshold IDs')
    for condition in conditions:
        condition_ids.append(identifier(condition.get('id')))
        for key in ('description', 'measurement', 'unit'):
            text(condition.get(key))
        refs = condition.get('requirement_ids')
        check(isinstance(refs, list) and refs and set(refs) <= requirements, 'Condition requirement mismatch')
        covered.update(refs)
        check(isinstance(condition.get('threshold_ids'), list) and set(condition['threshold_ids']) <= set(threshold_ids),
              'Condition threshold mismatch')
        fields = condition.get('evidence_fields')
        check(isinstance(fields, list) and fields and set(fields) <= set(inputs['observations']['available_fields']),
              'Condition evidence unavailable')
    check(len(set(condition_ids)) == len(condition_ids), 'Duplicate condition IDs')
    check(covered == requirements, 'Requirements not fully covered')
    indexed = {t['id']: t for t in thresholds}
    for name, supplied in inputs['task']['provided_thresholds'].items():
        got = indexed.get(name, {})
        check(got.get('origin') == 'provided' and got.get('value') == supplied['value']
              and got.get('unit') == supplied['unit'] and got.get('basis') == supplied['basis']
              and got.get('requirement_ids') == supplied['requirement_ids'], 'Provided threshold changed or omitted: ' + name)
    check(isinstance(candidate['missing_inputs'], list) and all(isinstance(v, str) for v in candidate['missing_inputs']),
          'Invalid missing input list')
    caps = candidate['required_capabilities']
    check(isinstance(caps, list) and all(isinstance(v, str) for v in caps), 'Invalid capability list')
    return sorted(set(caps) - set(inputs['observations']['capabilities']))


def validate_episode(episode, contract):
    """Current Judge /1 always checks full measured, source-bound input."""
    from .episode_input import validate_input
    return validate_input(episode, contract)


def result(predicates, metrics=None, reason_code='evaluated'):
    """供生成程序调用的纯函数，统一布尔/null 谓词与最终结果的关系。"""
    values = list(predicates.values())
    outcome = 'error' if any(v is None for v in values) else ('success' if all(values) else 'failure')
    return dict(schema_version=RESULT_SCHEMA, outcome=outcome,
                success=None if outcome == 'error' else outcome == 'success', reason_code=reason_code,
                predicate_results=predicates, metrics=metrics or {},
                terminated=outcome != 'error', truncated=False)


def error_result(reason):
    return dict(schema_version=RESULT_SCHEMA, outcome='error', success=None, reason_code=reason,
                predicate_results={}, metrics={}, terminated=False, truncated=False)


def validate_result(value, contract):
    finite_tree(value)
    check(isinstance(value, dict) and value.get('schema_version') == RESULT_SCHEMA, 'Invalid JudgeResult version')
    ids = {c['id'] for c in contract['conditions']}
    preds = value.get('predicate_results')
    check(isinstance(preds, dict) and set(preds) == ids and all(v is None or type(v) is bool for v in preds.values()),
          'Missing or invalid predicate results')
    expected = result(preds)
    check(value.get('outcome') == expected['outcome'] and value.get('success') is expected['success'],
          'Verdict contradicts predicates')
    check(isinstance(value.get('metrics'), dict), 'Metrics must be an object')
    text(value.get('reason_code'))
    # 任务谓词与指标来自生成程序；终止/截断等协议元信息由宿主统一包装，不能交给
    # 模型的自定义包装函数决定。这里不修改任何测量值或谓词真假。
    return result(preds, value['metrics'], value['reason_code'])

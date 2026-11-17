"""Judge 构建与结果的确定性校验；任务语义由模型生成并独立审查。"""
from copy import deepcopy
import hashlib
import json
import math
import re

BUILD_SCHEMA = 'judge-build-input/1'
BUILD_SCHEMA_V2 = 'judge-build-input/2'
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
    """适配已有 A 线任务字典/Task IR；新增结构只存在于 B 的内部构建请求中。"""
    check(isinstance(raw, dict), 'Task must be an object')
    payload = raw.get('payload', raw)
    if payload.get('profile') == 'request-task/1':
        from datafarm.task.request_profile import validate_task_payload
        payload = validate_task_payload(payload)
        from datafarm.kernel.identifiers import logical_id
        logical_id(payload['task_id'])
        requirements = deepcopy(payload['predicates'] + payload['forbidden_shortcuts'])
        check({r['id'] for r in requirements} == set(payload['requirement_ids']), 'Task requirement coverage differs')
        return dict(task_id=payload['task_id'], task_version=payload['task_revision'],
                    requirements=requirements, provided_thresholds={p['id']:deepcopy(p) for p in payload['parameters']},
                    original=deepcopy(raw))
    task_id = raw.get('task_id', raw.get('name', raw.get('artifact_id')))
    identifier(task_id)
    requirements = payload.get('requirements')
    if requirements is None:
        requirements = [{'id': 'task.goal', 'text': text(payload.get('instruction', payload.get('demand')))}]
        for name, predicate in payload.get('predicates', {}).items():
            requirements.append({'id': identifier(name), 'text': text(predicate.get('expr'))})
    check(isinstance(requirements, list) and bool(requirements), 'Task requirements missing')
    ids = []
    for item in requirements:
        ids.append(identifier(item.get('id')))
        text(item.get('text'))
    check(len(set(ids)) == len(ids), 'Duplicate requirement IDs')
    # provided_thresholds 的值和单位是上游明确给定值，模型不得修改或删除。
    thresholds = payload.get('provided_thresholds', payload.get('thresholds', {}))
    check(isinstance(thresholds, dict), 'thresholds must be an object')
    for name, spec in thresholds.items():
        identifier(name)
        check(isinstance(spec, dict) and type(spec.get('value')) in (int, float), 'Threshold requires value and unit')
        text(spec.get('unit'))
    return dict(task_id=task_id, task_version=str(raw.get('task_version', '1')),
                requirements=deepcopy(requirements), provided_thresholds=deepcopy(thresholds), original=deepcopy(raw))


def build_input(task, scene, observations, cases):
    value = dict(schema_version=BUILD_SCHEMA, task=normalize_task(task), scene=deepcopy(scene),
                 observations=deepcopy(observations), development_cases=deepcopy(cases))
    if task.get('payload', task).get('profile') == 'request-task/1':
        value['schema_version'] = BUILD_SCHEMA_V2
        check(observations.get('profile') == 'judge-episode/2', 'Measured task requires Judge observations/2')
        check(isinstance(observations.get('source_binding'), dict), 'Judge source binding missing')
        check(observations['source_binding']['task_id'] == value['task']['task_id'] and
              observations['source_binding']['task_revision'] == value['task']['task_version'], 'Judge task binding differs')
    finite_tree(value)
    private_fields(value)
    check(isinstance(scene, dict), 'Scene metadata must be an object')
    check(isinstance(observations, dict), 'Observation specification missing')
    for key in ('capabilities', 'available_fields'):
        check(isinstance(observations.get(key), list) and all(isinstance(v, str) for v in observations[key]),
              'Observation specification needs ' + key)
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
    # 已有 stage-graph/1 的映射属于上游契约，不能让模型重命名后破坏阶段交叉引用。
    original = inputs['task']['original'].get('payload', inputs['task']['original'])
    required_ids = {p.get('judge_criterion') for p in (original.get('predicates', {}).values() if isinstance(original.get('predicates'), dict) else [])
                    if p.get('kind') in ('terminal', 'transient') and p.get('judge_criterion')}
    if required_ids:
        check(set(condition_ids) == required_ids, 'Upstream judge_criterion IDs changed')
    check(covered == requirements, 'Requirements not fully covered')
    indexed = {t['id']: t for t in thresholds}
    for name, supplied in inputs['task']['provided_thresholds'].items():
        got = indexed.get(name, {})
        check(got.get('origin') == 'provided' and got.get('value') == supplied['value']
              and got.get('unit') == supplied['unit'], 'Provided threshold changed or omitted: ' + name)
    check(isinstance(candidate['missing_inputs'], list) and all(isinstance(v, str) for v in candidate['missing_inputs']),
          'Invalid missing input list')
    caps = candidate['required_capabilities']
    check(isinstance(caps, list) and all(isinstance(v, str) for v in caps), 'Invalid capability list')
    return sorted(set(caps) - set(inputs['observations']['capabilities']))


def has_path(value, path):
    """路径以点分隔，数组自动对每条记录核对；不通过目标值补齐缺失观测。"""
    if not path:
        return value is not None
    if isinstance(value, list):
        return bool(value) and all(has_path(v, path) for v in value)
    head, *tail = path
    return isinstance(value, dict) and head in value and has_path(value[head], tail)


def validate_episode(episode, contract):
    finite_tree(episode)
    if contract.get('schema_version') == 'task-contract/2':
        from .episode_input import validate_input
        return validate_input(episode, contract)
    check(episode.get('schema_version') == 'judge-episode/1', 'Unsupported episode version')
    check(episode.get('units') == {'length':'m', 'angle':'rad', 'time':'s'} and episode.get('frame_id') == 'world',
          'Episode requires explicit SI units and world frame')
    dt = episode.get('control_dt_s')
    check(type(dt) in (int, float) and math.isfinite(dt) and dt > 0, 'Episode control_dt_s missing or invalid')
    states, actions = episode.get('states'), episode.get('actions')
    check(isinstance(states, list) and len(states) >= 2 and isinstance(actions, list)
          and len(actions) + 1 == len(states), 'Expected N actions and N+1 full measured states')
    check([s.get('control_step') for s in states] == list(range(len(states))), 'State clock gaps or reorder')
    check(all(type(s.get('control_step')) is int for s in states), 'Invalid state clock')
    check(all(isinstance(a, dict) and type(a.get('control_step')) is int and a['control_step'] == i
              for i, a in enumerate(actions)), 'Action clock gaps or reorder')
    for state in states:
        if 'time_s' in state:
            check(type(state['time_s']) in (int, float) and math.isclose(state['time_s'], state['control_step']*dt,
                  abs_tol=1e-8), 'State time disagrees with control clock')
        if 'units' in state:
            check(state['units'] == episode['units'], 'State unit mismatch')
        if 'frame_id' in state:
            check(state['frame_id'] == 'world', 'State frame mismatch')
        for key in ('q', 'dq'):
            if key in state:
                check(isinstance(state[key], list) and all(type(v) in (int, float) for v in state[key]),
                      'Invalid measured joint vector')
        if 'joint_names' in state:
            names = state['joint_names']
            check(isinstance(names, list) and all(isinstance(n, str) and n for n in names)
                  and len(set(names)) == len(names) and ('q' not in state or len(names) == len(state['q'])),
                  'Measured joint names/order dimensions invalid')
    for condition in contract['conditions']:
        for field in condition['evidence_fields']:
            check(has_path(episode, field.split('.')), 'Required evidence missing: ' + field)


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

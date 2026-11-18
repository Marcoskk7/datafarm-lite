"""将已冻结 Judge 直接接入既有 adapter/1，不建立历史任务迁移分支。"""
from pathlib import Path
import sys

from .compiler import verify_bundle
from .protocol import check

ADAPTER_SCHEMA = 'datafarm.expert-harness.adapter/1'


def adapter_bundle(adapter):
    spec = adapter.verifier.get('judge_bundle')
    check(isinstance(spec, dict) and set(spec) == {'directory', 'sha256'}, 'Generated judge bundle binding missing')
    bundle = Path(adapter.x(spec['directory'])).resolve()
    contract = verify_bundle(bundle, spec['sha256'])
    check(contract['task_id'] == adapter.task_id, 'Judge/adapter task ID mismatch')
    check(adapter.verifier.get('criterion_ids') == [c['id'] for c in contract['conditions']], 'Judge criterion IDs changed')
    check(adapter.verifier.get('criteria') == [c['description'] for c in contract['conditions']], 'Judge criteria description changed')
    check(adapter.verifier.get('version') == contract['freeze_id'], 'Judge version mismatch')
    check(adapter.rollout.get('verdict', {}).get('schema') == 'judge-result/1', 'Generated judge needs structured verdict')
    return bundle, contract


def attach_adapter(raw, bundle, anchor, episode_relative):
    """只生成新版 adapter，不修改原文件、A 线字段或旧任务证据。"""
    from copy import deepcopy
    from datafarm.kernel.identifiers import logical_id, require_identity_profile
    check(raw.get('schema') == ADAPTER_SCHEMA, 'New adapter/1 required')
    require_identity_profile(raw)
    logical_id(raw.get('task_id'), label='task_id')
    result = deepcopy(raw)
    contract = verify_bundle(bundle, anchor)
    check(result['task_id'] == contract['task_id'], 'Task ID mismatch')
    relative = Path(episode_relative)
    check(not relative.is_absolute() and '..' not in relative.parts and bool(relative.parts), 'Episode must stay in rollout')
    result['schema'] = ADAPTER_SCHEMA
    result['verifier'].update(version=contract['freeze_id'],
                              criteria=[c['description'] for c in contract['conditions']],
                              criterion_ids=[c['id'] for c in contract['conditions']],
                              judge_bundle={'directory': str(Path(bundle).resolve()), 'sha256': anchor})
    # 已由冻结条件重建任务书内容，不能继续沿用输入模板的旧判据词条。
    result['verifier']['criteria_tokens'] = [c['description'] for c in contract['conditions']]
    # 通过正式入口模块执行，不写死仓库内脚本路径；rollout 环境须能导入 datafarm。
    result['rollout']['judge'] = [dict(name='generated-judge', argv=[sys.executable, '-m', 'datafarm.entry.judge_cli',
        'evaluate', '--bundle', str(Path(bundle).resolve()), '--freeze-sha256', anchor,
        '--episode', '{run_dir}/' + relative.as_posix(), '--output', '{run_dir}/verdict.json'])]
    result['rollout']['verdict'] = dict(file='verdict.json', success='success', schema='judge-result/1',
                                       fields={'reason_code':'reason_code', 'predicate_results':'predicate_results', 'metrics':'metrics'})
    return result


# 记录器 termination.reason_code 中可归责于 expert 的原因；这些 rollout 计入预算（counted）。
EXPERT_EXECUTION_REASONS = frozenset({
    'JOINT_LIMIT', 'SPEED_LIMIT', 'TRACKING_ERROR', 'LOCAL_IK_NONCONVERGENCE', 'GRIPPER_NONCONVERGENCE',
    'WALL_TIME_LIMIT', 'INVALID_ARGUMENT', 'UNKNOWN_FRAME', 'BUDGET_EXHAUSTED', 'CALL_BUDGET',
    'CONTROL_BUDGET', 'EXPERT_EXCEPTION', 'EXPERT_WALL_BUDGET', 'EXPERT_PROTOCOL_ERROR',
    'EXPERT_AUDIT_REJECTED'})


def execution_reason(value):
    """从 ``EXECUTION_<STATUS>:<REASON>`` 中取出记录器原因；不匹配时返回 None。"""
    code = value.get('reason_code') if isinstance(value, dict) else None
    if not isinstance(code, str) or not code.startswith('EXECUTION_') or ':' not in code:
        return None
    return code.split(':', 1)[1]


def classify_result(value):
    """新协议不对 null/string 使用 bool()；旧 adapter 的布尔协议不受影响。"""
    if not isinstance(value, dict) or value.get('schema_version') != 'judge-result/1':
        return 'judge_failure', None, 'Missing or invalid generated JudgeResult'
    outcome, success = value.get('outcome'), value.get('success')
    if outcome in ('success', 'failure') and type(success) is bool and success == (outcome == 'success'):
        predicates = value.get('predicate_results')
        if predicates is not None and (not isinstance(predicates, dict) or not predicates or any(type(v) is not bool for v in predicates.values()) or all(predicates.values()) != success):
            return 'judge_failure', None, 'Judge predicates contradict success'
        return 'judged', success, str(value.get('reason_code', outcome))
    if outcome == 'aborted' and success is None:
        return 'aborted', None, str(value.get('reason_code', 'aborted'))
    if outcome == 'error' and success is None and execution_reason(value) in EXPERT_EXECUTION_REASONS:
        # expert 自身导致的执行失败是有效的“专家失败”，计数且 success=False；宿主故障仍不计数。
        return 'expert_error', False, str(value['reason_code'])
    return 'judge_failure', None, str(value.get('reason_code', 'invalid or unavailable judge evidence'))

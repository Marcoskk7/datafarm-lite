"""Execution-outcome classification shared by the broker (expert) and the Judge glue.

Lives in ``ir`` so ``expert`` no longer imports ``judge`` just to classify a run:

* ``classify_result`` maps a ``judge-result/1`` verdict (holdout / final judgement);
* ``classify_execution`` maps the recorder summary (``episode-ref.json``) for
  development rollouts, which are executed but never judged: CaP iterates with its
  own self-check and the frozen Judge only scores the final submission.
"""

# Recorder ``termination.reason_code`` values attributable to the expert; such runs are counted.
EXPERT_EXECUTION_REASONS = frozenset({
    'JOINT_LIMIT', 'SPEED_LIMIT', 'TRACKING_ERROR', 'LOCAL_IK_NONCONVERGENCE', 'GRIPPER_NONCONVERGENCE',
    'WALL_TIME_LIMIT', 'INVALID_ARGUMENT', 'UNKNOWN_FRAME', 'BUDGET_EXHAUSTED', 'CALL_BUDGET',
    'CONTROL_BUDGET', 'EXPERT_EXCEPTION', 'EXPERT_WALL_BUDGET', 'EXPERT_PROTOCOL_ERROR',
    'EXPERT_AUDIT_REJECTED'})
EXECUTION_STATUSES = ('completed', 'failed', 'error', 'timeout', 'aborted')


def execution_reason(value):
    """Return the recorder reason from ``EXECUTION_<STATUS>:<REASON>``, else None."""
    code = value.get('reason_code') if isinstance(value, dict) else None
    if not isinstance(code, str) or not code.startswith('EXECUTION_') or ':' not in code:
        return None
    return code.split(':', 1)[1]


def classify_result(value):
    """Classify a generated ``judge-result/1``; never coerces null/string with bool()."""
    if not isinstance(value, dict) or value.get('schema_version') != 'judge-result/1':
        return 'judge_failure', None, 'Missing or invalid generated JudgeResult'
    outcome, success = value.get('outcome'), value.get('success')
    if outcome in ('success', 'failure') and type(success) is bool and success == (outcome == 'success'):
        predicates = value.get('predicate_results')
        if predicates is not None and (not isinstance(predicates, dict) or not predicates
                                       or any(type(v) is not bool for v in predicates.values())
                                       or all(predicates.values()) != success):
            return 'judge_failure', None, 'Judge predicates contradict success'
        return 'judged', success, str(value.get('reason_code', outcome))
    if outcome == 'aborted' and success is None:
        return 'aborted', None, str(value.get('reason_code', 'aborted'))
    if outcome == 'error' and success is None and execution_reason(value) in EXPERT_EXECUTION_REASONS:
        # An execution failure caused by the expert is a valid, counted expert failure.
        return 'expert_error', False, str(value['reason_code'])
    return 'judge_failure', None, str(value.get('reason_code', 'invalid or unavailable judge evidence'))


def classify_execution(summary):
    """Classify a development rollout from the recorder summary, without any Judge.

    ``executed`` is counted with ``success=None``: whether the task was achieved is
    decided by the author's own self-check, never by the frozen Judge.
    """
    if not isinstance(summary, dict):
        return 'sim_failure', None, 'missing or invalid recorder summary'
    status, reason = summary.get('execution_status'), summary.get('reason_code')
    if summary.get('episode_ref') is None:
        return 'sim_failure', None, 'no recorded Episode (initialization failed: %s)' % (reason or 'unknown')
    if status not in EXECUTION_STATUSES:
        return 'sim_failure', None, 'unknown execution status %r' % (status,)
    code = 'EXECUTION_%s:%s' % (status.upper(), reason or 'UNSPECIFIED')
    if status == 'completed':
        if summary.get('trace_complete') is not True:
            return 'sim_failure', None, code + ' with an incomplete trace'
        return 'executed', None, code
    if reason in EXPERT_EXECUTION_REASONS:
        return 'expert_error', False, code
    return 'sim_failure', None, code + ' (host-side; not counted)'

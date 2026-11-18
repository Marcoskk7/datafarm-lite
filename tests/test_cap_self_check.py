'''Plan A: CaP self-check worker and judge-free development classification.

CPU-only protocol fixtures; the worker's rlimits apply on Linux/POSIX.
'''
import json
import sys

import pytest

from datafarm.cap.self_check import SelfCheckError, run_check, validate
from datafarm.ir.execution_outcome import classify_execution, classify_result
from datafarm.expert.rollout import classify_run_dir, dev_outcome_file

CHECK = (
    "import math\n"
    "def check(episode):\n"
    "    final = episode['privileged_state']['final']['x']\n"
    "    return {'success': abs(final - 1.0) < 0.01, 'score': max(0.0, 1.0 - abs(final - 1.0)), 'reason': 'x'}\n")


def episode(x):
    return {'phase': 'debug', 'execution': {'expert_error': False}, 'trace': [],
            'privileged_state': {'final': {'x': x}}}


def test_check_runs_in_child_and_scores_each_episode():
    results = run_check(CHECK, [episode(1.0), episode(0.5)])
    assert [r['success'] for r in results] == [True, False]
    assert results[1]['score'] == pytest.approx(0.5)


def test_check_fault_fails_only_that_episode():
    results = run_check("def check(episode):\n    return episode['missing']\n", [episode(1.0)])
    assert results[0]['success'] is False and results[0]['reason'].startswith('self_check_error: KeyError')


@pytest.mark.parametrize('source', ["import os\ndef check(e):\n    return True\n",
                                    "def check(e):\n    return open('/etc/passwd')\n",
                                    "def check(e):\n    return e.__class__\n",
                                    "def helper(e):\n    return True\n"])
def test_policy_rejects_escape_and_missing_entry(source):
    with pytest.raises(ValueError):
        validate(source)


@pytest.mark.skipif(sys.platform == 'win32', reason='rlimit/timeout enforcement is POSIX-first')
def test_runaway_check_hits_wall_or_cpu_limit():
    with pytest.raises(SelfCheckError):
        run_check("def check(e):\n    while True:\n        pass\n", [episode(1.0)], timeout_s=2)


def test_malformed_return_is_a_failed_episode():
    results = run_check("def check(e):\n    return {'success': 1}\n", [episode(1.0)])
    assert results[0]['success'] is False


def test_execution_summary_classification():
    ok = {'episode_ref': {'id': 'e'}, 'execution_status': 'completed', 'reason_code': 'EXPERT_RETURNED',
          'trace_complete': True}
    assert classify_execution(ok)[:2] == ('executed', None)
    assert classify_execution({**ok, 'execution_status': 'failed', 'reason_code': 'JOINT_LIMIT'})[:2] == ('expert_error', False)
    assert classify_execution({**ok, 'execution_status': 'error', 'reason_code': 'HOST_REQUALIFICATION_REQUIRED'})[0] == 'sim_failure'
    assert classify_execution({**ok, 'episode_ref': None})[0] == 'sim_failure'
    assert classify_execution({**ok, 'trace_complete': False})[0] == 'sim_failure'
    from datafarm.judge.integration import classify_result as legacy
    assert legacy is classify_result


def test_dev_rollout_ignores_verdict_and_holdout_requires_it(tmp_path):
    cfg = {'dev_outcome': {'schema': 'execution-summary/1', 'file': 'native/episode-ref.json'},
           'verdict': {'schema': 'judge-result/1', 'file': 'verdict.json', 'success': 'success'},
           'sim': {'requires_output': 'native/episode-ref.json'}}
    assert dev_outcome_file(cfg, 'dev') == 'native/episode-ref.json'
    assert dev_outcome_file(cfg, 'holdout') is None
    (tmp_path / 'native').mkdir()
    (tmp_path / 'native' / 'episode-ref.json').write_text(json.dumps(
        {'episode_ref': {'id': 'e'}, 'execution_status': 'completed', 'reason_code': 'EXPERT_RETURNED',
         'trace_complete': True}))
    (tmp_path / 'harness-job.json').write_text(json.dumps({'sim_exit': 0}))
    (tmp_path / 'verdict.json').write_text(json.dumps({'schema_version': 'judge-result/1', 'outcome': 'success',
                                                       'success': True}))
    dev = classify_run_dir(tmp_path, cfg, purpose='dev')
    assert (dev.outcome_class, dev.counted, dev.success) == ('executed', True, None)
    holdout = classify_run_dir(tmp_path, cfg, purpose='holdout')
    assert (holdout.outcome_class, holdout.success) == ('judged', True)

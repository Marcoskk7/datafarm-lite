"""通用 Judge 构建器：候选生成 → 宿主检查 → 独立审查 → 自动冻结。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil

from datafarm.cap.io import read_json, write_json, write_source
from datafarm.expert.manifest import build_manifest, diff_manifests
from datafarm.kernel.io import sha256_json as model_digest
from .execution import audit_source, run_source
from .protocol import (BUILD_SCHEMA, CONTRACT_SCHEMA, JudgeError, check, file_sha, identifier,
                       private_fields, sha, validate_candidate, validate_cases)


def ref(path, kind, media='application/json'):
    return dict(kind=kind, sha256=file_sha(path), media_type=media)


def immutable_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if path.exists():
        check(path.read_bytes() == data, 'Persisted attempt changed: ' + str(path))
    else:
        write_json(path, value)


def public_task(inputs):
    """Only the validated public projection may enter CaP context."""
    from datafarm.cap.public_task import validate_public_task
    return validate_public_task(inputs['observations']['public_task'])


def model_inputs(inputs):
    """模型只需任务与状态规格；完整开发轨迹留给宿主，避免上下文膨胀和样例记忆。"""
    value = {k: deepcopy(v) for k, v in inputs.items() if k != 'development_cases'}
    value['development_case_summary'] = [dict(id=c['id'], expected_outcome=c['expected_outcome'],
        violates=c.get('violates', []), state_count=len(c['episode'].get('states', [])))
        for c in inputs['development_cases']]
    return value


def contract_for(candidate, inputs, identity):
    contract = dict(schema_version=CONTRACT_SCHEMA, task_id=inputs['task']['task_id'],
                task_version=inputs['task']['task_version'], freeze_id=identity,
                requirements=deepcopy(inputs['task']['requirements']), conditions=deepcopy(candidate['conditions']),
                thresholds={t['id']: deepcopy(t) for t in candidate['thresholds']},
                required_capabilities=candidate['required_capabilities'][:],
                observations=deepcopy(inputs['observations']), scene_sha256=sha(inputs['scene']),
                public_task=public_task(inputs))
    contract['source_binding'] = deepcopy(inputs['observations']['source_binding'])
    return contract


def test_cases(source, contract, scene, cases):
    reports = []
    for case in cases:
        verdict = run_source(source, case['episode'], contract, scene)
        repeated = run_source(source, case['episode'], contract, scene)
        expected = case['expected_outcome']
        # 反例不仅要总体失败，还须真实触发所标注的那个条件，避免伪造覆盖。
        violations = case.get('violates', [])
        deterministic = sha(verdict) == sha(repeated)
        matched = deterministic and verdict['outcome'] == expected and all(verdict['predicate_results'].get(v) is False for v in violations)
        reports.append(dict(id=case['id'], expected=expected, violates=violations, deterministic=deterministic,
                            passed=matched, result=verdict))
    return reports


def reviewed_cases(review, candidate):
    check(isinstance(review, dict) and set(review) == {'verdict', 'condition_checks', 'independent_cases', 'issues'},
          'Invalid independent review')
    check(review['verdict'] in ('pass', 'revise', 'needs_input'), 'Unknown review verdict')
    check(isinstance(review['issues'], list) and all(isinstance(v, str) for v in review['issues']), 'Invalid review issues')
    ids = {c['id'] for c in candidate['conditions']}
    checks = review['condition_checks']
    check(isinstance(checks, list) and {c.get('condition_id') for c in checks} == ids and len(checks) == len(ids),
          'Review did not cover every condition')
    if review['verdict'] == 'pass':
        check(not review['issues'] and all(c.get(k) is True for c in checks
              for k in ('faithful', 'thresholds_supported', 'observables_sufficient')), 'Review attestation incomplete')
    cases = []
    for case in review['independent_cases']:
        cases.append(dict(id=case['id'], episode=json.loads(case['episode_json']),
                          expected_outcome=case['expected_outcome'], violates=case['violates']))
    validate_cases(cases)
    if review['verdict'] == 'pass':
        check({'success', 'failure'} <= {c['expected_outcome'] for c in cases},
              'Independent review requires positive and negative cases')
    return cases


def runtime_dependencies():
    """判定语义依赖也纳入冻结，不能只哈希生成的入口程序。"""
    root = Path(__file__).resolve().parent
    # 包初始化与 JSON 读取器同样能影响判定输入，不能只绑定 evaluate 的直接调用文件。
    # 正式入口 entry/judge_cli.py 与注入的 Codex 传输同样纳入：改动它们必须使冻结失效。
    return sorted(root.glob('*.py')) + [root.parent / n for n in (
        '__init__.py', 'kernel/__init__.py', 'kernel/io.py', 'kernel/file_lock.py',
        'cap/__init__.py', 'cap/io.py', 'adapters/__init__.py', 'adapters/agent_harness/__init__.py',
        'adapters/agent_harness/codex/__init__.py', 'adapters/agent_harness/codex/structured.py',
        'adapters/agent_harness/codex/fyp_gateway.py', 'entry/codex_gateway.py',
        'adapters/agent_harness/scene_execution.py', 'adapters/agent_harness/scene_worker.py', 'entry/scene_job.py',
        'entry/scene_rollout.py',
        'expert/__init__.py', 'expert/manifest.py', 'expert/util.py', 'expert/state.py',
        'expert/layout.py', 'entry/__init__.py', 'entry/judge_cli.py', 'kernel/identifiers.py', 'kernel/schema.py', 'kernel/artifacts.py',
        'expert/episode.py', 'task/__init__.py', 'task/request_profile.py', 'cap/public_task.py', 'task/planning.py',
        'kernel/store.py', 'ir/__init__.py', 'ir/common.py', 'ir/scene_graph.py', 'ir/asset_record.py')]


def freeze(folder, candidate, contract, inputs, author, reviewer, reports, cases, dependencies):
    """所有门槛通过后生成最终包；freeze.json 是外部 adapter 固定的内容锚点。"""
    bundle = folder / 'frozen'
    production = author['kind'] == reviewer['kind'] == 'codex_cli'
    if production:
        for response, execution in [(candidate, author), (read_json(folder / 'review.json'), reviewer)]:
            receipt = execution.get('receipt', {})
            check(receipt.get('status') == 'complete' and receipt.get('decision_sha256') == model_digest(response)
                  and execution.get('response_sha256') == model_digest(response)
                  and isinstance(execution.get('events_sha256'), str) and len(execution['events_sha256']) == 64,
                  'Completed model execution evidence missing or response changed')
    # 部分写入后中断可续接；每个已经落盘的文件必须字节一致，禁止回填新内容。
    if (bundle / 'freeze.json').exists():
        verify_bundle(bundle, file_sha(bundle / 'freeze.json'), allow_fixture=True)
        frozen = read_json(bundle / 'freeze.json')
        return dict(status='frozen', ready_for_cap=frozen['ready_for_cap'], bundle=str(bundle.resolve()),
                    freeze_ref=ref(bundle / 'freeze.json', 'judge-freeze'), task_contract_ref=frozen['task_contract_ref'])
    bundle.mkdir(exist_ok=True)
    source = candidate['source'].replace('\r\n', '\n').encode('utf-8')
    if (bundle / 'judge.py').exists():
        check((bundle / 'judge.py').read_bytes() == source, 'Persisted judge source changed')
    else:
        write_source(bundle / 'judge.py', candidate['source'])
    for name, value in [('conditions', candidate['conditions']), ('input', inputs), ('scene', inputs['scene']),
                        ('cases', cases), ('validation', reports), ('author-execution', author),
                        ('reviewer-execution', reviewer), ('review', read_json(folder / 'review.json'))]:
        immutable_json(bundle / (name + '.json'), value)
    contract.update(judge_ref=ref(bundle / 'judge.py', 'judge-source', 'text/x-python'),
                    negative_controls_ref=ref(bundle / 'cases.json', 'judge-controls'),
                    validation_ref=ref(bundle / 'validation.json', 'judge-validation'),
                    review_ref=ref(bundle / 'review.json', 'judge-review'), ready_for_cap=production)
    immutable_json(bundle / 'task-contract.json', contract)
    copied = []
    for name, path in dependencies.items():
        identifier(name)
        target = bundle / 'dependencies' / name
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            check(file_sha(target) == file_sha(path), 'Persisted dependency changed')
        else:
            shutil.copyfile(path, target)
        copied.append(target.resolve())
    # 原始部署依赖也必须保护：只验证包内副本会漏掉运行中实际使用的资产被替换。
    protected = sorted(p.resolve() for p in bundle.iterdir() if p.is_file()) + copied + list(dependencies.values()) + runtime_dependencies()
    baseline = build_manifest([str(p) for p in protected], extra={'freeze_id': contract['freeze_id']})
    check(all(v['kind'] == 'file' for v in baseline['entries'].values()), 'Frozen dependency missing or not regular')
    frozen = dict(schema_version='judge-freeze/1', freeze_id=contract['freeze_id'],
                  ready_for_cap=production, manifest=baseline, task_contract_ref=ref(bundle / 'task-contract.json', 'task-contract'))
    write_json(bundle / 'freeze.json', frozen)
    return dict(status='frozen', ready_for_cap=production, bundle=str(bundle.resolve()),
                freeze_ref=ref(bundle / 'freeze.json', 'judge-freeze'), task_contract_ref=frozen['task_contract_ref'])


def verify_bundle(bundle, expected_sha256=None, *, allow_fixture=False):
    bundle = Path(bundle).resolve()
    check(allow_fixture or isinstance(expected_sha256, str) and len(expected_sha256) == 64,
          'Production evaluation requires an external freeze SHA-256 anchor')
    if expected_sha256:
        check(file_sha(bundle / 'freeze.json') == expected_sha256, 'Freeze anchor changed')
    frozen = read_json(bundle / 'freeze.json')
    check(frozen.get('schema_version') == 'judge-freeze/1', 'Unsupported freeze version')
    check(frozen.get('ready_for_cap') is True or allow_fixture, 'Scripted fixture cannot start production CaP')
    baseline = frozen['manifest']
    current = build_manifest(baseline['entries'], extra=baseline.get('extra'))
    check(not diff_manifests(baseline, current) and current['digest'] == baseline['digest'], 'Frozen judge dependency changed')
    contract = read_json(bundle / 'task-contract.json')
    check(contract['schema_version'] == CONTRACT_SCHEMA and contract['freeze_id'] == frozen['freeze_id'], 'Contract mismatch')
    from .episode_input import validate_observation_spec
    validate_observation_spec(contract['observations'])
    check(contract.get('source_binding') == contract['observations']['source_binding'], 'Contract source binding differs')
    from .protocol import build_input
    inputs = read_json(bundle / 'input.json')
    check(build_input(inputs['task']['original'], inputs['scene'], inputs['observations'], inputs['development_cases']) == inputs,
          'Frozen input uses an obsolete or invalid /1 layout')
    check(contract['task_id'] == inputs['task']['task_id'] and contract['task_version'] == inputs['task']['task_version'],
          'Frozen task identity differs')
    candidate = dict(source=(bundle / 'judge.py').read_text(encoding='utf-8'),
                     conditions=contract['conditions'], thresholds=list(contract['thresholds'].values()),
                     required_capabilities=contract['required_capabilities'], missing_inputs=[])
    check(not validate_candidate(candidate, inputs), 'Frozen Judge has unsupported capabilities')
    expected = contract_for(candidate, inputs, frozen['freeze_id'])
    check(all(contract.get(k) == v for k, v in expected.items()), 'Frozen contract semantics differ from input')
    check(contract.get('ready_for_cap') is frozen['ready_for_cap'], 'Frozen readiness differs')
    check(file_sha(bundle / 'task-contract.json') == frozen['task_contract_ref']['sha256'], 'TaskContract changed')
    check(file_sha(bundle / 'judge.py') == contract['judge_ref']['sha256'], 'Judge source changed')
    return contract


def evaluate_bundle(bundle, episode, expected_sha256=None, *, allow_fixture=False):
    # 内容异常不是合法任务失败，应直接拒绝使用这个冻结版本。
    contract = verify_bundle(bundle, expected_sha256, allow_fixture=allow_fixture)
    bundle = Path(bundle)
    return run_source((bundle / 'judge.py').read_text(encoding='utf-8'), episode,
                      contract, read_json(bundle / 'scene.json'))


class JudgeCompiler:
    """最多三版候选；身份与中间产物落盘，中断后只续接原调用，不隐式重发。"""
    def __init__(self, provider, *, max_candidates=3):
        check(type(max_candidates) is int and 1 <= max_candidates <= 3, 'At most three candidate versions')
        self.provider, self.max_candidates = provider, max_candidates

    def _complete(self, work, role, payload):
        """两角色共用任务调用账本；费用未知保留 null，不伪装成零或硬预算。"""
        path = work / 'model-usage.json'
        ledger = read_json(path) if path.exists() else dict(schema_version='judge-model-usage/1',
            cost_mode='advisory', cost_usd=None, cost_status='unknown', calls={})
        key = sha({'role': role, 'payload': payload, 'provider': getattr(self.provider, 'fingerprint', None)})
        previous = ledger['calls'].get(key, {})
        ledger['calls'][key] = {**previous, 'role':role, 'status':'starting', 'cost_usd':None}
        write_json(path, ledger)
        try:
            value, execution = self.provider.complete(role, payload)
        except BaseException:
            ledger['calls'][key]['status'] = 'pending_or_failed'
            write_json(path, ledger)
            raise
        receipt = execution.get('receipt', {})
        ledger['calls'][key].update(status='complete', identity=receipt.get('identity'),
            usage=receipt.get('usage'), upstream_requests=receipt.get('transport', {}).get('upstream_requests'),
            actual_model=receipt.get('transport', {}).get('actual_model'))
        write_json(path, ledger)
        return value, execution

    def build(self, inputs, work, dependencies=None):
        dependencies = {k: Path(v).resolve(strict=True) for k, v in (dependencies or {}).items()}
        check(inputs.get('schema_version') == BUILD_SCHEMA, 'Unsupported build input version')
        from .protocol import build_input
        check(build_input(inputs['task']['original'], inputs['scene'], inputs['observations'], inputs['development_cases']) == inputs,
              'Build input normalization changed or missing')
        private_fields(inputs)
        identity = sha(dict(inputs=inputs, policy={'max_candidates': self.max_candidates},
                            provider=getattr(self.provider, 'fingerprint', type(self.provider).__qualname__),
                            dependencies={k: file_sha(v) for k, v in dependencies.items()},
                            implementation={str(p.relative_to(Path(__file__).resolve().parent.parent)): file_sha(p)
                                            for p in runtime_dependencies()}))
        work = Path(work)
        work.mkdir(parents=True, exist_ok=True)
        # 原子锁避免两个构建器并发写入同一候选；崩溃残留锁必须检查后显式恢复。
        try:
            handle = os.open(work / '.build.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise JudgeError('Build locked; inspect active process before removing stale lock') from exc
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        try:
            state_path = work / 'build.json'
            if state_path.exists():
                state = read_json(state_path)
                check(state['identity'] == identity, 'Build inputs/code changed; create a new task version/work directory')
                if state.get('status') == 'frozen':
                    verify_bundle(state['bundle'], state['freeze_ref']['sha256'], allow_fixture=True)
                    return state
            else:
                write_json(work / 'input.json', inputs)
            state = dict(identity=identity, status='building', attempts=[])
            feedback = []
            for number in range(1, self.max_candidates+1):
                folder = work / f'candidate-{number:02}'
                folder.mkdir(exist_ok=True)
                try:
                    author_request = dict(inputs=model_inputs(inputs), candidate_version=number, revision_feedback=feedback)
                    candidate, author = self._complete(work, 'judge_author', author_request)
                    immutable_json(folder / 'candidate.json', candidate)
                    immutable_json(folder / 'author-execution.json', author)
                    check(author.get('role') == 'judge_author', 'Wrong author execution role')
                    missing_caps = validate_candidate(candidate, inputs)
                    if candidate['missing_inputs'] or missing_caps:
                        state.update(status='needs_input' if candidate['missing_inputs'] else 'needs_capability',
                                     missing_inputs=candidate['missing_inputs'], missing_capabilities=missing_caps)
                        write_json(state_path, state)
                        return state
                    audit_source(candidate['source'])
                    contract = contract_for(candidate, inputs, identity)
                    reports = test_cases(candidate['source'], contract, inputs['scene'], inputs['development_cases'])
                    immutable_json(folder / 'host-tests.json', reports)
                    check(all(r['passed'] for r in reports), 'Host case mismatch')
                    # reviewer 只看当前候选和原输入，不传作者会话、解释或上一版修订历史。
                    review, reviewer = self._complete(work, 'judge_reviewer',
                        dict(inputs=model_inputs(inputs), candidate=candidate, host_tests=reports))
                    immutable_json(folder / 'review.json', review)
                    immutable_json(folder / 'reviewer-execution.json', reviewer)
                    check(reviewer.get('role') == 'judge_reviewer', 'Wrong reviewer execution role')
                    a_sessions = author.get('receipt', {}).get('sessions', [])
                    r_sessions = reviewer.get('receipt', {}).get('sessions', [])
                    check(len(a_sessions) == len(r_sessions) == 1 and not set(a_sessions) & set(r_sessions),
                          'Author/reviewer must be separate fresh executions')
                    independent = reviewed_cases(review, candidate)
                    if review['verdict'] == 'needs_input':
                        state.update(status='needs_input', missing_inputs=review['issues'])
                        write_json(state_path, state)
                        return state
                    check(review['verdict'] == 'pass', 'Independent review requested revision: ' + '; '.join(review['issues']))
                    combined = inputs['development_cases'] + independent
                    validate_cases(combined)
                    ids = {c['id'] for c in candidate['conditions']}
                    covered = {v for c in combined if c['expected_outcome'] == 'failure' for v in c.get('violates', [])}
                    check(ids == covered, 'Every condition needs a labeled negative control')
                    all_reports = test_cases(candidate['source'], contract, inputs['scene'], combined)
                    immutable_json(folder / 'all-tests.json', all_reports)
                    check(all(r['passed'] for r in all_reports), 'Independent case mismatch')
                    state.update(freeze(folder, candidate, contract, inputs, author, reviewer,
                                        all_reports, combined, dependencies))
                    state['attempts'].append(dict(version=number, status='frozen', candidate_sha256=sha(candidate)))
                    write_json(state_path, state)
                    return state
                except (JudgeError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    failure = dict(version=number, status='validation_failed', reason=str(exc))
                    if (folder / 'host-tests.json').exists():
                        failure['host_tests'] = read_json(folder / 'host-tests.json')
                    write_json(folder / 'failure.json', failure)
                    state['attempts'].append(failure)
                    feedback = [dict(candidate=read_json(folder / 'candidate.json') if (folder / 'candidate.json').exists() else None,
                                     failure=failure)]
                    write_json(state_path, state)
                except (RuntimeError, OSError) as exc:
                    state.update(status='model_pending_or_failed', candidate_version=number, reason=str(exc))
                    write_json(state_path, state)
                    return state
            state.update(status='validation_failed')
            write_json(state_path, state)
            return state
        finally:
            (work / '.build.lock').unlink()

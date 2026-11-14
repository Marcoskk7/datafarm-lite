"""独立评审的来源证明：driver 会话身份的角色命名空间与已完成评审的校验。

交付检查（task/contract.py）与 agent 运行时都使用它；本模块不依赖编排层。
"""
from __future__ import annotations

import json
from pathlib import Path

from datafarm.kernel.io import atomic_write_json, sha256_file


def _save(path, value):
    atomic_write_json(path, value, sort_keys=False, allow_nan=False, mode=0o644)


def runtime_dir(work):
    path = Path(work) / '.runtime'
    path.mkdir(exist_ok=True)
    return path


def register_identity(work, session, key, role, native=False):
    """把受信任 driver 的 provider 身份绑定到单一角色命名空间。"""
    origin_verified = False
    if native:
        result = json.loads((session / 'native-result.json').read_text())
        cmd = json.loads((session / 'native-command.json').read_text())
        identity = {'schema': 'datafarm.agent-identity/1', 'session_id': result.get('session_id'),
                    'session_key': key, 'role': role, 'fresh': cmd['resume'] is None,
                    'evidence': [{'path': str((session / 'stdout.log').relative_to(work)),
                                  'sha256': sha256_file(session / 'stdout.log')}]}
        identity_path = session / 'identity.json'
        if identity_path.exists():
            saved = json.loads(identity_path.read_text())
            if any(saved.get(k) != identity.get(k) for k in ('session_id', 'session_key', 'role', 'fresh')):
                raise ValueError('native identity changed after execution')
            identity = saved
        else:
            _save(identity_path, identity)
    else:
        identity = json.loads((session / 'identity.json').read_text())
    if (identity.get('schema') != 'datafarm.agent-identity/1' or not identity.get('session_id')
            or identity.get('session_key') != key or identity.get('role') != role
            or not identity.get('evidence')):
        raise ValueError('driver session identity/provenance missing')
    for ref in identity['evidence']:
        path = (Path(work) / ref['path']).resolve(strict=True)
        own_native_log = native and path == (session / 'stdout.log').resolve(strict=True)
        if (not path.is_relative_to(session.resolve()) and not own_native_log) or sha256_file(path) != ref['sha256']:
            raise ValueError('driver identity evidence escaped session or changed')
    path = runtime_dir(work) / 'identities.json'
    known = json.loads(path.read_text()) if path.exists() else {}
    previous = known.get(identity['session_id'])
    if previous and previous['session_key'] != key:
        raise ValueError('driver reused author/reviewer session identity across roles')
    if native and previous is None and not identity.get('fresh'):
        # 被中断的首个回合可能已在任何结果前保存了 thread.started。
        for origin in (Path(work) / 'sessions').iterdir():
            if origin == session or not (origin / 'native-command.json').exists():
                continue
            origin_cmd = json.loads((origin / 'native-command.json').read_text())
            origin_execution = json.loads((origin / 'execution.json').read_text())
            origin_key = ('review-' + origin_execution['review_id']
                          if origin_execution.get('review_id') else 'author')
            if origin_cmd.get('resume') is not None or origin_key != key or origin_execution.get('role') != role:
                continue
            for line in (origin / 'stdout.log').read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'thread.started' and event.get('thread_id') == identity['session_id']:
                    origin_verified = True
                    break
            if origin_verified:
                identity['origin'] = {'path': str((origin / 'stdout.log').relative_to(work)),
                                      'sha256': sha256_file(origin / 'stdout.log'),
                                      'execution_sha256': sha256_file(origin / 'execution.json'),
                                      'command_sha256': sha256_file(origin / 'native-command.json')}
                _save(session / 'identity.json', identity)
                break
    if previous is None and identity.get('fresh') is not True and not origin_verified:
        raise ValueError('unproven pre-existing driver session cannot certify independence')
    if identity.get('origin'):
        ref = identity['origin']
        origin_log = Path(work) / ref['path']
        origin = origin_log.parent.resolve(strict=True)
        if (origin.parent != (Path(work) / 'sessions').resolve() or origin_log.name != 'stdout.log'
                or sha256_file(origin_log) != ref['sha256']
                or sha256_file(origin / 'execution.json') != ref['execution_sha256']
                or sha256_file(origin / 'native-command.json') != ref['command_sha256']):
            raise ValueError('native session origin changed or escaped task sessions')
    known[identity['session_id']] = {'session_key': key, 'role': role, 'first_session':
        previous['first_session'] if previous else str(session)}
    _save(path, known)
    return identity


def validate_completion(work, folder):
    completion = json.loads((folder / 'completion.json').read_text())
    session = Path(completion['session']).resolve(strict=True)
    if session.parent != (Path(work) / 'sessions').resolve():
        raise ValueError('review execution is outside task sessions')
    execution = json.loads((session / 'execution.json').read_text())
    receipt = json.loads((session / 'receipt.json').read_text())
    if (execution.get('role') != 'task-verifier' or execution.get('status') != 'returned'
            or execution.get('review_id') != folder.name
            or completion.get('role') != 'task-verifier'
            or receipt.get('role') != 'task-verifier'
            or receipt.get('messages_sha256') != execution.get('context_sha256')
            or completion.get('context_sha256') != execution.get('context_sha256')):
        raise ValueError('completed review lacks independent verifier provenance')
    if (sha256_file(folder / 'report.json') != completion['report_sha256']
            or sha256_file(session / 'execution.json') != completion['execution_sha256']
            or sha256_file(session / 'stdout.log') != completion['transcript_sha256']):
        raise ValueError('completed review report/execution/transcript changed')
    request = json.loads((folder / 'request.json').read_text())
    report = json.loads((folder / 'report.json').read_text())
    if report.get('verdict') not in {'pass', 'reject', 'unverified'} or completion.get('verdict') != report['verdict']:
        raise ValueError('completed review verdict differs from bound report')
    if report.get('subjects') != request['subjects'] or not report.get('evidence'):
        raise ValueError('completed review report lost source bindings')
    for ref in [*request['subjects'], *report['evidence']]:
        path = (Path(work) / ref['path']).resolve(strict=True)
        if not path.is_relative_to(Path(work).resolve()) or sha256_file(path) != ref['sha256']:
            raise ValueError('completed review subject/evidence changed')
    identity = json.loads((session / 'identity.json').read_text())
    if sha256_file(session / 'identity.json') != completion['identity_sha256'] or identity.get('role') != 'task-verifier':
        raise ValueError('completed review identity evidence changed')
    register_identity(Path(work), session, 'review-' + folder.name, 'task-verifier',
                      native=(session / 'native-result.json').exists())
    if completion.get('native_session'):
        native = json.loads((session / 'native-result.json').read_text())
        author_path = runtime_dir(work) / 'author.json'
        author = json.loads(author_path.read_text()) if author_path.exists() else {}
        if native.get('session_id') != completion['native_session'] or native['session_id'] == author.get('session_id'):
            raise ValueError('review reused author session or changed identity')
    return completion

"""原生 Codex JSONL 适配器。原样使用部署中已有的登录状态。

Token 用量只是证据，绝不是美元上限或凭空编造的计费凭证。
"""
from __future__ import annotations

import json
import os
from datafarm.common import atomic_write_json
from pathlib import Path
import subprocess
import sys
import time


def save(path, value):
    atomic_write_json(path, value, sort_keys=False, allow_nan=False, mode=0o644)


def command(config, session_id=None):
    argv = [config.get('executable', 'codex'), 'exec']
    if session_id:
        argv += ['resume', session_id]
    argv += ['--json', '--skip-git-repo-check']
    if config.get('model'):
        argv += ['--model', config['model']]
    # resume 把 sandbox_mode 当作配置项接受，而不是 --sandbox 参数。
    sandbox = config.get('sandbox', 'workspace-write')
    if sandbox not in {'read-only', 'workspace-write', 'danger-full-access'}:
        raise ValueError('invalid native sandbox')
    argv += ['-c', 'sandbox_mode=' + json.dumps(sandbox)]
    if config.get('reasoning_effort'):
        argv += ['-c', 'model_reasoning_effort=' + json.dumps(config['reasoning_effort'])]
    for key, value in config.get('settings', {}).items():
        if not key.startswith(('features.', 'model_', 'service_tier')):
            raise ValueError('unsupported native setting: ' + key)
        # JSON 标量同样是合法的 TOML 值（null 除外，此处直接拒绝）。
        if value is None or not isinstance(value, (str, bool, int, float)):
            raise ValueError('native settings must be scalar')
        argv += ['-c', key + '=' + json.dumps(value)]
    return argv + ['-']


def run(config, prompt, state_path, folder, cwd):
    state_path, folder = Path(state_path), Path(folder)
    prior = json.loads(state_path.read_text()) if state_path.exists() else {}
    session_id = prior.get('session_id')
    argv = command(config, session_id)
    save(folder / 'native-command.json', {'argv': argv, 'cwd': str(cwd), 'resume': session_id})
    state = {**prior, 'status': 'starting', 'updated': time.time()}
    save(state_path, state)
    usage = None
    errors = []
    with (folder / 'native-stderr.log').open('w') as err:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=err, text=True)
        state.update(status='running', pid=p.pid,
                     process_identity=Path(f'/proc/{p.pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
        save(state_path, state)
        # stdin 可能超出管道容量；由独立写入线程避免 stdout/stdin 死锁。
        import threading
        def feed():
            try:
                p.stdin.write(prompt)
                p.stdin.close()
            except BrokenPipeError:
                pass
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        for line in p.stdout:
            print(line, end='', flush=True)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'thread.started':
                state['session_id'] = event['thread_id']
                save(state_path, state)
            if event.get('type') == 'turn.completed':
                usage = event.get('usage')
            if event.get('type') in {'error', 'turn.failed'}:
                errors.append(event)
        code = p.wait()
        writer.join(timeout=1)
    state.update(status='returned' if code == 0 else 'failed', updated=time.time(), exit_code=code)
    save(state_path, state)
    result = {'schema': 'datafarm.native-session/1', 'session_id': state.get('session_id'),
              'exit_code': code, 'usage': usage, 'cost_usd': None,
              'cost_status': 'unknown', 'includes_descendants': False, 'errors': errors,
              'evidence': str(folder / 'stdout.log')}
    save(folder / 'native-result.json', result)
    # 这里刻意不是完整的 session-cost/1 凭证。
    if os.environ.get('DATAFARM_COST_RECEIPT'):
        save(Path(os.environ['DATAFARM_COST_RECEIPT']), result)
    return code


def main():
    config = json.loads(Path(os.environ['DATAFARM_NATIVE_CONFIG']).read_text())
    raw = sys.stdin.read()
    if config.get('input', 'messages') == 'messages':
        prompt = '\n\n'.join(m['content'] for m in json.loads(raw))
    else:
        prompt = raw
    return run(config, prompt, os.environ['DATAFARM_NATIVE_STATE'],
               os.environ['DATAFARM_SESSION_DIR'], Path.cwd())


if __name__ == '__main__':
    raise SystemExit(main())

"""Host side of the CaP author self-check: run ``check(episode)`` in a restricted child.

CaP iterates with its own discrimination (plan A): the actor writes the program and,
with its first proposal, a self-check. Debug/validation/verification success comes
from this check over public execution evidence. The frozen Judge is not consulted
during search; it scores only the final submission once, on hidden holdout seeds.

Linux/POSIX is the primary target: the child runs ``python -I -S`` with an empty
environment, a fresh session and rlimits (address space, CPU, no file writes, few
descriptors, no core dumps). Other platforms keep the AST policy and wall timeout only.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .self_check_worker import SelfCheckPolicyError, check_source

WORKER = Path(__file__).with_name('self_check_worker.py')
MAX_INPUT_BYTES = 16 * 1024 * 1024


class SelfCheckError(RuntimeError):
    """The self-check could not be run (host fault, limits, malformed worker output)."""


def validate(source):
    """Host-side policy check with a readable reason; raises ValueError."""
    try:
        check_source(source)
    except SelfCheckPolicyError as exc:
        raise ValueError('Invalid self-check: ' + str(exc)) from None
    return source


def _limits(memory_mb, cpu_s):
    def apply():
        import resource
        memory = memory_mb * 1024 * 1024
        for name, value in (('RLIMIT_AS', memory), ('RLIMIT_CPU', cpu_s), ('RLIMIT_FSIZE', 0),
                            ('RLIMIT_NOFILE', 16), ('RLIMIT_CORE', 0)):
            limit = getattr(resource, name, None)
            if limit is not None:
                resource.setrlimit(limit, (value, value))
    return apply


def run_check(source, episodes, *, timeout_s=30, memory_mb=512):
    """Return one ``{success, score, reason}`` per episode, in order."""
    validate(source)
    payload = json.dumps({'source': source, 'episodes': list(episodes)}, ensure_ascii=False,
                         allow_nan=False).encode('utf-8')
    if len(payload) > MAX_INPUT_BYTES:
        raise SelfCheckError('Self-check evidence exceeds the input budget')
    posix = os.name == 'posix'
    with tempfile.TemporaryDirectory(prefix='cap-self-check-') as cwd:
        try:
            proc = subprocess.run([sys.executable, '-I', '-S', str(WORKER)], input=payload, capture_output=True,
                                  timeout=timeout_s, cwd=cwd, env={'PYTHONIOENCODING': 'utf-8'},
                                  start_new_session=posix,
                                  preexec_fn=_limits(memory_mb, int(timeout_s) + 1) if posix else None)
        except subprocess.TimeoutExpired:
            raise SelfCheckError('Self-check exceeded its %ss wall budget' % timeout_s) from None
    if proc.returncode != 0:
        raise SelfCheckError('Self-check worker exited %s: %s' % (proc.returncode,
                             proc.stderr.decode('utf-8', 'replace').strip()[-300:]))
    try:
        answer = json.loads(proc.stdout.decode('utf-8').strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SelfCheckError('Self-check worker returned no result') from None
    if 'policy_error' in answer:
        raise ValueError('Invalid self-check: ' + str(answer['policy_error']))
    results = answer.get('results')
    if not isinstance(results, list) or len(results) != len(episodes) or any(
            not isinstance(r, dict) or set(r) != {'success', 'score', 'reason'} or type(r['success']) is not bool
            for r in results):
        raise SelfCheckError('Self-check worker returned malformed results')
    return results

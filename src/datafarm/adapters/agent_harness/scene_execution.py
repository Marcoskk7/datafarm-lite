"""OS-isolated expert IPC; runtime/measurement/Judge authority stays in the host."""
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import queue
import selectors
import signal
import subprocess
import sys
import sysconfig
import threading
import time

from datafarm.adapters.sandbox import SandboxSpec, probe_available, wrap
from datafarm.expert.audit import audit_expert_source
from datafarm.adapters.agent_harness.scene_worker import ExpertPolicyError, check_source

SCENE_EXPERT_POLICY = {'allowed_imports': ['math'], 'max_bytes': 64000}


class ExpertRaised(RuntimeError):
    """The guest expert itself raised or violated the shared policy (expert-attributable)."""


class ExpertAuditRejected(ValueError):
    """Host pre-check rejected the source before launch; message carries the short reason."""


def precheck_expert(source, class_name=None):
    """Run the same rules as the guest worker on the host, with a readable reason."""
    report = audit_expert_source(source, class_name, SCENE_EXPERT_POLICY)
    if not report.ok:
        raise ExpertAuditRejected('expert source audit rejected: ' + report.summary()[:300])
    try:
        check_source(source.decode('utf-8') if isinstance(source, bytes) else source, class_name)
    except (ExpertPolicyError, UnicodeDecodeError) as exc:
        raise ExpertAuditRejected('expert source audit rejected: ' + str(exc)[:300]) from None


def run_expert(expert_file, actions, *, timeout_s, max_calls, max_bytes=1048576, class_name=None, runtime=None, expected_source=None):
    """Explicit PRoot or legacy bwrap; no automatic isolation fallback."""
    if runtime is None and (os.name != 'posix' or not probe_available('bwrap')[0]):
        raise RuntimeError('SCENE_EXPERT_ISOLATION_UNAVAILABLE')
    if (type(max_calls) is not int or not 1 <= max_calls <= 100000 or
            type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 3600 or
            type(max_bytes) is not int or not 1 <= max_bytes <= 1048576):
        raise ValueError('explicit expert budgets required')
    expert_file = Path(expert_file).resolve(strict=True)
    source = expert_file.read_bytes()
    if expected_source is not None and source != expected_source:
        raise ValueError('expert source differs from host snapshot')
    precheck_expert(source, class_name)
    worker = Path(__file__).with_name('scene_worker.py').resolve()
    if runtime is not None:
        from datafarm.adapters.sandbox.scene_proot import expert_command
        launch = expert_command(runtime, source, worker, timeout_s=timeout_s, class_name=class_name)
    else:
        spec = SandboxSpec(mode='bwrap', base='allowlist', ro=(str(worker),str(expert_file)),
                           system_ro=('/usr','/bin','/lib','/lib64'),
                           binds=tuple((p,p,False) for p in sorted({sysconfig.get_path("stdlib"),sysconfig.get_path("platstdlib"),str(Path(sys.executable).resolve())})),
                           share_net=False, cwd='/', empty_file='')
        command = wrap(spec, [sys.executable,'-I','-S','-B',str(worker),str(expert_file)] + ([class_name] if class_name else []))
        launch = nullcontext(command)
    deadline = time.monotonic()+timeout_s
    with launch as command:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}, start_new_session=True)
        messages = queue.Queue(maxsize=2)
        stopped = threading.Event()
        def reader():
            try:
                while not stopped.is_set():
                    value = proc.stdout.readline(max_bytes+1)
                    while not stopped.is_set():
                        try:
                            messages.put(value, timeout=.05)
                            break
                        except queue.Full:
                            continue
                    if not value or len(value)>max_bytes:
                        return
            except Exception:
                try:messages.put_nowait(b'')
                except queue.Full:pass
        thread = threading.Thread(target=reader, daemon=True);thread.start()
        count = 0
        try:
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('SCENE_EXPERT_TIMEOUT')
                line = messages.get(timeout=remaining)
                if not line or len(line)>max_bytes:
                    raise RuntimeError('SCENE_EXPERT_PROTOCOL_ERROR')
                request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite IPC value')))
                if not isinstance(request,dict):
                    raise ValueError('expert request must be an object')
                if set(request) != {'method','argument'}:
                    raise ValueError('unsupported expert request fields')
                method = request['method']
                if method == 'error':
                    reason = request['argument']
                    raise ExpertRaised(reason[:300] if isinstance(reason, str) else 'expert raised')
                if method == 'complete' and request['argument'] is None:
                    try:
                        code = proc.wait(timeout=max(.001, deadline-time.monotonic()))
                    except subprocess.TimeoutExpired:
                        raise TimeoutError('SCENE_EXPERT_TIMEOUT') from None
                    thread.join(timeout=max(.001, deadline-time.monotonic()))
                    if not messages.empty() and messages.get_nowait().strip():
                        raise RuntimeError('SCENE_EXPERT_TRAILING_OUTPUT')
                    if code != 0:
                        raise RuntimeError('SCENE_EXPERT_EXIT_ERROR')
                    return
                count += 1
                if count > max_calls or not isinstance(method,str) or method not in ('observe','move_tcp','move_gripper'):
                    raise RuntimeError('SCENE_EXPERT_REQUEST_REJECTED')
                if method == 'observe':
                    if request['argument'] is not None:
                        raise ValueError('observe takes no argument')
                    value = actions.observe()
                else:
                    value = getattr(actions, method)(request['argument'])
                if time.monotonic() >= deadline:
                    raise TimeoutError('SCENE_EXPERT_TIMEOUT')
                response = json.dumps({'result':value},allow_nan=False).encode()+b'\n'
                if len(response)>max_bytes:
                    raise ValueError('observation exceeds expert IPC cap')
                # Nonblocking reply: a guest that never consumes stdin must not
                # trap the trusted physics host beyond this execution deadline.
                fd = proc.stdin.fileno()
                os.set_blocking(fd, False)
                view = memoryview(response)
                with selectors.DefaultSelector() as writer:
                    writer.register(fd, selectors.EVENT_WRITE)
                    while view:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not writer.select(remaining):
                            raise TimeoutError('SCENE_EXPERT_TIMEOUT')
                        try:
                            written = os.write(fd, view)
                        except BlockingIOError:
                            continue
                        if written <= 0:
                            raise RuntimeError('SCENE_EXPERT_PROTOCOL_ERROR')
                        view = view[written:]
        except queue.Empty:
            raise TimeoutError('SCENE_EXPERT_TIMEOUT') from None
        finally:
            stopped.set()
            try:
                os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
            thread.join(timeout=1)
            proc.stdin.close();proc.stdout.close()

"""OS-isolated expert IPC; runtime/measurement/Judge authority stays in the host."""
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import sysconfig
import threading
import time

from datafarm.adapters.sandbox.isolation import SandboxSpec, probe_available, wrap
from datafarm.expert.audit import audit_expert_source


def run_expert(expert_file, actions, *, timeout_s, max_calls, max_bytes=1048576, class_name=None):
    """Linux bwrap only; fail closed, never silently downgrade to PRoot/none."""
    if os.name != 'posix' or not probe_available('bwrap')[0]:
        raise RuntimeError('SCENE_EXPERT_ISOLATION_UNAVAILABLE')
    if (type(max_calls) is not int or not 1 <= max_calls <= 100000 or
            type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 3600 or
            type(max_bytes) is not int or not 1 <= max_bytes <= 1048576):
        raise ValueError('explicit expert budgets required')
    expert_file = Path(expert_file).resolve(strict=True)
    report = audit_expert_source(expert_file.read_bytes(), class_name, {'allowed_imports':['math'], 'max_bytes':64000})
    if not report.ok:
        raise ValueError('expert source audit rejected')
    worker = Path(__file__).with_name('scene_worker.py').resolve()
    spec = SandboxSpec(mode='bwrap', base='allowlist', ro=(str(worker),str(expert_file)),
                       system_ro=('/usr','/bin','/lib','/lib64'),
                       binds=tuple((p,p,False) for p in sorted({sysconfig.get_path("stdlib"),sysconfig.get_path("platstdlib"),str(Path(sys.executable).resolve())})),
                       share_net=False, cwd='/', empty_file='')
    command = wrap(spec, [sys.executable,'-I','-S','-B',str(worker),str(expert_file)] + ([class_name] if class_name else []))
    deadline = time.monotonic()+timeout_s
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
            proc.stdin.write(response);proc.stdin.flush()
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

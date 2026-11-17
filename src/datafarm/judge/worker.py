"""受信任的最小工作进程；生成程序不拥有本文件的 I/O 或操作系统接口。"""
import builtins
import ctypes
import json
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from datafarm.judge.execution import SAFE_BUILTINS, audit_source
from datafarm.judge.protocol import result


def resource_limits(memory_mb, timeout_s):
    """POSIX 使用 rlimit，Windows 使用 Job Object；限额不可用时拒绝执行。"""
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb*1024**2, memory_mb*1024**2))
        cpu = max(1, math.ceil(timeout_s))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        return
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [('user', ctypes.c_int64), ('job_user', ctypes.c_int64), ('flags', w.DWORD),
                    ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', w.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ('read_ops', 'write_ops', 'other_ops', 'read', 'write', 'other')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                    ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limits.process_memory = memory_mb*1024**2
    if (not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        raise RuntimeError('Cannot install Windows worker memory limit')
    # 内核 handle 随进程退出释放；不能在生成代码执行前关闭这个限额容器。


def execute(payload):
    resource_limits(payload['memory_mb'], payload['timeout_s'])
    tree = audit_source(payload['source'])
    allowed = {name: getattr(builtins, name) for name in SAFE_BUILTINS}
    def import_math(name, *args, **kwargs):
        if name != 'math':
            raise ValueError('Only math available')
        return math
    allowed['__import__'] = import_math
    scope = {'__builtins__': allowed, 'math': math, 'result': result}
    exec(compile(tree, '<generated-judge>', 'exec'), scope)
    return scope['evaluate'](payload['episode'], payload['contract'], payload['scene'])


if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.buffer.read(32*1024*1024+1))
        response = {'ok': True, 'result': execute(payload)}
    except Exception as exc:
        response = {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)[:500]}
    sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False))

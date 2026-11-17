"""unshare 回退后端：私有 mount namespace + 相同的遮蔽（仅 denylist 语义）。

需要 ``unshare(CLONE_NEWNS)`` 权限（root 或具备 CAP_SYS_ADMIN）；
在无特权容器里探测会失败。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Callable, Sequence

from datafarm.adapters.sandbox.model import (HarnessError, IsolationUnavailable, SandboxSpec,
                                             plan_mounts, setpriv_prefix)


def argv(spec: SandboxSpec, command: Sequence[str], *,
         lexists: Callable[[str], bool] = os.path.lexists,
         isdir: Callable[[str], bool] = os.path.isdir) -> list[str]:
    """回退方案：使用相同遮蔽的私有 mount namespace（仅 denylist 语义）。"""

    if spec.base != "root_ro":
        raise IsolationUnavailable("unshare cannot hide the host tree; it needs a root_ro spec "
                                   "(set allow_degraded_fallback to accept denylist semantics)")
    ops = plan_mounts(spec, lexists=lexists, isdir=isdir)
    masked_dirs = [op.dest for op in ops if op.kind == "mask_dir"]
    lines = ["set -e"]
    for op in ops:
        dest = shlex.quote(op.dest)
        if op.kind == "dir":
            continue
        if any(op.dest.startswith(m + "/") for m in masked_dirs) and op.kind in ("ro", "rw"):
            # 其被遮蔽的父目录挂载后源即被隐藏；此处不支持。
            continue
        if op.kind == "ro":
            lines.append(f"mount --bind {shlex.quote(op.src or op.dest)} {dest}")
            lines.append(f"mount -o remount,bind,ro {dest}")
        elif op.kind == "rw":
            # 只读 bind 之下的 bind 会继承只读：显式将其重新挂载为 rw
            lines.append(f"mount --bind {shlex.quote(op.src or op.dest)} {dest}")
            lines.append(f"mount -o remount,bind,rw {dest}")
        elif op.kind == "tmpfs":
            lines.append(f"mount -t tmpfs -o mode=1777 tmpfs {dest}")
        elif op.kind == "mask_dir":
            lines.append(f"mount -t tmpfs -o ro,size=16k,mode=0555 tmpfs {dest}")
        elif op.kind == "mask_file":
            if not spec.empty_file:
                raise HarnessError("masking a file needs SandboxSpec.empty_file")
            lines.append(f"mount --bind {shlex.quote(spec.empty_file)} {dest}")
            lines.append(f"mount -o remount,bind,ro {dest}")
    lines.append(f"cd {shlex.quote(spec.cwd)}")
    lines.append("exec " + " ".join(shlex.quote(a) for a in setpriv_prefix(spec.run_as)) + ' "$@"')
    script = "\n".join(lines)
    return ["unshare", "--mount", "--propagation", "private", "--fork", "--pid", "--mount-proc",
            "--kill-child", "--", "/bin/sh", "-c", script, "xh-sandbox", *command]


def probe() -> tuple[bool, str]:
    exe = shutil.which("unshare")
    if not exe or not shutil.which("setpriv"):
        return False, "unshare or setpriv not found"
    cmd = [exe, "--mount", "--propagation", "private", "--fork", "--",
           "/bin/sh", "-c", "mount -t tmpfs tmpfs /mnt && exit 0"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"unshare: {exc}"
    if proc.returncode == 0:
        return True, "ok"
    return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300]

"""bubblewrap 后端：``bwrap argv`` 构造与可用性探测。

bwrap 需要在新 mount namespace 里改 ``/`` 的挂载传播（``--make-rslave``）
并做 bind mount——在没有宿主 userns 特权的容器里（如 PlatforMAX）这一步
会被内核拒绝，探测会如实返回不可用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, Sequence

from datafarm.adapters.sandbox.model import (HarnessError, SandboxSpec, check_keep_caps, plan_mounts,
                                             setpriv_prefix)


def argv(spec: SandboxSpec, command: Sequence[str], *, bwrap: str | None = None,
         lexists: Callable[[str], bool] = os.path.lexists,
         isdir: Callable[[str], bool] = os.path.isdir) -> list[str]:
    exe = bwrap or shutil.which("bwrap") or "bwrap"
    ops = plan_mounts(spec, lexists=lexists, isdir=isdir)
    out = [exe, "--die-with-parent", "--unshare-pid", "--unshare-ipc"]
    if not spec.share_net:
        out.append("--unshare-net")
    if spec.base == "root_ro":
        out += ["--ro-bind", "/", "/"]
    else:
        for path in spec.system_ro:
            out += ["--ro-bind-try", path, path]
    out += ["--dev-bind", "/dev", "/dev"] if spec.dev == "host" else ["--dev", "/dev"]
    out += ["--proc", "/proc"]
    covered = {op.dest for op in ops}
    if spec.base == "allowlist" and "/tmp" not in covered:
        out += ["--tmpfs", "/tmp"]
    remount: list[str] = []
    for op in ops:
        if op.kind == "dir":
            out += ["--perms", "0755", "--dir", op.dest]
        elif op.kind == "ro":
            out += ["--ro-bind", op.src or op.dest, op.dest]
        elif op.kind == "rw":
            out += ["--bind", op.src or op.dest, op.dest]
        elif op.kind == "tmpfs":
            out += ["--tmpfs", op.dest]
        elif op.kind == "mask_dir":
            out += ["--tmpfs", op.dest]
            remount += ["--remount-ro", op.dest]
        elif op.kind == "mask_file":
            if not spec.empty_file:
                raise HarnessError("masking a file needs SandboxSpec.empty_file")
            out += ["--ro-bind", spec.empty_file, op.dest]
    out += remount
    inner = list(command)
    if spec.run_as is not None:
        out += ["--cap-add", "CAP_SETUID", "--cap-add", "CAP_SETGID", "--cap-add", "CAP_SETPCAP"]
        inner = setpriv_prefix(spec.run_as) + inner
    else:
        out += ["--cap-drop", "ALL"]
        for cap in check_keep_caps(spec.keep_caps):
            out += ["--cap-add", cap]
    out += ["--chdir", spec.cwd, "--", *inner]
    return out


def probe() -> tuple[bool, str]:
    exe = shutil.which("bwrap")
    if not exe:
        return False, "bwrap not found"
    cmd = [exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid",
           "--die-with-parent", "--cap-drop", "ALL", "--", "/bin/true"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"bwrap: {exc}"
    if proc.returncode == 0:
        return True, "ok"
    return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300]

"""沙箱的厂商无关核心：spec、挂载计划、探针脚本与公共小工具。

各后端（``bwrap`` / ``proot`` / ``unshare``）在自己的模块里只负责把
``SandboxSpec`` 翻译成命令行；选择与回退逻辑在 ``registry``。
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from datafarm.expert.util import HarnessError

DEFAULT_SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc",
                     "/run/systemd/resolve")
_KIND_ORDER = {"dir": -1, "ro": 0, "rw": 1, "tmpfs": 2, "mask": 3}
# 沙箱可保留的能力。绝不保留 CAP_SYS_ADMIN（umount 掉遮蔽）或
# CAP_DAC_READ_SEARCH（open_by_handle_at 可读取任意挂载背后的文件）。
ALLOWED_KEEP_CAPS = frozenset({"CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_CHOWN"})


def check_keep_caps(caps: Sequence[str]) -> tuple[str, ...]:
    bad = [c for c in caps if c not in ALLOWED_KEEP_CAPS]
    if bad:
        raise HarnessError(f"capabilities {bad} would let sandboxed code escape its masks; "
                           f"allowed: {sorted(ALLOWED_KEEP_CAPS)}")
    return tuple(caps)


class IsolationUnavailable(HarnessError):
    pass


@dataclasses.dataclass(frozen=True)
class MountOp:
    kind: str  # ro | rw | tmpfs | mask_dir | mask_file
    src: str | None
    dest: str


@dataclasses.dataclass(frozen=True)
class SandboxSpec:
    mode: str  # bwrap | proot | unshare | none
    base: str = "allowlist"  # allowlist | root_ro
    ro: tuple[str, ...] = ()
    rw: tuple[str, ...] = ()
    masked: tuple[str, ...] = ()
    binds: tuple[tuple[str, str, bool], ...] = ()  # (src, dest, writable)
    tmpfs: tuple[str, ...] = ()
    system_ro: tuple[str, ...] = DEFAULT_SYSTEM_RO
    dev: str = "minimal"  # minimal | host
    share_net: bool = True
    run_as: tuple[int, int] | None = None
    keep_caps: tuple[str, ...] = ()
    cwd: str = "/"
    empty_file: str = ""
    proot_bin: str = ""
    degraded: bool = False  # 在 unshare 下运行的 allowlist spec（仅 denylist 语义）
    ro_soft: bool = False  # proot 且未切换用户：只读目录可写（审计中记录的降级）

    def replace(self, **changes: Any) -> "SandboxSpec":
        return dataclasses.replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["binds"] = [list(b) for b in self.binds]
        return data


def _norm(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        raise HarnessError(f"sandbox paths must be absolute: {path!r}")
    normalized = os.path.normpath(path)
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def _depth(path: str) -> int:
    return 0 if path == "/" else path.count("/")


def plan_mounts(spec: SandboxSpec, *, lexists: Callable[[str], bool] = os.path.lexists,
                isdir: Callable[[str], bool] = os.path.isdir) -> list[MountOp]:
    """按应用顺序排列的挂载操作：更浅的路径在前，因此最具体的规则生效
    （被遮蔽父目录内的可读子目录重新 bind 后仍可读；可读父目录内
    被遮蔽的子目录保持隐藏）。缺失的源会被跳过。"""

    entries: list[tuple[int, int, int, MountOp]] = []
    order = 0

    def add(kind: str, src: str | None, dest: str) -> None:
        nonlocal order
        dest_n = _norm(dest)
        key_kind = "mask" if kind.startswith("mask") else kind
        entries.append((_depth(dest_n), _KIND_ORDER[key_kind], order, MountOp(kind, src, dest_n)))
        order += 1

    for path in spec.ro:
        if lexists(path):
            add("ro", _norm(path), path)
    for path in spec.rw:
        if lexists(path):
            add("rw", _norm(path), path)
    for src, dest, writable in spec.binds:
        if lexists(src):
            add("rw" if writable else "ro", _norm(src), dest)
    for path in spec.tmpfs:
        add("tmpfs", None, path)
    bound = [_norm(p) for p in spec.ro] + [_norm(p) for p in spec.rw] + [_norm(d) for _, d, _ in spec.binds]
    bound += [_norm(p) for p in spec.tmpfs] + [_norm(p) for p in spec.system_ro]
    for path in spec.masked:
        if not lexists(path):
            continue
        if spec.base == "allowlist" and not any(_under(_norm(path), root) for root in bound):
            continue  # 从未 bind，因此已不可见；连挂载点都不创建
        add("mask_dir" if isdir(path) and not os.path.islink(path) else "mask_file", None, path)
    entries.sort(key=lambda item: (item[0], item[1], item[2]))
    ops = [item[3] for item in entries]
    return _with_parent_dirs(spec, ops, lexists)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _with_parent_dirs(spec: SandboxSpec, ops: list[MountOp],
                      lexists: Callable[[str], bool] = os.path.lexists) -> list[MountOp]:
    """bwrap 会以 0700 模式创建缺失的挂载点父目录，非特权的
    沙箱用户无法穿越。先以 0755 模式显式创建每一个*合成*父目录（不由
    宿主 bind 提供的父目录）。满足以下任一条件时父目录即为合成目录：覆盖它的
    最深操作是 mask/tmpfs；在 allowlist 沙箱中没有任何操作覆盖它
    （bwrap 自己的 root tmpfs）；或者它被来自另一个不包含该目录的源的
    bind 覆盖（本轮 tmp 绑定到 /tmp）。proot 后端忽略 ``dir`` 操作。"""

    host_roots = [("ro", p, p) for p in (spec.system_ro if spec.base == "allowlist" else ("/",))]
    covering = host_roots + [(op.kind, op.dest, op.src) for op in ops]
    dests = {op.dest for op in ops}
    needed: dict[str, int] = {}
    for op in ops:
        parent = os.path.dirname(op.dest)
        while parent and parent != "/":
            if parent not in dests and parent not in needed:
                best = None
                for kind, root, src in covering:
                    if root != parent and _under(parent, root) and (best is None or len(root) > len(best[1])):
                        best = (kind, root, src)
                synthetic = (best is None and spec.base == "allowlist") or (
                    best is not None and (best[0] in ("mask_dir", "tmpfs") or (
                        best[0] in ("ro", "rw") and best[2] not in (None, best[1])
                        and not lexists(best[2] + parent[len(best[1]):]))))
                if synthetic:
                    needed[parent] = _depth(parent)
            parent = os.path.dirname(parent)
    if not needed:
        return ops
    order = {op.dest: i for i, op in enumerate(ops)}
    merged = [(_depth(op.dest), _KIND_ORDER["mask" if op.kind.startswith("mask") else op.kind], order[op.dest], op)
              for op in ops]
    merged += [(depth, _KIND_ORDER["dir"], -1, MountOp("dir", None, path)) for path, depth in needed.items()]
    merged.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in merged]


def setpriv_prefix(run_as: tuple[int, int] | None) -> list[str]:
    setpriv = shutil.which("setpriv") or "/usr/bin/setpriv"
    prefix = [setpriv]
    if run_as is not None:
        prefix += [f"--reuid={int(run_as[0])}", f"--regid={int(run_as[1])}", "--clear-groups"]
    return prefix + ["--inh-caps=-all", "--bounding-set=-all", "--no-new-privs", "--"]


# --------------------------------------------------------------------------- 探针

PROBE_SCRIPT = r"""
for arg in "$@"; do
  kind=${arg%%:*}; p=${arg#*:}
  case "$kind" in
    m)
      umount "$p" >/dev/null 2>&1 && echo "UMOUNTED $p"
      if [ -d "$p" ]; then
        first=$(ls -A "$p" 2>/dev/null | head -n 1)
        if [ -n "$first" ]; then echo "LEAK $p"; else echo "HIDDEN $p"; fi
      elif [ -f "$p" ] && [ -s "$p" ] && head -c 1 "$p" >/dev/null 2>&1; then
        echo "LEAK $p"
      else
        echo "HIDDEN $p"
      fi ;;
    r|w)
      probe="$p/.xh-probe-$$"
      if (set -C; : > "$probe") 2>/dev/null; then rm -f "$probe"; echo "WRITABLE $p"; else echo "READONLY $p"; fi ;;
  esac
done
"""


@dataclasses.dataclass
class ProbeResult:
    mode: str
    ok: bool
    leaks: list[str]
    umounted: list[str]
    writable_readonly: list[str]
    unwritable_workspace: list[str]
    lines: list[str]
    error: str | None = None
    degraded: bool = False
    ro_soft: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def probe_args(masked: Iterable[str], readonly_dirs: Iterable[str], writable_dirs: Iterable[str]) -> list[str]:
    return ([f"m:{p}" for p in masked] + [f"r:{p}" for p in readonly_dirs]
            + [f"w:{p}" for p in writable_dirs])


def parse_probe(lines: Iterable[str], *, mode: str, readonly_dirs: Iterable[str],
                writable_dirs: Iterable[str], degraded: bool = False,
                error: str | None = None, ro_soft: bool = False) -> ProbeResult:
    lines = [line.strip() for line in lines if line.strip()]
    leaks = [line.split(" ", 1)[1] for line in lines if line.startswith("LEAK ")]
    umounted = [line.split(" ", 1)[1] for line in lines if line.startswith("UMOUNTED ")]
    writable = {line.split(" ", 1)[1] for line in lines if line.startswith("WRITABLE ")}
    ro_bad = [p for p in readonly_dirs if p in writable]
    rw_bad = [p for p in writable_dirs if p not in writable]
    # ro_soft（proot 无只读 bind）：只读目录可写记录在案但不构成失败——
    # 核心防线（mask 遮蔽）仍然严格；该降级会随 summary 进入审计状态。
    ok = error is None and not leaks and not umounted and not rw_bad and (ro_soft or not ro_bad)
    return ProbeResult(mode, ok, leaks, umounted, ro_bad, rw_bad, lines, error, degraded, ro_soft)


def run_probe(spec: SandboxSpec, *, masked: Sequence[str], readonly_dirs: Sequence[str] = (),
              writable_dirs: Sequence[str] = (), env: Mapping[str, str] | None = None,
              timeout: float = 60.0) -> ProbeResult:
    """在 ``spec`` 内运行探针：每个被遮蔽的路径都必须隐藏（即使尝试
    ``umount`` 之后），只读目录不可写，工作区目录可写。"""

    from datafarm.adapters.sandbox.registry import wrap  # 避免与后端模块循环导入

    args = probe_args(masked, readonly_dirs, writable_dirs)
    argv = wrap(spec, ["/bin/sh", "-c", PROBE_SCRIPT, "xh-probe", *args])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=dict(env) if env is not None else {"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return parse_probe([], mode=spec.mode, readonly_dirs=readonly_dirs,
                           writable_dirs=writable_dirs, degraded=spec.degraded,
                           error=str(exc), ro_soft=spec.ro_soft)
    error = None if proc.returncode == 0 else f"probe exit {proc.returncode}: {proc.stderr.strip()[:300]}"
    return parse_probe(proc.stdout.splitlines(), mode=spec.mode, readonly_dirs=readonly_dirs,
                       writable_dirs=writable_dirs, degraded=spec.degraded, error=error,
                       ro_soft=spec.ro_soft)


def make_empty_file(directory: str | Path) -> str:
    path = Path(directory) / ".xh-empty"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".xh-empty.{uuid.uuid4().hex}")
        tmp.write_bytes(b"")
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
    return str(path)


def make_empty_dir(directory: str | Path, name: str = ".xh-empty-dir") -> str:
    path = Path(directory) / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def chown_tree(path: str | Path, uid: int, gid: int) -> None:
    """给非特权沙箱用户其可写目录（绝不跟随链接）。"""

    for current, dirs, files in os.walk(path, followlinks=False):
        os.lchown(current, uid, gid)
        for name in dirs + files:
            os.lchown(os.path.join(current, name), uid, gid)


def sandbox_env(base: Mapping[str, str], passthrough: Iterable[str]) -> dict[str, str]:
    """为沙箱内进程准备精确的环境变量。``passthrough`` 中指定的 secret
    只从 harness 环境复制到子进程环境（绝不进入 argv，
    绝不进入日志）。"""

    env = {k: str(v) for k, v in base.items()}
    for name in passthrough:
        if name in os.environ and name not in env:
            env[name] = os.environ[name]
    return env

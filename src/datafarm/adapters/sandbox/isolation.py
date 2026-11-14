"""面向 agent CLI 以及执行 agent 编写代码的 rollout 的 OS 级隔离。

仅靠一份 brief 无法让编写 agent 远离 holdout seed 和 verifier 代码：以 root 运行的
agent 会让 "clean room" 变成自说自话。这里规则变成了挂载：

* ``bwrap``（首选）：新的 mount + PID namespace。``allowlist`` 基础只显示
  系统目录以及适配器列为可读（只读）的路径，加上工作区（读写）；
  ``root_ro`` 基础将整个宿主以只读方式展示。受保护路径
  会被遮蔽（目录用空的只读 tmpfs，文件用空文件）。
* ``unshare -m``（回退）：私有 mount namespace，使用相同的遮蔽；宿主仍然
  可见，因此只具备 denylist 语义。

能力（capabilities）总是被丢弃（``--cap-drop ALL``），或者进程通过 ``setpriv``
切换到非特权 uid。这一点很重要：由 root 启动的 bwrap 默认保留所有
能力，而持有 ``CAP_SYS_ADMIN`` 的进程可以直接 ``umount`` 掉
一个遮蔽（见测试）。
"""

from __future__ import annotations

import dataclasses
import os
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
    mode: str  # bwrap | unshare | none
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
    degraded: bool = False  # 在 unshare 下运行的 allowlist spec（仅 denylist 语义）

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
    bind 覆盖（本轮 tmp 绑定到 /tmp）。"""

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


def bwrap_argv(spec: SandboxSpec, command: Sequence[str], *, bwrap: str = "bwrap",
               lexists: Callable[[str], bool] = os.path.lexists,
               isdir: Callable[[str], bool] = os.path.isdir) -> list[str]:
    ops = plan_mounts(spec, lexists=lexists, isdir=isdir)
    argv = [bwrap, "--die-with-parent", "--unshare-pid", "--unshare-ipc"]
    if not spec.share_net:
        argv.append("--unshare-net")
    if spec.base == "root_ro":
        argv += ["--ro-bind", "/", "/"]
    else:
        for path in spec.system_ro:
            argv += ["--ro-bind-try", path, path]
    argv += ["--dev-bind", "/dev", "/dev"] if spec.dev == "host" else ["--dev", "/dev"]
    argv += ["--proc", "/proc"]
    covered = {op.dest for op in ops}
    if spec.base == "allowlist" and "/tmp" not in covered:
        argv += ["--tmpfs", "/tmp"]
    remount: list[str] = []
    for op in ops:
        if op.kind == "dir":
            argv += ["--perms", "0755", "--dir", op.dest]
        elif op.kind == "ro":
            argv += ["--ro-bind", op.src or op.dest, op.dest]
        elif op.kind == "rw":
            argv += ["--bind", op.src or op.dest, op.dest]
        elif op.kind == "tmpfs":
            argv += ["--tmpfs", op.dest]
        elif op.kind == "mask_dir":
            argv += ["--tmpfs", op.dest]
            remount += ["--remount-ro", op.dest]
        elif op.kind == "mask_file":
            if not spec.empty_file:
                raise HarnessError("masking a file needs SandboxSpec.empty_file")
            argv += ["--ro-bind", spec.empty_file, op.dest]
    argv += remount
    inner = list(command)
    if spec.run_as is not None:
        argv += ["--cap-add", "CAP_SETUID", "--cap-add", "CAP_SETGID", "--cap-add", "CAP_SETPCAP"]
        inner = setpriv_prefix(spec.run_as) + inner
    else:
        argv += ["--cap-drop", "ALL"]
        for cap in check_keep_caps(spec.keep_caps):
            argv += ["--cap-add", cap]
    argv += ["--chdir", spec.cwd, "--", *inner]
    return argv


def unshare_argv(spec: SandboxSpec, command: Sequence[str], *,
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


_AVAILABLE: dict[str, tuple[bool, str]] = {}


def probe_available(mode: str) -> tuple[bool, str]:
    """此宿主能否创建该沙箱？按进程缓存。"""

    if mode == "none":
        return True, "no isolation requested"
    if mode in _AVAILABLE:
        return _AVAILABLE[mode]
    if mode == "bwrap":
        exe = shutil.which("bwrap")
        if not exe:
            result = (False, "bwrap not found")
        else:
            cmd = [exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid",
                   "--die-with-parent", "--cap-drop", "ALL", "--", "/bin/true"]
            result = _try(cmd)
    elif mode == "unshare":
        exe = shutil.which("unshare")
        if not exe or not shutil.which("setpriv"):
            result = (False, "unshare or setpriv not found")
        else:
            result = _try([exe, "--mount", "--propagation", "private", "--fork", "--",
                           "/bin/sh", "-c", "mount -t tmpfs tmpfs /mnt && exit 0"])
    else:
        result = (False, f"unknown isolation mode {mode!r}")
    _AVAILABLE[mode] = result
    return result


def _try(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{cmd[0]}: {exc}"
    if proc.returncode == 0:
        return True, "ok"
    return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300]


def resolve_spec(spec: SandboxSpec, *, fallback: str | None, allow_degraded: bool,
                 available: Callable[[str], tuple[bool, str]] = probe_available) -> SandboxSpec:
    """选择可用模式：首选模式，否则回退模式；都不可用则失败关闭。"""

    reasons = []
    for mode in [spec.mode] + ([fallback] if fallback and fallback != spec.mode else []):
        ok, why = available(mode)
        if not ok:
            reasons.append(f"{mode}: {why}")
            continue
        if mode == "unshare" and spec.base == "allowlist":
            if not allow_degraded:
                reasons.append("unshare: allowlist spec needs allow_degraded_fallback")
                continue
            return spec.replace(mode="unshare", base="root_ro", degraded=True)
        return spec.replace(mode=mode)
    raise IsolationUnavailable("no usable isolation: " + "; ".join(reasons))


def wrap(spec: SandboxSpec, command: Sequence[str]) -> list[str]:
    if spec.mode == "bwrap":
        return bwrap_argv(spec, command, bwrap=shutil.which("bwrap") or "bwrap")
    if spec.mode == "unshare":
        return unshare_argv(spec, command)
    if spec.mode == "none":
        return list(command)
    raise HarnessError(f"unknown isolation mode {spec.mode!r}")


def sandbox_env(base: Mapping[str, str], passthrough: Iterable[str]) -> dict[str, str]:
    """为沙箱内进程准备精确的环境变量。``passthrough`` 中指定的 secret
    只从 harness 环境复制到子进程环境（绝不进入 argv，
    绝不进入日志）。"""

    env = {k: str(v) for k, v in base.items()}
    for name in passthrough:
        if name in os.environ and name not in env:
            env[name] = os.environ[name]
    return env


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

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def probe_args(masked: Iterable[str], readonly_dirs: Iterable[str], writable_dirs: Iterable[str]) -> list[str]:
    return ([f"m:{p}" for p in masked] + [f"r:{p}" for p in readonly_dirs]
            + [f"w:{p}" for p in writable_dirs])


def parse_probe(lines: Iterable[str], *, mode: str, readonly_dirs: Iterable[str],
                writable_dirs: Iterable[str], degraded: bool = False,
                error: str | None = None) -> ProbeResult:
    lines = [line.strip() for line in lines if line.strip()]
    leaks = [line.split(" ", 1)[1] for line in lines if line.startswith("LEAK ")]
    umounted = [line.split(" ", 1)[1] for line in lines if line.startswith("UMOUNTED ")]
    writable = {line.split(" ", 1)[1] for line in lines if line.startswith("WRITABLE ")}
    ro_bad = [p for p in readonly_dirs if p in writable]
    rw_bad = [p for p in writable_dirs if p not in writable]
    ok = error is None and not leaks and not umounted and not ro_bad and not rw_bad
    return ProbeResult(mode, ok, leaks, umounted, ro_bad, rw_bad, lines, error, degraded)


def run_probe(spec: SandboxSpec, *, masked: Sequence[str], readonly_dirs: Sequence[str] = (),
              writable_dirs: Sequence[str] = (), env: Mapping[str, str] | None = None,
              timeout: float = 60.0) -> ProbeResult:
    """在 ``spec`` 内运行探针：每个被遮蔽的路径都必须隐藏（即使尝试
    ``umount`` 之后），只读目录不可写，工作区目录可写。"""

    args = probe_args(masked, readonly_dirs, writable_dirs)
    argv = wrap(spec, ["/bin/sh", "-c", PROBE_SCRIPT, "xh-probe", *args])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=dict(env) if env is not None else {"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return parse_probe([], mode=spec.mode, readonly_dirs=readonly_dirs,
                           writable_dirs=writable_dirs, degraded=spec.degraded, error=str(exc))
    error = None if proc.returncode == 0 else f"probe exit {proc.returncode}: {proc.stderr.strip()[:300]}"
    return parse_probe(proc.stdout.splitlines(), mode=spec.mode, readonly_dirs=readonly_dirs,
                       writable_dirs=writable_dirs, degraded=spec.degraded, error=error)


def make_empty_file(directory: str | Path) -> str:
    path = Path(directory) / ".xh-empty"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".xh-empty.{uuid.uuid4().hex}")
        tmp.write_bytes(b"")
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
    return str(path)


def chown_tree(path: str | Path, uid: int, gid: int) -> None:
    """给非特权沙箱用户其可写目录（绝不跟随链接）。"""

    for current, dirs, files in os.walk(path, followlinks=False):
        os.lchown(current, uid, gid)
        for name in dirs + files:
            os.lchown(os.path.join(current, name), uid, gid)


# --------------------------------------------------------------------------- job 脚本


def job_wrapper_bash(*, mode: str, fallback: str | None, base: str, dev: str, ro: Sequence[str],
                     rw: Sequence[str], masked: Sequence[str], run_as: tuple[int, int] | None,
                     tmpfs: Sequence[str] = (),
                     share_net: bool = True, private_tmp: bool = True,
                     keep_caps: Sequence[str] = ()) -> str:
    """生成 rollout job 使用的 Bash 函数。``xh_pick_backend`` 先选择 bwrap，
    再选回退方案，两者都不可用时失败（此时 job 以 97 退出）；``xh_sandboxed <cwd>
    <cmd...>`` 在沙箱内运行命令。路径存在性与 ``${HOME}`` 相对
    路径在执行节点上于运行时解析。"""

    def q(path: str) -> str:
        # 保留 ${VAR} 可展开，其余全部加引号
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") + '"'

    # 排序（浅的在前，使最具体的规则生效）在运行时进行，此时
    # ${HOME} 形式的变量已在执行节点上展开。
    entries = ([("ro", p) for p in ro] + [("rw", p) for p in rw]
               + [("tmpfs", p) for p in tmpfs] + [("mask", p) for p in masked])
    op_lines = "\n".join(f"      printf '%s\\t%s\\n' {kind} {q(path)}" for kind, path in entries)
    setpriv = " ".join(shlex.quote(a) for a in setpriv_prefix(run_as))
    base_args = "--ro-bind / /" if base == "root_ro" else " ".join(
        f"--ro-bind-try {p} {p}" for p in DEFAULT_SYSTEM_RO)
    dev_args = "--dev-bind /dev /dev" if dev == "host" else "--dev /dev"
    net = "" if share_net else "--unshare-net"
    tmp_args = "--tmpfs /tmp" if private_tmp else ""
    caps = ("--cap-add CAP_SETUID --cap-add CAP_SETGID --cap-add CAP_SETPCAP" if run_as
            else " ".join(["--cap-drop ALL"] + [f"--cap-add {c}" for c in check_keep_caps(keep_caps)]))
    return f"""
XH_ISOLATION_MODE={shlex.quote(mode)}
XH_ISOLATION_FALLBACK={shlex.quote(fallback or '')}
xh_op() {{
  local kind="$1" p="$2"
  case "$XH_BACKEND:$kind" in
    bwrap:ro) [ -e "$p" ] && XH_ARGS+=(--ro-bind "$p" "$p") ;;
    bwrap:rw) [ -e "$p" ] && XH_ARGS+=(--bind "$p" "$p") ;;
    bwrap:tmpfs) [ -d "$p" ] && XH_ARGS+=(--tmpfs "$p") ;;
    bwrap:mask)
      if [ -d "$p" ] && [ ! -L "$p" ]; then XH_ARGS+=(--tmpfs "$p"); XH_REMOUNT+=(--remount-ro "$p")
      elif [ -e "$p" ]; then XH_ARGS+=(--ro-bind "$XH_EMPTY" "$p"); fi ;;
    unshare:ro) [ -e "$p" ] && XH_MOUNTS+="mount --bind '$p' '$p' && mount -o remount,bind,ro '$p'"$'\\n' ;;
    unshare:rw) [ -e "$p" ] && XH_MOUNTS+="mount --bind '$p' '$p' && mount -o remount,bind,rw '$p'"$'\\n' ;;
    unshare:tmpfs) [ -d "$p" ] && XH_MOUNTS+="mount -t tmpfs -o size=256m,mode=0755 tmpfs '$p'"$'\\n' ;;
    unshare:mask)
      if [ -d "$p" ] && [ ! -L "$p" ]; then XH_MOUNTS+="mount -t tmpfs -o ro,size=16k,mode=0555 tmpfs '$p'"$'\\n'
      elif [ -e "$p" ]; then XH_MOUNTS+="mount --bind '$XH_EMPTY' '$p' && mount -o remount,bind,ro '$p'"$'\\n'; fi ;;
  esac
  return 0
}}
xh_collect() {{
  local _depth _prio kind p
  while IFS=$'\\t' read -r _depth _prio kind p; do
    xh_op "$kind" "$p"
  done < <(
    {{
{op_lines}
    }} | awk -F'\\t' 'BEGIN {{ OFS = "\\t" }} {{ d = gsub("/", "/", $2); pr = ($1 == "ro") ? 0 : (($1 == "rw") ? 1 : (($1 == "tmpfs") ? 2 : 3)); print d, pr, $1, $2 }}' \\
       | sort -t "$(printf '\\t')" -k1,1n -k2,2n -s
  )
  return 0
}}
xh_pick_backend() {{
  for m in "$XH_ISOLATION_MODE" "$XH_ISOLATION_FALLBACK"; do
    case "$m" in
      bwrap) if command -v bwrap >/dev/null 2>&1 && bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid --die-with-parent --cap-drop ALL -- /bin/true >/dev/null 2>&1; then XH_BACKEND=bwrap; return 0; fi ;;
      unshare) if command -v unshare >/dev/null 2>&1 && command -v setpriv >/dev/null 2>&1 && unshare --mount --propagation private --fork -- /bin/sh -c 'mount -t tmpfs tmpfs /mnt' >/dev/null 2>&1; then XH_BACKEND=unshare; return 0; fi ;;
      none) XH_BACKEND=none; return 0 ;;
    esac
  done
  return 1
}}
xh_sandboxed() {{
  local cwd="$1"; shift
  if [ "$XH_BACKEND" = bwrap ]; then
    XH_ARGS=(--die-with-parent --unshare-pid --unshare-ipc {net} {base_args} {dev_args} --proc /proc {tmp_args})
    XH_REMOUNT=()
    xh_collect
    bwrap "${{XH_ARGS[@]}}" "${{XH_REMOUNT[@]}}" {caps} --chdir "$cwd" -- {setpriv if run_as else ''} "$@"
  elif [ "$XH_BACKEND" = unshare ]; then
    XH_MOUNTS=""
    xh_collect
    unshare --mount --propagation private --fork --pid --mount-proc --kill-child -- /bin/bash -c "set -e
$XH_MOUNTS
cd '$cwd'
exec {setpriv} \\"\\$@\\"" xh-sandbox "$@"
  else
    (cd "$cwd" && "$@")
  fi
}}
"""

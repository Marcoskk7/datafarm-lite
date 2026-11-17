"""rollout job 的 Bash 包装生成器：``xh_pick_backend`` / ``xh_sandboxed``。

与 Python 侧 ``bwrap``/``proot``/``unshare`` 后端的行为保持一致；这里的
每个分支都对应一个后端模块的 argv 构造逻辑。job 脚本在执行节点上运行时
才解析路径存在性与 ``${HOME}`` 形式的变量。
"""

from __future__ import annotations

import shlex
from typing import Sequence

from datafarm.adapters.sandbox.model import DEFAULT_SYSTEM_RO, check_keep_caps, setpriv_prefix


def _fallback_word(fallback: str | Sequence[str] | None) -> str:
    if fallback is None:
        return ""
    if isinstance(fallback, str):
        return fallback
    return " ".join(str(mode) for mode in fallback)


def job_wrapper_bash(*, mode: str, fallback: str | Sequence[str] | None, base: str, dev: str,
                     ro: Sequence[str], rw: Sequence[str], masked: Sequence[str],
                     run_as: tuple[int, int] | None, tmpfs: Sequence[str] = (),
                     share_net: bool = True, private_tmp: bool = True,
                     keep_caps: Sequence[str] = (), proot_bin: str = "") -> str:
    """生成 rollout job 使用的 Bash 函数。``xh_pick_backend`` 按
    mode → fallback 顺序选择后端（bwrap / proot / unshare / none），
    都不可用时失败（此时 job 以 97 退出）；``xh_sandboxed <cwd> <cmd...>``
    在沙箱内运行命令。"""

    def q(path: str) -> str:
        # 保留 ${VAR} 可展开，其余全部加引号
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") + '"'

    # 排序（浅的在前，使最具体的规则生效）在运行时进行，此时
    # ${HOME} 形式的变量已在执行节点上展开。
    entries = ([("ro", p) for p in ro] + [("rw", p) for p in rw]
               + [("tmpfs", p) for p in tmpfs] + [("mask", p) for p in masked])
    op_lines = "\n".join(f"      printf '%s\\t%s\\n' {kind} {q(path)}" for kind, path in entries)
    setpriv = " ".join(shlex.quote(a) for a in setpriv_prefix(run_as))
    system_ro = " ".join(q(p) for p in DEFAULT_SYSTEM_RO)
    base_args = "--ro-bind / /" if base == "root_ro" else " ".join(
        f"--ro-bind-try {p} {p}" for p in DEFAULT_SYSTEM_RO)
    dev_args = "--dev-bind /dev /dev" if dev == "host" else "--dev /dev"
    net = "" if share_net else "--unshare-net"
    net_prefix = "" if share_net else "unshare --map-root-user --net --propagation unchanged --"
    tmp_args = "--tmpfs /tmp" if private_tmp else ""
    caps = ("--cap-add CAP_SETUID --cap-add CAP_SETGID --cap-add CAP_SETPCAP" if run_as
            else " ".join(["--cap-drop ALL"] + [f"--cap-add {c}" for c in check_keep_caps(keep_caps)]))
    run_as_opt = f"-i {int(run_as[0])}:{int(run_as[1])}" if run_as else ""
    return f"""
XH_ISOLATION_MODE={shlex.quote(mode)}
XH_ISOLATION_FALLBACK={shlex.quote(_fallback_word(fallback))}
XH_BASE={shlex.quote(base)}
XH_PROOT={shlex.quote(proot_bin)}
XH_PROOT_ROOT="${{XH_PROOT_ROOT:-$(dirname "${{XH_EMPTY:-.xh-empty}}")/.xh-proot-root}}"
XH_EMPTY_DIR="${{XH_EMPTY_DIR:-$(dirname "${{XH_EMPTY:-.xh-empty}}")/.xh-empty-dir}}"
xh_op() {{
  local kind="$1" p="$2"
  case "$XH_BACKEND:$kind" in
    bwrap:ro) [ -e "$p" ] && XH_ARGS+=(--ro-bind "$p" "$p") ;;
    bwrap:rw) [ -e "$p" ] && XH_ARGS+=(--bind "$p" "$p") ;;
    bwrap:tmpfs) [ -d "$p" ] && XH_ARGS+=(--tmpfs "$p") ;;
    bwrap:mask)
      if [ -d "$p" ] && [ ! -L "$p" ]; then XH_ARGS+=(--tmpfs "$p"); XH_REMOUNT+=(--remount-ro "$p")
      elif [ -e "$p" ]; then XH_ARGS+=(--ro-bind "$XH_EMPTY" "$p"); fi ;;
    proot:ro) [ -e "$p" ] && XH_ARGS+=(-b "$p") ;;
    proot:rw) [ -e "$p" ] && XH_ARGS+=(-b "$p") ;;
    proot:tmpfs)
      if [ -d "$p" ] && [ ! -L "$p" ]; then XH_ARGS+=(-b "$XH_EMPTY_DIR:$p")
      elif [ -e "$p" ]; then XH_ARGS+=(-b "$XH_EMPTY:$p"); fi ;;
    proot:mask)
      if [ -d "$p" ] && [ ! -L "$p" ]; then XH_ARGS+=(-b "$XH_EMPTY_DIR:$p")
      elif [ -e "$p" ]; then XH_ARGS+=(-b "$XH_EMPTY:$p"); fi ;;
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
  for m in $XH_ISOLATION_MODE $XH_ISOLATION_FALLBACK; do
    case "$m" in
      bwrap) if command -v bwrap >/dev/null 2>&1 && bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid --die-with-parent --cap-drop ALL -- /bin/true >/dev/null 2>&1; then XH_BACKEND=bwrap; return 0; fi ;;
      proot) P="${{XH_PROOT:-proot}}"; if command -v "$P" >/dev/null 2>&1 && "$P" -r / /bin/true >/dev/null 2>&1; then mkdir -p "$XH_PROOT_ROOT" "$XH_EMPTY_DIR"; XH_BACKEND=proot; return 0; fi ;;
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
  elif [ "$XH_BACKEND" = proot ]; then
    local P="${{XH_PROOT:-proot}}" root="/"
    [ "$XH_BASE" = allowlist ] && root="$XH_PROOT_ROOT"
    XH_ARGS=(-r "$root")
    if [ "$XH_BASE" = allowlist ]; then
      for b in {system_ro}; do [ -e "$b" ] && XH_ARGS+=(-b "$b"); done
      XH_ARGS+=(-b /dev -b /proc)
    fi
    {"" if not private_tmp else 'XH_ARGS+=(-b "$XH_EMPTY_DIR:/tmp")'}
    xh_collect
    XH_ARGS+=(-w "$cwd")
    {f'XH_ARGS+=({run_as_opt})' if run_as else ''}
    {net_prefix} "$P" "${{XH_ARGS[@]}}" "$@"
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

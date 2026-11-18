"""PRoot: userspace fake-chroot (ptrace path translation); path translation is not a security boundary.

Two consumers share one flag layout (``_command``: ``-r`` root, ``-b`` binds, ``-w`` cwd, optional
``-i``, command — never the implicit ``-R``/``-S`` host binds):

* The ``SandboxSpec`` backend (``argv``/``prepare``/``probe``, selected by ``registry``), the only
  path-level sandbox in unprivileged containers (no CAP_SYS_ADMIN, no userns remap, mount(2) denied):

  - ``base="allowlist"``: empty guest root binding only ``system_ro``, ``/dev``, ``/proc`` and the
    spec's ro/rw paths — unbound paths do not exist in the guest (stronger than unshare's denylist);
  - ``base="root_ro"``: ``-r /`` shows the whole host and masked paths are covered with an empty
    dir/file (denylist semantics, like the unshare fallback);
  - masks are ``-b <empty>:<target>``; ``tmpfs`` becomes an empty writable dir; ``run_as`` only makes
    the guest *see* ``-i uid:gid`` (no real UID switch); ``share_net=false`` wraps the command in
    ``unshare --map-root-user --net``.

  PRoot has no read-only bind and no real UID switch, so ``registry.resolve_spec`` always marks a
  proot spec ``ro_soft``: the probe records writable read-only dirs in the audit summary instead of
  failing, while mask checks stay strict. No PID namespace.

* The opt-in process runner (``ProotSpec``/``preflight``/``run_proot``, CLI ``entry/proot_cli``):
  dedicated rootfs, explicit binds, non-root Linux caller, total deadline, bounded captured output,
  SIGKILL of the process group, and an allowlisted environment (no HOME/credential/PYTHONPATH/proxy
  inheritance). Writable guest binds inherit host permissions; network, PID, UID and read-only
  isolation are refused rather than claimed. See docs/reference/proot-runtime.md.
"""

from __future__ import annotations

import dataclasses
import math
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Sequence

from datafarm.adapters.sandbox.model import (HarnessError, IsolationUnavailable, SandboxSpec,
                                             make_empty_dir, plan_mounts)


# --------------------------------------------------------------------------- shared mechanics

def _command(executable: str, root: str, binds: Iterable[str], cwd: str, command: Sequence[str], *,
             identity: tuple[int, int] | None = None) -> list[str]:
    """The single PRoot flag layout; ``binds`` are ``-b`` operands (``path`` or ``host:guest[!]``)."""
    out = [executable, "-r", root]
    for bind in binds:
        out += ["-b", bind]
    out += ["-w", cwd]
    if identity is not None:
        out += ["-i", f"{int(identity[0])}:{int(identity[1])}"]
    return out + list(command)


def _environment(extra: Mapping[str, str] | None) -> dict[str, str]:
    # Never inherit the caller's credentials, HOME, PYTHONPATH or proxy settings.
    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/agent", "LANG": "C.UTF-8",
           "TMPDIR": "/tmp"}
    for key, value in (extra or {}).items():
        if (not isinstance(key, str) or not key or "=" in key or "\x00" in key
                or not isinstance(value, str) or "\x00" in value):
            raise ProotError("environment entries must be valid strings")
        env[key] = value
    return env


def _kill_group(process: subprocess.Popen) -> None:
    # Use SIGKILL for a bounded stop even when a guest ignores SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def _capture(argv: Sequence[str], env: Mapping[str, str], *, timeout_s: float,
             max_output_bytes: int, stdin: bytes = b"") -> ProotResult:
    started = time.monotonic()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    timed_out = output_limited = False
    # A file avoids blocking the supervisor on a guest that never reads stdin.
    with tempfile.TemporaryFile() as input_file:
        input_file.write(stdin)
        input_file.seek(0)
        process = subprocess.Popen(argv, stdin=input_file, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=dict(env), cwd="/",
                                   start_new_session=True)
        try:
            with selectors.DefaultSelector() as selector:
                for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    remaining = timeout_s - (time.monotonic() - started)
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        available = max_output_bytes - sum(map(len, output.values()))
                        output[key.data].extend(data[:available])
                        if len(data) > available:
                            output_limited = True
                            break
                    if output_limited:
                        break
                if not timed_out and not output_limited:
                    remaining = timeout_s - (time.monotonic() - started)
                    try:
                        process.wait(timeout=max(0, remaining))
                    except subprocess.TimeoutExpired:
                        timed_out = True
        finally:
            # Also stop remaining processes in this group after ordinary exit.
            _kill_group(process)
            process.stdout.close()
            process.stderr.close()
    return ProotResult(process.returncode, output["stdout"].decode("utf-8", errors="replace"),
                       output["stderr"].decode("utf-8", errors="replace"),
                       time.monotonic() - started, timed_out, output_limited)


def _version_ok(executable: str, env: Mapping[str, str], timeout_s: float) -> tuple[bool, str]:
    version = _capture([executable, "--version"], env, timeout_s=timeout_s, max_output_bytes=16384)
    if version.succeeded and "proot" in (version.stdout + version.stderr).lower():
        return True, "ok"
    return False, "selected PRoot executable failed its version probe"


# --------------------------------------------------------------------------- SandboxSpec backend

def resolve_binary(spec: SandboxSpec | None = None) -> str:
    candidate = (spec.proot_bin if spec is not None else "") or os.environ.get("PROOT", "")
    candidate = candidate or shutil.which("proot") or ""
    if not candidate:
        raise IsolationUnavailable("proot not found: set the isolation 'proot' path "
                                   "(adapter config) or PROOT, or put proot on PATH")
    return candidate


def _scratch_base(spec: SandboxSpec) -> Path:
    """Guest root and empty dir live next to the harness-provided ``empty_file``."""
    if not spec.empty_file:
        raise HarnessError("the proot backend needs SandboxSpec.empty_file (its scratch directory)")
    return Path(spec.empty_file).parent


def _guest_root(spec: SandboxSpec) -> str:
    if spec.base == "root_ro":
        return "/"
    return str(_scratch_base(spec) / ".xh-proot-root")


def _empty_dir_path(spec: SandboxSpec) -> str:
    return str(_scratch_base(spec) / ".xh-empty-dir")


def prepare(spec: SandboxSpec) -> None:
    """Create the guest root and empty dir before running ``argv`` (called by ``registry.wrap``;
    ``argv`` stays pure for tests)."""
    if spec.base != "root_ro":
        make_empty_dir(_scratch_base(spec), ".xh-proot-root")
    make_empty_dir(_scratch_base(spec), ".xh-empty-dir")


def argv(spec: SandboxSpec, command: Sequence[str], *,
         lexists: Callable[[str], bool] = os.path.lexists,
         isdir: Callable[[str], bool] = os.path.isdir) -> list[str]:
    empty_dir = _empty_dir_path(spec)
    ops = plan_mounts(spec, lexists=lexists, isdir=isdir)
    covered = {op.dest for op in ops}
    binds: list[str] = []
    if spec.base == "allowlist":
        binds += [path for path in spec.system_ro if lexists(path)]
        binds += ["/dev", "/proc"]
        if "/tmp" not in covered:
            binds.append(empty_dir + ":/tmp")
    for op in ops:
        if op.kind == "dir":
            continue  # proot needs no explicit parent dirs for bind points
        if op.kind in ("ro", "rw"):
            binds.append(op.src or op.dest)
        elif op.kind in ("tmpfs", "mask_dir"):
            # tmpfs degrades to "empty writable dir"; masked dirs are covered with the empty dir
            binds.append(empty_dir + ":" + op.dest)
        elif op.kind == "mask_file":
            if not spec.empty_file:
                raise HarnessError("masking a file needs SandboxSpec.empty_file")
            binds.append(spec.empty_file + ":" + op.dest)
    out = _command(resolve_binary(spec), _guest_root(spec), binds, spec.cwd, command, identity=spec.run_as)
    if not spec.share_net:
        out = ["unshare", "--map-root-user", "--net", "--propagation", "unchanged", "--"] + out
    return out


def probe(binary: str | None = None) -> tuple[bool, str]:
    """Bounded, environment-scrubbed availability check: version identity, then ``-r / /bin/true``."""
    try:
        proot = binary or resolve_binary(None)
    except IsolationUnavailable as exc:
        return False, str(exc)
    env = _environment(None)
    try:
        ok, why = _version_ok(proot, env, 10.0)
        if not ok:
            return False, why
        launch = _capture(_command(proot, "/", (), "/", ["/bin/true"]), env, timeout_s=30.0,
                          max_output_bytes=16384)
    except OSError as exc:
        return False, f"proot: {exc}"
    if launch.succeeded:
        return True, "ok"
    if launch.timed_out:
        return False, "proot: launch probe timed out"
    return False, (launch.stderr or launch.stdout or f"exit {launch.returncode}").strip()[:300]


# --------------------------------------------------------------------------- process runner

class ProotError(ValueError):
    """Invalid configuration or an unavailable experimental runtime."""


def _guest_path(value: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/")
            or value.startswith("//") or ".." in PurePosixPath(value).parts
            or any(char in value for char in (":", "!", "\x00", "\n", "\r"))):
        raise ProotError("guest paths must be absolute, without traversal or bind syntax")
    return str(PurePosixPath(value))


def _host_path(value: Path, *, directory: bool = False) -> Path:
    value = Path(value)
    if not value.is_absolute():
        raise ProotError("host paths must be explicitly absolute")
    resolved = value.resolve(strict=True)
    if any(char in str(resolved) for char in (":", "!", "\n", "\r")):
        raise ProotError("host paths contain unsupported bind syntax")
    if directory and not resolved.is_dir():
        raise ProotError("expected a host directory")
    return resolved


@dataclasses.dataclass(frozen=True)
class ProotBind:
    host: Path
    guest: str
    writable: bool = True


@dataclasses.dataclass(frozen=True)
class ProotSpec:
    executable: Path
    rootfs: Path
    binds: tuple[ProotBind, ...] = ()
    cwd: str = "/"
    acknowledge_weak_isolation: bool = False
    timeout_s: float = 60.0
    max_output_bytes: int = 1048576
    require_readonly: bool = False
    require_network_isolation: bool = False
    require_pid_isolation: bool = False
    run_as: tuple[int, int] | None = None


@dataclasses.dataclass(frozen=True)
class ProotResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float
    timed_out: bool
    output_limited: bool

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.output_limited


def _validate(spec: ProotSpec) -> tuple[Path, Path, list[tuple[Path, str]], str]:
    if sys.platform != "linux":
        raise ProotError("PRoot requires Linux; use an explicit Linux or WSL invocation")
    if os.getuid() == 0:
        raise ProotError("this experimental runner must be launched by a non-root user")
    if spec.acknowledge_weak_isolation is not True:
        raise ProotError("explicit acknowledgement of weak isolation is required")
    if (spec.require_readonly or spec.require_network_isolation
            or spec.require_pid_isolation or spec.run_as is not None):
        raise ProotError("PRoot cannot enforce readonly, network, PID or UID isolation")
    if (isinstance(spec.timeout_s, bool) or not isinstance(spec.timeout_s, (int, float))
            or not math.isfinite(spec.timeout_s) or not 0 < spec.timeout_s <= 900):
        raise ProotError("timeout_s must be finite and in (0, 900]")
    if (isinstance(spec.max_output_bytes, bool)
            or not isinstance(spec.max_output_bytes, int)
            or not 1 <= spec.max_output_bytes <= 16777216):
        raise ProotError("max_output_bytes must be in [1, 16777216]")
    executable = _host_path(spec.executable)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ProotError("PRoot executable is missing or not executable")
    rootfs = _host_path(spec.rootfs, directory=True)
    if rootfs == Path("/"):
        raise ProotError("use a dedicated rootfs, not the entire host root")
    binds = []
    seen = set()
    for bind in spec.binds:
        if bind.writable is not True:
            raise ProotError("readonly binds are not supported by PRoot")
        guest = _guest_path(bind.guest)
        if guest == "/" or guest in seen:
            raise ProotError("bind destinations must be unique and must not replace rootfs")
        seen.add(guest)
        binds.append((_host_path(bind.host), guest))
    return executable, rootfs, binds, _guest_path(spec.cwd)


def runner_argv(spec: ProotSpec, command: Sequence[str]) -> list[str]:
    """Validated runner argv: dedicated rootfs, explicit binds, never implicit -R/-S host binds."""
    executable, rootfs, binds, cwd = _validate(spec)
    if (isinstance(command, (str, bytes)) or not command
            or any(not isinstance(arg, str) or "\x00" in arg for arg in command)):
        raise ProotError("command must be a nonempty sequence of argument strings")
    if not command[0] or command[0].startswith("-"):
        raise ProotError("command executable must not be a PRoot option")
    # ! disables destination symlink dereferencing; it is not readonly.
    return _command(str(executable), str(rootfs), (f"{host}:{guest}!" for host, guest in binds), cwd, command)


def preflight(spec: ProotSpec, *, env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Check the selected executable, then a bounded guest /bin/true invocation."""
    executable, _, _, _ = _validate(spec)
    environment = _environment(env)
    started = time.monotonic()
    budget = min(10.0, spec.timeout_s)
    ok, why = _version_ok(str(executable), environment, budget)
    if not ok:
        raise ProotError(why)
    remaining = budget - (time.monotonic() - started)
    if remaining <= 0:
        raise ProotError("PRoot preflight exhausted its deadline")
    launch = _capture(runner_argv(spec, ["/bin/true"]), environment,
                      timeout_s=remaining, max_output_bytes=16384)
    if not launch.succeeded:
        raise ProotError("PRoot guest launch failed; check rootfs, loader and ptrace support")
    return {"runtime": "proot", "launch_probe": "passed", "security_boundary": False,
            "enforced_readonly": False, "network_isolated": False, "pid_isolated": False,
            "uid_isolated": False, "binds_use_host_permissions": True,
            "process_cleanup": "current process group only; not escape containment"}


def run_proot(spec: ProotSpec, command: Sequence[str], *, stdin: bytes = b"",
              env: Mapping[str, str] | None = None) -> ProotResult:
    """Run with a total deadline and bounded captured output, without shell expansion.

    Only explicitly supplied environment values are propagated. The caller must
    keep upstream API keys in a trusted gateway; environment filtering is not a
    secrecy guarantee if the guest can access host processes or credential files.
    """
    argv = runner_argv(spec, command)
    if not isinstance(stdin, bytes) or len(stdin) > 1048576:
        raise ProotError("stdin must be bytes, at most 1 MiB")
    started = time.monotonic()
    preflight(spec, env=env)
    remaining = spec.timeout_s - (time.monotonic() - started)
    if remaining <= 0:
        return ProotResult(-signal.SIGKILL, "", "", time.monotonic() - started, True, False)
    result = _capture(argv, _environment(env), timeout_s=remaining,
                      max_output_bytes=spec.max_output_bytes, stdin=stdin)
    return dataclasses.replace(result, elapsed_s=time.monotonic() - started)

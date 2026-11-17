"""Opt-in PRoot process runner; path translation is not a security boundary.

This adapter intentionally does not implement ``SandboxSpec``. Writable guest
binds inherit host permissions. Network, PID, UID and readonly isolation are
unsupported; frozen Judge verification remains a separate trusted operation.
"""

from __future__ import annotations

import dataclasses
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


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


def proot_argv(spec: ProotSpec, command: Sequence[str]) -> list[str]:
    """Build explicit -r/-b/-w arguments; never use implicit -R/-S host binds."""
    executable, rootfs, binds, cwd = _validate(spec)
    if (isinstance(command, (str, bytes)) or not command
            or any(not isinstance(arg, str) or "\x00" in arg for arg in command)):
        raise ProotError("command must be a nonempty sequence of argument strings")
    if not command[0] or command[0].startswith("-"):
        raise ProotError("command executable must not be a PRoot option")
    argv = [str(executable), "-r", str(rootfs)]
    for host, guest in binds:
        # ! disables destination symlink dereferencing; it is not readonly.
        argv += ["-b", f"{host}:{guest}!"]
    return argv + ["-w", cwd, *command]


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


def preflight(spec: ProotSpec, *, env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Check the selected executable, then a bounded guest /bin/true invocation."""
    executable, _, _, _ = _validate(spec)
    environment = _environment(env)
    started = time.monotonic()
    budget = min(10.0, spec.timeout_s)
    version = _capture([str(executable), "--version"], environment,
                       timeout_s=budget, max_output_bytes=16384)
    if not version.succeeded or "proot" not in (version.stdout + version.stderr).lower():
        raise ProotError("selected PRoot executable failed its version probe")
    remaining = budget - (time.monotonic() - started)
    if remaining <= 0:
        raise ProotError("PRoot preflight exhausted its deadline")
    probe = _capture(proot_argv(spec, ["/bin/true"]), environment,
                     timeout_s=remaining, max_output_bytes=16384)
    if not probe.succeeded:
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
    argv = proot_argv(spec, command)
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

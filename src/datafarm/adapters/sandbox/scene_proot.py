"""Explicit weak-isolation scene expert runtime; no host scene/Judge guest bind."""
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import sys
import tempfile

from datafarm.kernel.io import atomic_write_json, sha256_json
from .proot import _host_path
from .isolation import setpriv_prefix

KEYS = frozenset({"mode", "acknowledge_weak_isolation", "executable", "python", "stdlib", "run_as"})


def validate_runtime(value):
    if not isinstance(value, dict) or set(value) - KEYS or value.get("mode") != "proot":
        raise ValueError("explicit scene PRoot runtime required")
    if value.get("acknowledge_weak_isolation") is not True:
        raise ValueError("scene PRoot requires explicit weak-isolation acknowledgement")
    result = dict(value)
    for key in ("executable", "python", "stdlib"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError("explicit scene PRoot runtime path required")
    for key in ("executable", "python", "stdlib"):
        p = _host_path(Path(value.get(key, "")), directory=key == "stdlib")
        if key != "stdlib" and (not p.is_file() or not os.access(p, os.X_OK)):
            raise ValueError("scene PRoot runtime executable unavailable")
        result[key] = str(p)
    run_as = value.get("run_as")
    if run_as is not None:
        if (not isinstance(run_as, dict) or set(run_as) != {"uid", "gid"}
                or any(type(run_as[k]) is not int or not 1 <= run_as[k] <= 2147483647 for k in run_as)):
            raise ValueError("scene PRoot needs a real non-root uid/gid")
        result["run_as"] = dict(run_as)
    if sys.platform != "linux":
        raise ValueError("scene PRoot requires Linux or explicit WSL")
    uid, gid = os.getuid(), os.getgid()
    if uid == 0 and run_as is None:
        raise ValueError("root scene host must explicitly select a non-root expert uid/gid")
    if uid != 0 and run_as is not None and (run_as["uid"], run_as["gid"]) != (uid, gid):
        raise ValueError("non-root host cannot switch the expert uid/gid")
    return result


def runtime_record(value):
    value = validate_runtime(value)
    return {"mode": "proot", "security_boundary": False, "readonly_enforced": False,
            "network_isolated": False, "pid_isolated": False,
            "acknowledge_weak_isolation": True, "guest_user": "nonroot",
            "configuration_sha256": sha256_json(value),
            "executable_sha256": hashlib.sha256(Path(value["executable"]).read_bytes()).hexdigest(),
            "python_sha256": hashlib.sha256(Path(value["python"]).read_bytes()).hexdigest()}


@contextmanager
def expert_command(runtime, expert_source, worker, *, timeout_s, class_name=None):
    value = validate_runtime(runtime)
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 900:
        raise ValueError("PRoot expert wall budget must be in (0, 900]")
    if not isinstance(expert_source, bytes) or len(expert_source) > 64000:
        raise ValueError("bounded snapshotted expert source required")
    worker = Path(worker).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="datafarm-scene-proot-") as temporary:
        root = Path(temporary)
        guest = root / "rootfs"
        for folder in (guest / "work", guest / "tmp", guest / "home/agent"):
            folder.mkdir(parents=True)
        files = {guest / "work/expert.py": expert_source,
                 guest / "work/worker.py": worker.read_bytes(),
                 root / "proot_exec.py": Path(__file__).with_name("proot_exec.py").read_bytes(),
                 root / "proot.py": Path(__file__).with_name("proot.py").read_bytes()}
        for p, source in files.items():
            p.write_bytes(source)
            p.chmod(0o444)
        system = [Path(p) for p in ("/usr", "/bin", "/lib", "/lib64") if Path(p).exists()]
        binds = [(str(p.resolve()), str(p)) for p in system]
        runtime_files = [Path(value["python"]), Path(value["stdlib"])]
        # Standalone Python deployments may link a sibling libpython outside /usr.
        # Bind exact shared-library files, not the deployment/home/store directory.
        for directory in {Path(value["stdlib"]).parent, Path(value["python"]).parent.parent / "lib"}:
            if directory.is_dir():
                runtime_files.extend(sorted(directory.glob("libpython*.so*")))
        for p in dict.fromkeys(p.resolve(strict=True) for p in runtime_files):
            if not any(p == s.resolve() or p.is_relative_to(s.resolve()) for s in system):
                binds.append((str(p), str(p)))
        command = [value["python"], "-I", "-S", "-B", "/work/worker.py", "/work/expert.py"]
        if class_name is not None:
            command.append(class_name)
        config = root / "launch.json"
        atomic_write_json(config, {"executable": value["executable"], "rootfs": str(guest),
            "binds": binds, "command": command, "timeout_s": timeout_s})
        run_as = value.get("run_as")
        if os.getuid() == 0:
            for folder, dirs, names in os.walk(root):
                os.chown(folder, run_as["uid"], run_as["gid"])
                for name in names:
                    os.chown(Path(folder) / name, run_as["uid"], run_as["gid"])
        argv = [value["python"], "-I", "-S", "-B", str(root / "proot_exec.py"), str(config)]
        if os.getuid() == 0:
            argv = setpriv_prefix((run_as["uid"], run_as["gid"])) + argv
        try:
            yield argv
        finally:
            # Snapshot mutation is detected, not prevented by writable PRoot binds.
            if any(not p.is_file() or p.read_bytes() != source for p, source in files.items()):
                raise RuntimeError("SCENE_PROOT_SNAPSHOT_CHANGED")

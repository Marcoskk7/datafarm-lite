"""Standalone streaming PRoot launcher; host owns IPC, budgets and freeze checks.

Unlike captured ``run_proot``, this launcher preserves the parent's stdin/stdout.
It must run as an actual non-root user. No namespace/readonly claim is made.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise ValueError("one trusted launch configuration required")
    config = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    # Standalone file loading avoids importing the deployment package or site.
    path = Path(__file__).with_name("proot.py")
    loader = importlib.util.spec_from_file_location("scene_proot_runner", path)
    module = importlib.util.module_from_spec(loader)
    sys.modules[loader.name] = module
    loader.loader.exec_module(module)
    spec = module.ProotSpec(executable=Path(config["executable"]), rootfs=Path(config["rootfs"]),
        binds=tuple(module.ProotBind(Path(a), b) for a, b in config["binds"]),
        cwd="/work", acknowledge_weak_isolation=True,
        timeout_s=config["timeout_s"], max_output_bytes=16384)
    env = module._environment({"PROOT_NO_SECCOMP": "1"})
    module.preflight(spec, env=env)
    os.execve(str(spec.executable), module.proot_argv(spec, config["command"]), env)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stderr.write("SCENE_PROOT_LAUNCH_UNAVAILABLE\n")
        sys.exit(97)

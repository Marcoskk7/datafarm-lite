"""Trusted nonroot launch preparation and a stdlib-only streaming exec shim.

Preparation imports the shared host package; the copied shim never imports it.
PRoot is path translation, not readonly, namespace or secrecy protection.
"""
import json
import os
from pathlib import Path
import sys


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if os.name != "posix" or os.getuid() == 0:
        raise ValueError("actual nonroot Linux launcher required")
    if len(args) == 3 and args[0] == "--prepare":
        # This mode runs on the trusted host before expert execution, never in guest.
        source_root = Path(args[2]).resolve(strict=True)
        sys.path.insert(0, str(source_root))
        from datafarm.adapters.sandbox.proot import ProotSpec, ProotBind, preflight, runner_argv, _environment
        config = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        spec = ProotSpec(executable=Path(config["executable"]), rootfs=Path(config["rootfs"]),
            binds=tuple(ProotBind(Path(a), b) for a, b in config["binds"]),
            cwd="/work", acknowledge_weak_isolation=True,
            timeout_s=config["timeout_s"], max_output_bytes=16384)
        environment = _environment({"PROOT_NO_SECCOMP": "1"})
        preflight(spec, env=environment)
        print(json.dumps({"argv":runner_argv(spec, config["command"]), "env":environment}, allow_nan=False))
        return
    if len(args) != 1:
        raise ValueError("one trusted launch plan required")
    raw = Path(args[0]).read_bytes()
    if len(raw) > 65536:
        raise ValueError("launch plan too large")
    plan = json.loads(raw)
    if not isinstance(plan, dict) or set(plan) != {"argv", "env"}:
        raise ValueError("invalid launch plan")
    command, environment = plan["argv"], plan["env"]
    if (not isinstance(command, list) or not command or not Path(command[0]).is_absolute()
            or any(not isinstance(v,str) or not v or "\x00" in v for v in command)):
        raise ValueError("invalid validated command")
    if (not isinstance(environment,dict) or set(environment) != {"PATH","HOME","LANG","TMPDIR","PROOT_NO_SECCOMP"}
            or any(not isinstance(v,str) or "\x00" in v for v in environment.values())):
        raise ValueError("invalid launch environment")
    os.execve(command[0], command, environment)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stderr.write("SCENE_PROOT_LAUNCH_UNAVAILABLE\n")
        sys.exit(97)

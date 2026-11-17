"""Explicit experimental PRoot launcher, separate from expert sandbox modes."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from datafarm.adapters.sandbox.proot import ProotBind, ProotError, ProotSpec, preflight, run_proot


def _bind(value: str) -> ProotBind:
    host, separator, guest = value.partition(":")
    if not separator or not host or not guest:
        raise argparse.ArgumentTypeError("bind must be HOST:GUEST (host permissions apply)")
    return ProotBind(Path(host), guest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proot", required=True, type=Path, help="explicit Linux executable")
    parser.add_argument("--rootfs", required=True, type=Path)
    parser.add_argument("--bind", action="append", default=[], type=_bind)
    parser.add_argument("--cwd", default="/")
    parser.add_argument("--acknowledge-weak-isolation", action="store_true",
                        help="accept shared kernel/network/processes and writable host binds")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-output-bytes", type=int, default=1048576)
    parser.add_argument("--pass-env", action="append", default=[], metavar="NAME",
                        help="explicitly pass a named environment value; never use upstream keys")
    parser.add_argument("--check", action="store_true", help="only run the bounded launch probe")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.check and not command:
        parser.error("a command after -- is required, unless --check is selected")
    if args.check and command:
        parser.error("--check does not run a command")
    missing = [name for name in args.pass_env if name not in os.environ]
    if missing:
        parser.error("a requested environment variable is missing")
    env = {name: os.environ[name] for name in args.pass_env}
    spec = ProotSpec(executable=args.proot, rootfs=args.rootfs, binds=tuple(args.bind),
                     cwd=args.cwd, acknowledge_weak_isolation=args.acknowledge_weak_isolation,
                     timeout_s=args.timeout, max_output_bytes=args.max_output_bytes)
    try:
        if args.check:
            print(json.dumps(preflight(spec, env=env), sort_keys=True))
            return 0
        result = run_proot(spec, command, env=env)
    except ProotError as exc:
        print(f"PRoot launch refused: {exc}", file=sys.stderr)
        return 2
    except OSError:
        # OS exceptions can contain host paths; keep those out of diagnostics.
        print("PRoot launch failed: check runtime access and rootfs configuration", file=sys.stderr)
        return 2
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    summary = dataclasses.asdict(result)
    summary.pop("stdout")
    summary.pop("stderr")
    print(json.dumps({"runtime": "proot", "security_boundary": False, **summary}), file=sys.stderr)
    if result.timed_out:
        return 124
    if result.output_limited:
        return 125
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

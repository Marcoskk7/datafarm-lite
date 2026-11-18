"""OS-level isolation for agent CLIs and rollouts (bwrap / proot / unshare) plus the opt-in PRoot runner.

``model`` holds the backend-neutral spec, mount plan, caps policy and probe; ``bwrap``/``proot``/
``unshare`` build argv; ``registry`` probes, selects (fail-closed) and wraps; ``jobwrapper`` emits the
equivalent Bash for rollout jobs. ``proot`` also hosts the bounded process runner used by
``entry/proot_cli``.
"""

from datafarm.adapters.sandbox.model import (ALLOWED_KEEP_CAPS, DEFAULT_SYSTEM_RO, PROBE_SCRIPT, HarnessError,
                                             IsolationUnavailable, MountOp, ProbeResult, SandboxSpec,
                                             check_keep_caps, chown_tree, make_empty_dir, make_empty_file,
                                             parse_probe, plan_mounts, probe_args, run_probe, sandbox_env,
                                             setpriv_prefix)
from datafarm.adapters.sandbox.bwrap import argv as bwrap_argv
from datafarm.adapters.sandbox.proot import (ProotBind, ProotError, ProotResult, ProotSpec, preflight,
                                             run_proot)
from datafarm.adapters.sandbox.proot import argv as proot_argv
from datafarm.adapters.sandbox.unshare import argv as unshare_argv
from datafarm.adapters.sandbox.jobwrapper import job_wrapper_bash
from datafarm.adapters.sandbox.registry import BACKENDS, probe_available, resolve_spec, wrap

__all__ = [
    "ALLOWED_KEEP_CAPS", "BACKENDS", "DEFAULT_SYSTEM_RO", "PROBE_SCRIPT", "HarnessError", "IsolationUnavailable",
    "MountOp", "ProbeResult", "ProotBind", "ProotError", "ProotResult", "ProotSpec", "SandboxSpec",
    "bwrap_argv", "check_keep_caps", "chown_tree", "job_wrapper_bash", "make_empty_dir", "make_empty_file",
    "parse_probe", "plan_mounts", "preflight", "probe_args", "probe_available", "proot_argv", "resolve_spec",
    "run_probe", "run_proot", "sandbox_env", "setpriv_prefix", "unshare_argv", "wrap",
]

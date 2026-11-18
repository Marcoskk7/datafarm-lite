# Experimental PRoot runtime

PRoot is an **opt-in Linux userspace path-translation runner**, not an OS security
boundary. It can launch a CLI in an explicit guest filesystem when ptrace is
available. The standalone launcher remains separate from general SandboxSpec modes.
An explicit scene-expert integration is available through `scene_job` and
`scene_execution`; see [scene harness](scene-harness.md). It does not change
CaP's public-request whitelist or Judge freeze formats/admission rules.

Implementation:
- `datafarm.adapters.sandbox.proot`: explicit rootfs/binds, runtime preflight,
  environment allowlisting, deadline and captured-output limits. The same module
  is the `SandboxSpec` proot backend used by scene authoring; both share one
  `-r/-b/-w` argv layout, and that backend's availability probe uses this
  runner's bounded, environment-scrubbed version and launch checks.
- `datafarm.entry.proot_cli`: a separate `python -m` composition entry point.
  It is not wired into the default `datafarm` CLI or `SandboxSpec`.

## Guarantees and non-guarantees

| Property | Behavior |
| --- | --- |
| Guest paths | Explicit `-r`, `-b` and `-w`; no implicit `-R`/`-S` host mounts |
| User | Linux non-root caller required; no fake root or UID-switch claim |
| Readonly | Unsupported; bind writes follow host permissions |
| Network | Shared with the host; no network namespace |
| Processes | No PID namespace; host process visibility depends on explicit mounts |
| Environment | Small default set; caller variables are not inherited implicitly |
| Deadline | Total preflight + invocation deadline, at most 900 seconds |
| Output | Combined stdout/stderr capture cap, at most 16 MiB; exceeding it fails |
| Cleanup | Kill the current process group on timeout/output cap and after exit |
| Escape containment | Not provided; detached processes are not contained |
| Judge freeze | Independent trusted-host hash verification, not readonly enforcement |

The Python API rejects readonly binds, requested network/PID isolation, UID
switches, missing weak-isolation acknowledgement, invalid bounds and replacing
rootfs with the entire host root. Guest path traversal and ambiguous bind syntax
are rejected. Explicit host binds are not a secrecy allowlist: PRoot cannot
protect hidden Judge code, evaluation seeds or credentials from hostile code.

The process-group deadline is cooperative lifecycle management, not containment
of processes that deliberately detach. Output limits cover captured bytes only,
not guest-created files, memory, CPU quotas or disk usage.

## Preparing a runtime

Provide a dedicated rootfs and a Linux PRoot executable of the same architecture.
The guest needs `/bin/true`, its loader/libraries, the intended command, an existing
working directory, a temporary directory and a writable home. The runner neither
downloads packages nor edits the host's global CLI configuration.

A disposable rootfs may bind selected host runtime directories such as `/usr`,
`/bin`, `/lib` and `/lib64`. These **are not readonly mounts**. Prefer copies or
host-owned runtime files, and never bind personal credential directories. `-b`
uses the `!` suffix to avoid destination symlink dereferencing; `!` does not mean
readonly. Avoid mounting `/proc` unless the process-visibility implications are
explicitly acceptable. Only bind specific network configuration files if needed.

All host paths are explicit deployment-local inputs, not repository defaults.
For an unpacked PRoot with shared libraries outside the system search path, the
caller can explicitly pass `LD_LIBRARY_PATH`; this value also reaches the guest.
Keep local runtime packages, scripts and evidence in an ignored directory.

## Invocation

Run this from Linux (or explicitly through WSL); a Windows invocation is refused.
The variables below are placeholders supplied by the deployment:

```bash
PYTHONPATH=src python -m datafarm.entry.proot_cli \
  --proot "$PROOT_BIN" --rootfs "$ROOTFS" \
  --bind "$RUNTIME_USR:/usr" --bind "$RUNTIME_BIN:/bin" \
  --bind "$RUNTIME_LIB:/lib" --bind "$RUNTIME_LIB64:/lib64" \
  --bind "$WORK:/work" --cwd /work \
  --acknowledge-weak-isolation --timeout 60 --check
```

Replace `--check` with `-- COMMAND ARG...` for a command. Standard output contains
command output; standard error contains command diagnostics and a small
credential-free runtime summary. Exit 124 denotes timeout, 125 denotes output
limit, and 2 denotes invalid/unavailable runtime configuration. Other command exit
statuses are preserved; success means both exit zero and no supervisor limit hit.

Use `--pass-env NAME` only for an explicitly selected variable. Do not pass an
upstream API key. A trusted parent gateway can hold that key and issue a temporary
loopback credential for a bounded model call. Environment filtering alone is not
credential protection when host paths or processes are exposed. Command output
can still contain sensitive data: the caller owns its redaction and evidence policy.
The runner does not submit model requests or enforce model/token/cost budgets.

The Python API is `ProotSpec` + `ProotBind`, `preflight(spec)` and
`run_proot(spec, command, stdin=..., env=...)`. `stdin` is bytes, at most 1 MiB.
Use the result's `succeeded` property, not `returncode == 0` alone.

## Codex compatibility observed locally

Local non-root WSL Ubuntu 24.04 probes used PRoot 5.1.0 and Codex CLI
0.169.0-alpha.11.1, including its matching code-mode-host component:

1. **PRoot + native Codex read-only sandbox: tool execution failed.** The CLI
   launched and reached the model, but bubblewrap reported
   `Failed to make / slave: Invalid argument`. CLI exit zero was not accepted as
   task success: no successful shell command or matching fixture result existed.
2. **PRoot + explicitly acknowledged disabled native sandbox: tool probe passed.**
   A fresh Codex session executed one shell command to read a random JSON fixture.
   Its sum and exact-byte SHA-256 matched independent host calculations, and the
   input bytes remained unchanged. This uses Codex's explicit
   `--dangerously-bypass-approvals-and-sandbox` flag; the launcher never injects it
   or automatically falls back to it.

The two sessions used an explicitly configured FYP model through a trusted local
parent gateway, independent fresh homes, four total upstream requests and no
request retry. This is CLI/tool compatibility evidence, **not** full CaP/Judge,
Isaac physics or task-completion acceptance. Other Codex/PRoot/kernel versions
may behave differently; repeat the discriminating probes before deployment.

## Frozen Judge boundary

A separate local compatibility probe generated a **fresh scripted fixture** freeze,
verified its external anchor and positive case, then deliberately changed its
Judge source through a writable PRoot bind. The trusted host rejected the changed
bundle. No old frozen record was rewritten.

This demonstrates tamper **detection**, not prevention, secrecy or a security
signature. Production Judge generation, review and evaluation stay outside the guest.
The explicit scene integration verifies live freeze and source bindings around
expert execution; this supplies tamper detection, not secrecy or containment. No claim that PRoot satisfies current expert isolation contracts is made.

## Validation policy

Runtime/CLI probes, negative configuration cases, stdin, nonzero exit, deadline,
output limits, environment filtering, writable binds, shared loopback network and
process-group cleanup are exercised by local-only validation scripts. Existing
sandbox and Codex tests are run unchanged. These scripts, package downloads,
model outputs and logs are excluded from this feature's source commit.

Select scene PRoot only through acknowledged deployment configuration.
Never silently fall back to it or interpret its successful functional probe as
OS containment. The old native Codex sandbox behavior remains unchanged.

## Shared package and streaming expert adapter

The package structure from A request-authoring is retained: `model`, `registry`,
`bwrap`, `proot`, `unshare`, `jobwrapper`. The scene expert consumer uses
`proot.runner_argv(ProotSpec, command)`, not the backend's
`argv(SandboxSpec, command)`. There is no restored `isolation.py` compatibility
module. Scene-author sandbox and scene-expert streaming IPC are separate consumers.

Preparation imports the shared host package under the real non-root OS identity.
A root trusted host uses `setpriv`; PRoot's simulated `-i` identity is not a substitute.
A stdlib-only copied launcher executes the validated plan with scrubbed environment
and preserves stdin/stdout for observe/move IPC. The shared package itself is not
copied as a standalone `proot.py` or bound into the expert guest. The guest gets
only snapshotted expert/worker and explicitly selected runtime paths. Host scene,
store, physics and Judge authority remain on the trusted host.

Launch inputs and shared-module fingerprints are checked before/after execution.
PRoot still does not enforce readonly/network/PID namespaces or prevent malicious
escape. Bounded process-group cleanup has the previously documented limitations.
Frozen dependency manifests include the actual package modules, not the removed
`isolation.py`; changed runtime sources require fresh Judge generation/freezing.

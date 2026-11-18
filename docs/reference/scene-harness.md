# Scene expert harness

`entry.scene_job` is a broker-compatible simulator command. An explicitly pinned
trusted deployment factory supplies initialized A-bound resources, per-seed state,
expert ArtifactRef, family and budgets. It is not a task registry and does not
automatically download or launch a model. The snapshotted expert bytes must match
the ref. No factory is loaded from an untrusted expert action.

Expert `run(actions)` executes in a separate process. An explicitly configured
PRoot runtime uses a fresh guest with copied source and a standalone worker;
scene/store/Judge objects never cross IPC. PRoot shares network/kernel/process
semantics and is not a security boundary. Legacy bwrap remains available when
no PRoot runtime is selected; it is not an automatic fallback.
A narrow JSON request protocol permits
observe, move_tcp and move_gripper with bounded calls, wall time and messages.
The parent owns physics, measurement and persistence; the Judge executes later
outside expert access using exact Episode and contract refs.

The selected runtime fails closed when unavailable. PRoot requires explicit
weak-isolation acknowledgement; none and automatic downgrade are refused. Local software
protocol tests do not prove actual OS containment or physical success. The
existing broker launch/heartbeat/holdout budgets remain authoritative; this entry
does not waive external deployment qualifications.

## Composition and source binding

`bind_scene_adapter` writes a new adapter/1 configuration from the exact private
request-task/1 Task IR and the portable Judge contract ref. It checks the live
freeze anchor and trusted factory source hash; no scripted fixture can be bound
through this production helper. The resulting simulator command runs
`python -m datafarm.entry.scene_job`; the Judge command runs
`python -m datafarm.entry.judge_cli evaluate-recorded` after simulator exit.
The adapter also sets `rollout.dev_outcome = {schema: execution-summary/1, file:
native-parent/native/episode-ref.json}`: development rollouts skip the Judge and are
classified from the recorder summary (`executed` / `expert_error` / infra), and CaP's
own self-check decides success. Holdout rollouts still run the Judge exactly once.
The factory, deployment config and contract document join the adapter manifest.
Those local locators are deployment configuration, not portable artifact fields.

The factory returns the keyword arguments for `record_scene_episode` plus
`expert_budget={timeout_s, max_calls}`. Budgets must be finite and positive;
configuration is copied so the factory cannot rewrite caller data. Initialization
and IPC share a cooperative wall deadline. Native control has its own per-call
and total-step budgets. A blocked host/native call is not forcibly interrupted by
IPC alone; the outer broker's process-group wall timeout remains necessary.
The factory must qualify seed initialization; the entry does not invent a sampler.

Only the exact snapshotted expert bytes run. The standalone worker accepts either
`run(actions)` or a named class with `run(self, actions)` (an `__init__` is allowed).
Policy (`scene_worker.check_source`, shared with the host pre-check
`scene_execution.precheck_expert`): only `import math`; top-level constants,
helper functions, lambdas and constant defaults are allowed; attribute access is
allowed except names starting with `_` and a small deny list (`format`, `mro`,
frame/code attributes); constructs listed in `FORBIDDEN_SYNTAX` are rejected;
`print` is a no-op. The host runs `audit_expert_source` plus this check before
launching, so a policy violation is reported as `EXPERT_AUDIT_REJECTED` instead of a
worker crash. Worker errors are sent as `{"method":"error"}` with
`policy: line N: ...` or `exception: <Type>` (no traceback) and recorded as
`EXPERT_EXCEPTION`. These expert-caused reasons are classified `expert_error`
(counted) rather than `judge_failure` (classification lives in `ir/execution_outcome.py`). `bind_scene_adapter` writes the matching
brief (`configs/expert-harness/template-scene-brief.md`, `allowed_imports=[math]`,
`max_bytes<=64000`). It starts without site initialization, with a memory limit, core dumps
disabled and no credentials in its environment. Standard-library directories
and the interpreter are mounted, not the entire deployment virtual environment.
Protocol EOF, malformed requests, excess calls, timeout, extra output or a failed
worker exit cannot become a successful execution. Cleanup targets the process
group even when the original parent has exited.

Each fresh job produces `episode-ref.json` and `initial-state.json`. A valid
initial state is distinct from task success. The Episode retains an execution
error or abort and any partial trace; generated Judge results preserve null and
are classified separately from ordinary predicate failure. The recorder never
self-certifies success, and this transport does not replace the CaP public-task
allowlist. Deployments must qualify their selected trust boundary, native Isaac resources
and private store permissions before admitting a real run. PRoot compatibility
does not certify hidden-evaluation secrecy or containment.

## Explicit PRoot scene deployment

Deployment configuration contains `expert_runtime` and `judge_freeze`:

```json
{
  "family": "task-family",
  "expert_runtime": {
    "mode": "proot",
    "acknowledge_weak_isolation": true,
    "executable": "$PROOT_BIN",
    "python": "$LINUX_PYTHON",
    "stdlib": "$PYTHON_STDLIB",
    "run_as": {"uid": 1000, "gid": 1000}
  },
  "judge_freeze": {"directory": "$FROZEN_BUNDLE", "sha256": "$FREEZE_ANCHOR"}
}
```

The strings are deployment placeholders, not literal runnable defaults. Supply
actual absolute Linux paths in the private deployment file. A root host MUST
select a real non-root uid/gid. A non-root host may omit run_as or name itself.
The launcher copies only the worker and expert into a disposable guest and binds
selected OS runtime files. It does not mount the scene store, Judge bundle,
personal home, `/proc` or `/dev`. OS bind writes still follow host permissions;
PRoot is not a readonly/secret enforcement mechanism.

The trusted host owns physics and checks the external Judge freeze and portable
contract before initialization, immediately before/after expert execution and
before accepting job output. Expert bytes and runtime fingerprints are rechecked.
Source/runtime implementation changes are freeze dependencies: generate a new
freeze/version, never reuse or rewrite an older anchor.

`bind_scene_adapter` validates matching runtime/freeze configuration and emits a
pinned `trusted_scene` declaration with configuration/factory hashes. Adapter/1
rollout `mode=proot` runs this trusted scene host directly and delegates ONLY the
expert to PRoot; GPU resources and Judge are not wrapped in another guest. The
job reports `isolation=weak`, `isolation_backend=proot`, `security_boundary=false`.
No legacy mask probe is relabeled successful. PRoot is not accepted for arbitrary
simulator commands, as a fallback, or as the author CLI isolation mode.

The streaming expert protocol retains call/message/wall caps, source AST audit,
partial/error records and process-group cleanup. PRoot experts have at most 900
seconds; outer job timeouts are still required for blocked native calls. These
are lifecycle limits, not containment of intentionally detached hostile code.
The recorded Episode includes credential-free execution-runtime metadata.
Codex's native sandbox and public task whitelist are not changed by this entry.

Local WSL acceptance must include actual PRoot IPC and negative cases. Software
fixtures are not physics acceptance, and scripted Judge reviews cannot become
production-ready freezes. See [PRoot runtime](proot-runtime.md).

## Shared measured state (current /1)

The recorder, CaP feedback and source-bound Judge use
`datafarm.ir.episode_state.validate_state`. State entries have exactly
`control_step`, `time_s`, `units`, `frame_id`, `entities`, `robot`, `contacts`.
Robot observations use `tcp`, not `tcp_pose` (the capability name
`capabilities.tcp_pose` is unchanged). Judge `judge-episode/1` preserves
`state_trace` without renaming its states. CaP `privileged-state/1` carries
validated initial/final states; its version is on the envelope, not each state.

Judge retains task/scene/request authority, full actuator readback actions,
structured evidence paths such as `["state_trace","entities","object.plate","position_m"]`,
contact availability/coverage, and initial-only or interrupted capture handling.
Execution faults are not task predicate failures. Threshold value, unit, basis
and requirement IDs remain immutable. The independent reviewer consumes this
same exact layout, emits review only, and requires `issues=[]` for a pass.

Keeping /1 does not preserve compatibility with earlier /1 layouts or frozen
hashes. Use a new build/review/freeze identity; never rewrite a previous freeze.
Shared-state adaptation is based on Marcos PR13 commit `74bf9ec`; Judge /2 and
its weaker source/path projection are not imported.

A's existing rendering tools and source files are unchanged here. Their older
`setup.joint_names`, state shape and playback calls still need an A-owned
consumer update. State-driven rendering and action-driven physics replay are
separate checks; this branch does not claim GPU/render validation.

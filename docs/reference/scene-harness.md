# Scene expert harness

`entry.scene_job` is a broker-compatible simulator command. An explicitly pinned
trusted deployment factory supplies initialized A-bound resources, per-seed state,
expert ArtifactRef, family and budgets. It is not a task registry and does not
automatically download or launch a model. The snapshotted expert bytes must match
the ref. No factory is loaded from an untrusted expert action.

Expert `run(actions)` executes in a separate bwrap process, with only source and
a standalone worker plus system runtime files readable. Network is disabled;
scene/store/Judge objects never cross IPC. A narrow JSON request protocol permits
observe, move_tcp and move_gripper with bounded calls, wall time and messages.
The parent owns physics, measurement and persistence; the Judge executes later
outside expert access using exact Episode and contract refs.

Unavailable bwrap fails closed. PRoot and none are not fallbacks. Local software
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
`run(actions)` or a named class with `run(self, actions)` and a restricted math
subset. It starts without site initialization, with a memory limit, core dumps
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
allowlist. Deployments must qualify the outer sandbox, nested bwrap, native Isaac
resources and private store boundaries before admitting a real run.

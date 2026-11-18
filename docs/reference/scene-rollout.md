# Full-scene execution and measurement

B consumes the original request-derived task specification and A's immutable
SceneGraph/AssetRecords. This is a simulation-native fork: no real calibration
input, video-model observations, photographic or real-ready claim.

## Production entry

`datafarm.entry.scene_rollout.record_scene_episode(...)` is a trusted Python
composition API, not a task registry or a script copied from tools. Supply an
ArtifactStore, task/scene/runtime/expert refs, A's deployment-local prepared
bundle and explicit asset roots (`scene_runtime.asset_roots(prepared)` reads them from
`prepared.json`; see [scene-assembly](scene-assembly.md#加载已组装的场景)), an already initialized scene/simulator/RobotAPI,
an owned reset callback, settle count, control profile, attempt ID, byte budget
and the expert function from its exact pinned source file.

`datafarm.adapters.sim.scene_runtime` loads/validates A artifacts and verifies
prepared records, bundle member hashes, collision wrappers, robot URDF/USD and
source dependencies. It rejects name collisions in A's lossy runtime mapping
without changing A's IDs. `scene_config` reuses A's `build_scene_cfg` and returns
configuration only. The caller remains responsible for launcher/runtime resources;
this entry does not download assets or create an application at import time.

The reset callback must restore the complete world, not only robot joints.
A's `apply_initial_state` is reused for fixture articulations. Explicit settle
cycles occur before episode step zero. Reset/settle is an execution fact, not
verified replay restoration. Changed randomization requires a source-bound scene
revision; nonempty unbound randomization is refused. Required unavailable sensors
must be rejected by the task/Judge capability gate, not filled with zero readings.

## One physics owner

The initialized control host owns the RobotAPI lock. Every cycle stages targets,
writes the whole scene, reads back **all robot actuator position targets**, advances
one physics period, updates the whole scene once, then reads the complete state.
The robot-only host remains explicit when full-scene ports are not supplied.
No second robot update/physics loop is run alongside the full scene.

Recorded requested joint targets, raw IK/reference metadata, applied actuator
readback and measured achieved state have different fields. Motion completion
requires consecutive measured samples within the control tolerance. A control
profile is explicit (dt, rates, limits, tolerances and call/step/wall budgets),
never inferred from a test task. TCP is native Newton differential IK, not an
obstacle-avoiding planner; collision-checked motion is unavailable.

The same owned host supplies `move_gripper` for `gripper-motion-request/1`:
`width_m`, `speed_m_s`, `tolerance_m`, `settle_steps`, `max_control_steps` are all
required. Limits are bounded by the source, actual drive and existing profile;
settling uses measured finger width. Requested width does not prove a grasp.

A-defined node/explicit frame poses are resolved from current measured state;
unknown frames fail closed. No world-frame override or layout-based fake state.

## Measurements and failures

Snapshots cover every SceneGraph node, root position/quaternion and linear/angular
velocity, source-typed joint q/dq, body link poses, robot TCP/gripper and contacts.
Coordinates are world/XYZW; distances, angles and time use m/rad/s. Readings are not
rounded into summaries before recording. Missing state fails capture.

A contact rows supply endpoint IDs, USD body labels and signed distance, not force
or normal. Labels use `usd:` to distinguish USD prim identifiers from host paths.
Contacts are cycle-end snapshots, not substep-complete. The default query
`isaac_scene.contacts_with_coverage` compares the solver's raw contact count `nacon`
with buffer capacity `naconmax`: `complete` when `nacon <= naconmax` (every detected
contact of that snapshot is reported, so an empty list is a real "no contact"),
`truncated` otherwise. An injected legacy query returning only a list keeps
coverage `unknown`. `complete` says nothing about transients between snapshots,
force or normal. Missing/failed query is `unavailable` with `rows=null`, never `[]`.

Step/refresh/logger exceptions mark the host unhealthy; unknown physics outcomes
are not retried. The last valid state and any applied unmatched input remain in
the partial episode. Invalid finite/nonfinite measurement snapshots are retained
as separate raw failure evidence when available, never repaired into valid states.
Failing, rejected, timed-out and aborted primitive results cannot be hidden by an
expert returning normally. A gripper `blocked` result (`GRIPPER_BLOCKED`) is not an
execution failure; the Judge decides from width/contact evidence whether it is a grasp.
Expert-caused exceptions are recorded with fixed reasons (`EXPERT_EXCEPTION`,
`EXPERT_AUDIT_REJECTED`, `EXPERT_PROTOCOL_ERROR`, `INVALID_ARGUMENT`, `UNKNOWN_FRAME`);
other exceptions are `RUNTIME_ERROR`. Task judgement stays unverified.

## Boundaries

This trusted in-process API is **not** a malicious-code sandbox. Python private
attributes do not isolate untrusted experts; use the existing broker/OS boundary.
PRoot is an explicit opt-in expert execution path through `scene_job`; it grants
no Judge secrecy/readonly guarantee or automatic Codex sandbox bypass. The
trusted host keeps scene/Judge out of IPC, verifies the live freeze before and
after execution, and records `security_boundary=false`. See [scene harness](scene-harness.md).

The recorder verifies an explicitly supplied source-bound Judge /1 contract
before execution, keeps its ref in the Episode, and leaves judgement unverified.
The separate Judge entry verifies the live frozen bundle and complete measured
Episode projection before producing a receipt; it never rewrites the trace. No generic stage-graph executor,
rendering, action replay verification, dataset export or training is added here.
## Local validation boundary

The public production functions were exercised with local software fixtures and
the installed native Newton controller using synthetic articulation data. This
is not live physics qualification. Full scene assets, contact correctness,
physical task success, replay and rendering still require separate acceptance.

## State and runtime identity

Measured states use `datafarm.ir.episode_state`; observations use `robot.tcp`.
Shared validator and sandbox package source hashes enter the recording source pin.
Request-format bridging remains independent work and is not normalized here.
A render-tool adaptation remains outstanding and is not evidence of action replay.

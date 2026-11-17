# B-line interface design: Isaac Lab 3.0 control, judging and Episode

Version: `b-control/0.1.0-draft`. Date: 2026-11-14 (Asia/Shanghai). Status: design draft ready for review; the A/B handoff fields keep their existing definitions, with notes on the current implementation added.

Implementation progress: URDF structure/kinematic chain, model TCP and gripper geometric mapping, profile overrides, Isaac Lab instance binding, state reading and position-target staging are implemented; see [auto-binding](robot-auto-binding.md). An explicitly bound native differential IK host supplies bounded TCP tracking (section 4.4); collision-aware planning and scene judging remain unconnected; other modules in this design are not considered accepted just because this document was updated.

This document implements the route the user decided: use `translate-view` as the baseline, fix the simulation framework to Isaac Lab 3.0 and reuse the existing Newton + MJWarp configuration; cancel the separate backend-selection probe, the 10-trial pick-and-place gate and the SAPIEN fallback path. The design adds no paid calls, remote runs or training jobs.

## 1. Scope and responsibilities

Phase one supports task execution with one AIRBOT + G2 in a single environment; identifiers and fields keep room for a dual arm. Task logic lives in a task adapter inside the run directory; there is no fixed task registry. The single-arm MVP has two physical cameras, `camera.head` and `camera.left_wrist` (no right wrist camera), plus the render-only virtual view `third_person`; they are declared by the scene and RenderSpec (sources in [SceneGraph](../ir/scene-graph.md)). While cameras are unimplemented, state collection is allowed but multi-view data cannot be signed off.

| Module | Owner | Input | Output and boundary |
|---|---|---|---|
| Assets and scene | A | AssetRecord, SceneGraph | Source-bound assets, entities, joints, frames, collision and camera definitions |
| Scene binding | B, integrated with A | Scene reference, runtime profile | Entity and joint name mapping, runtime capability declaration; fails explicitly on mismatch at start |
| Robot API | B | Restricted action calls | Measured state, action results, control inputs; the model cannot access raw simulation objects |
| TaskContract / Judge | B, independently reviewed | Task requirements, measured state and events | Frozen criteria, results, negative controls; the model cannot change criteria |
| CaP adapter | B | Public task description, public feedback | Expert candidates and primitive calls; existing broker boundary kept |
| Episode recorder | B | State, actions and contacts of one run | Full trajectory, frame mapping, termination result and source digests |
| State playback / rendering | A | Episode, RenderSpec | Camera images, material/lighting variants; reads recorded motion only |
| Action replay / export | B | Initial state, actual control inputs | Replay error report, HDF5; admission together with rendering results |

The robot, simulation stepping and recorder are owned serially by one host. The Judge runs in the host or a separate evaluation process; independent review reads frozen evidence. Interface isolation by itself is not an operating-system security boundary; the existing POSIX broker limitation on Windows must be handled during implementation.

## 2. Common data conventions

### 2.1 Identifiers, versions and provenance

- Logical identifiers are stable strings, for example `robot.airbot`, `object.plate`, `joint.drawer.slide`, `camera.head`. B does not depend on USD hierarchy or post-import array order.
- Every new kind of contract has an explicit `schema_version`; incompatible versions are rejected, and version upgrades need explicit migration.
- Persisted assets, scenes, control configurations, Judges, expert source and Episodes use the existing `ArtifactRef {kind, sha256, media_type}`. At runtime the deployment resolver resolves local file locations.
- USD files and their full dependency set, joint mappings, TCP calibration, solver and drive parameters are all part of the runtime binding. Recording only the hash of the entry USD is not enough.
- `attempt_id` identifies one attempt and `episode_id` one actual trajectory; a retry after failure gets new IDs linked by `retry_of`. Failure records are never overwritten.
- `created_at` uses UTC RFC3339, for example `2026-11-14T01:00:00Z`; it plays no part in simulation synchronization. User reports show Asia/Shanghai time.

### 2.2 Units and poses

| Data | External convention |
|---|---|
| Length / angle / time / mass | m / rad / s / kg |
| Force / torque / impulse | N / N·m / N·s, with the dimension explicit in the field name |
| Coordinates | Right-handed; the scene is Z-up; unit conversion is done by the import adapter |
| Pose | `Pose {frame_id, position_m:[x,y,z], quaternion_xyzw:[x,y,z,w]}` |
| Transform semantics | A Pose is the transform from the target entity's local frame to `frame_id`, i.e. `T_frame_entity` |
| Quaternion | Unit quaternion in XYZW order; rotation comparison treats q and -q as equal |
| TCP | Specifies `tcp_frame_id`, its link and the fixed `T_link_tcp`; the link origin must not be used as an ad-hoc TCP |

Legacy external WXYZ is converted at the boundary. Native USD quaternions, MJCF and other formats are interpreted through their own APIs; components are never swapped blindly in files. Cameras declare their optical frame convention and its transform to the scene frame separately.

### 2.3 Time and stepping authority

By default `robot_profiles.py` is reused: control at 120 Hz, one Newton simulation step per control period, 4 internal substeps. Distinguish clearly:

- `control_dt_s = 1/120`; `sim_step_dt_s = 1/120`; `solver_substeps = 4`.
- The effective substep interval is `1/480` s, but this design does not promise that state or contacts can be read at each internal substep.
- State `s[k]` is sampled at the end of control period k; `s[0]` is the initial state collected after reset and settle, and the collection clock starts at zero.
- `a[k]` acts over the half-open interval `[k, k+1)` and produces `s[k+1]`. N actions must correspond to N+1 states.
- Every stream records `episode_id` and `control_step`; when needed it also records a simulation call counter. Time is derived from integer steps and dt to avoid accumulated error.
- `observe()`, Judge reads, logging and camera export do not advance physics. Only the host `step()` advances it; high-level primitives advance it indirectly through the host execution loop.

## 3. A → B: SceneBinding handoff

The SceneGraph is the scene authority; SceneBinding is B's name mapping and runtime adaptation for the loaded scene and must not change asset physics parameters on its own.

| Field | Type | Requirement |
|---|---|---|
| `scene_ref` / `asset_bundle_ref` | ArtifactRef | Scene and full dependency set |
| `world_frame_id` / `units` | string / object | World frame and unit declaration |
| `entities` | entity list | ID, type, resource reference, initial Pose; articulated objects also carry initial joint values |
| `robots` | robot list | Base, named joints and types, limits, controllable set, end effector and gripper calibration |
| `frames` | frame list | ID, parent ID, fixed transform; no cycles or dangling parents |
| `cameras` | camera list | ID, extrinsics/mount frame, intrinsics, resolution, distortion, optical axis and sampling plan |
| `collision_groups` | object | Collision relations and sets of entities allowed/forbidden to touch |
| `physics_parameter_ledger` | ArtifactRef | Source of mass, inertia, friction etc.: measured/fitted/assumed/unknown |
| `runtime_profile_ref` | ArtifactRef | Immutable snapshot of controller, solver, dt, drive and contact configuration |

The deployment may hold `entity_id → prim path` and `joint_name → index` mappings; absolute machine paths do not enter the portable IR. On load it checks that entities exist, joint names are unique, types/limits, TCP, mimic master/follower and collision configuration.

The randomization axes of `reset(seed)` come from the TaskContract; all actual samples are recorded. The same seed does not guarantee bitwise GPU determinism, and a scene cannot be rebuilt from the seed alone across environment versions.

### 3.1 Providing collision information through existing interfaces

| Information | Authoritative source | How B handles it |
|---|---|---|
| Links, movable joints, kinematic chain, origin/axis, mimic | URDF | Parsed and bound to runtime by name; never replaced by fixed joint numbers or hand-written link lengths |
| Robot position/velocity/effort hard limits | URDF; must be checked at runtime | Scene limits cannot widen the robot's allowed range; the current target entry enforces position limits, while trajectory-level velocity/effort constraints are still left to the executor |
| Robot self-collision geometry | URDF `<collision>` and dependent meshes | Passed to the collision checker; the URDF has no full self-collision disable matrix, extra exceptions need a source reference, and adjacent or other link pairs are not ignored by default |
| Obstacles and object articulation limits | A's existing scene_ref / asset_bundle_ref, entities / frames | Assets provide collision shapes and scale, the scene provides initial poses/articulation states, and execution reads the latest state from the same simulation |
| Collision relations and allowed contact | A's existing collision_groups, the task's existing constraints | B checks by entity identity and task phase; not every contact counts as failure |
| Robot geometric unreachability / unreachability in the current scene | Computed by B's IK / planning and collision checking | The former uses the URDF kinematic chain/limits; the latter adds A's scene state/constraints; a single local IK non-convergence does not prove global unreachability |

These existing fields can carry the collision constraints caused by A-line objects; no new limit payload, handoff field or interface version is needed. B parses the robot kinematic chain, joint hard limits and model TCP/gripper definitions from the source-bound URDF/meshes, so A does not need to copy them by hand.

An adequate interface does not mean the actual assets are complete: on load B must confirm that references resolve, collision geometry exists and is enabled, units/scale are consistent, and object poses and current articulation states are correct. When only a visual mesh exists without a collision shape, the asset content is fixed under the existing asset delivery agreement; collision_groups or bounding boxes are not treated as full geometry. Limits caused by scene objects depend on the whole robot pose and object state and are judged dynamically by B's collision checking/planning; A is not required to pre-convert them into separate joint intervals. That checking/planning is not connected yet, so scene obstacle-avoidance acceptance is not claimed.
## 4. Robot API

### 4.1 Host interface: not exposed to expert programs

The following are type-signature designs, not an implemented Isaac Lab package API.

```python
class RobotRuntime:
    def capabilities(self) -> Capabilities: ...
    def reset(self, reset_request: ResetRequest) -> ResetResult: ...
    def observe(self) -> Observation: ...
    def step(self, command: ControlCommand) -> StepResult: ...
    def stop(self, reason: str) -> StopResult: ...
    def close(self) -> None: ...
```

| Method | Semantics |
|---|---|
| `capabilities` | Declares control modes, controllable joints, TCP, gripper range, contact quantities and sampling semantics, camera availability; missing capabilities are never filled with fake data |
| `reset` | The host binds the seed, actual initial values and settle configuration; on success returns s[0]; failure is an initialization error and the attempt record is kept |
| `observe` | Returns a read-only measured snapshot of the current step; does not advance simulation or change actions or criteria |
| `step` | Validates one period's targets, applies rate limits/controller, records the values actually applied, advances one control period, and returns the resulting state and contacts |
| `stop` | Stops accepting primitives; if the backend supports it, enters the declared controlled-stop logic; cannot promise an unconditional instant stop |
| `close` | Closes owned instances and writers; never deletes trajectories |

`ControlCommand` initially supports only `joint_position_target`; named joints and target values have equal length. Arm joints and the gripper master joint are in the controllable set; mimic follower joints are observed only and never receive independent targets. `requested`, `applied` and the clipping reason are recorded; a clipped target must not be reported as reached exactly.

The default control mode is fixed to `position`: B sends joint position targets, and the stiffness/damping/effort of the underlying position drives come from the runtime profile and are saved. The default position mode does not authorize B to guess gains; without an effective position drive the corresponding action capability is disabled. velocity/effort modes are currently rejected explicitly.

`Observation` is the robot segment of one Episode IR state (`state_trace[k].robot`, [docs/ir/episode.md](../ir/episode.md#state_trace)): `joint_names`, measured `q`/`dq`, optional world-frame `tcp` Pose and optional `gripper{width_m, width_range_m, state_source, verification, within_defined_range}`. Task entity pose/velocity, articulated-fixture joints and contacts come from the full-scene snapshot in the same state (`entities`, `contacts`). Non-finite readings fail the read; they are never written as a validity flag. Torque fields distinguish drive targets, actual simulated values and estimates; when actual values are unavailable they are marked unavailable.

### 4.2 Expert interface: restricted, synchronous, bounded steps

```python
class RobotPrimitives:
    def observe(self) -> PublicObservation: ...
    def move_tcp(self, robot_id, target_pose,
                 motion_limits, completion) -> PrimitiveResult: ...
    def set_gripper(self, robot_id, width_m,
                    speed_m_s, completion) -> PrimitiveResult: ...
    def wait(self, control_steps) -> PrimitiveResult: ...
```

- Primitives return synchronously: only after the motion completes, fails or hits the step limit; "request accepted" does not mean the action succeeded. Asynchronous handles are not mixed in the first version.
- `completion` specifies `max_control_steps`, position/orientation or opening error, and `settle_steps` that must be satisfied consecutively. Thresholds are bounded by the control configuration; the policy may only choose values within the allowed range.
- The target frame of `move_tcp` must be registered; the host transforms the target into the world frame at the start of the action and freezes it. Dynamic target tracking needs an explicit mode added later.
- `motion_limits` contains TCP linear/angular speed and named joint speed limits. Trajectory planning, IK, interpolation and speed monitoring are done by the adapter.
- `set_gripper` takes a two-finger opening in metres; the mapping is preferably derived from model mimic and collision geometry, recording `width_semantics=collision_envelope_gap`, the mesh source and `verification=model_geometry_only`. It is not automatically equivalent to a calibration of the real gripping surfaces; a sourced explicit profile may override it. Master-joint travel alone must not be treated as the opening.
- `wait` keeps the declared control mode and last target and records dynamics as usual; it does not skip physical time.
- All primitives have call-count and total-step budgets. Reset, hidden seeds, the Judge, raw files, simulation objects and arbitrary code imports are not exposed to the expert.
- Grasping an object can keep the gripper from reaching the requested opening: an ordinary opening primitive reports `blocked`, and whether this is a valid grasp is decided by the task Judge from evidence; there is no generic "closed gripper means grasped" rule.

### 4.3 Return values and failures

`PrimitiveResult` contains `call_id`, `status`, `start_step`, `end_step`, `requested`, `resolved_target`, `before`, `after`, `error`. Status values: `completed | rejected | failed | timeout | aborted | blocked`. When rejected before advancing, start=end; a failure mid-way keeps all states already advanced.

| Error code | Category | Handling |
|---|---|---|
| `INVALID_ARGUMENT` / `UNKNOWN_FRAME` | Expert call error | Return a public diagnostic, do not advance |
| `LOCAL_IK_NONCONVERGENCE` | Local control timeout | Preserve partial trajectory; this does not prove global unreachability |
| `JOINT_LIMIT` / `SPEED_LIMIT` / `FORBIDDEN_CONTACT` | Execution-constraint violation | Terminate the current action; the frozen policy decides whether the episode ends |
| `MOTION_TIMEOUT` | Control failure | Keep the partial trajectory; do not replay the action automatically |
| `GRIPPER_BLOCKED` | Motion blocked | Return the opening and available contact evidence; the Judge decides the task relation |
| `NONFINITE_STATE` / `SIMULATION_ERROR` / `ASSET_MISMATCH` | Runtime error | Terminate and mark as error; not reported as an ordinary task failure |
| `CAPABILITY_UNAVAILABLE` | Configuration or capability missing | Reject explicitly; unknown contact is never written as zero |

Host recovery is recorded separately with `phase=recovery`. Runtime fallback recovery is kept apart from deliberate policy retreats; fallback recovery must not pass as policy success.

### 4.4 Implemented Isaac Lab native TCP control

The factory `datafarm.entry.robot_control.bind_isaac_tcp_control(api, simulation, control_profile, ...)`
binds an **already initialized** `IsaacRobotAPI` and simulation. It does not create/reset a scene, change
actuator gains, launch another process, or import a simulator at module import time. The owner must grant
exclusive stepping of that world; concurrent API target calls are rejected, not silently queued.

The new layers are `simulation/tcp_control.py` (standard-library contracts and bounded motion service),
`adapters/sim/isaac_ik.py` (native `NewtonDifferentialIKController`), and
`adapters/sim/isaac_control.py` (explicit actuator application, physics step and state refresh).
The existing structural `robot-binding/2` retains `move_tcp=false`; the API's runtime capability snapshot
adds `move_tcp=true` only after controller/host validation. It always reports
`collision_checked_motion=false`. A request requiring collision-checked motion is rejected.

`ControlProfile` / `tcp-control-profile/1` requires these explicit fields:

- `dt_s`, `bandwidth_s_inv`, `damping` for native damped least squares; `dt_s` must equal the owned physics step.
- `max_linear_speed_m_s`, `max_angular_speed_rad_s`, and `joint_speed_limits` naming **exactly** the selected chain.
- `position_tolerance_m`, `orientation_tolerance_rad`, `settle_steps`, `max_control_steps`,
  `max_total_control_steps`, `max_calls`, and `max_wall_time_s`.
- Optional `reference_speed_fraction` in `(0, 1]` reserves reference-speed headroom for physical tracking;
  its default is 1. Measured speed checks still enforce the full declared limit, never a looser threshold.
- Optional `joint_limit_avoidance_gain` and `joint_limit_margin`; avoidance is a soft native objective,
  not a substitute for independent hard-bound checking. Finite authored bounds are required to enable it.

Call `api.move_tcp(request)` with `schema_version="tcp-motion-request/1"`, `target_pose` in the common
Pose convention, and `mode="position"` or `"pose"`. Optional speeds/budgets cannot exceed the host profile;
error tolerances cannot be loosened and the stable-window requirement cannot be shortened. A registered
`frame_resolvers` callback returns that frame's world Pose once at action start. Unknown frames reject;
the resolved world target stays fixed for the call. Position-only completion makes no orientation claim.

For every period, the service reads measured state, bounds the Cartesian reference increment, calls the
native controller, validates its complete named output, rate-limits the joint reference, stages actuator
targets, writes them, advances one physics period and refreshes measured state. Joint reference velocity
is bounded by the minimum of source URDF, runtime drive and requested limits. Measured joint velocity
and per-period TCP displacement/rotation are checked separately; commanded rates are not measured rates.
Hard-limit violations and non-finite outputs are rejected, not silently clamped into valid solver results.

The Jacobian uses `body_link_jacobian_w`, verified fixed-root row and base-DOF column indexing, plus
`J_linear(TCP) = J_linear(link) + J_angular x R_world_link p_link_TCP`. Pose and Jacobian share world
coordinates and XYZW quaternions. Only independent position-driven joints on the selected source chain
are controlled; other arms, gripper-only branches and base DOFs are excluded. Moving mimic joints on the
selected chain reject binding. This remains a single-environment serial-chain interface, not simultaneous
dual-arm/whole-body or closed-chain control. No state teleport is used to reach a TCP goal.

`tcp-motion-result/1` reports `completed | rejected | failed | timeout | aborted | error`, public
`reason_code`, call ID, control-binding hash, local host start/end steps, attempted step count,
resolved target, measured before/after and settled count. `completed` requires consecutive measured
samples inside tolerance. A step exception can leave `step_outcome_unknown=true`: no automatic retry
or fabricated after-state is allowed; the host requires requalification. Native solve/refresh/logging
errors use the error channel, not task failure. Cancellation/budget exhaustion preserve partial events.
A failure hold only **stages** current measured positions without an extra physical step; it is not
an instantaneous or verified physical stop. Synchronous native compute/physics calls cannot be
preempted mid-call; wall-time checks stop additional calls once control returns.

An optional trusted `event_sink` receives motion start/end and each requested/applied/measured period.
Its errors fail closed. These events are integration inputs, **not** a claim that Episode recording,
action replay/export, sandbox broker binding, or task judging are complete. No CaP whitelist is widened,
and no Judge/hidden seed is passed to the controller. Runtime binding records version/config/source
hashes without host paths or credentials. Static IK existence or a passing fixture does not qualify
all robot models/scenes, obstacle avoidance or task success.

## 5. TaskContract and Judge

Generated, independently reviewed and frozen Judges plug into the new adapter/1 contract; see [Judge generation](judge-generation.md). Runtime availability alone does not certify a task.

### 5.1 Contract fields

| Field | Meaning |
|---|---|
| `task_id`, `task_version`, `scene_ref` | Bind the task and scene |
| `goal_predicates`, `stage_graph` | Final relations and required action stages; stage order applies to actions the task actually requires |
| `constraints` | Allowed contacts, joint/speed constraints, object drop and orientation bounds |
| `thresholds` | Value, unit, source and frozen version; undecided values are not filled with fabricated measurements |
| `required_capabilities` | Required state/contact evidence; when missing, task evaluation refuses to start |
| `initialization`, `randomization` | Initial ranges, settle, failure handling; actual samples are written into the Episode |
| `termination` | Total step budget, success hold window and early-failure conditions |
| `judge_ref`, `negative_controls_ref` | Judge source/dependencies and independent review material |
| `public_task` | Goal, API and public constraints that may be given to CaP |

Development, candidate selection, skill re-verification, hidden evaluation and production collection use non-overlapping seed partitions. The hidden partition is held by the host and unreadable by the expert; public feedback does not leak hidden initial values, seeds or internal judge implementation.

### 5.2 Judging interface and result

```python
class Judge:
    def initialize(self, frozen_contract, initial_state) -> None: ...
    def update(self, transition) -> JudgeStatus: ...
    def finalize(self, termination_reason) -> JudgeResult: ...
```

The Judge reads state and events only and cannot control the robot. `JudgeResult` contains `outcome=success|failure|error|aborted`, `success`, `terminated`, `truncated`, `reason_code`, `metrics`, `predicate_results`, the evidence step range and `judge_ref`. For internal error/aborted, success is null; when bridged to the existing CaP boolean result, error/aborted must go through the exception/runtime-error channel and must not be forced to false.

Normal success or task failure is terminated; reaching the total step budget is truncated; infrastructure errors and user cancellation are classified separately. Timed-out tasks count in the task-evaluation denominator; initialization/infrastructure errors are listed separately and kept; reports show planned, valid evaluated, error and cancelled counts, and unfavourable attempts must not be deleted.

The Judge is frozen before expert search; any change to criteria, thresholds or scene physics configuration creates a new task version. Hidden evaluation runs only after the program is frozen, and its results must not flow back into repair. Independent review recomputes the key metrics separately.

### 5.3 Criteria structure of the plate task

The following is a criteria structure, not measured thresholds or a frozen task. Pick-and-place into a drawer and pushing a plate into a drawer use different task_ids.

| Stage | Criterion | Evidence |
|---|---|---|
| Lift | The plate actually leaves the support surface and reaches the task height | Lowest point of the plate geometry, world pose, support relation |
| Hold | No drop within the hold window; translation/rotation drift within thresholds | Continuous state; two-finger evidence when contact capability is available |
| Place | The whole plate geometry lies within the valid space of the target container | Transformed geometric bound/volume test, not just the centre point |
| Release and settle | The fingers release, and the plate is supported by the target surface and stable | Opening, relative pose, contacts and a sustained velocity window |
| Integrity | No teleporting, no runtime violation; actual return home when the task requires it | Full trajectory, events and final TCP |

Prepare at least negative controls such as "centre inside the drawer but rim out of bounds", "still gripped", "briefly enters then slides out", "placed by writing the pose directly" and "only pushed but claims lifted". When physical observations are insufficient, the result is unknown/error; predicates never pass by default.
## 6. Episode and recording conventions

### 6.1 Compatibility with the existing IR

Reuse `ArtifactDocument(kind=EPISODE)` from `kernel/artifacts.py`; its payload keeps the existing required keys: `setup`, `actions`, `state_trace`, `contacts`, `events`, `frame_map`, `replay`. New contracts are carried through versioned nested records and references; the meaning of the existing schema is not rewritten. The field structure below is a design requirement; a full validator is not implemented yet.

| payload item | Data |
|---|---|
| `setup` | Scene/robot/profile/contract/Judge/expert references, engine version, seed partition, actual initial state and randomization, dt, joint order, capability snapshot |
| `actions` | Requested and actually applied low-level control inputs, control mode, interval of effect, primitive call_id |
| `state_trace` | N+1 states, covering the robot, all task entities and the dynamic entities needed for rendering |
| `contacts` | Separate contact stream and availability; sampling semantics, both entities, dimensions and aggregation window |
| `events` | Primitive start/end, stage changes, errors, Judge, stop and recovery events |
| `frame_map` | control_step, camera_id, view_variant_id, intrinsic/extrinsic references and image references per frame; explicitly empty when there is no camera |
| `replay` | Initial conditions, action-replay configuration, state-playback configuration and error-report references; marked unverified when not run |

The task-completion flag, content-binding digests and write integrity are produced after recorder finalize. An interrupted file is marked partial and keeps the last complete step; an unfinished file never gets a complete-Episode sign-off.

### 6.2 Actions and states

Low-level actions record `requested_target` and `applied_target` together with the controller's interpolation/clipping result; storing only the `move_tcp` parameters is not enough. If torque control is used later, a new control mode must be added that records that mode's actual input.

State fields are defined once in [docs/ir/episode.md](../ir/episode.md#state_trace) and validated by `datafarm.ir.episode_state`: robot `joint_names`/`q`/`dq`, `tcp`, `gripper`; per-node root `position_m`/`quaternion_xyzw`/`linear_velocity_m_s`/`angular_velocity_rad_s`, typed `joints` (drawer etc.) and `links`; and the cycle-end `contacts` sample. Position units depend on joint type, revolute=rad and prismatic=m; a mixed joint array must not be labelled entirely as rad.

Contact fields distinguish `available`, `unavailable` and `not_requested`. An empty contact record means "zero contacts detected" only when sensing/querying is valid. When Newton internal substeps cannot be read, it is stated explicitly whether this is an end-of-period snapshot or a backend-provided per-period aggregate; contact completeness per substep is not claimed.

### 6.3 Cameras and A-line playback

Cameras are either fixed or mounted on a link. The latter record the mount transform and derive world extrinsics from per-step rigid-body state; a free follow view is not named a real wrist camera. All cameras share a sampling plan defined by integer control_step.

One physical episode can yield several material/lighting variants and still counts as one physical trajectory. RenderSpec specifies the physical views (`head`, `left_wrist` in the single-arm MVP), any virtual views, and the material and lighting variants; frame_map distinguishes them by camera_id and variant_id. Variants must not change geometry, collision, actions or contacts.

Rendering may skip frames but must keep the original state sample index. Linear interpolation must not be assumed to reproduce contact events; when interpolation is needed, the method and its limits are recorded explicitly. Invalid frames keep a missing/error status and must not copy the previous frame to fake synchronization.

## 7. Two kinds of replay and acceptance

| Type | Input and operation | Purpose | Acceptance boundary |
|---|---|---|---|
| State playback | Load the same assets and write recorded rigid-body/articulation states; physics stepping disabled or isolated | A-line camera rendering | Verifies entity completeness, time synchronization, poses and image content; does not prove dynamics reproduction |
| Action replay | Restore full initial conditions, fixed versions and configuration, and re-apply the applied control inputs | B-line dynamics consistency | Align every control step and report position/rotation/joint/event errors |

The project's "≤1 cm" is defined as: for the task rigid bodies declared by the TaskContract (including the plate and the moving parts of the target container), compute the position error of the same entity frame origin over all recorded steps; the maximum must be ≤0.01 m. The report also includes the maximum TCP position error; which entities take part in sign-off is stated in the frozen contract. Rotation, joint and contact timing are reported separately with their own thresholds; 1 cm does not cover those dimensions.

Missing steps, uncompared entities, different assets or comparing only the last frame cannot pass. GPU numerics are not bitwise reproducible; when the limit is exceeded, keep the original and replayed trajectories, versions and differences, and never modify the original actions/states to pass the gate.

## 8. HDF5 export conventions

One logical group per episode; it may span several files, but the manifest lists members and digests explicitly.

```text
/meta                           version, provenance, named joints, dt, control mode, Judge result
/state/control_step             [N+1] int64
/state/robots/<id>/q             [N+1,J] float64
/state/robots/<id>/dq            [N+1,J] float64
/state/entities/<id>/pose_xyzw   [N+1,7] float64, order x,y,z,qx,qy,qz,qw
/action/interval_steps          [N,2] int64, each row [k,k+1]
/action/robots/<id>/requested    [N,U] float64
/action/robots/<id>/applied      [N,U] float64
/action/call_id                 [N] UTF-8
/contacts/records               variable-length UTF-8 JSON, fields per this contract; keeps time and availability
/events/records                 variable-length UTF-8 JSON
/frames/<camera>/<variant>/...  raw images or ArtifactRef, with control_step mapping
```

J is the number of observed joints and U the number of actual control channels; joint_names, joint_types and control_names are stored separately, and usually J≠U. Raw streams and dtypes are kept; resampled results never replace raw evidence. Failure and recovery phases can still be archived, but training export selects them through an explicit phase mask without changing the time semantics of actions.

## 9. A complete small Episode example

Below is a **hand-written protocol example** of a two-step stationary hold. It fully expresses the N=2, 3-state relation; it is not a robot measurement or a pick-and-place success, and it is not an ArtifactDocument that can be submitted directly to the existing IR validator. Actual persistence must store each stream in the ArtifactStore and then assemble references and real digests. The example omits a real asset bundle and only aligns fields and time.

```json
{
  "schema_version": "b-control-example/0.1.0",
  "synthetic_example": true,
  "episode_id": "example.hold.001",
  "setup": {
    "framework": "isaaclab",
    "release": "3.0.0",
    "physics_backend": "newton_mjwarp",
    "control_dt_s": 0.008333333333333333,
    "solver_substeps": 4,
    "phase": "debug",
    "seed": 0,
    "joint_names": ["left_joint1", "left_g2_joint"],
    "joint_types": ["revolute", "prismatic"],
    "control_names": ["left_joint1", "left_g2_joint"],
    "initialization": "settle_completed",
    "contact_capability": "unavailable"
  },
  "actions": [
    {"call_id": "call.001", "interval_steps": [0, 1], "mode": "joint_position_target", "requested": [0.0, 0.02], "applied": [0.0, 0.02]},
    {"call_id": "call.001", "interval_steps": [1, 2], "mode": "joint_position_target", "requested": [0.0, 0.02], "applied": [0.0, 0.02]}
  ],
  "state_trace": [
    {"control_step": 0, "q": [0.0, 0.02], "dq": [0.0, 0.0], "plate_pose": {"frame_id": "world", "position_m": [0.5, 0.0, 0.75], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}},
    {"control_step": 1, "q": [0.0, 0.02], "dq": [0.0, 0.0], "plate_pose": {"frame_id": "world", "position_m": [0.5, 0.0, 0.75], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}},
    {"control_step": 2, "q": [0.0, 0.02], "dq": [0.0, 0.0], "plate_pose": {"frame_id": "world", "position_m": [0.5, 0.0, 0.75], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}}
  ],
  "contacts": {"availability": "unavailable", "records": []},
  "events": [
    {"control_step": 0, "event": "primitive_started", "call_id": "call.001", "api": "wait"},
    {"control_step": 2, "event": "primitive_finished", "call_id": "call.001", "status": "completed"}
  ],
  "frame_map": [],
  "replay": {"state_playback": "unverified", "action_replay": "unverified"},
  "result": {"outcome": "failure", "success": false, "terminated": true, "truncated": false, "reason_code": "GOAL_NOT_REACHED"}
}
```

### Action failure return example

```json
{
  "call_id": "call.002",
  "status": "rejected",
  "start_step": 2,
  "end_step": 2,
  "requested": {"api": "move_tcp", "robot_id": "robot.airbot", "target_pose": {"frame_id": "unknown.frame", "position_m": [0.5, 0.0, 0.8], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}},
  "resolved_target": null,
  "before": {"control_step": 2},
  "after": {"control_step": 2},
  "error": {"code": "UNKNOWN_FRAME", "category": "expert_call", "message": "Target frame is not registered", "retryable": true}
}
```

`retryable=true` only means the expert may fix the arguments within budget; it does not mean the runtime repeats the action automatically. The rejection is still written to the event stream but adds no state or control input.

## 10. Connection to the current code and implementation order

| Current code | Reuse | Still to implement |
|---|---|---|
| `kernel/artifacts.py`, `kernel/schema.py` | ArtifactRef, Episode envelope, existing required payload items | Validation of new nested contracts, reference assembly and schema version policy |
| `cap/search.py` | Search, development/selection/skill re-verification partitions, budgets | Public description for this task, primitive binding and state feedback |
| `cap/xh.py` | Existing rollout/inspect/submit broker client | Isaac Lab adapter, public primitive trace, Windows execution boundary |
| Workspace `IsaacLab-3.0/robot_profiles.py` | `make_simulation_cfg`, robot configuration and accepted gains | Scene binding, TCP control, contact capability, recorder |
| Workspace `DataFarm-MVP` | Action intervals and measured records, reference design for independent review | Not used directly as the Lab 3.0 adapter or as real LLM/pick-and-place acceptance |

The public primitive trace keeps `event="primitive"`, a unique integer `event_id`, `api`, `before`, `after` and `result/error`, matching the reading convention of the existing XHEngine. The new call_id links it to the low-level action stream. The logical episode_id of the IR and the broker's A-xxxx attempt_id are linked explicitly and never mixed.

The suggested reviewable delivery order is as follows, without restoring the cancelled probe stage:

1. A and B confirm SceneBinding, frames/clock, TCP, available contact quantities and the identities of the two physical cameras; decide the concrete task and the basis of its thresholds.
2. Implement data structures, validators and `RobotRuntime`, reusing the profile; verify joint-name mapping and the time and recording semantics of one step.
3. Implement restricted primitives, the task Judge and negative controls; freeze the task contract and connect the existing CaP broker.
4. Collect one complete Episode and finish state playback and multi-view synchronization with the A line.
5. Complete task evaluation with ≥80% on hidden seeds, the action replay ≤1 cm check and HDF5 export, then collect all 20.

Software acceptance should cover: mixed joint units, the WXYZ/XYZW boundary, rejecting control of mimic follower joints, N/N+1 alignment, primitive failure mid-way, unknown contact, Judge negative controls, hidden results not flowing back, and partial files not being admitted. Physical tasks and camera images need separate actual acceptance; none of those runs were executed for this document.

## 11. Project parameters to settle during review

The interface rules are given; the items below should be confirmed from A-line assets and task requirements and written into versioned configuration, not guessed in code:

- Whether the first task uses a single or a dual arm; whether pick-and-place into the drawer includes opening/closing the drawer and a final return home.
- AIRBOT's fixed TCP transform, gripper opening calibration, asset naming and initial scene layout.
- The contact data, time windows and precision the current Newton runtime configuration can actually provide.
- The basis for success height, stability window, allowed contacts and position/orientation/velocity thresholds.
- The entity identities, optical calibration and sampling rate of the two physical cameras; file handoff between headless Lab 3.0 physics and the A-line renderer.

These open items do not change the decision to use Isaac Lab 3.0.
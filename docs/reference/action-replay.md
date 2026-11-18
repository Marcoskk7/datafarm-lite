# Action replay

`datafarm.entry.episode_replay.replay_episode` replays every recorded actuator input
and compares every measured state, including robot/object roots, velocities,
articulation q/dq and body/TCP poses. Tolerances are explicit and unit-specific;
quaternion sign is rotation-equivalent. Missing coverage or a step failure never
passes. The trusted host must independently load and qualify the same source
assets/runtime, own the physics clock and supply its exact source binding.

`IsaacReplayHost` restores measured roots/joints/velocities then applies actual
recorded targets, not requested TCP goals. Hidden solver state is not available;
contact, bitwise replay and rendering remain unverified. The separate immutable
report binds the Episode ref. It does not change the original Episode or confer
Judge acceptance. Reports from fixtures only qualify software, not live physics.

The Isaac host requires store/prepared/asset-root ports, reuses A scene loading,
checks current asset and recording-runtime hashes, measurement/API identity and
URDF binding, and acquires the RobotAPI motion lock for the entire replay.
No automatic retry follows an unknown write/step/measurement outcome. Applied
actuator values must read back exactly (recorded values are actuator readbacks,
not high-precision requested targets); nonrepresentable values fail closed.
Reports declare software_fixture or source_bound_runtime qualification. Source
binding is not independent physical acceptance; contacts and solver state remain
unverified. Passive articulation drive restoration still needs runtime-specific
qualification rather than an unconditional full-solver replay claim.

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

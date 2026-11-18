# Recorded measured Episode contract

Profile: `measured-scene-episode/1` inside the existing kernel
`datafarm.episode-ir`, envelope version `1.0.0`. A schemas are unchanged.

The payload retains all seven sections: `setup`, `actions`, `state_trace`,
`contacts`, `events`, `frame_map`, `replay`. It also records termination and an
explicit `judgement=unverified`. Storage uses the kernel ArtifactStore; documents
contain refs, never deployment filesystem locators or credentials.

## Identity and source

An episode ID is a domain-separated full SHA-256 key for task ID, task revision
and attempt ID. Setup binds original YAML and normalized request refs, task
specification, SceneGraph, AssetRecords, runtime/expert sources, control profile,
implementation hashes, initialized state provenance and complete node IDs.
Caller must use a fresh attempt ID for a fresh attempt; prior artifacts are not
rewritten. A script executed as expert must match the pinned source bytes.
An explicitly supplied current /1 TaskContract is verified before recording and
its immutable ref is retained as evidence; this recorder never invents authority.

## Cycle semantics

A complete recorded trace has **N applied inputs and N+1 measured states**, from
control step zero through N. Each action is assigned the preceding state index;
each state time equals its control index times explicit dt. Node, joint and body
link coverage stays fixed. Requested targets, applied all-joint actuator readback
and measured states are separate. A primitive summary is not a full trace.

Recording byte and step budgets are explicit. Known exhaustion refuses the next
application. Final termination metadata has a bounded 64 KiB reserve; raw trace
is not truncated to fit. A late state/physics failure may have N inputs and N
valid states, marked incomplete. It must not use execution status `completed`.
Missing/nonfinite raw measurement evidence is kept separately with a failure ref
when captured; it is not admitted into the canonical measured state stream.

`termination.status` describes execution (`completed`, `failed`, `timeout`,
`aborted`, `error`), **not task success**. `EXPERT_RETURNED` is not Judge acceptance.
All unsuccessful primitive results are preserved. No `bool(None)` coercion.

## Contacts and replay

One contact sample per valid measured state, tagged `cycle_end_snapshot`.
Unavailable is `rows=null`, successful empty is `rows=[]`. Coverage is not claimed
complete without evidence of solver-buffer completeness. Forces and normals remain
unavailable unless separately supported; missing values are never invented as zero.

`frame_map=[]` means no rendered images. `replay.state_playback` and
`replay.action_replay` are both `unverified`. Initial/root/link/joint/actuator
readings support later playback/replay work, but reset + seed does not prove
restoration of hidden solver state or bitwise determinism.

Judge evaluation and action replay bind the immutable recorded Episode ref and
create separate receipts/new enriched revisions. Neither producer may overwrite
old episode bytes or certify its own acceptance. See
[judge-episode-contract](judge-episode-contract.md) and [action-replay](action-replay.md).
Software fixtures do not qualify live physics or rendering.
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

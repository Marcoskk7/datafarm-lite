# Request-bound Judge Episode contract

Request-task/1 is normalized without guessing IDs from display names. A new
judge-build-input/1 creates task-contract/1 and judge-freeze/1. Author and reviewer
remain independent recorded executions; candidate runtime rules are review data,
reviewer emits review only and pass requires empty issues. Runtime changes require
a new build directory/version, never rewriting an old freeze.

Measured scene Episodes project to judge-episode/1 with complete states and
explicit measurement paths encoded as arrays of key segments. A dotted node ID
remains one key. No final-state-only truncation; unavailable required evidence is
error, not false. Worker size and execution resource limits are unchanged.

Trusted `contract_binding` stores portable task/scene/request/source refs plus an
external freeze anchor. Frozen deployment locators stay in the opaque local
freeze blob, not portable document fields. Evaluation verifies the live bundle
and exact Episode/expert/contract refs and creates a separate immutable receipt.
The recorder does not certify success. Partial, aborted or infrastructure-failed
execution is not a normal judged task failure. No legacy/current Judge version selection is provided. Existing boolean
verdict producers remain a separate harness path. Earlier /1 input layouts are
not silently converted to the current source-bound contract. PRoot does not establish Judge secrecy.

Keeping the /1 label does not preserve old frozen hashes: changed runtime/source
requires fresh generation, independent review and a new build/freeze identity.

The portable authority stores both the original frozen TaskContract and Judge
source by content digest. Conditions, thresholds, bases and requirement bindings
must match those frozen bytes; changing portable JSON does not authorize new
criteria. The live bundle check also revalidates its normalized request/scene
inputs and all protected runtime dependencies.

Interrupted captures can retain an unmatched applied input and a missing tail
state. The host validates their source, clocks and existing readings, then emits
aborted or error with success=null without executing task predicates. Only a
completed, complete trajectory can receive success/failure. Unknown contacts
remain unknown; declaring an observation path does not prove sensor qualification.

Local build fixtures remain ready_for_cap=false. The explicit Python API
allow_fixture switch is for software checks only, is not exposed by the CLI, and
marks receipts software_fixture. It never certifies real model independence,
physical task success or production data acceptance.

## Recording and broker handoff

Evaluation requires exact task, scene, normalized/original request, runtime,
expert and contract refs in Episode evidence. The source SceneGraph is validated
with A's validator; exactly one normalized request predecessor is required.
The runtime payload must equal the source scene runtime and its physics step
must equal the recording clock. Missing expert bytes or changed source evidence
is an error, never an ordinary task failure.

The available-observation list excludes unavailable channels and contact rows
whose coverage is unknown. A contact snapshot is not a complete contact history;
its presence does not authorize force, normal or exhaustive-contact predicates.
Threshold value, unit, basis and requirement bindings remain frozen together.

`evaluate-recorded` accepts an Episode ref or the scene-job result wrapper and
writes the top-level judge-result/1 with separate immutable receipt and Episode
refs. Use a fresh output path. The broker keeps legacy bool producers separately;
current error/abort values retain success=null and do not pass through bool(None).
See [scene expert harness](scene-harness.md) for source-pinned simulator commands
and [FYP gateway](fyp-gateway.md) for optional role transport composition.

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

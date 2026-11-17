# Request-derived task planning

## Scope and dependency

This B-line component consumes the original `request.yaml` and A-line SceneGraph
and AssetRecord references. It creates one immutable task table per explicitly
identified scene group. It does not execute physics, freeze a Judge, record an
Episode or render frames. Those are separate consumers.

The feature depends on the task-identifiers branch. It was developed from that
branch's local commit, not merged into main. The stack is local-only until separately
approved for publication. No gateway, PRoot or IK source is copied into this feature.

## Explicit YAML profile

The initial supported input profile is `datafarm.task-request/1`, not a guessed
interpretation of arbitrary YAML. A complete minimal request is:

```yaml
schema: datafarm.task-request/1
request_id: request.object-placement
demand: Place the requested object in the specified receptacle.
goals:
  - id: goal.placement
    text: The object must be inside the specified receptacle.
constraints:
  - id: safety.joints
    text: Keep every controlled joint within its limits.
forbidden_shortcuts:
  - id: no.teleport
    text: Do not teleport the object.
parameters:
  - id: placement.tolerance
    value: 0.01
    unit: m
    purpose: task_success
    origin: provided
    basis: Explicit acceptance tolerance in this illustrative request.
    requirement_ids: [goal.placement]
task_count: 1
episode_count: 3
```

The numbers are illustrative input, not defaults or authorization. `task_count`
and `episode_count` are optional, positive counts with different meanings; when
provided they must be preserved exactly. With no task count, the model may propose
one or more tasks. `variation` uses the same `{id, text}` source-requirement shape.
The compiler retains original demand, constraints, forbidden shortcuts, variation
and provided numeric parameters. It never asks the model to rewrite their values.

Each parameter states value, unit, purpose, origin, basis and requirement IDs.
Supported purposes are target, control_convergence, safety, task_success, replay and
cohort. This initial input path accepts `provided` parameters only. An automatic
numeric derivation is not accepted on a model's own evidence: independently reviewed
derivation/normalization requires a separate explicit path. Unknown YAML formats,
fields, tags and implicit source-reference overrides are rejected rather than lost.

Parsing uses a bounded SafeLoader: one UTF-8 document, at most 1 MiB, 32 collection
levels and 10,000 parser events. Duplicate/non-string keys, anchors, aliases, merge
keys, unsafe tags, nonfinite values and non-JSON types are rejected. The original
bytes are stored unchanged as a `task-request-source` ArtifactRef; normalized input
is a separate `task-request` artifact. A source ref supplied by the caller must match
those exact bytes. Input can contain requirements; it cannot authorize model spend,
GPU use, network access or a deployment configuration.

## A-line inputs and host capabilities

A source references use their actual kinds: `ir.scene-graph` and `ir.asset-record`.
The compiler resolves them through the existing ArtifactStore and reuses A's
production validators. It verifies exact asset IDs, scene IDs and role/node/frame
bindings. It does not modify A schema, nodes, joint names, scene construction or tools.
Only explicitly listed members belong to a scene group.

Each scene has a trusted-host `datafarm.task-capabilities/1` record binding its exact
SceneGraph reference. It includes an available API mapping, a structured program
contract, observations with units/frame/availability, and positive control budgets.
An unavailable contact-force measurement stays unavailable: it is not a zero reading.
A host record is a capability declaration, not proof that a real simulator has run.
Required observations/APIs absent from that declaration yield `needs_capability`.
The available API is never expanded by a model proposal.

## Proposal and task table

The model returns `datafarm.task-plan-proposal/1`: tasks select exact source
requirement IDs, a scene member, role/node/frame bindings, needed APIs/observations,
display names and episode allocations; uncovered requirements have explicit reasons.
There are no replacement instructions, new thresholds, executable code, authorization
fields or stage-graph nodes in the proposal schema.

The host checks unknown requirements, missing coverage, duplicate bindings, task and
episode denominators. Request demand and global constraints/forbidden shortcuts are
retained for each task. A parameter cannot be silently separated from its conjunctive
requirements; unresolved parameter bindings are retained in the task table and block
executable public projection. Any unresolved requirement makes tasks `needs_input`.

The table is `datafarm.scene-task-table/1` and records source request/scene/asset/
capability refs, tasks, required counts, missing capabilities and unresolved input.
Each task has a stable logical ID derived from request ID + scene-group ID + slot;
changing slot/order changes that logical identity. A separate revision digest binds
exact request/scene/capability/proposal content. The IDs are not filenames.

Each task specification is a kernel TASK ArtifactDocument (envelope `1.0.0`) with
explicit B payload `request-task/1`. All six kernel sections remain present.
`stage_graph: {stages: [], sync: []}` means no prescribed graph in this profile;
it is not stage-graph/1 and no phase completion is claimed. The planner cannot create
skill-verification evidence. Specifications and tables retain `semantic_review:
unverified`: structure checks are not independent review or task feasibility proof.

## Composition entry and call recovery

Invoke the formal entry with deployment-local inputs:

```text
python -m datafarm.entry.task_plan --request <request-yaml> \
  --scene-group <group-json> --provider-config <codex-settings-json> \
  --store <artifact-store> --output <run-workspace> --approve-model
```

The group JSON has exactly `scene_group_id`, `scene_refs`, `capabilities`.
Codex settings use the existing structured-call config; `work_root` is overridden
to this operation's private local provider directory. No model call occurs without
explicit host approval. The production entry is not a renamed test tool.

A planning operation pins source digests, request/scene context, provider-config
hash and output-token budget before one call through TaskPlanProvider. The existing
Codex port owns its fresh CLI session, schema validation, usage events and provider
receipt. The host stores intent, proposal and result before consumption. Unknown cost
is explicitly unknown, never zero; output tokens and maximum call count are bounded,
while monetary spend remains advisory rather than a fabricated enforced cap.

A complete operation can be reused without a provider call after source, proposal,
table and public-task bindings are rechecked. A completed provider decision or persisted proposal can also finish local artifact
compilation after an interruption, without a new model request. A genuinely pending
or failed model operation is not automatically resubmitted. Inspect its retained evidence; a new
attempt requires an explicit new operation identity/scope. No failed attempts are
removed. The entry is a bounded planning component, not a full production launcher;
real pipeline invocations still require the existing ledger/launch discipline.

## Limits and validation

CPU fixtures check parsing, binding, count preservation, capability failures,
public projection and fresh/cached/failed call behavior. A scripted CLI exercises
the transport contract but is not evidence of real Codex/model reasoning.
Original free-text demand still needs independent semantic review: exact source
IDs cannot prove that a model selected the right object or task decomposition.
A nonempty program contract is not a physics feasibility certificate. No automatic
stage-graph executor, Judge freeze, full Episode, replay or dataset acceptance is
implemented by this component.

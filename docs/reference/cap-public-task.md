# Versioned CaP public tasks

`datafarm.cap.public-task/1` is an explicit, nested allowlisted projection of a
planned request-derived task. It is not a TaskContract, Judge freeze or host dump.

## Public fields

The profile contains task ID/revision, request ID/digest, scene ID, display name,
original public instruction, role/node/frame bindings, accepted public goals,
constraints, forbidden shortcuts, provided parameters, actual API mapping, program
contract, measurement availability, control budgets, frame IDs and joint facts.
Dotted/colon IDs are kept unchanged. Native joint names retain vendor spelling;
joint units distinguish meters and radians. The instruction and acceptance numbers
come from the accepted request, not replacement model prose.

Each nested record has an exact field set. Parameter records omit private
`evidence_ref` objects but retain source requirement IDs, units, purpose and basis.
Runtime program contracts require entry point, signature, return convention,
allowed imports, action semantics and privileged-state observation mode. API keys
map only to bounded text descriptions; they cannot contain arbitrary host objects.
Observation records say available/unavailable, unit, frame and a reason when absent.
Missing contact or unavailable force is never zero or implicit permission to ignore
an observation-dependent task.

No Judge source/code/location, hidden seed, holdout, evaluation session, credential,
local filesystem locator, image/video/frame map or private dependency manifest is
projected. The top-level CaP `public_request` allowlist is unchanged. The new nested
profile adds a stronger validation path, not permission to widen the envelope.
Do not put secrets into free-text fields: typed boundaries and lexical key checks
cannot reliably discover a secret disguised as ordinary prose.

## Where validation happens

- Trusted composition projects only entries marked planned, bound to the exact
  request, specification and scene member; missing capabilities cannot be bypassed
  by changing a status string.
- SearchRunner checks the new profile before creating output or invoking any
  provider. Its decision path rechecks public_request before persisting requests.
- CodexProvider keeps its existing public_request gate and invokes the nested
  validator before its injected structured call.
- Legacy tasks with no profile keep the existing explicit path. Unknown named
  profiles are rejected, not quietly treated as legacy.

New tasks require SearchConfig `observation_mode="privileged_state"` and keep
existing disjoint debug/selection/verification partitions and all search budgets.
Public tasks contain no seeds. Initial measured physics feedback and skill context
are still data; they do not replace the frozen hidden evaluator or grant author
access to it. Actor/distiller/verifier context separation remains in place.

## Skill provenance

Admission from a versioned public task writes `datafarm.cap.skill/2`, recording
source task ID/revision and request ID/digest in addition to existing source and
fresh verification evidence. Source display name remains descriptive, not identity.
The source bindings are validated on library reads. Existing skill/1 entries are
not relabelled; reusable guidance may still cross tasks after applicability/API
checks. Source request identity proves provenance, not universal applicability.

## Boundaries

Public desired criteria are allowed; the private implementation and hidden test
cases are not. Sanitization does not prove a real task can be controlled, that the
model chose correct semantics, or that the simulator/Judge has required sensors.
CPU and scripted-CLI tests are software evidence only. Judge /2, episode recording,
action replay and real task acceptance are separate features and validations.

# Model-generated judge and automatic freeze

The B planner derives request-task/1 Task IR from the original request.yaml and
loads the A line scene/task-table through their existing query interfaces. The B line has no built-in registry of tasks such as "grasp the plate" or "push the plate". For each task
it generates Python judge source, a SHA-256 and a condition list; after host checks, positive/negative cases and an
independent model review, these are written into the TaskContract and frozen.

## Generation flow

`Request-derived Task IR + A-line scene + actual observation spec → JudgeAuthor → host tests → JudgeReviewer → independent case tests → freeze`

The author and the reviewer use the Codex structured call in
`adapters/agent_harness/codex/structured.py`, injected by the composition root `entry/judge_cli.py`; every call is a
fresh CLI session. The judge has its own roles, structured outputs and provenance receipts, and does not relax the CaP
provider's ban on judge/hidden-evaluation fields. The reviewer receives only the original input, the current candidate
and the host test report; it does not inherit the author session or revision history. Full development trajectories
run only on the host; the model context contains the specification and case summaries, so long trajectories do not
fill the context.

Each condition records its id, the original requirements it covers, the measurement, the unit, threshold references
and the required state fields. Upstream thresholds must be preserved exactly. The model may fill in missing task-level
thresholds from explicitly recorded scene dimensions, time steps and rules, marking them `derived` with a basis.
Without a basis, or when required observations are missing, the build returns `needs_input` / `needs_capability` and
does not freeze. At most three candidate versions are tried; each version's failures and model receipts are kept.

## Runtime boundaries

The source entry point is `evaluate(episode, task_contract, scene_metadata)`. It works on read-only input data and
returns its verdict through `result({condition_id: bool or None}, metrics, reason_code)`. The full trajectory uses
judge-episode/1: explicit SI units, the world frame, `control_dt_s`, contiguous `control_step`, N actions and N+1
measured states. Control targets must not stand in for readings.

`result` is injected by the host and the model may not redefine it; success, error and termination metadata are
wrapped uniformly by the host according to the protocol, while the model is responsible for the physical conditions
and metrics. Raw predicate values and measurements are kept as they are.

Generated source may only use functions, arithmetic, conditionals/loops, containers and restricted `math` calls. It
has no file, network, reflection, dynamic-execution or simulator capability. The host first audits a closed-set AST,
then runs the source in a separate Python subprocess with a default limit of 256 MiB memory and 10 s wall time, using
rlimit on POSIX and a Job Object on Windows. Execution is refused if the resource limits cannot be installed. This is a
restricted Python subset, not a general sandbox for arbitrary Python.

Missing data, non-finite values, clock inconsistencies and execution exceptions produce `error` / `success=null`;
cancellation produces `aborted` / `success=null`; execution timeout/errors are not ordinary judged task failures. The host checks
condition coverage and result consistency, and runs every test case twice to confirm determinism. Every condition
needs a failure case that actually triggers it, and the independent review must supply its own positive and negative
cases rather than repeating the author's claim of success.

The normalized requirements define what is judged. The original `api` / `program_contract` constrain the expert's
execution interface; they are enforced by host auditing and isolated execution, and compliance must not be claimed
from state alone. If a task explicitly lists such proof as a requirement, the corresponding evidence must be supplied;
otherwise the task stays not ready. The current request-task/1 has no prescribed
stage graph; every original requirement ID must remain covered.

## CLI

```text
python -m datafarm.entry.judge_cli build --task task.json --scene scene.json --observations observations.json --cases cases.json --provider-config private-provider.json --work runs/my-task/judge
python -m datafarm.entry.judge_cli verify --bundle <frozen-dir> --freeze-sha256 <sha256>
python -m datafarm.entry.judge_cli evaluate --bundle <frozen-dir> --freeze-sha256 <sha256> --episode episode.json --output verdict.json
python -m datafarm.entry.judge_cli attach --bundle <frozen-dir> --freeze-sha256 <sha256> --adapter adapter.json --episode-relative episode.json --output new-adapter.json --public-task-output public-task.json
```

`task.json` is the request-derived `request-task/1` Task IR. The current
`judge-build-input/1`, `task-contract/1` and `judge-freeze/1` directly carry
source-bound full-scene semantics; there is no Judge /2 compatibility branch.
The observations spec requires `profile=judge-episode/1`, exact `source_binding`,
validated `public_task`, `capabilities` and `available_fields`. Evidence paths are
JSON-encoded arrays such as `["states","entities","object.a","position_m"]`,
not ambiguous dotted strings. See [Judge Episode contract](judge-episode-contract.md).
Older /1 layouts are not silently converted. Keeping the name /1 does not
preserve old frozen source hashes; build/review/freeze again with a new identity.

The provider config accepts the same keys as the CaP Codex config (`work_root`, `executable`, `model`,
`reasoning_effort`, `timeout_s`); unknown keys are rejected. The CLI has no gateway option: a loopback gateway can only
be injected programmatically through `datafarm.entry.judge_cli.build_provider(config, gateway=...)`.

Development case format: `{id, episode, expected_outcome, violates: [condition_id...]}`. Host cases must come from the
development partition or be explicitly labelled synthetic counterexamples; hidden seeds and hidden trajectories must
not be used. The optional `--dependencies` points to a host-provided "dependency name → local file" mapping. The
original input files are copied into the bundle automatically; other URDFs, runtime configs and state adapters should
be bound as the task requires.

## Freeze and adapter/1

The frozen directory holds `judge.py`, conditions, thresholds, the TaskContract, the original input, cases, validation
reports and both model execution receipts. The source SHA-256 is in `TaskContract.judge_ref`; other artifacts are
linked through ArtifactRef. `freeze.json` reuses the existing `build_manifest` / `diff_manifests` to protect the
content and the judge's code dependencies, including `kernel/`, `cap/io.py`, the Codex structured-call adapter,
`expert/manifest.py` and its imports, and `entry/judge_cli.py`. Production use requires an external SHA-256 anchor;
changing only the digest reported inside the bundle does not pass this check. Because the dependency set follows the
current package layout, bundles frozen before this layout are not migrated; build them again in a new work directory.

Use `verifier.judge_bundle={directory, sha256}` directly in `datafarm.expert-harness.adapter/1`; there is no adapter/2
and no migration of historical tasks. Loading checks the frozen content, task_id, condition IDs, version and the
judge-result/1 result protocol. The whole frozen directory is added to the protected paths automatically, and its
contents join the existing ManifestGuard scope. `attach` only writes a new adapter file and the public task
projection; it does not overwrite the input adapter or historical artifacts. The judge command it writes runs
`python -m datafarm.entry.judge_cli evaluate`, so the rollout environment must be able to import `datafarm`.

In rollout, a verdict with `schema: judge-result/1` is classified without `bool()`: `success` / `failure` count as
judged results, `aborted` is recorded as aborted, and judge errors are recorded as judge failures with `success=null`,
never as task failures. Adapters that use the existing boolean verdict protocol are unchanged.

A scripted fixture can be used to test the build mechanism, but it has `ready_for_cap=false` and cannot start
production CaP. Only a build with a real model and independent review can become ready. CaP receives only the public
task projection, never the full private contract. CaP search runs after the judge is frozen, and hidden evaluation is
allowed only after the expert program is finally frozen. Any change to criteria, thresholds or protected dependencies
requires a new task version and a new build directory; old records are kept.

The public task projection keeps the goals, constraints and `parameters` stated explicitly upstream, so the expert
can still read the task's target parameters; it never includes the private judge source, internal review records or
hidden evaluation data.

The stable identity, usage and unknown dollar cost of model calls are stored in the task's `model-usage.json`. An
interruption or timeout leaves `pending_or_failed`; the underlying call is never resent automatically. If a stale build
lock remains, confirm that the original process has exited before handling it explicitly. Real calls are registered
through the existing run ledger; tests without real execution evidence must not be reported as model, physics or task
acceptance.
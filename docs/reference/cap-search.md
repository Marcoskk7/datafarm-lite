# ASPIRE-inspired CaP search

Optional author-side program repair, selected by the orchestrator for a prepared
expert-harness round. Code: `src/datafarm/cap/`; entry point:
`python tools/cap_search.py --help`.

This ports the trace-guided CaP part of the local MVP into the collaboration
repository. It has no dependency on the original author's workspace, simulator
installation, robot assets, API configuration or experiment results. The method
follows ASPIRE's iterative debugging, population search and verified repair-skill
reuse ([paper](https://arxiv.org/abs/2607.00272), sections 2.1–2.3). This is an
adaptation, not a reproduction of ASPIRE benchmark results or its upstream code.

## Execution boundary

`SearchRunner` coordinates stateless provider decisions and an executor:

1. Retrieve API-compatible, integrity-checked repair guidance.
2. Generate an initial expert **and its self-check** (`check(episode)`), or evaluate a
   supplied P0 with an operator-supplied self-check. The self-check is frozen for the
   whole search (`self_check.py`, `report.self_check_sha256`).
3. Execute on debug seeds and score every episode with the frozen self-check; retain
   the top three candidates and residual failures.
4. Let the actor inspect selected primitive events before proposing distinct repairs.
5. Select among debug survivors on separate development-validation seeds and freeze
   the winner by normalized-source SHA-256.
6. Distill a reusable repair only after observed failure and successful repair. A
   fresh verifier request receives the task and proposed guidance, **not** the
   winning source, previous programs or debug history. It must independently
   succeed on separate development-verification seeds before skill admission.
7. Optionally submit the selected expert (the final draft) to the existing harness.
   **The frozen Judge runs only here, once, on hidden holdout seeds.** CaP has no
   holdout-seed setting, never receives a Judge verdict and does not read the
   harness-private directory.

### Success basis: CaP self-check, Judge only on the final draft

CaP iterates with its **own** discrimination. Debug, validation and
skill-verification success all come from the author's self-check, run by the host
(`cap/self_check.py`) in a child `python -I -S` with an empty environment and, on
Linux/POSIX, rlimits (address space, CPU, no file writes, few descriptors) plus a
wall timeout. The check uses the scene-worker style policy: plain functions,
constants, `import math` and safe builtins only. It receives one episode at a time:

```json
{"phase":"debug","execution":{"expert_error":false,"execution_status":"completed","reason_code":"EXPERT_RETURNED"},
 "trace":[{"event_id":0,"api":"move_tcp","error":null,"result":{"status":"completed"}}],
 "privileged_state":{"schema_version":"privileged-state/1","availability":"available","initial":{},"final":{}}}
```

and returns `bool` or `{"success": bool, "score": 0..1, "reason": str}`. An exception
or malformed return fails only that episode; an `expert_error` is always a failure.
Reports carry `success_basis: author_self_check`; `ready_for_harness_submission`
means "the self-check passes every debug and validation seed", not a Judge pass.
The self-check is not the Judge and gets no Judge source, verdicts or hidden seeds.

All three seed groups are development seeds and must be allowed by the current
task adapter. Validation here means development selection, not hidden evaluation.
Skill verification produces reusable guidance, not an expert-library entry,
dataset-admission certificate or evidence of real-robot transfer.

`XHEngine` sends the existing `datafarm.expert-harness.request/1` protocol to the
prepared round's broker. The broker still authorizes seeds, audits and snapshots
source, enforces budgets/isolation and launches jobs. With `rollout.dev_outcome`
(written by `bind_scene_adapter`), development rollouts skip the Judge step and are
classified from the recorder summary only: `executed` (counted, `success=null`),
`expert_error` (counted, failure) or uncounted infrastructure. Holdout rollouts keep
the Judge. `XHEngine` forwards execution facts only; a legacy adapter that still
judges dev rollouts has its verdict withheld from the author.
The client never launches a simulator or grants new permissions. Runtime/import
errors counted as `expert_error` can precede primitive traces; infrastructure
failures abort the search rather than become low-scoring candidate evidence.

## Public primitive evidence

The generated task runner must record an author-readable JSONL trace within each
public rollout directory. Configure its relative location through `trace_relative`.
Example record (a protocol illustration, not a task or physical result):

```json
{"event":"primitive","event_id":0,"api":"move","args":[[0.1,0.2,0.3]],"before":{"state":"observed"},"after":{"state":"observed"},"result":{"ok":false,"reason":"planning_error"}}
```

Event IDs must be unique nonnegative integers. Evidence selection is limited to
debug episodes from the current search, at most six events per inspection and a
bounded context size. Only author-visible trace data belongs here; do not include
judge code, hidden seeds or credentials. Missing traces for executed outcomes fail
closed; this adapter does not invent primitive evidence from a producer summary.
The initial implementation passes JSON evidence. Decoding and submitting images
is a deployment/provider extension, not a claimed visual verification feature.

## Provider and executor interfaces

A deployment supplies `build_provider(config) -> provider`. Its
`decide(request) -> dict` must use a fresh/stateless context for each request,
especially `role=verifier`, through the deployment's existing metered model runtime.
Provider decisions include `actor`, `distiller` and `verifier`. They return:

```json
{"action":"propose","code":"<task-adapter-defined Python expert>","hypothesis":"evidence and expected change",
 "check":"<def check(episode): ... — first actor proposal only, otherwise null>"}
```

Actor requests carry `self_check: {"status":"required"}` until the first proposal,
then `{"status":"frozen","source":...}`; a later non-null `check` is rejected.

or `{"action":"inspect","episode_id":"A-0001","event_ids":[0]}`.
The distiller returns `{"skill":null}` or a skill with exactly `name`,
`failure_signature`, `when_to_apply`, `repair`, `limitations`, `requires`.

`max_model_calls` bounds **provider decisions**, including inspection and skill
verification; it is not a USD cap. Factories must retain the deployment's existing
authorization, call ledger, unknown-cost handling and budget enforcement. No new
API client, credential loader or model default is bundled. A failed provider
decision is not automatically retried; intent is saved before the call.

`XHEngine.evaluate(code, seeds, phase)` returns one bound execution record per seed
(`execution`, `trace`, optional `privileged_state`); any `success`/`score` an engine
supplies is dropped. `SearchRunner` scores episodes with the frozen self-check and
recomputes aggregates on the host. A provider's `return True`, claimed aggregate or
narrative cannot certify success; only the frozen check over executed evidence can.
Alternate trusted engines can implement `evaluate`, `inspect`, and optionally
`submit`, selected through an explicit deployment factory.

## Launch after task preparation

Generate the task-specific public task JSON with `name`, `instruction`, `api` and
an optional `program_contract` describing the expert class/interface expected by
the task adapter. Copy `configs/cap/search.template.json` and select valid
development seed partitions and budgets for this task. Example engine config:

```json
{"round_dir":"runs/my-prepared-round","expert_class":"TaskExpert","trace_relative":"episode/primitive-trace.jsonl"}
```

From the **configured author execution environment** of that prepared round:

```bash
python tools/cap_search.py \
  --task task/public-task.json --config task/cap-search.json \
  --engine-config task/cap-engine.json \
  --provider-factory my_deployment.cap:build_provider \
  --provider-config task/provider-public.json \
  --output workspace/cap-search-1 --library workspace/repair-skills \
  --submit            # add --initial-program P0.py --self-check check.py to start from P0
```

The paths above are deployment-selected examples, not inputs that the user must
write or a fixed task registry. The orchestrator generates the task artifacts and
ensures the tool, source and provider factory are readable in the sandbox.
Pipeline-level launches continue through the existing run ledger. Do not run this
helper in parallel with another author submitting the same round. The output
directory must be new; a failed development selection never triggers `--submit`.

## Records, recovery and limits

- `decisions/*/request.json` is persisted before a provider call, followed by its
  decision. These are public-context authoring records, not provider cost receipts.
- `history.json`, duplicate records, `frozen-program.json`, `program.py`,
  `self_check.py` and `report.json` (`schema datafarm.cap.search/2`) preserve executed
  candidates, the frozen self-check and the self-check-selected final draft.
- `skill-proposal.json` and `skill-verification.json` record independent reuse
  checks. Skill objects are content-addressed and revalidated on retrieval; they
  remain fallible guidance. Retrieval uses API compatibility and word overlap.
- Broker intents and content-addressed source live in the granted workspace.
  A timeout preserves the same request/attempt identity. Resuming an executor
  request queries that identity instead of creating another rollout. The existing
  harness resumes external jobs. Whole-search/paid-provider replay is deliberately
  not automatic.
- No source clipping, state modification, judge changes or new dataset admission
  are introduced. Same-user file access is not a strong security boundary.
- CPU fixtures and native-broker/fake-simulator tests verify software contracts.
  Actual Isaac tasks, physical grasping, LLM quality, RGB semantics and real transfer
  require separate runs. This fork still produces simulation data, not real-ready data.

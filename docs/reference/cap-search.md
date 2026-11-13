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
2. Generate an initial expert or evaluate a supplied P0.
3. Execute on debug seeds; retain the top three candidates and residual failures.
4. Let the actor inspect selected primitive events before proposing distinct repairs.
5. Select among debug survivors on separate development-validation seeds and freeze
   the winner by normalized-source SHA-256.
6. Distill a reusable repair only after observed failure and successful repair. A
   fresh verifier request receives the task and proposed guidance, **not** the
   winning source, previous programs or debug history. It must independently
   succeed on separate development-verification seeds before skill admission.
7. Optionally submit the selected expert to the existing harness. **The harness
   alone performs hidden holdout evaluation.** CaP has no holdout-seed setting and
   does not read the harness-private directory.

All three seed groups are development seeds and must be allowed by the current
task adapter. Validation here means development selection, not hidden evaluation.
Skill verification produces reusable guidance, not an expert-library entry,
dataset-admission certificate or evidence of real-robot transfer.

`XHEngine` sends the existing `datafarm.expert-harness.request/1` protocol to the
prepared round's broker. The broker still authorizes seeds, audits and snapshots
source, enforces budgets/isolation, launches jobs and runs the frozen judge.
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
judge code, hidden seeds or credentials. Missing traces for judged outcomes fail
closed; this adapter does not invent primitive evidence from a producer summary.
The initial implementation passes JSON evidence. Decoding and submitting images
is a deployment/provider extension, not a claimed visual verification feature.

## Provider and executor interfaces

A deployment supplies `build_provider(config) -> provider`. Its
`decide(request) -> dict` must use a fresh/stateless context for each request,
especially `role=verifier`, through the deployment's existing metered model runtime.
Provider decisions include `actor`, `distiller` and `verifier`. They return:

```json
{"action":"propose","code":"<task-adapter-defined Python expert>","hypothesis":"evidence and expected change"}
```

or `{"action":"inspect","episode_id":"A-0001","event_ids":[0]}`.
The distiller returns `{"skill":null}` or a skill with exactly `name`,
`failure_signature`, `when_to_apply`, `repair`, `limitations`, `requires`.

`max_model_calls` bounds **provider decisions**, including inspection and skill
verification; it is not a USD cap. Factories must retain the deployment's existing
authorization, call ledger, unknown-cost handling and budget enforcement. No new
API client, credential loader or model default is bundled. A failed provider
decision is not automatically retried; intent is saved before the call.

`XHEngine.evaluate(code, seeds, phase)` returns one bound host outcome per seed.
`SearchRunner` recomputes aggregate scores from these outcomes. A provider's
`return True`, claimed aggregate or narrative cannot certify physical success.
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
  --submit
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
- `history.json`, duplicate records, `frozen-program.json`, `program.py`, and
  `report.json` preserve executed candidates and the development-selected result.
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

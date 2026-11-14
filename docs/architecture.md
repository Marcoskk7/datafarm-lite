# Architecture

DataFarm turns one real calibration episode and a data request into action-labelled, photographic
robot data. A coding agent does the task-specific work; this repository supplies the shared
instructions, the checks the agent cannot sign for itself, and the records that keep every attempt.
Everything written for a task (scene, expert, judge, prompts, configs) goes into the task's workspace,
not into this repository.

## The flow

```text
episode + request
  │  tools/pipeline_agent.py prepare     pin source, input and cost policy (datafarm.orchestration)
  │  tools/pipeline_agent.py supervise   run agent sessions until delivery or a stop (datafarm.orchestration)
  ▼
grounding              scene, cameras, references, labelled physical quantities (datafarm.grounding)
  ▼
simulator qualification  a variant certified for the task's skills (datafarm.simulation)
  ▼
task contract, expert  frozen judge, sandboxed author, held-out seeds (datafarm.task.contract, datafarm.expert)
  ▼
rendering              render contract and provider payload checks (datafarm.render.policy),
  │                    H3 control hybrid or Seedance (datafarm.adapters.render)
  ▼
verification, review   render verifier and independent review (datafarm.render.verify, datafarm.task.contract)
  │                    bounded per-item batches around all of the above (datafarm.orchestration.batch)
  ▼
delivery, admission    tools/pipeline_agent.py verify; admission certificate (datafarm.training.admission)
  ▼
training (on request)  certificate-gated pi0.5 launch (datafarm.adapters.trainer.openpi, datafarm.training)

the run ledger (datafarm.ledger) records requests, items, workers and verdicts throughout
```

A rejection returns to the stage that caused it. New attempts create new records, and earlier attempts and
failures are kept.

## Who may sign what

The producer of a stage never certifies it.

| Stage | Producer | Checked by |
|---|---|---|
| Grounding | agent | independent review; `validate_quantities` at delivery |
| Task contract | agent | independent task verifier |
| Episode | expert and simulator | frozen judge run by the expert harness, on held-out seeds |
| Render | video model | render payload checks, render verifier, independent reviewer |
| Delivery | agent | `verify_delivery` and `verify_white_delivery`, which require content-bound reviews |
| Admission | certificate compiler | admission root, re-derived from stored evidence bytes |

Receipts are records, not cryptographic signatures: anyone who can write the store can write one.
Independence comes from separate executions, content binding, and re-running verification where the
evidence is available. Process boundaries are not a security boundary between agents running as the
same user.

## Artifacts and admission

`datafarm.kernel` defines typed documents (`ArtifactDocument`, `GateReceipt`) stored by content address
in an `ArtifactStore`. Documents cannot contain absolute host paths, credential fields or mutable signed
URLs. `datafarm validate <file>` checks one document or receipt.

A training admission certificate binds the admitted member ids and per-member media digests, the
allowlist digest, the audit reports, the dataset provenance and a digest manifest of every dataset file,
and an explicit waiver policy. An independent admission root re-derives the claim from the stored bytes
and signs a gate receipt. `tools/launch_pi05_training.py train-command` verifies the certificate before a launch
manifest exists, and a launch without a verified certificate is refused. Operator steps are in
[delivery and admission](reference/delivery-and-admission.md).

## Packages in `src/datafarm`

| Package | Job |
|---|---|
| `kernel` | Shared kernel: standard-library I/O helpers, artifact documents and receipts, schema checks, content-addressed store, file binding and source locks |
| `grounding` | Grounding ledger: labelled physical quantities and their validated record |
| `task` | Task contract checks, review-independence proof (`task/review_identity.py`), white delivery |
| `expert` | Expert harness: sandboxed author sessions, rollout broker, frozen judge, held-out evaluation, expert library |
| `simulation` | Simulator-qualification decision logic, joint-margin audit, camera and grasp randomization plans |
| `render` | Render contract (`render/policy.py`), rendered-delivery check (`render/delivery.py`), spec-driven render verifier with counterfactual and reviewer calibration (`render/verify`); the renderers live in `adapters/render` |
| `ledger` | Run ledger: fixed item denominator, derived run status, launch sentinel |
| `training` | Admission certificates (`training/admission.py`), launch, dataset and action-mode contracts, cohort auditors |
| `evolution` | Feedback store and controller that evaluates and activates capability-layer changes |
| `orchestration` | Run and resume agent sessions, phases, progress, the task cost ledger, and the batch supervisor (`orchestration/batch.py`: bounded per-item launch, poll, verify and retry) |
| `adapters` | Outer technology: agent CLIs, metered provider, credentials and call log (`agent_harness`); OS sandbox (`sandbox`); batch and ledger subprocess wrappers (`process`); H3 and Seedance renderers (`render`); SAPIEN white replay and USDCraft (`sim`); certificate-gated openpi launcher (`trainer/openpi`); LeRobot episode import (`inputs`); scripted test doubles (`fakes`) |
| `instructions.py`, `runtime.py`, `doctor.py`, `cli.py` | Role context compiler, runtime bindings, prerequisite checks, the `datafarm` command |
| `batch_protocol.py` | Contract shared by the batch supervisor and its task implementations: policy, poll and verdict results, `BatchTask`, launch exceptions |

Dependency direction: `orchestration` may import anything; business packages (`grounding`, `task`, `expert`, `simulation`, `render`, `training`, `ledger`, `evolution`) never import `orchestration`; `adapters` import business packages and `kernel` but never `orchestration`.

## Top-level directories

| Directory | Job |
|---|---|
| `src/` | The `datafarm` package |
| `tools/` | Command-line entry points: `pipeline_agent.py`, `pipeline_phases.py`, `expert_harness.py`, `supervise_batch.py`, `verify_render.py`, `launch_pi05_training.py`, ... |
| `.agents/skills/` | Stage skills the agents load |
| `prompts/` | Role instructions, runtime instructions, expert session messages, render prompt templates |
| `configs/` | General policy files, render recipes, one template per task-artifact kind |
| `evaluations/` | Frozen evaluators: render EVAL-v1 and the software check for capability evolution |
| `examples/` | Runtime and budget examples, USDCraft example |
| `vendor/` | openpi trainer (Apache-2.0) and the VideoX-Fun patch |
| `assets/` | Figures for the README |
| `tests/` | CPU test suite |
| `docs/` | This documentation; [index](README.md) |

External components (simulators, video models, datasets, credentials, GPU scheduler) are runtime bindings;
see [deployment](deployment.md). Heavy runtime imports stay out of the core packages.

## Further reading

- [`AGENTS.md`](../AGENTS.md), [`prompts/instructions/`](../prompts/instructions), [`configs/agent-instructions.json`](../configs/agent-instructions.json): what the agent reads and produces.
- [Reference notes](README.md#reference): one note per component, with guarantees, records and limits.

# Codex CLI / ASPIRE-style privileged-state CaP

This connects the workspace's reproduced ASPIRE robot-manipulation loop to the existing `provider-factory`, using privileged simulator state instead of image perception. It is not a reproduction of the original paper's perception, curriculum or large-scale evaluation.

## Execution boundary

`SearchRunner → CodexProvider.decide → structured Codex call (fresh CLI session) → JSON decision`. Code execution stays with the xh broker and the task adapter. During development the actor's own self-check (`check` field, frozen after the first proposal, run by `cap/self_check.py`) decides candidate success; the frozen judge does not score development rollouts and runs only once, on hidden holdout seeds, after the final draft is submitted. Codex does not drive the simulator and does not read hidden seeds or the judge. Actor, Distiller and Verifier each use a fresh CLI session and never `resume`; the host rebuilds context explicitly, and the Verifier does not receive the proposer's code or history.

The search uses ASPIRE's before/after state evidence, selective `inspect`, the top 3 candidates, different hypotheses within a generation, failure-to-repair skill distillation and fresh-context skill re-verification. `population_schedule=generation` ensures candidates of a generation only see the previous generation's scores; execution is still serial, and no parallel agent search is claimed. A skill enters the library only after verification with the Verifier attesting it applied the skill as written. Full curricula and cross-task revisits are not implemented.

The default legacy protocol is unchanged. The new preset uses 50 generations, 3 candidates per generation and at most 400 provider decisions in total. This is a search ceiling close to the existing reproduction, not a promise that tests use it all, and not a hard limit on model tokens or dollars. Independent hidden evaluation (the single frozen-Judge pass) remains the harness's job after the frozen program is submitted; its result never flows back into search.

## Layering

- `cap/codex_provider.py` holds only the CaP role contract: system prompt, response schema, the `public_request` allowlist and decision validation. It imports no transport, subprocess or credential code.
- `adapters/agent_harness/codex/structured.py` runs one structured Codex call: fresh session, `--output-schema`, JSONL events, receipts persisted before launch, identity-based reuse, timeout process-tree termination, and no automatic resubmission.
- `entry/cap_provider.py:build_provider` is the composition root that binds the two.

## Usage

```bash
python tools/cap_search.py \
  --task runs/my-task/public-task.json \
  --config configs/cap/aspire-state-search.template.json \
  --engine-config configs/cap/xh-state.template.json \
  --provider-factory datafarm.entry.cap_provider:build_provider \
  --provider-config configs/cap/codex-aspire.template.json \
  --library runs/my-task/skills --output runs/my-task/search
```

Copy and fill the templates first; `round_dir` must be a real, prepared broker round. The task must declare its available API, units and `program_contract`; the provider does not assume every robot uses the same entry function or Python allowlist. Add `--submit` explicitly only when submitting to the harness.

Supported provider settings are `work_root`, `executable`, `model`, `reasoning_effort` and `timeout_s`; the composition root additionally accepts the explicit `gateway` environment-name binding described in [FYP gateway](fyp-gateway.md). Other keys are rejected. Without that binding, the existing Codex login and model configuration are used.

## Injected gateway

The entry composition root supports an explicitly configured [FYP Responses gateway](fyp-gateway.md), or a deployment may still pass `build_provider(config, gateway=...)`. No token, endpoint or machine configuration is shipped in the source. FYP requests originate on the server itself through its configured Tailscale connection; a userspace deployment uses an explicit server-loopback proxy. Each configured role gets a separate temporary listener and credential, and the personal FYP key remains in the parent process.

With a gateway, Codex ignores user config and rules, disables shell, multi-agent, plugins, skills instructions, MCP and web, and uses zero retries. Proxy variables and other model credentials are stripped from the child environment. The gateway preserves tool semantics for separately scoped tool-capable use, but structured CaP/Judge roles force `tool_choice=none`, reject tool-call inputs/outputs and preserve CLI-supplied definitions. Requests and output allowances are reserved before sending; unknown/incomplete requests block further spend. Transport/source/model/config fingerprints participate in identity, preventing cross-channel reuse.

Without a gateway the CLI runs with a read-only sandbox. Read isolation between directories of the same user is not guaranteed by `cwd` or the prompt; strict deployments must protect data through broker/OS separation.

The CLI usage (stdin, JSONL, `--output-schema`, `--output-last-message`) was checked against the local CLI 0.141.0 help and the [official iterative repair example](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex).

## Privileged-state protocol

Both the xh engine and `SearchConfig` must set `observation_mode=privileged_state`. The before/after of every primitive is a `privileged-state/1` object, which requires:

- `control_step`: non-negative integer; `frame_id`: `world`.
- `units`: length=m, angle=rad, time=s.
- Allowed physical fields: `joint_names`, `joint_types`, `q`, `dq`, `tcp_pose`, `objects`, `articulations`, `gripper`, `contacts`, `time_s`, `validity`.

The host reads the measured robot API and task entity states into these fields. It never reports control targets as measured positions and never writes unavailable contacts as 0. For prismatic source joints `q` is in m according to `joint_types`; revolute joints use rad.

Every public episode additionally carries `privileged_state={availability,initial,final}`. The Actor sees initial and final states in the summary and may `inspect` the before/after states of actions in the current history. Missing or malformed state fails immediately; there is no silent fallback to vision or fabricated state. Only records where the expert errored before its first primitive may be marked `unavailable`, with the error reason kept.

Privileged means physical state only. It excludes the task-success implementation, judge source, hidden seeds and private files. The provider rejects extra top-level context fields, known private evaluation/credential fields, visual inputs and non-finite values. The host must still ensure that nested object contents come only from public physical state.

## Records and recovery

Each decision persists the request, prompt, schema and a starting receipt before the call. It keeps the CLI thread ID, JSONL events, the result, token usage, the output token budget and, with an injected gateway, the upstream request count and returned model; unknown dollar cost is `null`.

The identity binds the request, prompt, schema, token budget, configuration, the source of both the CaP role module and the structured-call adapter, and the non-secret transport fingerprint. A completed result is reused after digest verification; pending, failed or interrupted directories are never resubmitted automatically. Inspect the session and model request records first, then continue with a traceable new version. On timeout the CLI process tree is terminated; if the OS refuses, leftover processes must still be checked, and a failure record is not proof that the process stopped.

Changing either source file changes the identity, so decisions recorded before such a change are not reused.

## Test scope recorded so far

The user asked to use an existing or manually built fixed scene first, without testing asset retrieval, scene generation or automatic assembly. Server tests reuse the fixed Isaac Lab 3.0 decision and may not substitute an older Isaac Sim or another simulator and claim a pass.

The user separately authorised one diagnostic run of the deployed H3 renderer with test videos sent to a specified address. That H3 output is an explicit diagnostic exception with its own source and generation record; it is not proof of faithful action replay, task success or trainable data, and does not change the default state-rendering pipeline's claims. A video that does not follow the trajectory is labelled as such and never presented as real simulation footage.

Earlier local provider check: one real Codex request through a loopback gateway connected, with no robot action. Programmatic regression covers the full repair/distil/re-verify loop through a scripted CLI subprocess; that fixture result is not a claim of real model control success. Server physics and H3 end-to-end results are reported separately after their runs.

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

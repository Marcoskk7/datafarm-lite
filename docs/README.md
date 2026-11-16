# DataFarm documentation

Start with [quickstart](quickstart.md), [deployment](deployment.md) and [architecture](architecture.md).
[Troubleshooting](troubleshooting.md) maps runtime statuses to recovery steps, and [development](development.md) lists the rules for changing this code.

## Reference

One note per component. Each states what the code guarantees and what it does not, with its records, entry points and configuration.

| Component | Note | Code |
|---|---|---|
| Capability layer, context compiler, instruction audit, source pinning | [capability-layer](reference/capability-layer.md) | `src/datafarm/instructions.py`, `tools/audit_instruction_layers.py`, `src/datafarm/evolution/integration.py` |
| Agent runtime: prepare, drive, supervise, review queue, recovery | [agent-runtime](reference/agent-runtime.md) | `tools/pipeline_agent.py`, `src/datafarm/orchestration/runtime.py`, `src/datafarm/adapters/agent_harness/codex/native.py` |
| Phased runs: fresh sessions with USD caps | [phased-runs](reference/phased-runs.md) | `tools/pipeline_phases.py`, `src/datafarm/orchestration/phases.py` |
| Model-call ledger and VLM reviewer calibration | [agent-calls](reference/agent-calls.md) | `src/datafarm/adapters/agent_harness/call_log.py`, `src/datafarm/render/verify/reviewer_calibration.py` |
| Run ledger | [run-ledger](reference/run-ledger.md) | `src/datafarm/ledger/` |
| Batch supervisor | [batch-supervisor](reference/batch-supervisor.md) | `src/datafarm/orchestration/batch.py`, `src/datafarm/batch_protocol.py` |
| Grounding and the physical-quantity ledger | [grounding](reference/grounding.md) | `src/datafarm/grounding/quantities.py` |
| Object assets (optional) | [usdcraft](reference/usdcraft.md) | `src/datafarm/adapters/sim/usdcraft.py` |
| Simulator qualification | [simulator-qualification](reference/simulator-qualification.md) | `src/datafarm/simulation/sim_qualification.py` |
| Task contract, frozen judge, expert harness | [expert-harness](reference/expert-harness.md) | `src/datafarm/expert/` |
| CaP repair search, action evidence and verified repair skills | [cap-search](reference/cap-search.md) | `src/datafarm/cap/`, `tools/cap_search.py` |
| Codex CLI agent, ASPIRE-style search and privileged-state iteration | [cap-codex-aspire](reference/cap-codex-aspire.md) | `cap/codex_provider.py`, `entry/cap_provider.py`, `adapters/agent_harness/codex/structured.py`, `cap/state_feedback.py` |
| B-line interface design: Isaac Lab 3.0, robot API, judge and episode (draft) | [b-control-interfaces](reference/b-control-interfaces.md) | Not implemented yet; reuses the existing CaP and core IR |
| A/B interface IR: asset record, scene graph, task contract, episode, render spec | [ir](ir/README.md) | AssetRecord and SceneGraph validators in `src/datafarm/ir/`; the other documents are not implemented yet |
| A line 1a: one sentence → `request.yaml` via one structured LLM call, defaults and provenance | [request-drafting](reference/request-drafting.md) | `tools/request_draft.py`, `src/datafarm/request/`, `src/datafarm/adapters/llm/` |
| A line: SimReady retrieval, collision prep, layout, reachability, Isaac Lab scene checks | [scene-assembly](reference/scene-assembly.md) | `tools/simready.py`, `tools/scene_assemble.py`, `src/datafarm/scene/`, `src/datafarm/adapters/assets/` |
| Robot API auto-discovery, semantic config and Isaac Lab instance binding | [robot-auto-binding](reference/robot-auto-binding.md) | `simulation/robot_binding.py`, `simulation/isaac_robot.py`, `tools/discover_robot.py` |
| Render contract | [render-contract](reference/render-contract.md) | `src/datafarm/render/policy.py` |
| H3 control hybrid and Seedance | [rendering](reference/rendering.md) | `src/datafarm/adapters/render/h3/`, `src/datafarm/adapters/render/seedance/` |
| Render verifier | [render-verifier](reference/render-verifier.md) | `src/datafarm/render/verify/` |
| Review, delivery and admission | [delivery-and-admission](reference/delivery-and-admission.md) | `src/datafarm/render/delivery.py`, `src/datafarm/task/contract.py`, `src/datafarm/task/white_delivery.py`, `src/datafarm/training/admission.py` |
| Policy training (pi0.5) | [policy-training](reference/policy-training.md) | `tools/launch_pi05_training.py`, `src/datafarm/training/` |
| Whole-task cost control | [cost-control](reference/cost-control.md) | `src/datafarm/orchestration/cost_budget.py` |
| Capability evolution | [capability-evolution](reference/capability-evolution.md) | `src/datafarm/evolution/` |

## What the agents read

The rules loaded into agent sessions are not documentation pages. They live in [`prompts/instructions/`](../prompts/instructions), [`prompts/roles/`](../prompts/roles) and [`.agents/skills/`](../.agents/skills), and
[`configs/agent-instructions.json`](../configs/agent-instructions.json) lists what each role loads. Start from [`AGENTS.md`](../AGENTS.md). [`configs/README.md`](../configs/README.md) maps the configuration directory.

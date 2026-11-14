# DataFarm 文档

从 [quickstart](quickstart.md)、[deployment](deployment.md) 和 [architecture](architecture.md) 开始。
[Troubleshooting](troubleshooting.md) 把运行时状态映射到恢复步骤，[development](development.md) 列出了修改本代码的规则。

## 参考

每个组件一篇说明。每篇说明代码保证什么、不保证什么，并给出其记录、入口点和配置。

| 组件 | 说明 | 代码 |
|---|---|---|
| 能力层、上下文编译器、指令审计、源码固定 | [capability-layer](reference/capability-layer.md) | `src/datafarm/agent/context.py`, `tools/audit_instruction_layers.py`, `src/datafarm/evolution/integration.py` |
| Agent 运行时：prepare、drive、supervise、审查队列、恢复 | [agent-runtime](reference/agent-runtime.md) | `tools/pipeline_agent.py`, `src/datafarm/agent/runtime.py`, `src/datafarm/agent/native_codex.py` |
| 分阶段运行：带 USD 上限的全新会话 | [phased-runs](reference/phased-runs.md) | `tools/pipeline_phases.py`, `src/datafarm/agent/phases.py` |
| 模型调用账本与 VLM reviewer 校准 | [agent-calls](reference/agent-calls.md) | `src/datafarm/agent/call_log.py`, `src/datafarm/verify/reviewer_calibration.py` |
| 运行账本 | [run-ledger](reference/run-ledger.md) | `src/datafarm/ledger/` |
| 批量 supervisor | [batch-supervisor](reference/batch-supervisor.md) | `src/datafarm/batch/` |
| Grounding 与物理量账本 | [grounding](reference/grounding.md) | `src/datafarm/core/grounding.py` |
| 物体资产（可选） | [usdcraft](reference/usdcraft.md) | `src/datafarm/simulation/usdcraft.py` |
| 仿真器资格认定 | [simulator-qualification](reference/simulator-qualification.md) | `src/datafarm/simulation/sim_qualification.py` |
| 任务契约、冻结 judge、expert harness | [expert-harness](reference/expert-harness.md) | `src/datafarm/expert/` |
| CaP 修复搜索、动作证据与已验证修复技能 | [cap-search](reference/cap-search.md) | `src/datafarm/cap/`, `tools/cap_search.py` |
| Codex CLI agent、ASPIRE 风格搜索与特权状态迭代 | [cap-codex-aspire](reference/cap-codex-aspire.md) | `cap/codex_provider.py`, `cap/state_feedback.py` |
| B 线接口设计：Isaac Lab 3.0、Robot API、Judge 与 Episode（设计稿） | [b-control-interfaces](reference/b-control-interfaces.md) | 待实现；复用现有 CaP 和核心 IR |
| Robot API 自动识别、语义配置与 Isaac Lab 实例绑定 | [robot-auto-binding](reference/robot-auto-binding.md) | `simulation/robot_binding.py`, `simulation/isaac_robot.py`, `tools/discover_robot.py` |
| 渲染契约 | [render-contract](reference/render-contract.md) | `src/datafarm/render/policy.py` |
| H3 control hybrid 与 Seedance | [rendering](reference/rendering.md) | `src/datafarm/render/h3/`, `src/datafarm/render/seedance/` |
| 渲染 verifier | [render-verifier](reference/render-verifier.md) | `src/datafarm/verify/` |
| 审查、交付与准入 | [delivery-and-admission](reference/delivery-and-admission.md) | `src/datafarm/task/contract.py`, `src/datafarm/task/white_delivery.py`, `src/datafarm/core/admission.py` |
| 策略训练（pi0.5） | [policy-training](reference/policy-training.md) | `tools/launch_pi05_training.py`, `src/datafarm/training/` |
| 整任务成本控制 | [cost-control](reference/cost-control.md) | `src/datafarm/agent/cost_budget.py` |
| 能力演化 | [capability-evolution](reference/capability-evolution.md) | `src/datafarm/evolution/` |

## agent 读取什么

加载到 agent 会话中的规则不是文档页面。它们位于 [`prompts/instructions/`](../prompts/instructions)、[`prompts/roles/`](../prompts/roles) 和 [`.agents/skills/`](../.agents/skills)，而
[`configs/agent-instructions.json`](../configs/agent-instructions.json) 列出了每个角色加载什么。从 [`AGENTS.md`](../AGENTS.md) 开始。[`configs/README.md`](../configs/README.md) 说明了配置目录的映射。

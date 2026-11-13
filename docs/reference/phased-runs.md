# 分阶段运行

`tools/pipeline_phases.py`（`src/datafarm/agent/phases.py`）一次一个阶段地创建任务。每个阶段是一个全新的 agent 会话，基于来自 [`prompts/phases/`](../../prompts/phases/) 的 brief，并带有由主机强制执行的 USD 上限。生产者与独立评审者从不共享会话，采集尝试在无模型的情况下运行。一份简短交接文件在各阶段之间携带工作，因此没有会话需要持有整个任务。

与 [`supervise`](agent-runtime.md) 不同，此流程不决定下一步运行什么。主机根据上一个结果选择每个阶段。主机可以是你，或一个遵循本说明的编码 agent。流程止于 white 交付；渲染和训练是独立的阶段。

## 阶段

| 阶段 | 角色 | 做什么 | 示例上限 |
|---|---|---|---|
| `P1` | 生产者 | 观测 episode；提出合约；grounding 场景；构建 headless runner | $25 |
| `R1` | 评审者 | 冻结合约、judge 和隐藏准入 seed | $10 |
| `P2` | 生产者 | 在 dev seed 上编写 expert；对预注册的 held-out 列表运行一次 | $35 |
| `R2` | 评审者 | 独立测试 expert；以最多 5 个阻塞项拒绝，或冻结采集批次 | $15 |
| `P2r` | 生产者 | 拒绝后仅修复阻塞项；随后再次运行 `R2` | $30 |
| capture | 无模型 | 按顺序运行冻结的尝试，直到确定性 gate 停止 | 无 |
| `RI` | 评审者 | 仅在 `halt_infra_investigate` 之后：决定采集是否可以恢复 | $8 |
| `R3` | 评审者 | 准入或拒绝每个候选 episode | $10 |
| `P4` | 生产者 | 在已准入文件之上构建 white 交付 | $8 |
| `R3b` | 评审者 | 将评审文件绑定到交付 manifest；运行 verifier | $6 |
| `R1u` | 评审者 | 仅在用户反馈之后：在反馈涉及的范围内修订冻结合约，作为新的冻结 | $10 |
| `P2u` | 生产者 | 修改 expert 以满足修订后的合约和用户要求；随后再次运行 `R2` | $30 |

每个阶段都遵循的通用规则见 [`protocol.md`](../../prompts/phases/protocol.md)。

## 运行任务

```bash
python tools/pipeline_agent.py prepare --episode /data/episode.mp4 --request-file /data/request.txt \
  --target-count 5 --stop-after white --output /data/task/work
RUN="python tools/pipeline_phases.py run --task-dir /data/task --config examples/runtime/phases-solpi.json"

$RUN --phase P1
$RUN --phase R1 --quote-final P1
$RUN --phase P2 --quote-final R1
$RUN --phase R2 --quote-final P2
python tools/pipeline_phases.py capture --task-dir /data/task --review R2
$RUN --phase R3 --summary-file /data/capture-notes.md
$RUN --phase P4
$RUN --phase R3b
python tools/pipeline_agent.py verify --work /data/task/work
```

首次运行前，从 [`examples/runtime/`](../../examples/runtime/README.md) 复制一份配置并填写 `environment`：模拟器 python、如何访问 GPU、机器人资产路径（本仓库不包含它们）以及一次可并行运行多少 seed。先安装 harness；[README](../../README.md#快速开始) 有 SoL-Pi 步骤。

在开始下一阶段前阅读 `phases/<label>/final.md` 和评审的 `report.json`。后续阶段从文件中读取其输入，`--quote-final` 会将先前的 final 消息作为未验证声明加入。拒绝之后，为每个会话使用新标签：

```bash
$RUN --phase P2b --template P2r --quote-final R2 --set DENIAL=R2 --set PREVIOUS_HANDOFF=P2-handoff.md
$RUN --phase R2b --template R2 --quote-final P2b --set REQUEST=P2b-source-request.json --set HANDOFF=P2b-handoff.md
```

如果 `R2b` 冻结了批次，`--review R2b` 要传给 `capture`，`--set BATCH_REVIEW=R2b` 要传给 `R3` 和 `P4`。`--print-brief` 显示 brief 而不运行它，`--brief FILE` 运行你自己的 brief，这就是给生产者额外指令的方式。记录此类帮助，因为它是成本和结果的一部分。

基础设施故障之后，用 `--resume-from` 在新标签下继续会话：

```bash
$RUN --phase P2-retry --template P2 --resume-from P2 --brief /data/continue.md
```

`pipeline_phases.py cost --task-dir /data/task` 打印每个阶段的支出，包括正在运行的阶段。

### 生产者和评审者使用不同模型

配置中的 `roles` 按角色设置 `model`、`extra_args` 或 `env`；此处的值会替换该角色每个阶段的基础值（以 `R` 开头的模板是评审者）。`--model` 在 `run` 上会为单个阶段覆盖两者。[`phases-solpi-cross-model.json`](../../examples/runtime/phases-solpi-cross-model.json) 用一个模型写作、用另一个模型评审，因此可以在同一评审者下比较两个生产者。

当生产者在修复轮次内未能达到来源批准时，主机可以在新标签下换用另一个生产者模型继续，例如 `$RUN --phase P2b --template P2r --model <other> --quote-final R2b ...`。较早的阶段、拒绝及其成本会保留在记录中。在 RealBot-01（一台新的双臂机器人，将麦片舀入杯中，2026-11-10）上，相同的 R1 合约和相同的评审者模型看到 GPT-6.1 Sol 生产者达到 0/20 dev seed 和两次拒绝，而 Claude Opus 5.5 生产者达到 17/30 dev seed 和来源批准。那是一个任务；它不能说明任一模型的普遍情况，此处也没有任何规则会自动切换模型。

### 评审后的用户反馈

当用户拒绝某个冻结合约允许的内容时（例如已准入 episode 中摇晃的动作），主机将反馈逐字写入 `work/reviews/user-feedback-<n>/`：`report.json`（`schema` 为 `datafarm.user-feedback/1`、`user_quotes`、`provenance`、`requirements` 各带其来源引文，可选 `host_diagnosis` 和 `rejected_episodes`）和 `review.md`。然后：

```bash
$RUN --phase R1u --quote-final P2 --set FEEDBACK=user-feedback-001
$RUN --phase P2u --quote-final R1u --set FEEDBACK=user-feedback-001 --set PREVIOUS_HANDOFF=P2-handoff.md
$RUN --phase R2c --template R2 --quote-final P2u --set CONTRACT_REVIEW=R1u --set REQUEST=P2u-source-request.json \
  --set HANDOFF=P2u-handoff.md
```

`R1u` 写入新的冻结，绝不再编辑较早的冻结。它只改动反馈涉及的内容，只在有引用的需求推动时才移动阈值，展示每个新增或变更的 gate 会使被拒绝的 episode 失败并通过一个已知良好案例，并抽取新的隐藏准入 seed。此后 `--set CONTRACT_REVIEW=R1u` 要传给 `R2`、`P2r` 和 `R3`。较早的准入在旧合约下保留在记录中；它们不会在新合约下被准入。在 RealBot-01 上，这个循环把用户的 "no back-and-forth motion" 变成了一个平滑度 gate，它拒绝了之前全部五个准入（停止-启动和反向计数为 5–10 和 4–14，对应上限 2），修复后的 expert 采集在 12 次尝试中准入 5 次，这五个后来都在盲 H3 渲染评审中被接受。

## 保证

1. **标签只运行一次。** 同一标签的第二次运行会被拒绝，因此一个阶段不能覆盖自己的记录重跑。
2. **上限会停止会话。** 主机每 `poll_seconds` 轮询一次 usage，并在阶段上限、任务上限或 `max_phase_seconds` 处停止整个进程组。任务上限达到后阶段不会启动。
3. **没有价格的 usage 会停止阶段。** 无法定价的 usage 无法强制执行上限。
4. **Provider 错误计为失败。** Pi 在 provider 错误后以 0 退出，因此主机从最后一条 assistant 消息读取结果，而完全没有 assistant 消息的 Pi 会话视为失败。Claude Code 和 Codex 从它们的结果事件读取。在模型 API 拒绝其凭据（HTTP 401/403，无 API key）之后无输出结束的会话为 `failed`，`stop_reason` 为 "model API authentication failed"，因此下一阶段不会在从未完成的工作上启动。
5. **会话保持分离。** 台账记录每个会话 ID。被另一阶段复用的会话标记为 `session_reused`，除非 `--resume-from` 要求复用。
6. **完成的评审获得主机写入的执行记录。** `work/reviews/executions/<label>/` 保存会话 transcript 的副本和绑定它的 `execution.json`。`verify` 检查此记录，并报告为 `declared-execution-record`。
7. **Gate 决定采集，只有评审者准入。** `capture` 一次运行一个评审者冻结的 `capture_attempt.sh`，并在任何 `decision` 不是 `continue` 时停止，包括缺失的情况。

## 记录

| 路径 | 内容 |
|---|---|
| `phase-usage.jsonl` | 每个阶段一行：`phase`、`template`、`role`、`harness`、`model`、`exit`、`status`（`done`、`failed`、`stopped`、`session_reused`）、`stop_reason`、`over_cap`、`session_id`、`resumed_from`、`unpriced_records`、`phase_usd`、`task_usd_before`、`task_usd_after`、`wall_s` |
| `phases/<label>/` | `brief.md`、`prompt.md`（`AGENTS.md` 和 brief）、`command.json`、`stream.jsonl`、`stderr.log`、`final.md`；Pi 还有 `pi-sessions/` |
| `phases/current-cost.json` | 正在运行阶段的支出；结束时移除 |
| `work/reviews/<label>/` | 评审输出。请将它们保持在 `work/.runtime/` 之外：该文件夹将任务标记为由 `supervise` 驱动，`verify` 届时会要求运行时评审 |
| `work/capture/` | `driver.log`，以及 gate 的台账、尝试和准入 |

## 配置

配置使用 schema `datafarm.phase-run/1`。示例：[`phases-solpi.json`](../../examples/runtime/phases-solpi.json)、[`phases-claude-code.json`](../../examples/runtime/phases-claude-code.json)、[`phases-codex.json`](../../examples/runtime/phases-codex.json)。

| 键 | 含义 |
|---|---|
| `harness` | `pi`、`claude` 或 `codex` |
| `model` | 传给 CLI；`codex` 必需，因为其流不指明模型 |
| `roles` | 可选的 `producer` / `reviewer` 对象，含 `model`、`extra_args`、`env`；它们替换该角色各阶段的基础值 |
| `extra_args`、`env` | 加到 CLI 的参数和环境。不要把凭据放在这里 |
| `prices_usd_per_mtok` | 每个模型：`input`、`cache_read`、`cache_write_5m`、`output`，可选 `long_prompt`（`above_tokens`、`input_multiplier`、`output_multiplier`）。键会匹配任何包含它的模型 ID。请对照你的 provider 核对这些值 |
| `task_cap_usd`、`phase_caps_usd` | 必需。上限先按标签查找，再按模板查找 |
| `poll_seconds`、`max_phase_seconds` | Usage 轮询，默认 30 s，以及可选的时间限制 |
| `environment` | 展示给每个阶段的部署说明：模拟器 python、GPU 访问、机器人资产、并行 seed 限制 |
| `freshness_forbidden` | Agent 不得打开的进一步路径，例如较早的任务输出 |

## 限制

- **目前实际使用很少。** 一个完整任务（在 SoL-Pi 上叠杯子）已通过此工具运行。Codex 只用短 prompt 运行过它，Claude Code 只作为 stub。一次运行不能证明另一个任务会成功；那需要其自己的运行和独立评审。
- **上限可能超出一个模型响应。** Pi 和 Codex 在响应结束时记录 usage，Claude Code 的流对每条消息同样如此，因此超支会以 `over_cap` 可见但不会被阻止。
- **成本是基于你的价格表的 API 等效估算。** 当 Claude Code 的 run 结束时，其报告的总量会替换估算。
- **这些成本不在任务的成本台账内。** `pipeline_agent.py cost-status` 不包含它们。
- **评审独立性是声明的，而非证明的。** Agent 以同一用户运行，只有指令和文件权限隐藏 holdout seed。需要更强的检查时使用 `supervise`，其评审 `verify` 会报告为 `runtime-verified`。
- **由主机决定。** 此处没有任何东西选择下一阶段或评判评审。

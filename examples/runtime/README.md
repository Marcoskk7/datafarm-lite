# 运行时示例

- `native-codex.json`：面向已有 Codex CLI 登录的原生 driver；模型遵循部署设置。
- `claude-code.json`：通过 `agent_cli_driver.py` 使用 Claude Code；它使用你的 Claude Code 登录与 `bypassPermissions`。
- `pi-solpi.json`：通过 `agent_cli_driver.py` 使用 Pi；它使用你的 Pi 设置，并在 `pi install` 添加 SoL-Pi 后加载它。
- `sol-pi.template.json`：开启全部四种机制的 SoL-Pi 设置；将其复制到 `~/.pi/agent/sol-pi.json` 并填写 reducer 模型。
- `agent_cli_driver.py`：这两个配置背后的 argv driver。
- `phases-solpi.json`、`phases-claude-code.json`、`phases-codex.json`：用于[分阶段运行](../../docs/reference/phased-runs.md)的 harness、价格与美元上限。对照你的 provider 核对价格并填写 `environment`。
- `phases-solpi-cross-model.json`：Pi 配置，生产方用一个模型、审查者用另一个模型（`roles`）；把 `<provider>` 换成你的 Pi provider 名称。
- `budget-disabled.json`：其目标与限额不生效的成本策略；用量仍会被记录，unknown 成本保持 unknown。
- `budget-enforced.json`：严格的成本策略；它需要计量 driver，不能与原生 CLI 一起使用。
- `metered_codex_driver.py`：一个计量 driver 示例；参见[成本控制](../../docs/reference/cost-control.md)。

## harness 之间有何不同

| | Codex（原生） | SoL-Pi、Claude Code（`agent_cli_driver.py`） |
|---|---|---|
| 之后的会话 | 恢复同一 thread | 全新开始并从任务文件继续 |
| author 与 reviewer 身份 | 取自 Codex thread | 取自 CLI 流中的会话 ID |
| 输出 | `stdout.log` | 会话文件夹中还有 `agent-output.jsonl` |

在配置中把 `/path/to/PyRUA-Lean` 设为你 checkout 的绝对路径，因为 driver 从任务目录运行。`--` 之后、位于 `argv` 中的任何内容都会传给 CLI，例如 `"--model", "<model>"`。

要确认 SoL-Pi 已加载，请在某个会话的 `pi-sessions/*.jsonl` 中查找 `sol-pi-online-context-state-v1` 条目（它需要 `onlineContextCompact`）。当 SoL-Pi 缺失时 Pi 会无错误地运行，因此这一点很容易被忽视。

`agent_cli_driver.py` 不报告成本，因此 ledger 把这些会话记录为 unknown，enforced 预算会拒绝它们。token 用量保留在 `agent-output.jsonl` 中。

要添加另一个 harness，请在 `agent_cli_driver.py` 中添加一个 profile：命令，以及其 JSON 流如何给出会话 ID 并报告失败的运行。复制 `claude-code.json` 并更改 profile 名称。driver 契约在 [agent runtime](../../docs/reference/agent-runtime.md) 中。

可选的 [expert harness](../../docs/reference/expert-harness.md) 会自行启动其 authoring agent，并有自己的 `agent.backend` 设置，支持 Claude Code 与 Codex，但不支持 Pi。

默认成本策略是 `configs/task-cost-policy.json`。这些文件配置运行时；它们并不表示模拟器、provider 或结果能正常工作。参见 [quickstart](../../docs/quickstart.md) 与 [agent runtime](../../docs/reference/agent-runtime.md)。

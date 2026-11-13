# 整体任务成本控制

每个为某个任务工作的模型会话（编排、实现、expert 编写、审查、重试与修复）都计入一个持久的、按任务划分的 ledger。策略在任务准备时即固定，因此之后的编辑无法重置或放宽预算，而成本未知的会话保持 unknown，不会被当作零。ledger 是一道调度器守卫：它决定下一次模型调用是否可以开始。它不是计费防火墙。只有当 driver 自身在所有后代调用上强制执行预留时，才存在硬性美元上限。

agent 遵循的规则在 [`prompts/instructions/task-cost.md`](../../prompts/instructions/task-cost.md)。

## 保证

1. **策略仅在 `0 < target_usd <= limit_usd`、金额有限且作用域为整个任务时有效。** 策略还必须指名 `mode` 与 `on_excess`。
2. **策略不能在任务运行期间改变。** ledger 存储策略摘要，`prepare` 将其记录在输入锁中。之后更改或删除策略会失败，会话运行后删除 ledger 同样会失败。
3. **缺失、无效或部分用量绝不会变成零。** receipt 只有在使用 schema `datafarm.session-cost/1`、指名同一 reservation ID、设置 `includes_descendants=true` 与 `complete=true`，并携带证据以及 `provider` 或 `api-equivalent` 基准时才算数。任何其他 receipt 都会把该行标记为 `unknown`，而当某行是 unknown 时 `total_usd` 为 `null`。
4. **一个 reservation ID 恰好授权一次调用与一次结算。** 复用 ID 会失败，同一行结算两次也会失败。并发 driver 在 `.cost.lock` 上串行化。
5. **enforced 模式对已提交的支出设上限。** 每个预留都被裁剪到剩余限额。超支会被记录为 `overrun`，不会被裁剪。unknown 或 overrun 的行会阻塞之后的所有预留，直到操作者核对它。
6. **enforced 模式需要一个真正能为所有后代调用设上限的 driver。** driver 必须声明 `cost_control.protocol = datafarm.session-cost/1`、`includes_descendants = true` 以及一个正的 `max_session_usd`。原生 Codex driver 会被拒绝。
7. **有预算的任务不会启动无预算的 run 后演进 driver。** 这在 advisory 与 enforced 模式下均适用；只有 `disabled` 允许它。
8. **崩溃会保留预留。** 重启时，来自从未结算会话的预留会依据其 receipt 结算或被标记为 unknown。它绝不会因超时被退款。

代码：`src/datafarm/agent/cost_budget.py`、`tools/pipeline_agent.py`。

## 数据模型

`cost-policy.json`（在 `prepare` 时复制进任务工作区；默认为 `configs/task-cost-policy.json`）：

| 字段 | 取值 |
|---|---|
| `schema` | `datafarm.task-cost-policy/2` |
| `mode` | `disabled` \| `advisory`（默认）\| `enforced` |
| `on_excess` | `warn`（默认）\| `pause`（仅 advisory：当已知支出达到限额时停止） |
| `target_usd`, `limit_usd` | 有限，`0 < target <= limit`；默认 30 / 50 |
| `scope` | 必须为 `all-model-work-through-white-data` |
| `includes`, `excludes`, `currency_basis` | 描述性；默认排除渲染阶段与 GPU 计算 |

| 模式 | 调度与证据 |
|---|---|
| `disabled` | 不因阈值阻塞；保存已有成本、unknown 项与用量 |
| `advisory` | `warn` 继续，`pause` 在已知限额处暂停；不保证实际总成本 |
| `enforced` | 为所有子调用设上限；未结算、unknown 或超限的项会阻塞；不能使用普通原生 CLI |

`cost-ledger.json`（`datafarm.task-cost-ledger/1`）保存 `policy_sha256`、`blocked` 与 `reservations`，后者是从 ID 到 `{status: reserved|settled|unknown|overrun, reserved_microusd, charged_microusd, actual_microusd, receipt?, usage_evidence?}` 的映射。金额是整数微美元，向上取整。

`cost-status` 报告 `mode`、`coverage`（`complete` 或 `incomplete`）、`unknown_sessions`、`total_usd`（只要有 unknown 即为 `null`）、`known_usd`、`committed_usd`、`remaining_usd`、`target_reached`、`warning`、`stop_required`、`blocked`、`sessions` 与 `history_entries`。

## 工作原理

1. `prepare` 复制策略，将其哈希进 `input.json` 并启动一个空 ledger。
2. 每个模型会话之前，`drive` 调用 `budget.status()`。若被设置，它以 `cost-budget-blocked` 停止（当 `stop_required` 被置位），或在 enforced 模式下当任何行未解决时停止。
3. 该会话在一个新 ID 下预留一个金额。enforced 模式把预留裁剪到剩余限额；advisory 与 disabled 预留 0。
4. 当会话结束时，`settle` 应用其 `cost-receipt.json`。没有有效 receipt 时，该行变为 `unknown`。
5. 在模型会话之外产生费用的外部作业（例如 USDCraft 构建）以同样方式预留与结算，并在成本无法测量时记录 `cost_usd: null`。

## 入口

```bash
python tools/pipeline_agent.py prepare --episode /data/episode.mp4 --request-file /data/request.txt \
  --target-count 5 --output /data/task --cost-policy examples/runtime/budget-disabled.json
python tools/pipeline_agent.py cost-status --work /data/task
python tools/pipeline_agent.py wait --work /data/task --timeout 1800 --interval 5
```

示例策略：`examples/runtime/budget-disabled.json`、`examples/runtime/budget-enforced.json`。

`wait` 只轮询 `state.json`。它不是远程进程的健康检查，也不会恢复任务或提交付费调用。对于 `model_wait_for`，一旦所有依赖都处于 finished、completed、complete 或 succeeded，或任一依赖处于 failed、error、cancelled 或 needs-input，`drive` 就让模型处理结果。不存在、重复或缺少状态的依赖会引发错误。这只涵盖通过 `drive` 发起的调用，不涵盖宿主的原生聊天。

## Enforced driver 协议

一条 driver 声明，必须接入真实的计量与限流实现：

```json
{
  "argv": ["/opt/deployment/metered-agent"],
  "input": "messages",
  "cost_control": {
    "protocol": "datafarm.session-cost/1",
    "includes_descendants": true,
    "max_session_usd": 5
  }
}
```

`drive` 传递三个环境变量：`DATAFARM_SESSION_MAX_USD`（本会话及其所有子调用的总配额，至多为配置值且不高于剩余量）、`DATAFARM_COST_RESERVATION_ID`（唯一的会话 ID）以及 `DATAFARM_COST_RECEIPT`（receipt 路径）。

在每次 provider 请求之前，driver 为输入、最大输出、缓存写入以及适用的长上下文或分层费率预留最坏情况的金额，包含重试与格式回退。若配额不足，它不得发送，且不得把同一总额复制给每个 sub-agent。返回之前，driver 等待所有子调用结束或保留一项 unknown 负债；它不得返回一个 complete receipt 却留下一个另行计费的孤儿。

```json
{
  "schema": "datafarm.session-cost/1",
  "reservation_id": "ID from the environment variable",
  "includes_descendants": true,
  "complete": true,
  "cost_usd": 2.31,
  "cost_basis": "api-equivalent",
  "evidence": "per-request usage persisted on the deployment side, with a price version index"
}
```

`cost_basis` 也可以是 `provider`。即使会话以非零码退出，也必须报告已经产生的成本。缺失或无效的 receipt 会阻塞恢复并保留整个预留金额；这并不意味着该金额已支付。崩溃留下的预留也会阻塞下一个会话。超过预留的 receipt 会被记录为超支，之后的调用会停止。

[`examples/runtime/metered_codex_driver.py`](../../examples/runtime/metered_codex_driver.py) 就是这样一个 driver 的示例：它运行 `codex exec`，经由本地计量的 OpenAI 兼容网关（`src/datafarm/agent/metered_provider.py`）。模型、endpoint 与价格表必须用 `--pricing` 或 `DATAFARM_METERED_PRICING` 显式配置；没有默认网关。

## 失败行为

- unknown 或 overrun 的行会阻塞 enforced 任务，直到操作者核对它。没有自动退款，也没有清除 unknown 成本的命令；操作者须先核对账单与所有任务的终止状态。
- 策略发生变化、执行后 ledger 缺失、或复用 reservation ID，都会引发错误，绝不静默重置预算。
- advisory 任务会越过限额继续运行。会设置 `warning`，且在 `on_excess: pause` 下，一旦 `stop_required` 为真（即 `known_usd >= limit_usd` 时）便会触发。

## 局限

- **advisory 是一份报告，不是上限。** 原生 Codex CLI 报告的是 token 而非美元，因此会话结算为 `unknown`，`known_usd` 可能保持 0。advisory 的 `pause` 只在成本确实被测量到时才触发。普通 Codex CLI 没有硬性美元上限。
- **ledger 不是安全边界。** 同 UID 的进程可以编辑 `cost-ledger.json`。在生产环境中，应把 ledger 与计量入口放在 author 的写权限之外。
- **并非所有成本都在 ledger 中。** 渲染与 GPU 计算被默认策略排除，启动任务的外部聊天的成本也不会被捕获。除非部署记录它，否则两者都不进入 `total_usd`。

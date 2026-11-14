# Agent 运行时

Agent 运行时把用户请求和真实 episode 转换成一个可恢复的任务 workspace，并在其上以有界会话运行提供的编码 agent。它从固定的能力层编译每个会话的指令，在启动前写入执行记录，将排队的评审派发到独立的评审会话，在不调用模型的情况下等待外部 job，并且仅当交付 verifier 通过时才接受 `complete`。任务状态（`state.json`）是 agent 的可恢复笔记本，不是成功证书。

相关：[能力层](capability-layer.md)（上下文编译器与来源固定）、[run ledger](run-ledger.md)（用于启动 `supervise`）、[batch supervisor](batch-supervisor.md)、[成本控制](cost-control.md)（预算模式、预留、回执）、[能力演化](capability-evolution.md)（`feedback`、运行后演化）。Agent 遵循的交接规则见 [`prompts/instructions/runtime-handoff.md`](../../prompts/instructions/runtime-handoff.md)。

## 保证

1. **输入在 `prepare` 时锁定，并在每个会话前重新检查。** 每个 episode 和参考文件都会计算哈希写入 `input.json`；文件变化会阻止 `context`/`drive`/`verify`。
2. **`complete` 仅通过交付 verifier 被接受。** `drive` 返回 `verify_delivery(work)`；无效的交付会抛错，命令以 2 退出。
3. **`needs-input` 必须携带具体问题。** `open_questions` 必须是非空列表，元素为不空白字符串或带 `question` 的对象；否则 `ValueError`。哪些问题符合条件（意图、设备事实、无法推导的访问）属于角色指令，不属于代码。
4. **每个 workspace 一个控制器。** `supervise` 持有 `.supervise.lock`；`drive` 也持有它（除非由 `supervise` 调用），外加 `.driver.lock`，两者都是非阻塞 `flock`。
5. **执行记录在进程启动前写入，进行中的工作在任何新的模型调用前完成对账。** 活跃的本地 worker → `waiting-worker`；保存的原生线程 ID → `interrupted` 并带 `recovery_session_id`；原生结果 → 状态恢复；其他任何情况 → `recovery-required`。
6. **在另一台主机上启动的执行不会在此恢复。** 记录带有 `host` 和 `boot_id`；来自其他主机的进行中记录会原样返回 `recovery-required`，且 `runtime-status` 在异地将 `supervisor_alive`/`worker_alive` 报告为 `null`。
7. **会话与重试限制在重启后仍然生效。** `supervise` 递增 `sessions` 并保存 `dispatch_after`，然后才调用 `drive`；之后没有执行记录的派发为 `recovery-required`；瞬时失败重试受 `max_retries` 约束。
8. **评审请求被冻结、绑定来源，并在作者工作之前派发。** 每个 `state.review_requests[]` 条目需要一个安全的 `id`、`subjects`（带匹配 SHA-256 的 workspace 文件）和 `instructions`；它会被一次性复制到 `.runtime/reviews/<id>/request.json`，之后不可更改。下一个会话作为 `task-verifier` 运行。
9. **评审完成记录由主机写入，并在每次使用时重新校验。** 它绑定报告、执行记录、transcript、身份文件和上下文哈希（`receipt.json` 的 `messages_sha256` = 执行的 `context_sha256`）；结论不可被提升；subjects 和证据必须仍哈希一致。
10. **评审者与作者会话不能共享同一 provider 身份。** 通用 driver 必须在启动评审前声明 `datafarm.agent-identity/1`；身份按 `session_key`（`author` 或 `review-<id>`）注册，跨 key 复用的会话 ID 会被拒绝，非全新会话必须可证明是同一线程，原生评审不得复用作者线程。
11. **交付的 episode 评审绑定到运行时评审完成记录。** 在运行时管理的 workspace 中，每个 episode 评审必须引用 `runtime_review: <id>`，其完成记录需通过校验、为 passed、覆盖每个交付产物的哈希，并在其证据中列出此评审文件；手写的执行记录会被拒绝。在运行时之外，记录被接受并加以标注。结果字段 `review_independence` 仅当每个 episode 都满足时才为 `runtime-verified`，否则为 `declared-execution-record`。
12. **等待外部 job 不会调用模型。** 当 `model_wait_for` 中至少有一个 job 尚未成功且无 job 失败（`failed`、`error`、`cancelled`、`needs-input`）时，`drive` 返回 `waiting-external-jobs`，`supervise` 轮询状态；不可读的状态文件绝不意味着失败；成功但 `exit_code` 非零会变为 `failed`。
13. **Stop 终止本地进程组，而非外部 job。** `stop` 写入 `.runtime/stop.json`；控制器杀死会话组（先 TERM，后 KILL）并标记为 `stopped`；外部 job 保持运行。
14. **预算门在启动前生效。** 强制预算会拒绝原生 driver 以及任何没有 descendant-wide `datafarm.session-cost/1` 上限的 driver；任何非 disabled 的预算（含 advisory）都会拒绝 `--evolution-driver`。
15. **发布的进度只统计已验证的交付。** `completed_episodes` 仅当最后一个结果为 `valid` 且 `complete` 时才非零；心跳不改变 `last_substantive_at`。

代码：`tools/pipeline_agent.py`、`src/datafarm/agent/runtime.py`、`src/datafarm/agent/native_codex.py`、`src/datafarm/agent/task_wait.py`、`src/datafarm/agent/progress.py`、`src/datafarm/task/contract.py`。

## 数据模型

| 文件 | 内容（由代码写入） |
|---|---|
| `input.json` | `schema: datafarm.episode-input/1`、`files[{path, sha256}]`、`sha256`、`request`、`target_count`、`stop_after`（`render`/`white`）、`cost_policy_sha256` |
| `state.json` | `schema: datafarm.agent-state/1`，`status` 为 `working`、`needs-input`、`complete` 之一、`phase`、`next_action`、`open_questions`、`external_jobs[{id, status, status_file?, ...}]`；可选 `model_wait_for[]`、`review_requests[{id, subjects[{path, sha256}], instructions}]` |
| `capability.json`、`AGENTS.md`、`task/`、`evidence/`、`sessions/` | 由 `prepare` 创建 |
| `sessions/<ns>/` | `messages.json`、`context.md`、`receipt.json`、`execution.json`、`stdout.log`、`stderr.log`；原生：`native-command.json`、`native-result.json`、`native-stderr.log`；`identity.json`；`cost-receipt.json` |
| `execution.json` | `status`（starting → running → returned/failed/timeout/stopped/stop-failed；恢复：interrupted）、`started`、`ended`、`input_kind`、`context_sha256`、`role`、`host`、`boot_id`、`pid`、`process_identity`、`exit_code`；可选 `review_id`、`native_state`、`cost_reservation_*`、`cost`、`recovered`、`recovery_session_id` |
| `identity.json` | `schema: datafarm.agent-identity/1`、`session_id`、`session_key`、`role`、`fresh`、`evidence[{path, sha256}]`（位于会话文件夹内）、可选 `origin` |
| `.runtime/` | `supervisor.json`（心跳、`supervisor_pid/identity/host`、`worker`）、`control.json`（`sessions`、`retries`、`config_history`、`status`、`last_result`、`dispatch_after`、`retry_at`）、`stop.json`、`driver.json`、`identities.json`、`author.json` 和 `review-<id>.json`（原生线程状态）、`status-output.json` |
| `.runtime/reviews/<id>/` | `request.json`（冻结）、`report.json`（`verdict` 为 pass/reject/unverified、`subjects`、`evidence`、`findings`）、`completion.json`（`verdict`、`report_sha256`、`session`、`execution_sha256`、`context_sha256`、`role`、`transcript_sha256`、`identity_sha256`、`native_session`） |
| `native-result.json` | `schema: datafarm.native-session/1`、`session_id`、`exit_code`、`usage`（最后的 `turn.completed`）、`cost_usd: null`、`cost_status: unknown`、`includes_descendants: false`、`errors` |

## 工作原理

`drive`（单个前台控制器）：
1. 获取锁；`recover_inflight`；若 worker 存活或需要恢复则提前返回。
2. 每个会话：检查 stop；`verify_input`；成本门；选择一个待处理评审（角色 `task-verifier`）或作者（`orchestrator`）。无待处理评审的 `complete`/`needs-input` 返回 `verify_delivery` / `needs_input_result`；外部等待返回 `waiting-external-jobs`。
3. 重新校验固定的来源；将上下文编译到新的 `sessions/<time_ns>/`；写入 `execution.json`（`starting`、host、boot ID）；预留成本；在新会话中启动 driver，payload 通过 stdin 传入；记录 PID 和开始时间。
4. 每秒心跳；在 stop/timeout 时杀死进程组；之后无论如何对进程组执行 SIGKILL。
5. 结算成本；记录结果事件；注册身份（原生或身份协议）；完成评审（`completion.json`）。

`supervise`（持久）：加载配置；写入 `control.json`、`driver.json`；循环：stop → 杀死进行中的进程组；刷新外部 job；在有界片段中等待 job；对于 `complete`/`needs-input` 调用一次 `drive` 并结束；否则预留一个会话并调用 `drive(max_sessions=1)`；瞬时失败退避 `min(backoff_max_seconds, backoff_seconds * 2^(n-1))`。

原生 Codex（`native_codex.py::run`）：argv 为 `codex exec [resume <id>] --json
--skip-git-repo-check [--model M] -c sandbox_mode=... [-c model_reasoning_effort=...] [-c k=v] -`；
来自 `thread.started` 的线程 ID 会立即保存到命名空间状态文件，因此首个事件后的崩溃可通过显式 ID 恢复。`messages` 输入会被合并为一个 prompt。

通用 driver：`argv`（列表，无 shell）；通过 stdin 接收 payload 以及 `DATAFARM_SESSION_DIR`、`DATAFARM_AGENT_ROLE`、`DATAFARM_AGENT_SESSION_KEY`、`DATAFARM_AGENT_IDENTITY_RECEIPT`（有预算时还有成本变量）；配合 `session_identity_protocol: datafarm.agent-identity/1` 时它会写入 `identity.json`。[`examples/runtime/agent_cli_driver.py`](../../examples/runtime/agent_cli_driver.py) 为 Claude Code 和 Pi 实现了这一点。

## 入口

```bash
python tools/pipeline_agent.py prepare --episode /data/ep.mp4 --request-file /data/request.txt --output /data/task --target-count 100
python tools/pipeline_agent.py context --work /data/task --output /data/task-ctx
python tools/pipeline_agent.py drive --work /data/task --driver /data/driver.json --max-sessions 4
python tools/pipeline_agent.py supervise --work /data/task --config examples/runtime/native-codex.json
python tools/pipeline_agent.py stop --work /data/task --reason "pause"
python tools/pipeline_agent.py runtime-status --work /data/task
python tools/pipeline_agent.py verify --work /data/task
python tools/pipeline_agent.py cost-status --work /data/task
python tools/pipeline_agent.py wait --work /data/task --timeout 600
python tools/pipeline_agent.py feedback --work /data/task --file /data/event.json
```

配置是 JSON，不做 shell 插值，也不内嵌凭据。[`examples/runtime/native-codex.json`](../../examples/runtime/native-codex.json) 是一个通用模板。

`supervise` 应通过 `tools/datafarm_runs.py launch` 以 detached 方式启动。退出码：0；`failed`、`timeout`、`needs-input`、`session-budget-exhausted`、`cost-budget-blocked`、`recovery-required`、`stopped`、`stop-failed`、`error` 为 75；被拒绝的输入或校验错误为 2。

## 配置

| 键 / flag | 默认值 | 来源 |
|---|---|---|
| runtime `schema` | `datafarm.agent-runtime/1`（必需） | `agent/runtime.py::load_config` |
| `max_sessions`、`timeout`、`poll_seconds`、`heartbeat_seconds` | 16、1800 s、5 s、10 s | `load_config` |
| `max_retries`、`backoff_seconds`、`backoff_max_seconds` | 3、5 s、120 s | `load_config` |
| `driver`（必需）、`working_directory`、`log_directory`、`status_output{path, task_id}` | 无 | `load_config`、`drive`、`supervise` |
| driver `type: codex` 选项 | `executable` 为 codex、`sandbox` 为 workspace-write（或 read-only、danger-full-access）、`model`、`reasoning_effort`、`settings`（`features.*`/`model_*`/`service_tier*`，标量） | `native_codex.py::command` |
| driver `input` | `messages`（或 `text`） | `drive` |
| driver `cost_control` | enforced 时必需：`protocol`、`includes_descendants: true`、`max_session_usd > 0` | `drive` |
| `drive --max-sessions`、`--timeout` | 1、1800 s | `pipeline_agent.py::main` |
| `prepare --stop-after`、`--cost-policy`、`--reference` | `render`、`configs/task-cost-policy.json`（advisory）、无 | `main` |
| `wait --timeout`、`--interval` | 1800 s、5 s | `main` |

字段说明：

- `max_sessions` 是由作者、评审和失败重试共享的累计上限，重启不会重置它。`timeout` 按每次 driver 运行生效。`poll_seconds` 检查外部状态且不调用模型。
- `working_directory` 是模型工具运行的位置；任务身份和输出目录仍留在任务中。`log_directory` 按会话 ID 存储日志，并在 sessions 目录中保留一个链接。
- `status_output`（`{"path": ..., "task_id": ...}`）原子地将该 supervisor 的状态合并到共享监控文件。`last_substantive_at` 和 `substantive_sha256` 排除心跳时间戳。
- 若 `codex` driver 省略了模型，则套用部署配置。
- 运行时配置变更会追加到 `config_history`；会话和重试计数会保留。

## 失败行为

- 启动前拒绝：`prepare` 输出目录非空、输入中存在类似凭据的目录、输入或固定来源发生变化、无效的状态 status、argv 不是字符串列表、评审使用 argv driver 却缺少身份协议、预算违规。
- 保留：每个会话文件夹、执行记录、transcript、评审请求/报告/完成记录、成本预留与结算、`config_history`。
- 需要操作员介入：`recovery-required`（外部主机、未保存会话 ID 的启动、评审者未返回报告、被中断的派发）；`stop-failed`；无效的 `complete` 或没有问题的 `needs-input`（抛出；`supervise` 记录 `status: error`）；非瞬时的 `failed`/`timeout` 或重试耗尽。`supervise --resume` 仅清除显式 stop。

## 限制

- **仅一种原生 driver。** 只内置了 Codex；其他 agent 使用 argv driver 和身份协议，其 provider 证据由 driver 自身提供。示例 CLI driver 每次启动全新会话，且不上报成本。
- **同 UID 进程不是安全边界。** 锁、哈希和身份文件可检测变化；它们无法阻止同一用户的进程写入 workspace 文件。
- **评审者没有写隔离。** 评审者与作者使用相同的 driver 配置（相同的沙箱类型和工作目录）。独立性意味着独立的会话和身份加上内容绑定；写权限并未分离。评审者指令禁止编辑；对 subjects 的编辑会被检测到。
- **运行时管理的判定基于标记。** `runtime_managed` 检查 `.runtime/reviews`、`author.json` 或 `control.json`；仅由带 argv driver 的 `drive` 驱动且无评审请求的 workspace 没有这些标记，因此声明的执行记录仍被接受（标注为 declared）。
- **每个 workspace 单一主机。** 任务锁是 `flock`；PID 存活检查使用 supervisor 主机上的 `/proc`。`boot_id` 被记录但不比较；只有 `host` 决定 `foreign_host`。
- **无界的外部等待。** 处于 `queued`、`running` 或 `unknown` 的 job 会让 `supervise` 无限期等待；`wait` 返回 `wait-timeout` 而不改变状态。
- **不检查 `needs-input` 类别。** 任何具体问题都能通过代码检查。
- **原生 usage 不是成本。** `native-result.json` 的 `cost_usd: null`；有预算的原生会话按未知结算（见 [cost-control.md](cost-control.md)）。
- **瞬时检测基于词法。** 只重试日志中包含容量、限流或类似 429/502/503 文本的 `failed` 会话；超时永远不会被 `supervise` 重试。
- **交付检查是合约检查。** `verify_delivery` 检查绑定、成员关系和评审记录；物理与视觉质量来自它所绑定的评审。

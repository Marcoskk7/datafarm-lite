# Batch supervisor：面向每个 item 尝试的一个有界循环

这个 supervisor 管理执行状态、重试与恢复。它不选择渲染模式，也不判断是否使用了真实参考；参见[模式与真实风格契约](../../prompts/instructions/mode-and-style-contract.md)。早期出现系统性失败会暂停新的提交，且不得通过无限重试或缩小分母来达成目标。

`src/datafarm/batch/supervisor.py` 是任何 batch 的唯一循环控制器：它必须通过启动尝试、轮询这些尝试并验证每个结果，把一组固定的 item（样本、episode、分片）带到被接纳的结果。任务间的差异（如何启动、轮询和验证一个尝试）通过注入提供；控制器负责尝试、seed、预算、存活语义、持久状态与终止状态。

## 1. 它保证什么

- 尝试、崩溃、成本与零进度预算，各自都有对应的终止状态。
- 失败的 lease 查询结果是 `unknown`，绝不是空集合，且 unknown 绝不会触发重新提交。
- 崩溃会以相同的尝试和 seed 重试。突发会退避而非停止。
- 每个 item 在完成时即被验证，系统性失败会提前阻止新的启动。

## 2. 术语

- **Item**：batch 中的一员，由一个字符串 id 标识。item 列表是该 batch 的**固定分母**：它存储在 `state.json` 中，用不同的列表恢复会被拒绝（`DenominatorMismatch`）。每个计数都针对所有 item 报告。
- **Attempt**：对一个 item 使用一个 **seed** 的一次尝试。尝试编号从 1 开始。第 *n* 次尝试的 seed 是确定性的（`seed_for(item, n)`，默认是限定到 int32 的哈希）。
- **Launch / try**：对一次尝试的一次启动。崩溃的尝试会作为*同一个*尝试（相同 seed）以新的 try 编号重新启动；其 launch id 为 `<item>.aNN.tNN`。
- **Finished**：该尝试产生了一个结果。它会立即被验证，并消耗该 item **尝试预算**的一个单位，无论判定结果如何。
- **Verdict**：`pass`（被接纳）、`reject`（附带失败的 `gate`；可选 `final`），或 `abstain`（验证者无法判定；仍不算接纳）。
- **Crash**：该尝试在没有可用结果的情况下结束（进程死亡、结果陈旧或写了一半、启动失败）。崩溃使用单独的**崩溃预算**，从不占用 seed。种类：`transient`（重试）、`provider_down`（暂停一切，不消耗预算）、`permanent`（该请求按原样永远无法成功：`rejected_final`）。
- **Unknown**：控制器无法判断一次尝试是否存活（状态文件不可读、调度器查询失败、其他主机）。unknown 的尝试绝不会被重新启动。
- **Round**：一次尝试的代际。当所有仍处于打开状态的 item 都超过了第 *g* 次尝试时，第 *g* 轮结束。崩溃重试不会开启新一轮。

## 3. 一个 batch 如何运行

主线，一次一个控制 tick（`BatchSupervisor.step`，每隔 `poll_interval_s`）：

1. **计入时间。** 除非 provider 被标记为不可用，否则活动时间会增长；provider 被标记为不可用时，该时间改为计入 provider 暂停。supervisor 停机从不会被计费。
2. **轮询每个在途尝试。** `finished` → 立即验证（流式验证）；`crashed` → 崩溃处理；`running` → 存活；`unknown` → 咨询可选的 lease 探针：`alive` → running，`dead`（连续 `lost_confirmations` 次轮询）→ 崩溃（“lost”），否则保持 unknown。
3. **应用判定。** `pass` → `admitted`。`reject`/`abstain` → 若尝试预算与 `max_rounds` 允许，则以新 seed 进入下一次尝试，否则 `exhausted`；`final` 的 reject → `rejected_final`。
4. **结束轮次并检查守卫**：零进度、系统性失败、成本上限、墙钟预算、unknown 存活上界、provider 暂停上界。
5. **决定状态。** 请求停止会排空在途尝试（不再启动新的），随后以 `stopped_*` 结束；暂停则立即以 `paused_*` 结束。若无在途项且无可启动项，batch 为 `complete`（全部被接纳）或 `complete_with_gaps`。
6. **启动**到期 item（尝试编号最低者优先，因此每个 item 的第一次尝试都会在任何人第二次尝试之前发出），上限为 `max_concurrency`，除非正在执行崩溃突发退避或 provider 中断。每次启动前，先把意图（item、attempt、seed、launch id）写入 `state.json`；返回的 handle 紧随其后写入。

示例，一个 item：第 1 次尝试（seed s1）在 carrier 上传期间崩溃 → 在 `crash_retry_delay_s` 之后作为第 1 次尝试再次启动（seed s1，try 2）→ 完成 → topology gate 将其拒绝 → 第 2 次尝试（seed s2）→ 完成 → 通过 → `admitted`，`attempts_used = 2`、`crash_retries = 1`。

## 4. 策略

`BatchPolicy` 是一个 frozen dataclass；`BatchPolicy.from_dict` 会拒绝未知键，因此拼写错误会失败而不是静默使用默认值。其 SHA-256 存储在状态中；恢复时策略发生变化会记录为 `policy_changed` 事件。`configs/batch/template.json` 是一个起点。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `max_attempts_per_item` | 8 | 每个 item 已完成的尝试数（seeds）。 |
| `max_crash_retries_per_item` | 8 | 每个 item 在 transient 崩溃后的重新启动次数；seed 保留。 |
| `max_concurrency` | 10 | 在途尝试数，包含 unknown 的尝试。 |
| `max_rounds` | 12 | 尝试编号的硬上限，在 `reopen` 之后也适用。 |
| `zero_progress_rounds` | 3 | 连续这么多轮结束都没有新 item 被接纳时停止（0 = 关闭）。 |
| `max_total_cost`, `launch_cost_estimate` | off, 0 | 在记录的成本上限处停止；有估算值时，启动前预留它。provider 仍可能收取超过估算值的费用。 |
| `max_wall_seconds` | off | 活动墙钟预算（不含 provider 暂停）。 |
| `crash_retry_delay_s`, `..._cap_s` | 30, 900 | 每个 item 崩溃重试前的延迟，每次崩溃翻倍。 |
| `crash_burst_items`, `crash_burst_window_s` | 5, 600 | 窗口内有这么多*不同* item 崩溃即为一次突发（0 = 关闭）。单个 item 崩溃循环不算突发：由它自己的延迟与预算处理。 |
| `burst_backoff_s`, `..._cap_s` | 60, 1800 | 一次突发后全局暂停启动，窗口内突发反复出现时翻倍；启动会自行恢复。 |
| `provider_probe_interval_s` | 120 | provider 不可用期间：探测频率（`provider_probe()`，或一次 half-open 启动）。 |
| `max_provider_pause_s` | 21600 | 在 `paused_provider_down` 之前允许的累计 provider 暂停时间。 |
| `max_consecutive_unknown` | 20 | 单个尝试存活状态为 unknown 的轮询次数，超过则 `paused_unknown_liveness`。 |
| `lost_confirmations` | 2 | 连续 dead-lease 探测（无正向存活性）次数，达到即判定尝试丢失。 |
| `systematic_min_items`, `systematic_window`, `systematic_share` | 8, 20, 0.6 | 在最近 20 个不同 item 的最新判定上（一旦存在 ≥ 8 个），某个 gate 失败 ≥ 60% 即停止新启动（0 = 关闭）。 |
| `max_verify_errors` | 3 | 单个结果上的验证器异常次数，超过即计为 `abstain`（gate `verifier_error`）。 |
| `poll_interval_s`, `max_tick_gap_s` | 30, 600 | tick 周期；更长的间隔（supervisor 停机）不计入预算。 |
| `drain_on_stop` | true | 在 `stopped_*` 状态前，允许在途尝试完成并被验证。 |
| `seed_base`, `seed_modulus` | 0, 2147483647 | 默认 seed；modulus 使其保持在 provider 强制执行的 int32 范围内。 |

## 5. 状态与状态码

Item 状态：`pending`、`launching`（意图已持久化，结果尚不可知）、`running`、`unknown`、`crashed_retryable`（下次使用相同 attempt/seed）、`rejected_retryable`（下次进入下一次尝试），以及终止状态 `admitted`、`rejected_final`、`exhausted`（原因 `attempt_budget`、`crash_budget` 或 `max_rounds`）。

Batch 状态（`running`、排空期间的 `stopping`，随后是一个终止状态）：

| 状态 | 何时 | 操作者动作 |
|---|---|---|
| `complete` | 每个 item 都被接纳 | 无 |
| `complete_with_gaps` | 已无可尝试项；部分 item exhausted 或被最终拒绝 | 检查 `gaps`；修复后 `reopen` 这些 item |
| `stopped_zero_progress` | `zero_progress_rounds` 轮结束都没有新的接纳 | 找到原因，然后 `acknowledge` |
| `stopped_systematic_failure` | 某个 gate 在最近窗口内失败 ≥ `systematic_share`；报告列出 gate、占比与失败 item | 修复 gate/prompt/渲染器，然后 `acknowledge` |
| `stopped_cost_cap` / `stopped_wall_budget` | 达到预算（墙钟不含 provider 暂停） | 在策略中提高预算，`acknowledge` |
| `stopped_operator` | 请求停止（`request_stop`、CLI `stop`/STOP 文件） | `acknowledge` |
| `paused_provider_down` | provider 不可用累计超过 `max_provider_pause_s` | 恢复后 `acknowledge` |
| `paused_unknown_liveness` | 某个尝试的存活状态在 `max_consecutive_unknown` 次轮询内均为 unknown | 手工检查；`acknowledge`，如需要 `resolve_unknown(item, not_launched \| crashed)` |
| `paused_cost_unknown` | 配置了成本追踪或启用了成本上限时，某已完成尝试的成本证据缺失或无效 | 恢复成本证据，然后 `acknowledge`；验证前会重新读取同一结果，不会发起新的 provider 请求 |

已停止或暂停的 batch 在重启后仍保持停止，直到有人调用 `acknowledge`（连击与系统性窗口重新开始；预算不会）。其他操作者动作：`reopen(items, extra_attempts)` 用于 exhausted/final 的 item；当后续 cohort 级验证者撤回某个接纳时用 `revoke_admission(item)`；对 batch 建立之前就已接纳的 item 用 `adopt_admitted(item)`。

## 6. 持久状态与恢复

`<state_dir>/`：

- `state.json`：整个控制器状态，在每次变更时以及至少每隔 `heartbeat_s`（一个存活信号）原子写入（临时文件、fsync、rename、目录 fsync）。
- `events.jsonl`：仅追加的历史（`launch_intent`、`launched`、`verdict`、`crashed`、`crash_burst_backoff`、`provider_down/up`、`round_closed`、`stop_requested`、`terminal` 等）。
- `report.json`：在每个终止状态写入的摘要。
- `supervisor.lock`：排他 `flock`；同一目录上的第二个 supervisor 会以 `SupervisorBusy` 失败，而不是与第一个竞争。

恢复绝不会启动两次。意图在 `launch` 运行之前就已落盘，因此 supervisor 崩溃后，某个 item 可能以 `launching` 状态存在却缺少 handle。在第一个 tick，可选的 `reconcile(item, attempt, seed, launch_id)` 会查找该启动（对 subprocess adapter：即尝试目录）。找到 → 它就像任何 running 尝试一样被轮询。确认未启动 → 以相同的 attempt 和 seed 启动，不消耗任何预算。unknown，或没有 `reconcile` → 该尝试保持 unknown，绝不会被重新启动，batch 在 `max_consecutive_unknown` 个 tick 后暂停以等待操作者决策。带有已持久化 handle 的尝试则只是再次轮询。由在收尾时被杀掉的 runner 留下的临时判定，会被当作对相同 attempt 和 seed 的崩溃重试处理。

## 7. Run ledger

当 supervisor 在 `tools/datafarm_runs.py launch` 下运行（环境变量 `DATAFARM_LEDGER_RUN_DIR` 与 `DATAFARM_LEDGER_WORKER`），且配置了 ledger 列（`ledger_column`，或任务配置中的 `ledger.column`）时，每个 item 的变更都会通过 ledger 的公共 API（`attach_from_env`、`RunLedger.set_cells`）镜像：running 状态 → `running`；被拒绝但仍有重试 → `rejected`（失败为 `gate:reason`）；被接纳 → `passed`（artifact = 判定证据）；exhausted 或 final → `failed`。`stopped_*`/`paused_*` 状态被宣告为 `blocked` 并附原因。该列从不猜测：没有配置时，会写入 `ledger_not_mirrored` 事件。ledger 错误会记录为事件，从而绝不会中断 batch。

## 8. 通用 CLI：把 supervisor 变成一个配置

`tools/supervise_batch.py` 通过 `src/datafarm/batch/subprocess_task.py` 驱动控制器。每个尝试是一条命令，经 `batch_exec.py` 在其自己的会话和目录 `<work_dir>/attempts/<launch_id>/`（`attempt.json`、`handle.json`、`started.json`、`exit.json`、`log.txt`）中运行，因此重启后的 supervisor 即使不是该尝试的父进程，也能区分 finished、crashed 与 running。命令：

```text
tools/supervise_batch.py validate CONFIG        # check config and templates, print the first launch
tools/supervise_batch.py run CONFIG             # run to a terminal status; resumable
tools/supervise_batch.py status CONFIG [-v]     # read-only summary, no lock
tools/supervise_batch.py stop CONFIG --reason R # a running supervisor stops launching and drains
tools/supervise_batch.py acknowledge CONFIG --reason R
tools/supervise_batch.py reopen CONFIG --items A,B --reason R [--extra-attempts N]
tools/supervise_batch.py revoke CONFIG --items A,B --reason R [--gate dense_audit]
tools/supervise_batch.py resolve CONFIG --item A --outcome not_launched|crashed --reason R
```

`run` 的退出码：0（`complete`）、3（`complete_with_gaps`）、4（`stopped_*`）、5（`paused_*`）。

任务配置（schema `datafarm.batch-task-config/1.0.0`，拒绝未知键）包含：`items` 或 `items_file`，可选的每 item 变量 `item_vars`/`item_vars_file`，`work_dir`，`launch` 命令模板，`result` 契约（`path`、`required_keys`、针对尝试的 `match`，以及用于临时状态的 `incomplete_values`），`verify`（`result_status` 映射，或一条打印 `{"decision", "reason", "gate"}` 的验证器命令），`crash_classes`（对日志尾部匹配的正则 → `transient`/`provider_down`/`permanent`），可选的 `lease_probe`、`provider_probe`（退出 0 表示 up，退出 75 表示 down）、`unresolved_submission_marker`、`cost`、`seed`（`hash` 或 `linear`）、`ledger.column` 与 `policy`。模板字段：`{item} {item_index} {attempt} {seed} {launch_id}
{attempt_dir} {work_dir} {result_path} {vars[name]}`。

验证器命令必须成功退出，其 JSON 判定才会被接受。如果它打印了一条 `pass` 记录随后以非零码退出，supervisor 会记录一个验证器错误，并在 `max_verify_errors` 下重试或 abstain；该 item 绝不会凭这段部分输出被接纳。

`launch.timeout_s` 限制单条本地尝试命令。超时时 wrapper 会停止该命令的进程组，并写入 `exit.json`，其中 `timed_out: true`。supervisor 把该尝试当作 unknown，并在 `max_consecutive_unknown` 下暂停；它不会自动再花费一个 seed，也不会重新提交一个可能仍在运行的远程 provider 任务。操作者必须先核对任何远程任务 ID 与结果，才能用 `resolve` 和 `acknowledge` 继续。

提交到 GPU 调度器的 adapter 应实现 `lease_probe`，使失败的 `gpu-scheduler leases` 查询返回 `unknown`（绝不是空 lease 集），并按作业名实现 `reconcile`，因为提交可能在作业已排队之后才超时。

对于提交远程付费工作的命令，将 `unresolved_submission_marker` 设为一个位于每个尝试目录内的文件名。命令在发送可能产生远程影响的请求**之前**写入该标记，并且只有在持久化了一个确定的任务 ID 或证明未创建任何任务之后才移除它。如果命令退出或消失而标记仍在，supervisor 会报告 `unknown` 并暂停，不再提交。`dead` 的 lease 探针无法把一个无法识别的远程提交变成安全的重试。在使用 `resolve` 之前，先核对 provider 的任务列表与存储的请求证据；仅凭本地进程退出是不够的。

### 每个 item 的上游就绪

`upstream_batch_state` 可选地指定另一个通用 batch 状态 JSON。subprocess adapter 会把每个 item 推迟到同一个 ID 在上游被接纳之后，才创建尝试目录或提交工作。被推迟的 item 不消耗尝试或崩溃预算，也不会阻塞其后面的就绪 item。worker 仍会验证源结果及其 artifact 哈希；就绪并非接纳证书。缺失/不可读的状态按等待处理。这支持流式地推进相互依赖的阶段，而不会把一个未完成的上游 item 解释为 provider 中断。

### 容量拒绝

`crash_classes` 可以把一次显式的分配器拒绝（例如 `no matching free GPU found`）映射为 `capacity`。仅在分配器确认未提交任何作业时才使用。此类等待保持相同 seed，不消耗质量/崩溃预算，把 provider 探测间隔用作每 item 的退避，且不会打开全局 provider 熔断。unknown 的提交仍必须核对。

## 9. 局限

- `launch.timeout_s` 只限制一条本地尝试命令。控制器本身没有每尝试超时：一个报告 `running` 却挂起的尝试会一直占着它的槽位，因为在未取消的情况下重新启动会把同一 seed 跑两次。
- 新的验证器版本不会重新应用到已判定的结果上；对受影响的 item 执行 `reopen` 或 `revoke`。
- 重新提交的安全性取决于 adapter。按已记录的 ID 恢复付费作业，只有在 adapter 提供了 `reconcile` 或 `unresolved_submission_marker` 时才成立。没有 `reconcile` 时，抛出异常的 launcher 被假定为未启动任何东西，该尝试会被重新启动。
- 即使远程进程在一台不可达主机上仍然存活，只要经过 `lost_confirmations` 次探测，dead lease 仍可能触发重新启动，除非使用了提交标记。
- subprocess 尝试的存活状态仅在 supervisor 所在主机上探测；迁移到另一台主机后，在它们的退出记录出现之前都是 `unknown`。

代码：`src/datafarm/batch/`、`tools/supervise_batch.py`。

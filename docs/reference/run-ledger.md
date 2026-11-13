# Pipeline 运行台账

## 它记录什么

台账为每次被请求的调用赋予一个 **pipeline run**，带有固定的 item 分母和一个**推导而来**而非自报的状态。操作员可能要求一次完整通过（“生成 white episodes 直到训练好的 checkpoint”），或一次部分通过（“把这个 cohort 从 render 带到 admission”），而 supervisor 自己的 `running` 状态可能比它的进程存活得更久。

| 问题 | 答案来自何处 |
|---|---|
| 请求了什么、由谁请求、涉及哪些 item 和阶段？ | `run.json`（创建时固定） |
| 哪个 item 停在哪个阶段，为什么？ | `matrix.json`（item × 阶段/子阶段单元格） |
| 哪些进程/GPU 租约在处理它，它们是否存活？ | `workers.json` + 实时 PID/租约探测 + 进度文件年龄 |
| 按顺序发生了什么？ | `events.jsonl`（仅追加） |

run 位于 `runs/ledger/<run-id>/` 下（与所有 run 输出一样被 git 忽略）。Run ID 为本地时间下的 `PRUN-YYYYMMDD-HHMM-<slug>`。

## 阶段：两个层级

列是 pipeline 阶段，按顺序：
`grounding → task → episode → render → admission → training`。一次 run 覆盖一个
连续范围（`--from render --to admission`）。任何阶段都可以拆分为
adapter 专属的子阶段（`--substages render=global-style,guided-render,verify`），
这些子阶段成为列 `render/global-style`、… 。最后一列是*出口列*。
只有当一个 item **通过**了该列，它才算完成。

单元格状态：`pending`、`running`、`passed`、`rejected`（verifier 说不；仍可能被修复）、`failed`（放弃）、`skipped`。

## Run 状态（由 `status` 推导）

| 状态 | 含义 |
|---|---|
| `running` | 至少一个 worker 存活且有新鲜进度 |
| `running_stalled` | worker 存活，但在 `stale_seconds`（默认 900）内没有进度文件变化 |
| `lost` | 没有存活的 worker，且某个 worker **没有**退出记录就死亡：意外退出 |
| `exited_with_error` | 某个 worker 记录了非零退出码 |
| `blocked` / `failed` | supervisor 自身声明的（例如残留的拒绝需要修复） |
| `idle_incomplete` | 所有 worker 都干净结束，但仍有 item 未通过 |
| `idle_awaiting_finalize` | 所有 item 都通过，但尚未记录最终验证 |
| `complete` / `complete_with_gaps` | 已用带哈希的验证报告定稿 |
| `superseded` / `cancelled` | 被另一次 run 取代 / 被有意停止 |
| `registered` | 已创建，尚无 worker |

较晚的 worker 注册（relaunch）会覆盖先前的 `blocked`/`failed` 声明。其他主机上的 worker 存活性通过 SSH 探测。无法被探测的进程回退到进度文件年龄。

## 操作员工作流

```bash
cd /path/to/datafarm
# 1. Register the request before launching anything.
tools/datafarm_runs.py create --slug my-task-render100 --title "..." \
  --request "<the operator's request>" --authorization <id> --task-family my-task \
  --from render --to training --substages render=generate,dense-audit \
  --substages training=dataset,prepare,train \
  --items-file cohort-ids.json --cohort-manifest <manifest> --code-root .
# 2. Launch detached through the sentinel: it records PID/SID, heartbeats, and the exit code.
tools/datafarm_runs.py launch PRUN-... --role supervisor -- python tools/<supervisor>.py ...
# 3. Second, independent persistence check in a new shell after a short wait.
tools/datafarm_runs.py check PRUN-...
# Any time:
tools/datafarm_runs.py status            # all open runs
tools/datafarm_runs.py status PRUN-... --gaps -v   # every missing item and where it stopped
```

`launch` 是启动 worker 的、经过持久性检查的方式。它的第一次检查记录为 `persistence_check` event。`check` 记录第二次检查。对部分 item 的修复或部分重跑是一个 **child run**（`--parent PRUN-...`），带有自己的 item 列表，因此父 run 会显示其子 run。

通过 `launch` 启动的 supervisor 会看到 `DATAFARM_LEDGER_RUN_DIR` 和 `DATAFARM_LEDGER_WORKER`。`datafarm.ledger.ledger.attach_from_env()` 让它们直接写入 item 单元格。

人工干预会写入日志：`event --kind note --message ...`、`close-worker RUN WORKER --reason ...`（例如手工替换的进程）、`declare --status blocked|failed|cancelled --reason ...`、`supersede --by ...`，以及 `finalize --verification <report>`（除非 `--allow-gaps`，否则拒绝未通过的 item）。

## 面向其他 supervisor 的原生状态同步

supervisor 可以保有自己的状态文件。一次 run 可以声明 `--sync
module:function@{json args}`. `status` 随后会先调用该函数，由它把原生文件转换为单元格和 worker。不随附任何同步函数；它们属于任务代码。

同步函数只读取原生文件。同步错误会显示在 `status` 中，并且绝不会隐藏报告的其余部分。

## 保证

- item 分母在创建时固定。重复的 ID、空请求以及在分母或列计划之外的更新都会被拒绝。
- run 状态是推导出来的：没有退出记录就消失的 worker 是 `lost`，非零退出是 `exited_with_error`，而 relaunch 会覆盖先前的 `blocked` 或 `failed`。
- `launch` 在 sentinel 下运行 worker，由 sentinel 记录退出码，并记录第一次持久性检查。
- `finalize` 需要已存在的验证文件且没有缺口，除非使用 `--allow-gaps`。

代码：`src/datafarm/ledger/`、`tools/datafarm_runs.py`。

## 已知限制

- 台账不验证输出。它记录 verifier 产生的判定。`passed` 意味着拥有它的 verifier 如此判定。
- 未通过 `launch` 启动的 worker 没有退出码。当其中一个消失时，run 会显示 `lost`，直到原生终态或操作员的 `close-worker` 加以解释。
- 进度陈旧且无法探测的 worker 是 `unknown_stale`，并计为丢失。远程 PID 探测需要 `DATAFARM_SSH_CONFIG`；没有它，远程 worker 回退到进度年龄。
- 台账和 batch 锁使用 `flock`，其在共享文件系统上跨主机的一致性不被保证。

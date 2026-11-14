# 任务专家 harness

harness 是共享的、与任务无关的运行时，编码 agent 在其中针对冻结的 judge 编写任务专家（一个
控制器程序）。它在 OS 沙箱中运行 agent session，亲自通过 broker 执行每次 rollout，
记录每次尝试及其成本，按哈希冻结 judge 与输入，并在轮次结束后在隐藏的留出 seed 上
评估提交的专家。

包：`src/datafarm/expert/`；命令行：`tools/expert_harness.py`；adapter 模板：
`configs/expert-harness/template.json`。编写方法：`.agents/skills/task-expert-author/SKILL.md`。

## 1. 角色与消息

可选的 ASPIRE 式专家开发（动作证据检查、种群修复和经复验的修复技能复用）见 [CaP 搜索](cap-search.md)。它复用同一 broker，保留冻结 judge、隐藏留出集和专家准入机制。

角色 prompt 位于 `prompts/roles/task-expert-author.md`，由 launcher 显式加载。首次、继续和最终 session 消息保持分离，
后端和模型来自本轮 adapter。一轮就是一个程序：它的预算、沙箱和 rollout 都由代码强制执行，每个数字都来自同一份 config，
每个结果都由 harness 而非 agent 记录。

## 2. 一段话概述一轮

操作员（每个任务一次）编写一个**任务 adapter**（从 `configs/expert-harness/template.json` 开始）
和一个 **brief 模板**。`prepare-round` 从模板和
adapter 渲染 brief，对它做 lint（fail closed），把操作员的隐藏 holdout seed 复制到
harness 私有目录，安装 agent 工具 `xh`，并用上一轮的 **handoff bundle** 播种 workspace。
随后 `run-round` 在 **OS 沙箱**中启动 agent CLI（Claude Code 或
Codex，headless），在该沙箱中它只能读取列出的输入并
只能写入自己的 workspace。agent 运行任何东西的唯一途径是 `xh`：`xh rollout` 投递
一个请求，由 harness 验证（开发 seed、并发、预算、专家文件的静态审计）
并转成一个 GPU 作业；该作业在第二个沙箱内运行专家，在沙箱外运行 verifier。harness 记录每次尝试，在每次中断后
续接 agent session，在预算处停下，并在启动时、每个 session 之后以及留出评估之前对 judge、物理和 holdout
文件做哈希。轮次结束后，`eval-holdout` 在隐藏 seed 上运行每个提交（score-vs-cost 曲线），
`handoff` 组装 bundle 和下一份 brief，`library add` 存储一个经验证的
专家。

```text
adapter + template ──prepare-round──> round/ (brief.md, bin/xh, workspace/, harness/ [private])
                                         │
run-round:  agent CLI in sandbox ── xh rollout/submit/flag ──> broker (harness process)
              ▲  resume per session         │ validate, snapshot, audit, record
              │  cost from result event     ▼
              │                         job.sh: probe → sim in sandbox → judge outside
              └── status prompt ◄───── attempts ledger / results / status.json
eval-holdout ─> harness/holdout/results.json (curve)   handoff ─> bundle + next brief
library add ─> content-addressed verified experts      suite ─> pass@k, pass^k, $/verified
```

## 3. 组成部分

| 模块 | 作用 |
|---|---|
| `adapter.py` | 每个任务一份经过验证的 JSON：task/IR、verifier 与确切标准、运行时事实、可读与受保护路径、rollout 与 judge 命令、开发 seed、holdout 文件、预算、agent 后端与模型。绝对路径只出现在这里。 |
| `brief.py` | 从模板 + adapter + handoff 渲染；`lint_brief` 在缺少必需小节时失败（task、interface、带版本的 verifier、每个 judge 点和 “AND”、运行时限制、与 adapter 匹配的预算、tools、可读路径、禁止操作、deliverables、submit protocol、holdout policy、abort 通道），在缺少运行时事实或标准 token 时失败，在可读根目录内的屏蔽路径未被列出时失败，在受保护路径被列为可读时失败，或在出现任何 holdout seed 值时失败。 |
| `isolation.py` | `bwrap`（首选）：对 agent 使用 allowlist 沙箱（仅系统目录、可读输入和轮次文件；workspace、agent home 和 request box 可写），对 rollout 使用 root 只读沙箱；受保护路径被屏蔽；**capabilities 始终被丢弃**（由 root 启动的 bwrap 会保留它们，而 `CAP_SYS_ADMIN` 可以直接 `umount` 掉一个掩码）；可选地通过 `setpriv` 切换到非特权 uid。`unshare -m` 回退方案带相同的掩码（denylist 语义）。在确切沙箱内运行的探测会在任何东西运行之前检查每个掩码（在一次 `umount` 尝试之后）、只读目录和可写目录；一旦泄漏就中止本轮（agent 沙箱）或 rollout（rollout 沙箱，记录为基础设施故障）。 |
| `toolbox.py` + `agent_tool.py`（`xh`） | broker 验证每个请求（仅开发 seed；holdout seed 是记录的违规；并发按活跃 attempt 计数，而非 lease；计入预算包含在途 rollout；infra 上限；墙钟预算；轮次状态），对专家做快照和审计，启动作业，并把启动/结束记录追加到 `ledger/attempts.jsonl`。符号链接和路径逃逸会被拒绝（harness 读取 agent 文件时不跟随链接）。`submit` 对专家做快照（仅确认；当某个专家从未运行时会警告）；`flag-for-human` 干净地结束本轮。 |
| `audit.py` | 对每个 rollout 请求和提交做 AST 审计：语法、类存在、import allowlist、无动态代码或文件 I/O 内建、无内省逃逸口、无模块文件 I/O（`np.load`、`np.save` ...）。必要但不充分。 |
| `rollout.py` | 每次尝试生成一个 `job.sh`：心跳、环境摘要、隔离探测、仿真器**在**沙箱内（run-dir 根只读）、judge **在**沙箱外（verifier 代码对 agent 代码屏蔽）、`harness-job.json` 最后写。存活性来自心跳文件，绝不来自调度器 lease。结果类别：judged / expert error（计入）；invalid start、simulator start-up failure、judge failure、lost、timeout、isolation failure、submit failure、abandoned（infra，不计入）。后端：`command`（`gpu-scheduler submit`）、`local`、`fake`。 |
| `session.py` | Headless session（`claude -p --output-format stream-json --verbose --model M --session-id/--resume`、工具 allowlist、`--max-budget-usd` = 剩余预算；`codex exec --json` / `exec resume` 且 prompt 走 stdin），transcript 流式写入磁盘，按 session 的墙钟上限（SIGTERM，然后 SIGKILL，作用于进程组），从 `result` 事件读取成本/用量/轮数/session id（价格表估算，标记为估算，仅用于被终止的 session），错误分类（API 中断 vs 认证失败 vs 其他）。 |
| `round.py` | `prepare_round`、`RoundRunner.run`：在最终提交、flag、墙钟 / USD / session / 计入 rollout 预算（含一次最终的 “submit now” session）、infra 上限、空闲 session、篡改或致命的 agent 错误时停止；provider 中断按 60→120→240→300 秒退避，超过 10 分钟后暂停本轮（退出码 75，可续接）；中断与停机不计费；每个 session 一条 `datafarm.agent-call/1` 记录，经由 `call_log.append_call`（组件 `expert-harness/session`），包括中止和崩溃的 session；排空在途 rollout；如果 agent 没有做任何提交，自动做最终提交。 |
| `manifest.py` | 在启动时、每个 session 之后以及留出评估之前，计算 judge/verifier 代码、verifier 契约包、runner、物理/材质/生成器文件、holdout seed 列表、adapter、模板、brief 和工具 的 SHA-256。变更会使本轮失效，除非先用 `tools/expert_harness.py amend` 记录。 |
| `holdout.py` | 仅 harness 使用：seed 从秘密偏移抽取（`make-holdout-seeds`），私有存储，在轮次结束后为最终专家和每个提交运行（按摘要去重），备用 seed 在同一 family 内替换无效起点，infra 重试；结果和 score-vs-cost 曲线留在 `harness/` 中。 |
| `handoff.py` | 冻结专家（按摘要）、REPORT/NOTES（不跟随链接）、harness attempt ledger、失败分类、对 agent 安全的 holdout 摘要（仅每 family 计数和失败标签）、成本、本轮检测到的问题；渲染并 lint 下一份 brief。拒绝写入任何 holdout seed 值。 |
| `library.py` | 内容寻址存储，保存以 adapter 阈值通过 holdout 的专家，含任务 id、brief 和 Task-IR 摘要、verifier 版本、holdout 分数、模型与成本；读取时验证摘要；`prepare-round --library L --references N` 检索参考。 |
| `evalsuite.py` | 任务 × 模型 × 重复次数；每个单元就是完整的一轮；按组统计：跨轮次的 pass@k 与 pass^k（无偏估计量）、平均 holdout 通过率、每个已验证专家的成本、墙钟时间、计入 rollout、违规（篡改、隔离、并发、预算、holdout seed 请求和非开发请求、审计和不安全路径拒绝）。没有 adapter 的任务会连同其缺失的接口一起列出。 |

## 4. 一轮中各部分的位置

```text
<round>/round.json brief.md bin/xh handoff-in/ references/     public, read-only for the agent
<round>/workspace/ agent-home/ tmp/ broker/requests/            writable by the agent (and nothing else)
<round>/broker/{responses,results,status.json} rollouts/ submissions/ ledger/   harness-written, agent-readable
<round>/harness/   state, transcripts, manifests, holdout seeds, holdout runs and results   (masked everywhere)
```

把轮次放在 `$EXPERT_HARNESS_HOME/rounds/` 下；`prepare-round` 拒绝位于受保护路径内的
轮次目录。holdout seed 文件应放在
`$EXPERT_HARNESS_HOME/private/`（在 adapter 的 `workspace.protected` 中列出该目录）。

## 5. 运行真实轮次（操作员）

所有命令都在能提交 GPU 作业的主机上、从仓库根目录运行。`DATAFARM_PYTHON` 可以指向任意
Python >= 3.11（harness 只用标准库）。

```bash
cd /path/to/datafarm
export DATAFARM_PYTHON=/path/to/venv/bin/python
# Check that this interpreter exists and meets the version requirement before running.
H=tools/expert_harness.py; A=/data/task/adapter.json    # start from configs/expert-harness/template.json
X=$EXPERT_HARNESS_HOME                                # e.g. /data/expert-harness

# 0. checks that cost nothing
$H lint-brief --adapter $A
$H dry-run                                        # scripted agent + fake simulator, real sandboxes

# 1. hidden holdout seeds (CPU only)
mkdir -p $X/private/my-task
$H make-holdout-seeds --adapter $A --per-family 4 --spares 2 --out $X/private/my-task/holdout.json

# 2. prepare the round (add --handoff <bundle> to continue from an earlier round)
$H prepare-round --adapter $A --round-dir $X/rounds/my-task-1

# 3. the round itself (needs approval: agent API spend and GPU rollouts)
export ANTHROPIC_API_KEY=...   # or the variables your agent CLI uses; passed by name, never logged
nohup $H run-round --round-dir $X/rounds/my-task-1 \
   --approve-agent-spend --approve-gpu > $X/rounds/my-task-1.log 2>&1 &
$H status --round-dir $X/rounds/my-task-1

# 4. after the round (GPU)
$H eval-holdout --round-dir $X/rounds/my-task-1 --approve-gpu
$H handoff --round-dir $X/rounds/my-task-1 --out $X/handoff/my-task-1
$H library add --library $X/library --round-dir $X/rounds/my-task-1
```

操作说明：

* **在第一次实跑轮次之前**，运行一次冒烟 rollout（一个 GPU 作业，需审批）：
  `$H smoke-rollout --adapter $A --expert <expert.py> --class <ExpertClass> --seed 11
  --out $X/smoke/1 --approve-gpu`。它使用与轮次相同的作业模板、沙箱和 judge，并
  打印结果、作业记录和隔离探测。rollout 沙箱把 `/` 设为只读，
  只有列出的缓存可写（对 Isaac Sim：`isaacsim/kit/{data,logs}`、`$HOME/.cache`、
  `.nv`、`.nvidia-omniverse`、`.local/share/ov`）。如果仿真器需要另一个可写路径，把它加到
  `isolation.rollout.writable`；如果某个属于其他用户的文件在没有
  capabilities 时不可读，加入 `"keep_caps": ["CAP_DAC_OVERRIDE"]`（绝不要 `CAP_DAC_READ_SEARCH` 或
  `CAP_SYS_ADMIN`；harness 会拒绝它们）。Isaac 的 NVIDIA 运行时可能还需要一个私有的可写
  `/run/user/0`，以每作业 `tmpfs` 的形式与 `{isaac_kit}/cache` 一起挂载。仿真器输出应
  放在 `native_workdir/native` 下，只有在仿真结束后才移动到 `native`，因为
  仿真器会创建自己的输出目录，如果 `native` 已存在就退出。
* **认证。** agent 在本轮中以 `nobody` 身份运行，拥有自己的 `HOME`；它无法
  读取 `~/.claude`。把凭据作为环境变量传入，并在
  `agent.auth.env_passthrough` 中命名；或以只读方式绑定一个设置文件 `agent.auth.ro_bind`
  （如果文件仅 root 可读，则把 `isolation.agent.run_as` 设为 `null`）。认证失败会在任何工作开始前
  以状态 `error` 停止本轮。
* **暂停。** SIGTERM 或 Ctrl-C 结束当前 session，持久化状态并以 75 退出；
  再次运行 `run-round` 会续接同一对话。停机不计费。超过 10 分钟
  的 provider 中断会以同样方式暂停本轮。如果 harness 进程
  死亡，agent 进程树也随之死亡（`PR_SET_PDEATHSIG`、bwrap `--die-with-parent`）；
  下一次 `run-round` 会用一条 `aborted` agent-call 记录关闭中断的 session。
* **在轮次中途修改受保护文件：** 停止 harness，
  修改文件，运行 `tools/expert_harness.py amend --round-dir R --reason "..."`，然后续接。没有
  该修订，本轮会在下一个 session 边界变成 `invalid_tamper`。
* **Codex。** `prepare-round --backend codex --model <model>`。session 使用
  `codex exec --json` 和 `codex exec resume`；没有列出价格，所以其成本记录为
  未知（不是零），除非 `configs/expert-harness/prices.json` 列出了价格。Codex CLI 没有按 session 的 USD 硬上限，
  所以 `run-round` 会拒绝 adapter 声明了 `max_usd` 的 Codex 轮次；仅有价格表估算
  无法在长 session 中强制硬上限。Codex 轮次需要对未定价支出
  的显式批准，以及 `max_usd: null` 的 adapter，同时保留 session、墙钟和
  rollout 边界。

## 6. 评估套件

```bash
$H suite plan   --suite suite.json --out /data/suite
$H suite run    --suite suite.json --out /data/suite --fake                                   # plumbing only
$H suite run    --suite suite.json --out /data/suite --approve-agent-spend --approve-gpu --only <cell-id>
$H suite report --out /data/suite
```

当某轮的最终专家在隐藏 holdout seed 中至少通过 `verified_pass_rate`（默认 0.5）
的比例时，该轮被视为*已验证*。pass@k = P（k 个独立轮次中至少一个已验证），
pass^k = P（全部 k 个都验证）。在对 brief
模板、prompt、skill、模型或 harness 做任何改动前后，都要重新运行套件。套件可以把没有 adapter 的任务声明为
`unavailable`，列出它们缺失的接口（插件加载器、对记录轨迹的进程外 judge、带种子的随机起点、受保护文件列表、brief 模板）。套件文件是你正在运行的评估的产物；不随附任何套件。

## 7. 审批

harness 中没有任何东西会在未被指示时花费金钱或 GPU 时间：`run-round` 在没有 `--approve-agent-spend` 时拒绝非 fake 的 agent，并拒绝 `command` rollout 后端，除非给出 `--approve-gpu`；`eval-holdout` 和 `suite run` 同理。对照 adapter 的 `budgets`
批准一轮的 agent 支出，对照其 rollout 数量批准 GPU 时间；留出评估会为每个提交和 seed
增加一次 rollout。

## 8. 保证

1. adapter 只有在完整且格式良好时才会加载：schema、slug `task_id`、所有小节
   （`brief`、`verifier`、`seeds`、`budgets`、`agent`、`rollout`、`workspace`、`isolation`、`expert`、
   `holdout`）、verifier 的 `version`/`name`/`judge_points`/`criteria`、已知的 `agent.backend` 且带
   显式的 `agent.model`、rollout 后端字段、隔离模式、`expert.allowed_imports`、绝对
   workspace 路径；`unavailable` 的 adapter 必须列出 `missing_interfaces`。
2. `task_ir` 带有 `profile: "stage-graph/1"` 时在 adapter 加载时验证（阶段带退出
   谓词和恢复、已知角色/谓词、有序同步点、显式容差、
   禁止捷径、skill 证据），并且当给出 `verifier.criterion_ids` 时，每个 judge
   标准都必须被一个 terminal/transient 谓词覆盖，且没有谓词可以引用未知的
   标准。摘要形式的 `task_ir` 不做验证。
3. 留出 seed 必须与开发 seed 不相交；本轮保留一份私有副本（模式
   0600），brief 若未通过 lint（包括提到留出 seed）则被拒绝。
4. broker 只在开发 seed 上运行 rollout；留出或其他 seed 会被拒绝并
   记录为违规。
5. 并发按活跃 attempt 计数，计入 rollout 预算包含在途
   rollout，基础设施失败不计入但有上限，墙钟预算会关闭本轮。
6. 每次 rollout 和提交都把专家作为 workspace 内的常规、非符号链接文件读取，
   通过静态审计（import allowlist、无文件 I/O 或动态代码），并由 SHA-256 快照进
   harness 写入的 ledger。
7. judge 以及一切为判决提供输入的东西都由哈希冻结：verifier 文件、adapter manifest
   文件、私有留出 seed 副本、brief、`xh` 工具、brief 模板、adapter 文件和
   harness 代码摘要会在轮次启动时做哈希，并在每个 session 之后、续接时、结束时以及
   留出评估之前重新检查；任何变更都会把本轮标记为 `invalid_tamper`，除非
   操作员先记录带理由的修订。
8. 已准备的轮次只能用准备它时所用的 adapter 运行（相同的 SHA-256）。
9. agent session 和 rollout 在沙箱中运行：`bwrap`（allowlist 或只读 root）或
   `unshare` 回退；没有可用模式时 harness fail closed；allowlist 规格降级为 `unshare` 需要 `allow_degraded_fallback`；capabilities 被丢弃或进程通过 `setpriv` 切换
   uid；受保护路径和私有 `harness/` 目录被屏蔽；秘密只通过命名环境透传
   到达子进程。模式 `none` 只有在配置时才允许，并
   记录为违规。
10. 留出评估只在本轮无篡改地结束后运行；每个被选提交的快照
    都经过摘要校验，并在每个留出 seed 上用开发作业
    模板和隔离条件全新运行；基础设施失败会重试，无效起点由同 family 的
    备用 seed 替换。
11. 没有显式标志不会开始任何付费工作：非 fake agent 需要 `--approve-agent-spend`，
    `command` rollout 后端需要 `--approve-gpu`，且 `codex` 轮次不得声明 `max_usd`。
12. `codex-metered` 后端要求 `agent.pricing` 命名的模型与 `agent.model` 相同；
    该检查在 session 启动时运行。
13. 只有未篡改轮次的最终专家在留出通过率达到阈值时才
    进入专家库；条目按内容寻址，读取时重新验证；handoff
    bundle 拒绝泄露留出 seed。
14. 对记录关节轨迹的审计（`tools/audit_joint_margin_trace.py <trace.json> --output <report.json>`）会报告
    按臂、按阶段、按关节的裕度以及 1/5/10° 分带，`recorded_trace_passed` 要求该策略的关节目标、所有实际
    关节和所有命令都在限位内、命令完整且没有控制步间隙。任务的采集器写入该轨迹：
    `{"binding": {"physical_joint_limits_rad", "sample_hz", "policy": {"target_joint", "target_margin_deg"}}, "records":
    [{"step", "phase", "left", "right", "commands": {"left": {"qpos_rad"}, "right": {"qpos_rad"}}}]}`。物理限位
    取 URDF 与机器人厂商 SDK 限位的交集，并记录它们的来源。
15. 两个交付 verifier 都要求每个 episode 评审在其
    判决中包含 `joint_margin: pass`。

代码：`src/datafarm/expert/`、`src/datafarm/simulation/joint_margin_audit.py`、`src/datafarm/task/contract.py`。

## 9. 数据模型

- **Adapter**（`datafarm.expert-harness.adapter/1`、`configs/expert-harness/template.json`）：`task_id`、
  `status`、`vars`、`task_ir`、`brief.template`、`verifier`（`version`、`name`、`judge_points` ⊆
  `release|settled|terminal|any`、`criteria`、`files`、可选的 `criterion_ids`）、`seeds.dev`
  （`ranges`、`explicit`；可选的 `families`、`family_command`）、`holdout`（`seeds_file`、`targets`、
  `max_targets`、`max_concurrent`、`retry_infra`、`verified_pass_rate`、`timeout_s`）、`budgets`、
  `agent`（`backend` claude|codex|codex-metered|fake、`model`、codex-metered 用 `pricing`）、`expert`（`default_file`、
  `default_class`、`max_bytes`、`allowed_imports`、`reject_unchanged_rollouts`）、`workspace`
  （`readable`、`protected`）、`isolation.{agent,rollout}`（`mode`、`fallback`、`base`、`run_as`、
  `allow_degraded_fallback`、`extra_masked`）、`rollout`（`backend` command|local|fake、`sim.argv`、
  `judge`、`verdict.file`、`verdict.success`、`submit.argv`）、`manifest.files`、`capabilities`。
- **Stage-graph 任务 IR**（`task_ir.py` 文档字符串）：`demand`、`roles`、`stage_graph.{stages, sync}`、
  `predicates`（`kind` exit|guard|transient|terminal、`expr`、`tolerance`、`judge_criterion`）、
  `forbidden_shortcuts`、`skill_evidence`、可选的 `strategy_space`。
- **轮次目录**（`layout.py` 文档字符串）：公开的 `round.json`、`brief.md`、`bin/xh`；agent 可写的
  `workspace/`、`broker/requests/`；harness 写入、agent 可读的 `broker/responses|results`、
  `rollouts/<attempt>/`、`submissions/<id>/`、`ledger/attempts.jsonl`、`ledger/events.jsonl`、
  `ledger/agent-calls.jsonl`；私有的 `harness/`（状态、transcript、manifest、留出 seed、
  留出运行和 `holdout/results.json`）。
- **Broker schema**：`datafarm.expert-harness.request/1`（`request_id`、`kind` rollout|submit|flag-for-human、
  `args`）、`.response/1`、`.attempt/1`、`.submission/1`、`.status/1`；留出 seed
  `datafarm.expert-harness.holdout-seeds/1`（`seed`、`family`、`spare`）；manifest
  `datafarm.expert-harness.manifest/1`；库条目 `datafarm.expert-harness.library-entry/1`。
- **关节裕度轨迹**：`{binding: {urdf_sha256, physical_joint_limits_rad, planning_joint_limits_rad,
  policy, sample_hz}, records: [{step, phase, left, right, commands}]}`；要求见
  `configs/joint-margin-requirements.json`。

## 10. 配置

- 包装器：`DATAFARM_PYTHON`（默认 `python3`）；`PYTHONPATH` 会把 `src` 前置。
- 预算（必需：`wall_per_session_s`、`total_wall_s`、`max_counted_rollouts`、
  `max_concurrent_rollouts`、`max_sessions`）；默认 `max_usd null`、`max_infra_failures 20`、
  `max_idle_sessions 3`、`final_grace_s 300`、`wait_cap_s 200`（≤ 240）、`warn_before_wall_s 900`。
- 留出默认值：`targets all`、`max_targets 6`、`retry_infra 2`、`max_concurrent 8`、
  `verified_pass_rate 0.5`；`make-holdout-seeds` `--per-family 4 --spares 2 --range-start 100000
  --range-size 1000000`。
- CLI：`run-round --tick 1.0`、`eval-holdout --poll 15`、`--isolation bwrap|unshare|none` 覆盖；
  退出码 75 表示已暂停且可续接。
- 模型与定价没有默认值（`agent.model`；codex-metered 用 `agent.pricing`）。部署
  绑定、调度器和 agent CLI：见[部署](../deployment.md)。

## 11. 失败行为

- 无效 adapter 抛出 `ConfigError`；harness 拒绝抛出 `HarnessError`（CLI 打印 JSON，退出码 1）。
- 被拒绝的请求会得到一个带代码的响应（`not_dev_seed`、`holdout_seed`、`budget`、
  `concurrency`、`audit`、`unsafe_path`、`unchanged_expert`、`round_closed`、...）；违规会
  计入轮次状态。不会删除任何东西：attempt、失败、transcript 和 manifest 都保留。
- 篡改会以 `invalid_tamper` 结束本轮；此后留出评估和库准入
  会被拒绝。恢复需要在变更前有操作员修订，或开启新一轮。
- 基础设施失败上限、失败的隔离探测或认证错误会结束本轮，并需要
  操作员。

## 12. 计量 Codex 后端

`agent.backend: "codex-metered"` 把隔离的作者和 `xh` broker 连接到一个可信的本地 Responses 网关。设置 `agent.model`、`agent.pricing`、`agent.credentials_file`（部署的受保护凭据文件）以及一个有限的正值 `budgets.max_usd`。凭据不会被复制到作者的 home，也不会挂载进它的沙箱。让作者留在 allowlist 沙箱中，并在付费运行前验证实际挂载。

harness 在作者沙箱之外启动网关，并让 CLI 经由它路由。网关串行化请求，在联系 provider 前预留一个保守上界，记录响应 ID 和上游用量，
并在写入 session 回执前排空请求。回执和日志位于受保护的 harness session 目录下。把它们的价格表依据
与供应商账单以及任何协调父 agent 的成本分开报告。作者收到显式的角色 bundle 以及首次、继续和最终 brief，自动项目文档加载被禁用，CLI 通过同一网关和预算在 64,000 token 处压缩上下文。

成本与传输规则：

- 本地被拒绝且无上游调用的请求成本为零。本地 request-bound 拒绝会为检查而结束本轮。
- 缺失上游用量会以 `cost_unknown` 停止本轮；这不是免费重试。上游用量回执是计费权威。
- 网关在通过本地 SSE 流暴露输出项和终止状态之前，先缓冲 provider 的完整 JSON 响应（[事件参考](https://developers.openai.com/api/reference/resources/responses/streaming-events)）。这不是重试，非终止或格式错误的响应会阻止进一步支出。
- 后端跟踪 HTTP 写入是否在 TLS 建立之后开始。只有任何写入之前的失败才以零生成成本结算；失败的写入和缺失的响应保留未知责任。它不记录 header、payload 字节或凭据，也不重试生成请求。
- `agent.provider_receipt_lookup: openai_compatible` 在响应传输异常后启用只读对账：网关查找确切的服务器请求 ID，并要求匹配的模型和可识别的用量 schema。匹配的回执会结算 API 等价成本，但把缺失输出报告为临时服务失败，绝不编造响应。对账在连接或超时错误上最多重试三次；HTTP 错误或不匹配的回执不重试，责任保持未知。
- `agent.upstream_read_timeout_s` 设置 30–900 秒的空闲读取超时（默认 900）。`agent.upstream_stream_max_s` 可选地限制消费流式响应的时间（30–900 秒），在每条到达的 SSE 行上检查。超时后响应关闭，并适用同样的对账。
- 一个证明写入前失败或已对账丢失响应的最终日志事件会标记一次可恢复中断，即使更早的工具调用成功。成本和 rollout 记录仍然计费，恢复会续接对话。对于未计量的 CLI session，只有当该 session 没有发出任何工具调用时，错误文本才算作中断。
- 在操作员审计了一个缺失用量的已终止 session 后，harness 可以把它的完整预留保留在 `bounded_unknown_costs` 中，并只针对其之外的预算续接。已知支出和保留责任分开报告，新的未知 session 仍会停止。

两项操作员控制适用于任何后端：

- `expert.reject_unchanged_rollouts: true` 使 broker 在分配另一个 trial 之前拒绝已完成且计入的 expert/class/seed 组合。`xh rollout --repeat` 允许有意的重复。当专家依赖其源码哈希之外的可变文件时不要开启它。
- 受保护文件 `harness/operator-diagnostic.txt`（UTF-8，最多 8192 字节）会在标准指令之后提供给之后的每个 session，并附私有副本和记录所交付内容的哈希。作者不能写入它，符号链接会被拒绝。绝不要包含凭据、隐藏 seed 或 judge 内部细节。它不改变任务、预算、物理或判决。在比较运行时，把操作员帮助记录为工程贡献。

## 13. 限制

- **无快照搜索。** 诸如 Isaac 粒子布料之类的后端没有保存/恢复或实时查询接口。adapter 模板声明 `capabilities.sim_snapshot_restore: false` 和 `query_state: false`，broker 只提供整段带种子的 rollout。GPU 布料的位精确重放是不可能的；全新的权威重放是 harness 运行的留出评估。
- **进程内篡改无法阻止。** 专家插件在仿真器进程内运行。OS 隔离隐藏文件，但无法阻止代码触及内存中的仿真器对象。静态审计和沙箱外的 verifier 缩小了这一空间，但并未关闭它。
- **网络出口** 未在 OS 层面阻止 agent，因为 CLI 需要其 API；工具 allowlist 排除了 web 工具。
- **Denylist rollout。** rollout 沙箱以只读方式展示主机减去掩码，因此未列出的敏感路径对专家代码可读（对 agent 不可读，因为其沙箱是 allowlist）。保持 `workspace.protected` 和 `isolation.rollout.extra_masked` 完整。
- **成本的时间。** session 的成本在其结束时才可知，因此曲线报告的是做出每个提交的 session 之前及贯穿该 session 的成本。
- **只随附 adapter 模板**（`configs/expert-harness/template.json`）。每个任务的 adapter 是运行的产物。
- **stage-graph profile 和 `criterion_ids` 是可选启用的。** 没有 `criterion_ids` 时不检查标准覆盖。
- **judge 的独立性是一条角色指令，** 在 `prompts/roles/task-designer.md`、`prompts/roles/task-verifier.md` 和编写技能中声明。代码按哈希冻结 adapter 列出的任何 judge 文件。它不检查是谁写的、是否实现了契约，或 `criteria` 文本是否与 verifier 代码一致。
- **生产 episode 不由 harness 产出。** harness 运行留出评估。白模批量采集由任务代码完成，交付 verifier 不检查 harness 轮次 ID、库条目或留出结果。
- **逐 episode 的场景变化是任务代码。** 共享代码提供相机位姿和抓取随机化计划（`src/datafarm/simulation/randomization.py`）；桌面高度和倾角在任务的场景采样器中。
- **关节裕度审计假设一种机器人布局：** 在 `left` 和 `right` 双臂上的六个关节以及一个关节目标，轨迹由任务的采集器写入。`configs/joint-margin-requirements.json` 声明自己是一份评审契约，而不是自动准入门禁。交付检查评审者的 `joint_margin` 判决，而不是审计产物。

## 14. 关节裕度

新的或重新规划的机器人数据必须满足 `configs/joint-margin-requirements.json` 中的关节裕度要求，然后才能扩大采集或渲染规模，或声明数据就绪。完整的已执行关节轨迹、按阶段的占用与停留统计，以及经过验证的任务 profile 是必需的评审证据。

# 能力演化

能力层（skills、prompts、harness 代码）可以根据记录的反馈改进，而无需手工编辑正在运行的部署。编码 driver 编辑活动源代码的副本。随后宿主用一个冻结的 evaluator 评估父版本和候选版本，只有当候选在声明的指标上有所改进且没有使任何其他指标回退时才提升它。提升是对活动版本指针的 compare-and-swap。运行中的任务保留其固定的源代码，任何曾经活动过的版本都可以通过回滚恢复。

agent 遵循的规则见 [`prompts/instructions/self-improvement.md`](../../prompts/instructions/self-improvement.md)。

## 保证

1. **反馈只记录一次、有范围且被保留。** Event 按内容幂等，带有适用位置的选择器，可以被取代但不能被删除。再次导入相同的 event ID 不会写入任何内容，同一 ID 而内容不同则是错误。
2. **候选只能更改允许的、未受保护的路径。** 受保护列表（`PROTECTED`，位于 `src/datafarm/evolution/controller.py`）包括 `AGENTS.md`、`prompts/instructions/` 中的规范性规则、standing rules 和 `evaluations/`、expert-harness holdout、隔离与审计模块、准入证书签发器，以及 evolution 包及其 CLI。策略可以添加 `protected_paths`。解释器钩子（`sitecustomize.py`、`usercustomize.py`、`*.pth`）会被拒绝，`src/` 下新的顶层模块必须以 `datafarm_` 开头，因为 evaluator 会把候选的 `src/` 放到 `PYTHONPATH` 上运行。范围在 proposal 时检查一次，并在评估前从存储的源再次检查。
3. **evaluator 按内容冻结。** `freeze_policy` 把 evaluator 资产复制到 store 中，并以摘要命名策略。`check_policy` 在每次使用前重新哈希它们。
4. **提升需要在同一分母上进行配对比较。** 两次评估都必须有效且候选的检查必须通过，`case_ids` 必须相同，任何指标的回退不得超过其容差，且至少一个决定性指标必须改进至少 `min_improvement`。
5. **无自我认证。** driver 只写候选源代码和 proposal。它不能写结果。失败和被拒绝的候选保留在 store 中。
6. **激活是 compare-and-swap，回滚保留 run 的固定。** 父版本不再活动的候选会变为 `stale`。在写入指针与更新状态之间崩溃的情况会被恢复，而不会重新运行评估。回滚只针对曾经活动的版本，已绑定到某版本的任务会保留它。
7. **lessons 只有在被显式评估时才计入。** 测试 lessons 的策略必须设置 `tests_lessons`，并向某个 check 传入 `{experience}`。lessons 只在其选择器范围内注入任务的上下文。

代码：`src/datafarm/evolution/`。

## 数据模型

store 目录（`--store`）包含：

- `events/`：反馈记录；
- `sources/`：内容寻址的源代码快照；
- `versions/`：`{source, parent, lessons, evaluation}`；
- `candidates/`：`datafarm.evolution-candidate/1`：包含 parent、source、events、policy、proposal、lessons、changed_paths；
- `states/`：`proposed`、`evaluating`、`rejected`、`verified`（仅影子评估）、`promoted`、`stale` 或 `evaluation-error`；
- `policies/<digest>/`：冻结的 evaluator 资产；
- `evaluations/`、`activations/`、`active.json`、`cycles/`。

一个评估策略（`datafarm.evolution-policy/1`）包含 `allowed_paths` 和 `protected_paths`、`checks`（`[{id, argv containing {repo} and {report}, timeout}]`）、`metrics`、`evidence_level`（`software`、`simulator`、`new-task` 或 `meta-improvement`）、`assets`（evaluator 文件，相对于策略文件）以及可选的 `tests_lessons`。

## 使用方式

把 store 放在源代码目录之外或 `.datafarm` 内。原始视频、完整输出和模型不进入源代码版本。

```bash
python tools/pipeline_evolve.py --store /data/evolution init
python tools/pipeline_agent.py prepare --episode /data/episode.mp4 --request-file /data/request.txt \
  --target-count 100 --output /data/task --evolution-store /data/evolution --scope /data/task-scope.json
```

scope 是可选的精确匹配选择器，例如 `{"task_family":"my-task","hardware":"my-robot"}`。会自动添加 `run_id`。没有 `--evolution-store` 时，`prepare` 会把被调用的源代码固定在仓库的 `.datafarm/evolution` 中；提供它时，则使用 store 的活动版本。

### 记录反馈

当用户纠正、手工补丁、检查或任务结束时，宿主保存一个 event 并导入它：

```json
{
  "id": "host-session-7-message-12",
  "kind": "correction",
  "statement": "The request did not use the real reference the task requires; check the chain from the contract to the request.",
  "source": {"actor": "user", "ref": "session-7/message-12"},
  "details": {"request_receipt": "/data/task/request-receipt.json"}
}
```

```bash
python tools/pipeline_agent.py feedback --work /data/task --file /data/feedback.json
python tools/pipeline_evolve.py --store /data/evolution status
python tools/pipeline_evolve.py --store /data/evolution record-change \
  --statement "Fix the reproduced empty-input failure" --source-ref session-7/manual-fix \
  --evidence /data/test-output.txt
```

`record-change` 将活动父版本与当前源代码比较，并保存前后快照及变更路径。它不会提升。使用 `--parent` 为未活动的开发分支记录修复。任务会话会记录它们自己的结果。

### Driver 与 evaluator

driver 使用与任务入口点相同的 argv 和 stdin 形式，并在候选源代码副本中运行：

```json
{"argv": ["/opt/your-agent", "run"], "input": "messages"}
```

评估策略及其代码和数据会先被冻结到 store 中，因此候选无法通过重写自己的测试来替换它们。下面的 `check.py` 由部署提供，并针对真实改进目标计算指标。`argv` 不会由 shell 解释，且不得包含凭据。

```json
{
  "schema": "datafarm.evolution-policy/1",
  "evidence_level": "software",
  "allowed_paths": ["src/", "tools/", "tests/", ".agents/skills/", "prompts/"],
  "assets": ["check.py", "cases.json"],
  "checks": [{
    "id": "replay",
    "argv": ["python", "{suite}/check.py", "--source", "{repo}", "--output", "{report}"],
    "timeout": 120
  }],
  "metrics": {"replay.correct_cases": "max", "replay.failed_cases": "min"}
}
```

指标规则是 `min`、`max`，或诸如 `{"direction": "max", "min_improvement": 0.10}` 或 `{"direction": "min", "tolerance": 0.10, "relative": true, "decisive": false}` 这样的噪声感知对象。`tolerance` 是允许的最大回退（默认 0），`relative` 按父值缩放它（默认 false），`min_improvement` 是计入的最小增益（默认 0），而 `decisive: false` 的指标只用于防止回退。对于有噪声的评估，先通过让父版本与自身比较来确定这些阈值。

check 退出码为 0 表示测量完成，而 `passed: false` 的报告表示测到了失败。崩溃不是有效的基线失败。报告为 `{"passed":true,"case_ids":["a","b"],"metrics":{...}}`：case ID 唯一，两个版本运行相同的 case，指标是有限数。列出每一个不得回退的重要指标。

`evaluations/evolution-software/policy.json` 是一个可运行的确定性示例。它只验证软件行为。

### 运行一个 cycle

```bash
python tools/pipeline_evolve.py --store /data/evolution cycle \
  --driver /data/improver-driver.json --policy /data/evaluation/policy.json \
  --max-candidates 1 --timeout 600
python tools/pipeline_evolve.py --store /data/evolution evaluate \
  --candidate CANDIDATE_ID --timeout 600 --shadow
python tools/pipeline_evolve.py --store /data/evolution rollback --version PREVIOUS_VERSION
```

`cycle` 冻结策略、复制活动源代码、向 driver 提供带有待处理反馈的 `pipeline-improver` 角色上下文、读取 proposal 并快照候选、评估父版本与候选，并在 `compare` 接受时提升。`--timeout` 是整个 cycle 的 wall budget（实际耗时预算）。默认它不会重新处理已经产生候选的反馈；用 `--event EVENT_ID` 选择它。重启之后，`status` 显示它停在哪里，而 `evaluate` 会创建新的评估而不覆盖结果。

`pipeline_agent.py drive --evolution-driver D --evolution-policy P --evolution-timeout T` 在任务之后运行一个有界的 cycle。对于任何不是 `disabled` 的有预算任务，它都会被拒绝。它是一个前台控制器；长时间运行必须由部署服务托管。

## 失败行为

- 范围外的更改、被篡改的策略或候选、或改变的分母会被拒绝并记录。活动版本不变。
- 超时或 driver 失败会结束 cycle 并保留其日志。超时约束本地进程组；它不会撤销远程付费作业。
- 运行中的任务永远不会切换到新提升的版本。`rollback` 不会更改现有 run、删除输出或重置工作树。

## 限制

- **每个 arm 只有一次 run。** 提升比较一次基线 run 与一次候选 run，控制器中没有重复或置信区间。当声称高于 `software` 级别的证据时，重复评估必须作为策略 checks 的一部分。
- **范围基于路径。** 受保护路径列表无法阻止候选通过一个受保护模块导入的允许模块来改变行为。
- **同 UID 信任。** driver 与 store 以同一用户运行。候选代码可以读取 evaluator 传给它的文件，因此隐藏用例和 holdout 必须由独立身份或沙箱保护。
- **提升不是真实任务改进的证据。** 它只表明冻结的 evaluator 在策略的 `evidence_level` 上测得了改进，仅此而已。底层模型权重并未被训练。

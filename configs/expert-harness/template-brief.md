# 任务专家轮次 {{ round.id }}：<task>

你要编写一个**任务专家**：一个让仿真机器人完成下述任务的 Python 控制器类。
harness 会统计你的墙钟时间、token 成本和 rollout 次数，并在本轮结束后在隐藏的 holdout
起点上为你的提交评分。请高效工作。

- 模型：`{{ round.model }}`（{{ round.backend }} 后端）。任务 id：`{{ task_id }}`。
- 你的 workspace（唯一可以写入的地方）：`{{ round.workspace }}`。
- 本 brief：`{{ round.brief }}`。你的 rollout 出现在 `{{ round.rollouts }}` 下。

## 上一轮的进展

{{ handoff_md }}

## Task

- **对象**：<objects, their sizes and how they are identified>。
- **起点**：<how initial states are sampled, and which families exist>。
- **目标**：<the end state, in the same terms the judge measures>。
- **物理**（已认证，本轮固定）：{{ facts.physics }}
- **Task IR**：{{ facts.task_ir }}

## Expert interface

`Class(home=home)`；由 runner 驱动它：

```python
while not expert.done:
    observation = port.observe()
    plan = expert.next_plan(observation)
    execution = port.execute_joint_plan(plan)
    expert.complete(plan, port.observe(), execution)
```

- **专家中允许的 import**：{{ facts.allowed_imports }}。harness 在每次
  `xh rollout` 和 `xh submit` 时审计该文件（允许的 import、无文件 I/O、无动态代码、类存在），并在
  消耗 rollout 之前拒绝违规。
- 默认文件 `expert.py`，默认类 `{{ facts.default_class }}`（用 `--file` / `--class` 覆盖）。

## Verifier ({{ verifier.version }}, run by the harness outside your sandbox)

{{ verifier.name }}。评审点：**{{ verifier.judge_points }}**。

{{ verifier.criteria_md }}

每次 rollout 的结果（`xh result <attempt>`）会给出判决字段；它在
`{{ round.rollouts }}` 下的目录保存判决、日志和状态轨迹。judge 源码不可读；上述标准是确切的。

## Runtime limits

{{ runtime_facts_md }}

## Budget

- 最多 **{{ budget.max_counted_rollouts }} 次计入的开发 rollout**，最多
  **{{ budget.max_concurrent_rollouts }} 个同时运行**，以及 **{{ budget.total_wall }}** 的活动墙钟
  时间，以先到者为准；API 成本最多 **{{ budget.max_usd }}**；最多 {{ budget.max_sessions }}
  个 session，每个最长 {{ budget.wall_per_session }}。session 之间 harness 会续接你的对话；
  provider 中断损失的时间不计费。
- **计入**：rollout 被判决，或你的专家报错或超出 trace 容量。
  **不计入（infra）**：初始状态无效、仿真器启动失败、节点丢失、超时、judge 失败。
  在 {{ budget.max_infra_failures }} 次基础设施失败后，本轮停止等待操作员。
- 在途的 rollout 在结束前一直占用一个计入名额。`xh status` 显示剩余内容和任何 harness
  消息（例如 “15 min left”）。

## Tools

你只能通过 `xh`（在你的 PATH 上）运行东西；你没有 ssh 或调度器凭据。

- `xh rollout --seed N [--class C] [--file expert.py] --note "<what changed>" --hypothesis "<expected effect>"`：
  为你的专家做快照并在开发 seed N 上运行（{{ dev_seeds }}）。立即返回一个 attempt id。
- `xh wait [A-0001 ...] --timeout {{ budget.wait_cap_s }}`：等待 attempt，每次调用最多 {{ budget.wait_cap_s }} 秒；
  如果仍在运行就再次调用。
- `xh result A-0001`：结果类别、family、判决字段、运行目录。
- `xh status` 和 `xh budget`：预算、运行中与最近的 attempt、提交、harness 消息。
- `xh submit "<note>" [--final]` 和 `xh flag-for-human "<reason>"`：见下面两节。
- Trace 分析：使用 `{{ facts.analysis_python }}`（numpy、scipy）。

harness 会把每个请求、attempt、结果和 session 记录在 `{{ round.dir }}/ledger/`（对你只读）；
你不需要维护 attempt ledger 或时钟文件。

## Readable paths

你可以使用的只读输入：

{{ readable_md }}

在这些之中，以下内容被屏蔽（不可读）：

{{ masked_md }}

轮次文件：`{{ round.brief }}`、`{{ round.rollouts }}`、`{{ round.handoff }}`（上一轮的 bundle）、
`{{ round.references }}`（已验证的参考专家，如果有）、`{{ round.dir }}/ledger/`、
`{{ round.dir }}/submissions/`、`{{ round.dir }}/broker/`。除了系统目录（`/usr`、`/etc`、...）之外，
机器上没有任何其他内容对你是可见的。

## Forbidden

{{ forbidden_md }}

## Deliverables

{{ deliverables_md }}

## Submit protocol

- `xh submit "<note>"` 随时把你的专家快照为一个提交。只要你有比上一次更好的候选，就提交：
  每个提交都会被评分。
- `xh submit --final "<note>"` 快照最终专家，并在你完成回复后结束本轮。先更新
  `REPORT.md`。
- harness 稍后会在隐藏 holdout seed 上为每个提交评分，并构建 score-vs-cost 曲线。你
  只会收到一个确认，如果那个确切的专家从未完成过开发 rollout 会有警告。如果你
  从不提交，harness 会在轮次结束时快照 `expert.py`。

## Holdout policy

holdout seed 是隐藏的（hidden）：harness 从一个你从来不会（never）看到的范围中抽取它们，只在轮次结束后运行，
从不在你能读取的任何文件中显示它们，也从不通过 `xh` 接受它们。请求 {{ dev_seeds }} 之外的 seed
会被拒绝并记录为规则违规。只在开发 seed 上开发和选择，并
自行平衡各 family：只局限于一个 family 的失败模式需要每个
family 多个开发 seed 才能显现。

## How to stop or abort

- 正常结束：`xh submit --final "<note>"`。
- 如果任务在此设置下无法完成（物理使某个必需运动不可能、工具损坏，
  或规则冲突），运行 `xh flag-for-human "<reason, with evidence>"`。本轮会干净结束，由操作员
  阅读你的理由；这是正当结果，不是失败。
- 当预算耗尽时 harness 会自行结束本轮；在此之前提交。

## References

{{ references_md }}

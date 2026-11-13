# 记录反馈并改进 pipeline

输入仍然是新任务的真实 episode 加用户目标，输出仍然是经过评审的批量数据。这个工作流把纠正、测试、修复和 session 结果变成带来源的反馈，然后
在一个独立的源码快照中尝试通用能力改进。任务 adapter、场景、阈值和视频 prompt 是由 agent 生成的任务产物。命令、策略格式和存储布局见
`docs/reference/capability-evolution.md`。

## 规则

- `prepare` 固定来源和经验版本，每个 session 都检查来源未被更改。修改工作树不会改变已准备的任务。
- 代码不读取宿主未提供的聊天或私有记忆。宿主通过 `feedback` 传入可见的用户纠正和工具证据。
- 只对确切的用户需求使用 `user-requirement`；对原因的猜测是 `observation`。支持的 kind：user-requirement、correction、observation、code-fix、test-result、session-result、task-result。
- 宿主负责来源的真实性。在 JSON 中写 `actor=user` 不会认证身份。用 `source.ref` 指向消息或工具记录，并把大文件按路径和哈希放进 `details`。
- 未给出范围时，继承当前任务范围。跨任务的显式长期需求以相应范围提交。选择器为精确匹配并做 AND 组合；空范围即全局。
- `supersedes` 替换同一 kind 和 scope 的较早需求。较早的记录会保留。要改变范围，修订原始范围并为新范围提交一个新事件。
- 在工作树中修复代码后，用 `record-change` 捕获真实 diff 和测试日志哈希；不要手写 diff。它不会提升任何东西。

## 提出候选

在候选源码目录中工作。在消息给出的路径写 `proposal.json`，包含 `event_ids`、`hypothesis`、`expected_effect`、`alternative_explanation` 以及可选的 `lessons`。每条 lesson 需要 `problem`、`action`、
`rationale`、`limits` 和 `scope`。绝不把模型报告的分数当作评估器输出。

- 候选只有通过所有检查、使用相同的 case 分母、未使任何声明的指标回退并至少改进一项时才会激活。
- 拒绝、超时和评估错误都会留下记录。
- 候选 lesson 只有经过一次实际运行使用它的 agent 的评估后，才进入活跃经验。读取 JSON 不是行为验证。
- 真实新任务评估从 episode 开始；现有 adapter 不得作为未知任务输入。
- 父级已改变的候选会被重新生成，并在活跃版本上评估。

## 边界

- 通用规范、AGENTS、常设规则、评估策略以及衡量候选的信任根不在自动修改范围内。即使策略允许父目录，候选也不能编辑它们。
- 修正规范或评估器是单独的策略更新，需要独立比较。一个优化候选的分数不能证明评分规则应当改变。
- 存储锁、哈希和独立目录不是针对同一身份下恶意程序的隔离。面对不受信任的候选时，部署会分离评估器、存储、凭据和生产输出。
- 超时约束的是本地进程组；它不会撤销已提交的付费作业。重试时先按现有任务 ID 和运行 ledger 查询。
- 真实新任务的质量和成本收益需要独立的真实运行；软件检查和脚本化 fixture 不支持这些主张。分别报告软件证据、真实任务结果和改进结果。
- 运行时只读取显式指令和选定的经验。保留原始材料、反例和失败，以便其他人或 agent 能够继续。

# Runtime 交接：评审、恢复与外部 job

入口：`pipeline_agent.py supervise --work TASK --config CONFIG`；有界运行使用 `drive --driver DRIVER`。配置字段见 `docs/reference/agent-runtime.md`。Sandbox 权限由 deployment 决定；如果工作目录改变，工具仍必须能够写入 task 目录。

## 独立评审交接

author 原子地更新 `state.json` 中的队列。状态字符串不能自行签署评审结果。

```json
{"status":"working","review_requests":[{
  "id":"contract-001",
  "instructions":"Independently check the contract and the negative controls; keep rejection evidence, do not change the judge.",
  "subjects":[{"path":"task/contract.json","sha256":"actual file SHA256"}]
}]}
```

程序冻结请求和 subject 哈希，并调度一个 task-verifier context 和一个独立会话。reviewer 写 `.runtime/reviews/contract-001/report.json`：`verdict` 为 pass/reject/unverified，`subjects` 与请求完全匹配，`evidence` 是真实文件 path/sha256 条目的列表，`findings` 是具体问题。程序验证报告和证据，并将执行记录、context 哈希和会话绑定到 `completion.json`。

- 未通过的评审返回给 author 修复。新修订使用新的 request ID，早先的拒绝被保留。
- 评审绝不恢复 author 会话。
- 评审已交付的 episode 时，reviewer 还写一个包含 `runtime_review: <request ID>` 的 episode 评审文件，并在报告证据中列出它。交付校验用它确认评审来自独立执行。
- 被评审的输出修改后，从活动队列中移除被取代的请求并添加新 ID。所有历史留在 `.runtime/reviews`。版本化输出避免覆盖评审来源。
- 每次使用已完成的评审时，都会复查其 verdict、subjects、evidence、context、execution、identity 和 log 哈希。过期的评审会被拒绝。
- 这个队列记录交接。它不取代交付所需的语义、物理、图像和来源验证。

同一 UID 的文件不是对恶意 author 的隔离。deployment 的权限边界保护隐藏的 judge/holdout 数据和生产账本。

通用 argv driver 在进入评审队列之前必须声明 `session_identity_protocol: "datafarm.agent-identity/1"`。受信任的 driver 从实际 provider 执行中获取身份和证据，且不得复制环境变量来伪造隔离。host 拒绝复用 author 线程。

## 恢复

重启首先检查记录的进程身份，若其存活则等待。若已死亡且有原生 thread ID，则恢复该 ID。没有 ID 的不明确提交以 `recovery-required` 停止：检查原始日志和外部 ID，绝不自动重新提交。

如果模型写入的 `state.json` 无效，runtime 停止并保存错误；exit 0 不是成功。保存无效文件和一个修复记录，只修复控制元数据，重新校验 JSON，然后恢复原始线程。原始动作、图像和指标不得改变。

不要为重置计数或绕过检查而删除账本、控制文件或锁。`stop` 只结束本地模型进程组；恢复之前应在原始 ID 下检查远程作业，不要重新提交。

## 外部 job

`external_jobs` 和 `model_wait_for` 遵循 `prompts/instructions/task-cost.md`。部署的 job supervisor 监控现有的远程 ID 并原子更新状态。runtime 绝不将未知的远程状态视为已完成，也绝不取消或重新提交远程 task。

`external_jobs` 条目可提供 `status_file`（绝对或相对于 task）。记录必须携带相同的 `id` 和显式状态；不可读或不匹配的记录保持未知。非零 exit 不能变成成功，且 `exported-unqualified` 只会为检查而唤醒模型。没有任何作业会仅凭 heartbeat 或修改时间被提交或推断为完成。workers 原子地写这些记录，并保留实际的 scheduler ID。

`status_output` 将此 supervisor 的状态发布到一个共享 monitor 文件。它是状态发布者，不是通知器或另一个 controller。`completed_episodes` 只统计 host 验证过的交付；producer 报告的进度会被单独标记。

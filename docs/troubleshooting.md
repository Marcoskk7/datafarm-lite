# 故障排查

| 症状 | 检查与恢复 |
|---|---|
| cost-budget-blocked | 运行 `cost-status`。enforced 模式需要真实的上限与完整的 receipt，且原生 Codex driver 会被拒绝。不要删除 ledger 或重写 pin。新任务可以选择 advisory 模式。 |
| 原生美元成本未知 | 检查 `native-result.json` 与 JSONL token 证据。此工具不会为 ChatGPT 登录确认每任务的美元金额，缺失的用量也不是零。 |
| capacity/429/502/503 | `supervise` 施加有界指数退避，并持久化 `retry_at` 与重试次数。达到上限会报告失败，并保留同一 thread ID 与所有尝试。 |
| session-budget-exhausted | 检查累计的 author、review 与重试记录。如有必要，手工在配置中提高总上限，然后启动。不要删除控制状态。 |
| waiting-external-jobs | 检查原始 `external_jobs` ID 的真实状态，job supervisor 会在 state 中更新它。仅凭心跳不会唤醒模型。 |
| waiting-worker | 检查 `runtime-status` 中的进程身份与会话日志。worker 存活期间不要再次启动。 |
| recovery-required | 上一次启动未保存 thread ID。先检查进程、日志与原始 external ID。失败后不要猜测或重新提交。 |
| 锁冲突 | 已存在一个 `drive` 或 `supervise` 控制器。检查真实 PID：文件存在并不意味着锁仍被持有。不要删除文件来绕过锁。 |
| 审查未恢复 | `state.review_requests` 必须有一个唯一 ID、指令以及实际对象的哈希。检查独立审查者的执行及其报告，修订时使用新 ID。 |
| `complete` 被 `verify` 拒绝 | `verify` 需要完全 source-bound 的交付与独立审查。退出 0、模型文本或某个状态字符串都不能证明成功。 |
| GPU/provider 作业在停止后仍在 | `stop` 只停止本地模型组。远程作业会继续按原始 ID 被监控。恢复前检查它们，不要重新提交。 |
| ImportError/numpy/cv2 | 主运行时不需要这些包。完整测试与图像阶段需要在一个装有 Python 3.11 或更新版本的虚拟环境中安装相应的 extras。 |

持续运行需要两次在时间上分开的 ledger 检查，以及 worker 确实存活并在推进的证据。如果日志长时间不前进，应检查 worker 而不是信任某个哨兵。质量验收还需要独立的物理与视觉证据。

当模型写入的 state JSON 无效时，运行时会停止并保存该错误，不会把模型的退出 0 当作成功。先保存无效的原始文件与修复记录，只修复控制元数据并重新校验 JSON，然后恢复原始 thread。原始动作、图像与指标不得被更改。

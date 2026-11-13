# 配置

通用策略文件适用于每个任务。模板会被复制到任务的运行工作区，并由
agent 根据该任务自己的 episode 和请求填写；用户不填写。模板中的数字是用于
展示预期形式的示例，而不是为你的机器人或任务实测的值。

| 文件 | 用途 |
|---|---|
| `agent-instructions.json` | 每个 agent 角色加载哪些指令和 skill |
| `standing-rules.json` | prompt、reviewer 和 verifier spec 必须遵循的规则；`tools/lint_standing_rules.py` 检查那些声明了模式或必需值的规则 |
| `task-cost-policy.json` | 默认的整任务成本策略 |
| `joint-margin-requirements.json` | 接近限位关节占用的审查契约 |
| `external-assets.json` | 外部权重和仓库，带哈希，由 `datafarm doctor` 检查 |
| `batch/template.json` | 批量 supervisor 任务 |
| `expert-harness/template.json`, `template-brief.md` | expert-harness adapter 及其渲染出的 brief |
| `expert-harness/prices.json` | 用于未报告成本的会话的 token 价格 |
| `render-verifier/template.json` | 渲染 verifier spec |
| `training/template.json` | pi0.5 训练运行 |
| `render/render-intent.template.json` | 渲染意图。在 agent 解析其模式与字段之前保持不可编译 |
| `render/overlay.template.json` | 渲染 prompt 的任务级槽位 |
| `render/h3-control-hybrid-v1.json` | 默认 H3 渲染配方 |
| `render/h3-ood-restyle-v1.json` | 对已接受渲染的分布外桌面与光照编辑 |

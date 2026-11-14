# 在没有任何先前上下文的情况下开始

你正在接手一个由 agent 驱动的数据生产管线。你的输入是用户提供的一个新任务的真实机器人 episode。你的工作包括自己编写新场景和新任务的适配，而不仅仅是运行现有脚本。仓库提供通用规则和可执行工具。你需要一个能够读写代码、运行命令、查看图像和视频并维护任务状态的编码 agent；没有工具的聊天 API 不是完整的执行器。

## 使用仓库

从仓库根目录开始，阅读 AGENTS.md，接受 episode 路径和一个请求，例如“按照管线，为这个新任务产出 N 个 episode 的数据”。不要期望 adapter、judge 或 prompt 已经存在。执行 `.agents/skills/pipeline-orchestrate/SKILL.md` 并自行准备任务目录：

```bash
python tools/pipeline_agent.py prepare \
  --episode /data/new-task/episode.mp4 \
  --request-file /data/request.txt --output /data/runs/new-task
python tools/pipeline_agent.py context \
  --work /data/runs/new-task --role orchestrator --output /data/runs/new-task/context
```

- `request.txt` 是用户的请求；你可以写它，它不是要求用户撰写的规格说明。
- `prepare` 只锁定输入身份并创建状态文件。没有预设的任务类型，它也不检查 adapter 是否存在。`--reference`（可重复）绑定额外的真实照片和设备证据。绝不要把凭据目录当作输入。
- 一段视频就足以开始观察。发现并验证 episode 旁边或部署配置中的动作、标定和机器人资产。
- 从真实视频中提取帧作为外观参考。不要让用户重新提供已有的图像。
- 如果单段视频无法确定尺度，先用机器人尺寸、已知物体或多个视角进行拟合和交叉验证。仅当确实无法解决时才提出明确的缺失项。“用户没有写技术配置”不算缺失输入。
- 要从工作反馈中改进通用能力，参见 `prompts/instructions/self-improvement.md`。其他任务的输出不是前置输入。

## 使用另一个 agent

`context` 生成 `context.md`、`messages.json` 和 `receipt.json`。消息包含通用指令和角色；receipt 记录每个文件哈希和最终消息哈希。任何具备工具能力的宿主都可以把 system 和 user 消息传给它的 agent，CLI 宿主可以使用 `context.md`。这些规则不依赖宿主发现隐藏目录。

`supervise` 是持续运行的入口点，由 run ledger 托管；`drive` 是有界的前台运行：

```bash
python tools/pipeline_agent.py drive --work /data/runs/new-task \
  --driver /data/agent-driver.json --max-sessions 8 --timeout 1800
```

driver 文件为 `{"argv":["/opt/agent-wrapper"],"input":"messages"}`（`input` 也可以是 `text`）。wrapper 在 stdin 上接收消息 JSON 或文本，在任务目录中工作，使用自己的工具循环并返回退出码。没有 shell 插值，也没有密钥进入 argv。围绕普通 API 的 wrapper 必须提供读/写、命令、图像和持续执行工具。退出后，harness 会重新读取实际状态和输出，而不是信任 stdout。它最多运行明确指定的会话数，超时时终止进程组，并为每个会话保留日志和加载 receipt。它绝不会自动加载其他任务的输出。按照 `datafarm-run-launch` skill 托管长时间运行。

`prepare` 默认采用咨询式成本模式。强制模式需要一个能真正限制所有子调用的 driver 以及 `cost_control` 声明，普通 CLI 无法满足。参见 `prompts/instructions/task-cost.md`。

## 任务状态

`state.json` 是断点和下一步记录，而不是成功证书。保持 `phase`、`status`、`next_action`、`open_questions` 和 `external_jobs` 为最新。adapter、judge 和媒体放在 `task/`、`evidence/` 等目录中。`status` 只能是 `working`、`needs-input` 或 `complete`；在 `complete` 之后，必须存在一个 `delivery.json`，它通过 `python tools/pipeline_agent.py verify --work ...`。评审任务与撰写任务分开启动。expert harness 仍然隔离受保护的 judge 和 holdout，orchestrator 能写文件这一点不得让撰写者读取隐藏的评估。参见 `prompts/instructions/task-artifact-contract.md`。

## 主线

观察完整 episode → 总结目标与物体关系 → 拟合并验证场景、相机和物理 → 生成任务和一个独立的 judge → 编写专家、初始化与 adapter → 小预算 rollout，读取日志和视频，修正 → 冻结 holdout 验证 → 完成一个 white 模型小样本 → 绑定真实参考和显式模式 → 编译任务 prompt → 小批量照片级生成与独立评审 → 流式扩展 → 源绑定的准入和数据导出 → 检查数量、对齐和失败记录并交付。

每个箭头都有可检查的产出。代码能编译、provider 调用成功或一个状态字符串，都不能跳过物理和视频评审。当用户只需要数据时，工作在数据交付处结束；训练不是强制的。

仅在缺少一个无法推导的目标（例如未说明 OOD 或非 OOD）、某个外部设备事实确实无法解决，或访问/资源授权不足时提问。当已有明确目标或长期授权时不要重复询问，并在等待时继续做不依赖该答案的工作。

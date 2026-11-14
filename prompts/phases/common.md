## 环境

- 你以非交互方式运行。绝不提问或等待输入：做出决定、记录理由并继续。绝不打印、复制或搜索凭据或 API key。
- 任务目录：`{T}`。Workspace W = `{W}`，由 `tools/pipeline_agent.py prepare` 准备（`input.json`、`state.json`、`task/`、`evidence/`）。
- 固定的通用源代码，只读：`{SOURCE}`。
- 强制规则：`{SOURCE}/prompts/phases/protocol.md`。用户意图：`W/input.json`（`request`）。也不要打开：{FORBIDDEN}。
- 花费：本任务所有阶段合计有 ${TASK_CAP} 的硬性上限，本阶段有 ${PCAP} 的硬性上限。达到任一上限时宿主会终止会话。用 `{COST_COMMAND}` 检查累计总额。如果接近上限，写出你的输出并停止。
- 节约：总结工具输出，绝不批量倾倒大型文件或图像。
- 在环境备注允许的地方并行运行独立 seeds。等待仿真器通常比模型本身耗时得多。
- 不要自行启动其他模型或 agent 会话。宿主会把独立评审作为单独的会话启动。如果你的阶段需要评审，写出请求文件并停止。
{ENVIRONMENT}

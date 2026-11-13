# Codex CLI / ASPIRE 风格特权状态 CaP

本实现把工作区已复刻的 ASPIRE robot 操控流程接入现有 `provider-factory`，采用特权仿真状态替代图像感知；不是完整原论文的感知、课程或大规模评测复现。

## 运行边界

`SearchRunner → CodexProvider.decide → 新 Codex CLI 会话 → JSON 决策`；代码执行仍由 xh broker / 任务 adapter / 冻结 Judge 负责。Codex 不直接操控仿真，不读取隐藏种子和 Judge。Actor、Distiller、Verifier 每次使用新 CLI 会话，不使用 resume；上下文由宿主显式重建，Verifier 不接收提案作者代码和历史。

使用现有 ASPIRE 的前后状态证据、选择性 inspect、前 3 个候选、同代不同假设、失败到修复技能提炼和新上下文技能复验。`population_schedule=generation` 保证本代候选只能使用上一代成绩；当前执行仍串行，不声称并行 agent 搜索。技能必须通过验证且 Verifier 明确按原样应用，才允许入库。未实现完整课程或跨任务回访。

默认旧协议保持不变。新 preset 使用 50 代、每代 3 个候选、总计最多 400 次 provider 决策；这只是接近已有复刻的搜索上限，不意味着测试将自动用满，也不等于模型内部 token 或美元硬上限。独立的隐藏评估仍由 harness 在提交冻结程序后负责。

## 接入

```bash
python tools/cap_search.py \
  --task runs/my-task/public-task.json \
  --config configs/cap/aspire-state-search.template.json \
  --engine-config configs/cap/xh-state.template.json \
  --provider-factory datafarm.cap.codex_provider:build_provider \
  --provider-config configs/cap/codex-aspire.template.json \
  --library runs/my-task/skills --output runs/my-task/search
```

先复制并填写模板；`round_dir` 必须是已准备的真实 broker round。任务必须声明可用 API、单位和 program_contract，provider 不假定所有机器人都使用同一个入口函数或 Python 白名单。`--submit` 仅在需要向 harness 提交时显式添加。

普通模式使用部署已有的 Codex 登录与模型配置；模型也可在 provider config 中显式指定。FYP 模式在 config 中增加 `fyp_env_file`（指向用户授权且留在本机的原始 env 文件）和 `model: gpt-6.1-sol`。个人 token 只在父进程内存中，CLI 得到临时回环凭据；不复制到服务器、不写入 argv 或运行产物。

FYP 模式可从 `configs/cap/codex-fyp.template.json` 复制配置，填写原始 env 路径后作为 `--provider-config` 传入；模板不包含密钥。当前 ChatGPT 登录方式不支持本机默认配置中的 `gpt-6.1-sol`，本轮实际验证采用的是该 FYP Responses 路线。

FYP 桥复用 `aspire-agent/tools/aspire_codex_provider.py` 的 LocalTransport：仅转发无状态内联 Responses，移除缓存/保留扩展，一次 decision 最多一次上游请求，关闭 CLI 自动重试、shell、web、plugins 和 agent 委派。token 不进入 CLI 子进程。标准非 FYP 模式使用 read-only sandbox；同用户目录间的读取隔离不由 cwd 或 prompt 保证，严格部署仍须由 broker/OS 分隔保护数据。

CLI 命令通过本机 0.141.0 help 以及[官方 iterative repair 示例](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)核对，使用 stdin、JSONL、output-schema 和 output-last-message。

## 特权状态协议

xh engine 与 SearchConfig 都须配置 `observation_mode=privileged_state`。每条 primitive 的 before/after 是 `privileged-state/1` 对象，必需：

- control_step：非负整数；frame_id：world。
- units：length=m、angle=rad、time=s。
- 允许的物理字段：joint_names、joint_types、q、dq、tcp_pose、objects、articulations、gripper、contacts、time_s、validity。

host 读取实测 Robot API 和任务实体状态写入这些字段，不把控制目标当实测位置，不把不可用接触写成 0。源关节为移动关节时，其 q 单位按 joint_types 使用 m；角度关节使用 rad。

每个公开 episode 额外提供 `privileged_state={availability,initial,final}`；Actor 在摘要中直接看到初末状态，可 inspect 当前历史中的动作前后状态。状态缺失或格式不匹配立即失败，禁止自动退回视觉或虚构状态。只有专家在第一条原语前报错的记录可标记 unavailable，并保留错误原因。

特权只指物理状态，不包含任务成功标志实现、Judge 源码、隐藏种子或私有文件。provider 拒绝额外的顶层上下文字段、已知私有评估/密钥字段、视觉输入与非有限值。嵌套对象内容仍需宿主确保只来自公开物理状态。

## 记录与恢复

每次决策在调用前持久化 request、prompt、schema 和 starting receipt。保存 CLI thread ID、JSONL、结果、token 用量、FYP 实际上游请求数及返回模型；美元成本未知时为 null。

身份绑定请求、prompt、schema、provider 源码和配置。完成结果经摘要验证后可以复用；pending、失败或中断目录不自动重发。需要先检查会话和模型请求记录，修复后以可追溯新版本继续。CLI 超时尝试终止子进程树；操作系统若拒绝终止，仍需检查遗留进程，不能把失败记录当作已停止证明。

## 本轮测试范围

用户明确要求先使用已有或手动构建的固定场景，不测试资产检索、场景生成或自动组装。服务器测试沿用固定 Isaac Lab 3.0 决策，不能以旧 Isaac Sim 或其他仿真器替代并声称通过。

用户另行授权调用已部署 H3 并把测试视频发送至指定邮箱。本次 H3 是明确的诊断视频输出例外，保留其独立来源与生成记录；它不能作为忠实动作重放、任务成功或可训练数据的证明，不改变默认状态渲染管线的声明。若视频不忠于轨迹，明确标注，不冒充真实仿真画面。

本机 provider 实测：一次真实 FYP/Codex 请求已接通，无机器人动作。程序化回归含脚本化 CLI 子进程的完整修复/提炼/复验流程，不把该夹具成绩视为真实模型控制成功。服务器物理与 H3 全链路结果在运行后单独追加。

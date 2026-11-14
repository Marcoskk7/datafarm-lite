# datafarm-lite：任何编码 agent 的指令

这是 DataFarm 的一个 fork（`DAGroup-PKU/PyRUA-Lean`，分支 `DataFarm`，提交 `d788388`，MIT）。
它接收一个 YAML 请求，在仿真中构建场景，并产出带动作标注的 episode。
它不接收真实标定 episode，也不渲染任何视频模型观测：它的输出
**不是可直接用于真实世界的就绪数据**，绝不能这样报告。见下文「Fork 范围」。

你的工作是把一个数据请求转换成在仿真中、经过独立验证且带动作标注的合成
episode。你是通用的任务编译器，而不是预置任务 adapter 的选择器。阅读 prompts/instructions/agent-start.md 并遵循 .agents/skills/pipeline-orchestrate/SKILL.md。
找不到匹配的任务名、adapter、任务 brief 或视频 prompt 是预期情况：
观察、推导、实现、测试并生成它们。绝不要要求用户替你写这些。

## 权限与指令加载

1. 阅读 prompts/instructions/instruction-layers.md、authority-and-scope.md 和 mode-and-style-contract.md。
2. 阅读 prompts/roles/orchestrator.md 以及 configs/agent-instructions.json 中列出的 stage skill。
3. 使用 `python tools/pipeline_agent.py context --role orchestrator --output /tmp/datafarm-context`
   获得完全展开、带哈希记录的上下文。即使你的宿主没有发现 .agents/skills，这也会显式加载指令。遵循它的文件路由。
4. 用户当前的指令优先。保留有效的范围内授权。只有在检查了可用内容之后，才询问
   真正缺失的意图、无法访问的输入/资源或权限。自行推断并验证技术选择。未知的 OOD 与 sim2real 意图
   必须在渲染前解决；它不阻塞 grounding 或本地实现。

## Fork 范围

从上游保留：任务专家 harness（`xh` broker、OS sandbox、AST 审计、轮次续接、
预算）、冻结的 judge 与隐藏 holdout、运行 ledger、准入检查、agent
运行时以及 `core/` 中的 IR 验证器。

本 fork 替换：grounding（输入是请求 YAML，不是真实 episode）、
仿真器 adapter（Isaac Sim）、渲染器（从记录状态出发的 Isaac RTX / Replicator，不使用
视频模型）以及数据集导出（HDF5）。

仍仅属于上游、不得照搬：`render/h3/`、`render/seedance/`、
`vendor/videox_fun/`、`vendor/trainer/`、`training/`，以及真实 episode 技能
`sim-grounding` 和 `build-observed-object`。

## 不变量

- 通用规则/技能/角色是第一层来源。某个任务的 adapter、brief、judge spec、场景、
  相机拟合、prompt、运行配置和报告是第二层产物。其他任务的产物
  不是新任务的前提、全局规则或授权。
- 以完整 episode 为单位；为视频传输而拆分不改变动作语义。
- 渲染复现的是仿真 episode：相机、材质和灯光来自渲染
  spec，记录的状态轨迹是运动与接触的唯一来源。本 fork 中不存在
  真实世界图像条件化；不要声称摄影级真实感或真实就绪数据。
- 生产者不得自证成功。分离任务契约评审、专家开发、
  仿真器/judge 评估与渲染准入评审。独立意味着带证据的单独评审
  执行；JSON 字段里换一个字符串不是隔离的证明。
- 保留所有尝试、失败、provider ID、seed、预算、代码/prompt/输入哈希和替换关系。
  再次提交前先续接已有的 ID。用进度两次确认后台持久化。
- 不要为了通过某个 gate 而修改原始动作/图像/指标。重新规划需要匹配的重新采集。
  审计完整的关节限位占用；候选裕度不是通用的硬件事实。
- 保护凭据、隐藏评估 seed 和 judge。绝不记录密钥，也不要放进 argv。
  使用部署的调度器和权限范围，而不是从别处复制的机器或额度。
- 数据集使用来源绑定的准入。训练属于下游，且只在被请求时进行；显式声明
  状态/动作模式并验证部署兼容性。
- 运行时只读取显式指令和范围内的经验。把新的任务输出保留在运行
  workspace 下。
- 保留用户改动。新的通用行为属于 core/skills，并配以有意义的测试；
  新的按任务逻辑属于生成的任务产物。不要构建冻结的任务注册表。

工程：用 `pip install -e '.[dev,workflow]'` 安装，然后运行 `make test lint audit`。
分别报告实现、CPU 测试、真实运行和未知项。API/fixture 成功不是
物理或视觉质量的证明。源码 checkout 就是交付物。

## 从本仓库的工作中学习

在记录纠正或改变通用行为时，阅读 prompts/instructions/self-improvement.md。
通过 pipeline_agent feedback 或 pipeline_evolve record 捕获可见的用户反馈、修复 diff 和测试证据。需求事件需要真实的用户来源和确切范围。绝不要
假装代码库能访问宿主未提供的聊天。任务 session 会自动记录
其结果；使用可选的运行后 evolution driver/policy 做有界的自动改进。
用 pipeline-improve 处理候选变更；由宿主评估决定激活。保留
运行的 source pin、已有 provider ID、失败和先前证据。新 adapter 仍是产物。
分别报告软件证据、真实任务结果和元改进结果。

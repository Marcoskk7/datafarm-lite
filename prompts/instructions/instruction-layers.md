# 通用能力与 task 输出

DataFarm 接收一个它此前未见过的机器人 task、场景和真实 episode，由 agent 自主完成理解、场景适配、expert 开发、验证、批量采集和摄影级数据生产。输入 episode 是一份完整的真实机器人记录：单个视频，或带 actions/states、相机参数和设备信息的多相机视频。输出是一个与模拟动作保持对应关系的数据集，带有来源记录和独立评审记录。训练是可选的后续步骤。

## 第一层：共享能力

- `.agents/skills/`：如何观测、提出可验证假设、生成实现、试运行、诊断、纠正、扩展批次和恢复。
- `prompts/roles/`：每个角色的职责。
- `prompts/render/`：task 无法覆盖的真实参考、动作保真度和风格要求。
- `prompts/instructions/`：权限、证据以及各阶段之间的交接。
- `src/datafarm/agent/context.py` 和 `src/datafarm/task/contract.py`：显式加载这些指令并验证 task 输出。
- expert harness、run ledger、batch supervisor、providers 和 admission：执行工具。

仓库中没有对应 adapter 的新 task 并不是能力缺口：agent 会从新 task 生成新的 adapter、brief、judge 和 initial-state sampler。判断某个通用能力是否缺失，要看 agent 是否知道如何生成它、能否调用工具验证它、以及是否会诊断并从失败中恢复。

## 第二层：单个 task 的产物

| 输出 | 由谁、基于什么生成 | 用途 |
|---|---|---|
| grounding.json / grounding.md | orchestrator 观测完整真实 episode、读取设备/标定证据并进行拟合 | 几何、相机、物体、动作、可辨识范围与不确定性 |
| task.json / requirements.md | task designer，依据观测和用户目标 | 成功关系、完整动作阶段、随机化轴、物理和采集准则 |
| scene / init / expert / adapter.json | agent，使用 simulator API 和通用接口 | 随机化初始状态、observation-to-action 控制、供 broker 使用的 rollout 命令 |
| judge / verifier spec / brief | 独立的 contract-review 和验证角色，之后冻结 | 成功准则和负向对照；expert author 不能编辑 judge |
| render-overlay.json / render prompt | orchestrator，依据 task、相机和真实参考 | 本 task 的物体、事件、视角和连续性；它们不能取消通用规则 |
| batch configs / dataset spec / receipts | orchestrator 和一个独立 verifier | 批次扩展、评审、admission、数据导出和复现 |

这些文件放在 `runs/<run-id>/task/` 或用户指定的工作区中，并登记 manifest 和哈希。`configs/` 下的模板是起点，不是 task 结果。绝不要自动加载无关 task 的阈值、模式授权、模型选择或 prompt。文件扩展名不能区分层次。`tools/audit_instruction_layers.py` 按层对文件分类；加载 manifest 是 `configs/agent-instructions.json`。

## 修改各层

新 task 特定的要求只放在第二层。只有在存在跨 task 的可验证缺陷时才修改第一层，并同时更新 skill、prompt、verifier 和测试。在一个 task 上的成功不授权在另一个 task 上做 OOD，且一个 task 的数值阈值不是通用真理。保留早先的输出和失败证据，绝不通过改写现有证书来伪造对新策略的合规。

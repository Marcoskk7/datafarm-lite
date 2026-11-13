# 任务专家编写与隔离评估

任务专家是让仿真机器人完成该任务的控制程序。编写者通过 `tools/expert_harness.py` 工作，不得启动隐藏 rollout、修改 judge 或查看 holdout seeds。

这里的操作者默认是编排 agent：它从新 episode 生成 adapter，不要求用户填写。在 adapter 中，orchestrator 声明接口、物理与相机事实、开发 seeds、预算、并发、允许与受保护路径、verifier 版本、验收条件以及 backend/model。harness 生成并 lint brief。编写者使用 broker 工具 `xh` 请求 rollout、提交和标记。harness 统计尝试次数、成本、版本和状态；编写者的自报不能替代它们。

开发过程只读取已批准的输入，并且只修改指定的工作目录。限制按原样继承，报告中的建议不是权限。每一轮都保留失败分类、最佳专家哈希、测试证据和安全的交接摘要。harness 在隔离侧执行 holdout，交接不得泄露 seeds 或逐项的可识别信息。

第一条消息包含任务和完整 brief。continue 消息携带当前会话、rollout、预算、进行中任务和状态信息。预算耗尽时，final 消息只允许冻结提交并报告。一个通用 system prompt 不能替代这三条消息。环境是否真正实现了隔离、完整的成本核算或 holdout 泄露防护，取决于代码测试和本轮的探测，而不是设计描述。

## 支撑面随机化

仿真采集默认对每个 episode 随机化支撑面：桌面高度在 ±20 mm 内均匀随机，桌面倾角幅度在 1-2° 内均匀随机且方向任意，在任务的场景采样器中实现。桌面及其上的物体绕桌面原点刚性倾斜；机器人基座和相机保持固定。plan 记录 `table_tilt` 和 `table_tilt_policy`，物化后的对齐记录 `support_plane_tilt`（一个平面点和法线），而 `support_z_at` 给出任意 xy 处的支撑高度。关闭倾角需要记录原因。带倾角的样本需要一次新的完整物理 rollout 和一个独立 judge；动作或图像不得离线修改。

## 边界

确认渲染模式和真实参考是 orchestrator 的职责。编写几何随机化方案的专家编写者不会因此获得 OOD 外观授权。训练和渲染走各自的流程。

创建与调试方法在 `.agents/skills/task-expert-author/SKILL.md` 中。专家运行时接口（任何仿真器都实现相同语义）：阶段标记、谓词记录（仅摘要）、仅规划的可达性探测、有界的逐 setup 策略搜索（专家自检、全新重置和尝试计数）以及 sweep 参数。单次运行的方差很大，因此只有在包含 held-out 任务的、带重复的配对比较之后，才采纳对编写方法的更改。

使用已有的阶段摘要和交接产物，而不是检查会话或 provider 日志，并且不要启动另一个 orchestrator。专家通过软件检查并不是任务能在目标成本内完成的证据。backend 和传输细节见 `docs/reference/expert-harness.md`。

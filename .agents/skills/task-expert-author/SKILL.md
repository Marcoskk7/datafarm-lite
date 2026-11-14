---
name: task-expert-author
description: 生成新的 task adapter 与 expert，然后通过隔离、受预算约束的 simulator 试验和独立评估来改进观测驱动的控制器。
---

# 编写并迭代一个新的 task expert

orchestration/task-design 角色首先生成 adapter、brief、带种子的 init 和一个独立 judge；只有在评审并冻结之后，才会为受限的 author 启动 expert-harness。author 角色文本位于 prompts/roles/task-expert-author.md。由 orchestration agent 配置 adapter；没有任何人手工填写它。

1. 针对这个真实 task，定义观测、动作、state/units/clock 和成功条件；禁止读取未来的目标轨迹，或将固定动画回放冒充为闭环 expert。短动作序列可用于可验证的子技能，但状态判断必须来自实际观测。
2. 使用通用的 expert-harness adapter schema 接好 rollout 命令、结果字段、development seeds、budget、可读的 code/assets 和 protected paths。编写 observation→action 插件和 seed→initial-state 插件；主动实现缺失的接口。
   默认情况下，simulator 采集的 expert 轨迹会对每个 episode 施加恒定的支撑面随机化：桌面高度（默认 ±20 mm 均匀）和桌面倾斜（默认幅度 1–2° 均匀，方向 0–360° 均匀；桌面及其上的物体绕桌面原点刚性倾斜，而机器人基座和相机保持固定），在 task 的 scene sampler 中实现。初始状态物体必须落在倾斜平面上；expert 从观测中读取实际支撑平面和实时物体位姿，不得假设桌面水平。只有在有记录原因时才关闭它（例如成对的水平对照或一项硬件事实）；将原因写入 plan。这是几何随机化，需要新的完整物理 rollout 来验证，并且不授权外观/OOD 变更。
   初始物体放置有两种模式，记录为 task/scene.json 中的 `placement_mode`，由 orchestrator 选择：`local_robustness`（默认）围绕从真实 episode 推导出的标称位置抖动每个被操作的物体（默认 ±30 mm），用于小型验证运行和调试；`broad_table` 在可达桌面上为每个物体独立均匀采样（grounding 中的 `reachable_tabletop_xy`，在桌面上用前向运动学和关节限位探测），用于训练数据和广泛部署。两者都保持最小物体间距（默认 80 mm）。使用 `tools/prepare_object_placement_plan.py` 生成 plan，并让带种子的 scene sampler 读取它；在 `broad_table` 下 expert 必须处理任何合法布局，不得假设标称位置。
3. 首先运行 lint-brief、prepare-round、run-round 的内置 preflight，以及一个最小完整 round。确认输出 media/actions/state、独立 judge receipts、失败分类和成本核算。当隔离不可用时，修复环境或如实报告受阻；绝不静默切换为 none 并声称独立通过。
4. author 仅通过 xh broker 运行 development seeds；读取实际的 rollout 失败、事件帧、关节回读和物理日志，并修改抓取、接近、运输、释放/稳定或恢复控制；重试受限，所有质量和基础设施失败都被保留。
5. 独立 verifier 检查成功语义和负向对照；author 不得编辑或读取受保护的 judge、holdout 或 budget ledger。当发现可利用的漏洞时，应报告并修复通用契约，而不是利用 judge 漏洞。
6. 在每次可计分的改进时提交；当预算耗尽时，冻结最佳 expert。harness 运行隐藏 holdout、记录所有分母，并交付 expert/hash/adapter/brief/qualifications；不要把精挑细选的子集呈现为 100% 首次尝试成功。
7. holdout 失败后，orchestrator 基于不会泄露隐藏 seeds 的摘要决定是否继续开发；新的 round 保持测试隔离。一旦合格，转入 white-model 批量采集；代码作者本身不得签署 admission。

将三条消息 FIRST、CONTINUE 和 FINAL 与 budget/in-flight 字段放在一起；role 文本是额外约束，不能取代这些 runtime 消息。参见 docs/reference/expert-harness.md 和 prompts/instructions/expert-authoring.md。

## 高效的创建与调试

按顺序执行：

1. 尽快获得第一个有效信号：最短的端到端草稿（演示顺序、标注点、planner）+ 一张 ≤8 行的阶段表（stage、arm、退出谓词和容差）+ 阶段标记，然后立即运行 multi-seed rollout；在第一次 rollout 之前不要写任何替代草稿或恢复逻辑。以 stage-graph profile 编写 Task IR，保持简短，并依据证据迭代。
2. 依据证据逐级升级：仅针对结构性失败编写一个替代方案并在批次中比较；否则修复最大失败组中第一个失败的阶段，每轮一个假设，每组最多 3 轮。
3. 先用 2–3 个 seed 的探针测量不确定的量；在不可逆操作之前，运行仅规划的 reachability 探针并选择一个可行候选。
4. 数值余量（offset/height/angle）通过参数扫描找到，而不是靠猜测。
5. 对于依赖 setup 的失败，使用有界的每次 setup 重试：expert 的自检只使用自身的终止谓词（绝不读取 judge），第一次尝试编号为 0，所有尝试都计入分母。
6. 只有在 20 个未见过的 dev seeds 上达到 ≥95% 才停止；在 85% 到 95% 之间继续修复最大的失败组；提交实测最佳版本，并报告首次尝试成功和尝试次数。
7. 用阶段摘要而不是原始日志来调试；只有在摘要无法解释失败时才请求图像。
8. 等待 rollout 会消耗模型调用：保持 rollout 快速（常驻热 worker、足够的 GPU 容量），等待时阻塞一次，不要以短间隔轮询。

## white-model 批量扩展前的低成本检查

在第一次完整的 white-model 采集之前，完成对若干位姿从三个视角（或契约规定的视角）进行的 sim-grounding 真实图像检查，并保存独立评审 receipt 和 camera/source 哈希；一次成功的完整物理动作无法弥补错误的视角。纯物理的开发探针可以先运行，但不得被视为相机合格（camera-qualified）或完整的 white-model 通过。

在第一次完整 rollout 之前，检查 real-source 的 home、模拟 reset 和所有绑定的关节限位；分别记录 SDK/URDF 交集、规划余量和实际回读。Home 不带有隐式容差，一个关节通过并不意味着其他关节通过。如果需要新的 reset，保留 source 并明确记录改动，并重新采集匹配的 images/actions；绝不裁剪旧的标签。从实际的抓取/释放状态开始，演练完整的运输和撤退路径，包括返回 home；单个 endpoint IK 成功不足以扩展批次。从第一个 episode 起，采集器就保存契约要求的状态（包括实际 EEF）、初始帧、每个控制步和最终状态，并在扩展到用户指定的数量之前完成全关节/时钟/字段覆盖检查。对于 SR-006 joint-margin 评审，编写 docs/reference/expert-harness.md 中描述的 trace，并运行 `tools/audit_joint_margin_trace.py <trace.json> --output <report.json>`。审计的 recorded_trace_passed 只证明给定的记录，不能取代对初始帧和完整 episode 覆盖的评审。

在放置/释放阶段，存在碰撞流形不能等同于已经承重：首先检查实际碰撞几何的表面间隙、接触冲量，或其他证明支撑的观测，然后才释放。在第一条 development 轨迹上，记录释放前后整个沉降窗口，并区分由刚体旋转引起的原点变化与实际下落；不要只比较两个相邻帧。数值显示/光照要求必须说明图像区域、像素算法和阶段区间，并从媒体中测量；display-state 元数据不能取代像素证据。低成本顺序是首先定位物理失败，然后采集完整 white model，同时保持独立评审；绝不通过依赖漏检来降低成本。

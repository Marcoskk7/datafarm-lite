---
name: pipeline-orchestrate
description: 使用仓库自有的指令和生成的任务产物，把任意新的真实机器人任务/场景 episode 适配为经过验证的批量数据。
---

# 从新任务到数据交付

按照 prompts/instructions/task-cost.md 执行：所有模型角色共享一份持久的按任务成本账本；默认模式为 advisory，目标为 30 USD，参考上限为 50 USD，可手动配置；disabled/advisory/enforced 的语义见该文件，且照片级生成服务和 GPU 不计入。Orchestration 只加载当前阶段的 skill，不会每次都注入全部阶段上下文；在进入下面的步骤之前，务必实际阅读对应的 .agents/skills/<name>/SKILL.md。
父对话不重复调度，等待由程序处理。
如果唯一的下一步是等待已提交的外部作业，把它的 external_jobs.id 写入 state.model_wait_for；
在依赖结束前，drive 拒绝启动下一个模型会话。supervisor 核实进程并更新状态；模型不得通过反复查询来充当轮询器。若仍有独立工作，不要设置等待依赖。协议见 docs/reference/cost-control.md。

首先阅读 AGENTS.md、prompts/instructions/agent-start.md 和 prompts/instructions/instruction-layers.md。
你是本流水线的通用编排能力；不要因为缺少任务 adapter 就把它当作阻塞项。

1. 保存用户的原始请求，锁定输入 episode（video/record 目录）和目标数量，并运行 `python tools/pipeline_agent.py prepare`。阅读用户提供的部署配置、现有资源和授权；检查实际文件、完整视频元数据和关键动作，然后才判断缺少什么。技术默认值由你选择并验证；不要让用户替你规划。未知模式只阻塞付费渲染，不阻塞观察和编码。
2. 按照 new-task-adaptation skill 生成新的任务产物；按照 sim-grounding 构建场景，按照 task-expert-author 构建可观测控制和独立 judge。你必须生成任务的输入/输出边界、完整的成功条件、随机化初始状态以及可执行的 adapter。除非用户已经选定，否则由你选择物体摆放模式（`placement_mode`）：目标最多 10 个 episode 或有明确验证目标时用 `local_robustness`；50 个或更多 episode 或训练/策略/广泛部署目标时用 `broad_table`；把选择及其理由记录在 state.json 中，并传给 task-expert-author。对于 `broad_table`，在开始采集前检查计划的覆盖跨度是否与交付将要声明的分布主张一致（`src/datafarm/training/distribution.py`、`broad_deployment`）。若输入的机器人不在部署的硬件资产中，则它自带机器人包：用 `tools/check_robot_package.py` 检查；LeRobot 数据集用 `tools/import_lerobot_episode.py` 在 `prepare` 前转为一个自包含的 episode 目录。
3. 专家试错：记录假设、最小判别实验、实际轨迹/图像、失败分类、纠正。区分环境启动错误、感知/标定错误、物理模型错误、控制错误和验证器误判。主动编写缺失的接口和通用修复，而不是只报告问题。至少保留一种替代解释；禁止通过修改成功条件或删除失败分母来“提高成功率”。
4. 达到小样本目标后，冻结代码/契约，并在独立留出的完整 episode 上验证；检查 action/state/image 是否同源、左右腕身份、坐标/单位、时钟、关节限位余量、释放/终止状态/重置。只有基于明确证据才决定规模化批量。
5. 调用 render-batch：从真实机器人输入生成可追溯的真实参考，遵循 render-policy 编译核心加任务槽位，并检查实际的 provider payload。agent 必须亲自解码并查看真实原始图像/episode 和生成的视频，并排对比，覆盖真实机器人本体、实际安装的相机/线缆和确实存在的物体、桌面/背景，以及当前任务声明的每个视角的照片质感（三视角 episode 即全部三个）；局部纹理的相似不能等同于采用了整个场景。OOD 只允许相对真实基线沿已批准轴做合理的真实改动。先跑一个有代表性的小样本，再规模化批量；流式审查，有限次重试，遇到系统性失败时提前停止。真实参考只提供外观；不要复制其中的动作/布局，也不要添加实体。若发生相机或参考泄漏，做单变量对比；不要通过移除参考来解决。被拒绝后，先调用 verifier-rejection-recheck。
6. 调用 dataset-delivery：完整审查后，由独立角色出具源绑定的证据，导出对齐后的数据并检查可读性。按固定目标计数，同时给出所有尝试/失败/替换；如果数量不足，继续已授权的恢复与重新采集。
7. 通过 `pipeline_agent supervise --config` 持续运行现有 drive；原生 Codex 会保存显式的 session ID，未知用量不按零或硬上限处理。独立审查写入 state.review_requests（唯一 ID、指令、审查对象的路径/sha256），由程序实际调度一个独立的 reviewer 会话；审查结果与执行来源记录在 .runtime/reviews 中，不允许自签。用户停止会保留原始 ID，恢复不会重置成本/session 计数。全程按 datafarm-run-launch 保持账目、双重确认持久化并复用远程 ID。会话中断前，维护好 state.json 和 next_action；恢复时先核实作业/文件的当前状态。不要因为 chat/命令返回就停止真实任务。
8. 输出 delivery.json 和一份自包含报告，并运行 verify。报告要区分真实验证、模拟夹具和未验证项。仅在用户要求时才进入训练（dataset-delivery 第 6 步），不要扩大目标。

状态转换以证据为依据。新任务的 adapter、需求文档和视频 prompt 是你的交付物，不能预先要求用户提供。独立审查失败后，修复产物并重新验证；从未真正执行过的生成脚本不算完成。

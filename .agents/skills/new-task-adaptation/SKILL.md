---
name: new-task-adaptation
description: 从一段此前未见过的真实 episode 推断任务，并生成其场景、adapter、判据、brief、渲染 overlay 与 batch 契约。
---

# 从新 episode 生成任务产物

输入：一段完整的真实机器人 episode、当前用户目标，以及可用的机器人/模拟器部署。输出：本 run/task 下的代码与契约，绝不依赖任务名出现在某个注册表中。请阅读 prompts/instructions/task-artifact-contract.md。

1. 观察整段视频，而不只是最后一帧。记录每路相机的身份、机器人/夹爪、物体及其数量、抓取/接触/转移/释放/最终状态的时间窗口、遮挡与初始状态。提取有代表性的真实机器人帧，并保留原始视频 SHA、帧编号/时间与裁剪映射。当有动作/状态文件时，读取其 schema、单位与时钟；若没有，须承认动作估计的不确定性。导入的数据集 episode 带有 `episode.json`（相机映射、状态/动作字段、fps、任务文本）：从这里开始。
2. 将“做了什么”写成一个与外观无关的物体关系/事件契约；区分演示中偶然的轨迹与任务所要求的条件。推断阶段顺序、完成条件、可随机化范围（默认包括桌面高度和 1–2° 的桌面倾斜，以及物体摆放模式 `local_robustness` 或 `broad_table`，见 task-expert-author）、不变量与失败类型。把每个事实都绑定到观察到的帧或设备信息上；将拟合参数标记为推断。只在任务目标不清楚时才提问；技术层面的适配由你完成。
3. 生成 requirements.md、task.json、场景/初始化代码、adapter.json、brief、verifier 规范、render-overlay.json 以及 batch/dataset 配置。它不必一次就做到完美：先用一段最小但完整的 episode 验证接口，再依据失败证据迭代。示例不得把另一个任务的数值或授权带入新任务。
4. 独立的任务契约审查者将真实机器人与预定义的成功条件进行对比，并设计正/负对照，例如未抓取、仅接近、未释放、物体身份错误、时机错误、未稳定。judge 必须依据实际外部 trace/图像作出判定，而不是返回 expert 的 check_success()。先用失败 trace 验证它不会被误判为通过，然后冻结 judge，让作者开始开发。
5. 新 adapter 使用 docs/reference/expert-harness.md 中现有的通用 JSON/命令接口。当需要机器人插件时，编写 observation→action 接口与 seed→initial_state；结果落在一份 trace 和一个独立判定中。用 `tools/expert_harness.py --help` / lint-brief / prepare-round / run-round 的内置 preflight 来验证接口，而不只是验证 JSON 可解析。当缺少任务级接口时，自行实现；只有在确实缺少模拟器/硬件事实或访问权限时，才标记为外部阻塞。
6. 每当场景发生变化，都要重新验证相机、物体尺寸/接触、随机初始状态覆盖与动力学，而不只是替换任务文本。render overlay 说明物体身份、事件、视角/时钟与连续性；它不会取消真实图像、忽略动作或自行选择一个 OOD。
7. 在批量扩展之前，冻结本契约/代码/配置的哈希；审查者与生产方分别执行，且证据要明确指出所用版本。修订 judge 时，记录通用原因、对其进行独立审查并创建一个新版本；保留先前的分母与结果。

参见 sim-grounding、task-expert-author、render-batch、dataset-delivery。运行时所需的全部知识都写入本任务的产物中，不得只停留在聊天里；通用经验在测试后写回通用层。

# 新任务产物与交接契约

生成这些文件，而不是向用户索要：grounding、task/requirements、场景与随机化初始化、专家、独立 judge、expert-harness adapter 与 brief、render overlay、batch 与数据编码配置。目录名称不限制任务类型。一个实现可以跨越多个文件；`task/manifest.json` 用路径、sha256 和一个说明其职责的 role 注册它们。在工作副本上迭代，然后冻结通过验证的版本。

`tools/pipeline_agent.py` 处理输入锁定、指令注入、会话边界以及状态和交付完整性检查；它不把生成的文件当作物理成功。生成的代码会实际通过 expert harness、仿真器、render provider、run ledger、batch supervisor 和准入来执行。API 细节请阅读每个工具的 `--help`。

## Expert adapter

新 adapter 使用 `datafarm.expert-harness.adapter/1`（`src/datafarm/expert/adapter.py`）。从接口和 schema 推导它，而不是从已完成的任务推导。你可以参考 `tools/expert_harness.py dry-run` 生成的 fake adapter（仅用于协议演示）和 `configs/expert-harness/template.json`，但要重建当前场景、命令、judge、路径、seeds 和预算。用 `lint-brief`、`prepare-round` 和 `run-round` 的 preflight 进行验证。真实 rollout 使用可用的仿真器命令；fake 仅用于软件测试。当未知设备缺少插件时，编写该插件。

## delivery.json（数据完成的边界）

Schema 为 `datafarm.agent-delivery/1`。文件引用是相对于 workspace 的路径加上 sha256。大型外部文件在数据集 manifest 中以绝对位置和校验和记录；验证器读取它们而不复制。

- `input_sha256`：来自 prepare 的 input-lock 摘要。
- `producer_id`、`target_count`。
- `attempts`：绑定所有 attempt JSON 的文件引用。一个非空数组，元素带 `id` 和 `outcome`（`admitted`、`rejected` 或 `infrastructure_failure`）。admitted 的 ID 必须等于交付的 episodes。绝不删除失败项。
- `dataset`：指向数据 manifest 的文件引用，schema 为 `datafarm.dataset-files/1`：`episode_ids` 和 `files`（workspace 相对路径或绝对外部路径）。文件必须非空，哈希必须与真实文件匹配。
- `grounding`：指向 `datafarm.grounding/1` 物理量台账的文件引用（参见 `sim-grounding` skill）。
- `episodes`：唯一的已准入 episodes，每个带 `id`、`artifacts`（文件引用：`action`、`source`、`video`、`prompt`、`references`）、`review`、`render_contract`（编译后的契约）和 `submission`（提交前验证记录）。

仅 white 交付见 `docs/reference/delivery-and-admission.md`。

## 评审

Schema `datafarm.task-review/2`：

- `producer_id`、`reviewer_id`（必须不同）。
- `execution`：指向独立执行证据的文件引用，记录 `role=reviewer`、`actor_id`、`producer_id` 以及指向实际评审 transcript 的文件引用。
- `subjects`：五个产物的 SHA。
- `verdicts`：`task`、`motion`、`reference`、`style`、`alignment` 和 `joint_margin` 必须全部为 pass。
- `evidence`：非空的文件引用列表。
- `visual_review`。

验证器检查 role、来源和内容绑定。它**无法仅凭 JSON 证明外部宿主真的隔离了身份**；宿主负责独立会话和写权限。

对于由 `drive` 或 `supervise` 运行的任务（存在 `.runtime` 记录），不接受手写的 execution。评审写入 `runtime_review`（`state.review_requests` 中的请求 ID）。验证器会重新核实该评审的完成情况、`verdict=pass`、请求 subjects 覆盖所有产物，以及报告 evidence 绑定了此评审文件。结果中的 `review_independence` 为 `runtime-verified`，否则为 `declared-execution-record`。绝不要以生产者身份构造评审者 execution。expert harness 仍负责隔离的专家评估。

`visual_review` 记录 `inspection_method=decoded-real-reference-and-generated-video`，绑定最终生成的视频和所有真实参考的 SHA，并逐项记录 `reference_baseline`、`robot_body_and_attached_hardware`、`task_objects`、`table_and_background`、`declared_view_photography` 和 `overall_photographic_realism`。

- 每一项都有真实参考与生成结果的 pass、生成帧号、实际参考 SHA、视角，以及评审 evidence 中可解码的 PNG/JPEG/WebP 对比图。
- baseline、table/background、declared view 和 overall realism 这几项需要全视角证据，而 view 项覆盖渲染意图声明的每一个视角。仅有文字或局部细节无法证明整个场景的真实风格。
- 不存在的附加硬件或任务物体可以带原因和对比证据标记为 `not_applicable`。绝不要为了满足 schema 而虚构实体。`not_applicable` 必须与渲染意图中的 `appearance_requirements.required=false` 一致；原因不能豁免必需项。
- 验证器会解码对比图并核实结构与内容绑定。它无法证明评审者的主观结论或媒体确实被查看过；独立执行记录和评审者的问责负责这一点。

这种机器检查验证交付来源和数量。它不能替代独立评审的视频、物理 judge 或 data-loader 检查。

交付验证还会检查 render contract、submission、最终 prompt 和真实参考在内容上一致，以及参考来源属于 prepare 锁定的输入。用 `prepare --reference` 绑定额外的真实照片，不要为 episode 中已有的图像重新询问用户。

## 在新会话中恢复

`state.json` 的 schema 为 `datafarm.agent-state/1`；orchestrator 持续更新 `phase`、`status`、`next_action`、`open_questions` 和 `external_jobs`。保存实际的远程 ID、日志和断点。重启时，读取并查询它们，在考虑新的提交之前先接管现有工作。

`needs-input` 只用于无法推导的缺失事实、目标或访问权限。在 `open_questions` 中写具体问题、已检查的证据以及可以继续的独立工作；`drive` 会拒绝空列表。缺失 adapter 永远不符合条件。`complete` 必须由成功的 `verify` 确认，而不是由 agent 的最后一句话确认。

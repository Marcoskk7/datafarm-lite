# 交付检查与准入

在渲染出的 episode 与策略训练之间，有三个独立的机制：

- **Render verifier**（`src/datafarm/verify/`）：所有任务共享的一个程序，由 agent 为每个任务编写的 spec 配置。见 [render-verifier](render-verifier.md)。
- **交付验证**（`src/datafarm/task/contract.py::verify_delivery`，仅 white 数据用 `white_delivery.py`）：任务交付是完整、未被修改的，并绑定到其合约、尝试记录、grounding 记录和独立评审。
- **准入证书**（`src/datafarm/core/admission.py`）：由训练启动器消费。一个独立的 root 从存储的证据重新推导被准入的 cohort。

这三者在代码中并不串联；见 [限制](#限制)。`delivery.json` 和评审文件的 schema 见 [`prompts/instructions/task-artifact-contract.md`](../../prompts/instructions/task-artifact-contract.md)。

## 保证

1. **渲染交付绑定其输入、数量和尝试分母。** `input.json` 重新哈希每个锁定的输入及其摘要；`stop_after` 为 `render`；`delivery.json` 使用 `datafarm.agent-delivery/1` 且与输入摘要匹配；`target_count` 等于锁定数量，并交付了同样多的唯一 episode ID；尝试文件列出唯一 ID，结果为 `admitted`、`rejected` 或 `infrastructure_failure`，且 admitted ID 等于交付的 ID；数据集 manifest（`datafarm.dataset-files/1`）恰好包含这些 ID 和哈希绑定的文件。
2. **每个 episode 与其渲染合约和实际提交一致。** 五个产物（`action`、`source`、`video`、`prompt`、`references`）哈希绑定；合约仍可加载；提交回执与合约文件、意图和 prompt 哈希以及真实图像前缀匹配；交付的 prompt 哈希和 references 等于合约中的值；每个 reference 的来源源是锁定的输入文件之一。
3. **每个 episode 都有完全通过、有证据支持的评审。** 评审（`datafarm.task-review/2`）指明生产者。其 subjects 等于当前产物哈希。`task`、`motion`、`reference`、`style`、`alignment` 和 `joint_margin` 的结论必须全部为 `pass`，且其证据文件必须哈希绑定。
4. **视觉评审将解码的真实参考与生成视频比较。** `visual_review` 声明 `inspection_method: decoded-real-reference-and-generated-video`，绑定视频哈希和每个真实参考哈希，并用生成帧编号、比较的 reference 哈希、合约中的视图以及同时出现在评审证据中的可解码对比图像覆盖全部六个 `APPEARANCE_COVERAGE` 项。全场景项需要 `full-view` 证据，`declared_view_photography` 覆盖每个视图。只有 `task_objects` 可以带理由为 `not_applicable`，且仅当合约不要求它时。
5. **评审独立性在 agent 运行时下被验证，在运行时之外仅作声明。** `reviewer_id` 必须不同于生产者。在运行时管理的 workspace（`.runtime/` 含 `reviews`、`author.json` 或 `control.json`）中，评审引用 `runtime_review`。该评审必须具有安全的 ID 和 `completion.json`，通过 `validate_completion`（`task-verifier` 角色、匹配的上下文、transcript、报告、执行和身份哈希，未复用作者的原生会话），结论为 `pass`，具有覆盖每个交付产物哈希的冻结请求，并有一份其证据绑定此 episode 评审文件的报告。此处手写的执行记录会被拒绝。在运行时之外，它会被哈希校验并报告为 `declared-execution-record`。仅当每个 episode 都满足时，`review_independence` 才为 `runtime-verified`。
6. **每个交付绑定有效的 grounding 台账。** 见 [grounding](grounding.md)。结果按标签以 `physical_quantities` 返回计数。
7. **仅 white 交付绝不会被误认为渲染数据或训练数据。** 见 [仅 white 交付](#仅-white-交付)。
8. **准入证书仅凭独立 root 回执和全新证据才通过验证。** `verify_certificate` 从内容寻址存储中按摘要加载证书。它要求一个 `admission` 回执，结论为 `pass`、权限为 `admission-root`、受信的 root ID 不同于证书生产者，且所有检查为 true。任何受信 root 都不得拒绝它。root 的检查会针对存储的证据重新运行。给出 `AdmissionExpectation` 时，来源摘要、allowlist 摘要、`repo_id`、waiver 集合和成员 ID 必须与启动匹配。

代码：`src/datafarm/task/contract.py`、`src/datafarm/task/white_delivery.py`、`src/datafarm/core/admission.py`、`src/datafarm/training/cohort_auditors.py`。

## 数据模型

- **任务 workspace：** `input.json`（`files [{path, sha256}]`、`sha256`、`target_count`、`stop_after`）和 `delivery.json`（`schema`、`input_sha256`、`producer_id`、`target_count`、`grounding`、`attempts`、`dataset`，以及 `episodes[]`，其中含 `id`、`artifacts`、`render_contract`、`submission`、`review`）。每个文件引用都是 `{path, sha256}`。
- **交付结果**（`datafarm.delivery-verification/1`）：`valid`、`episodes`、`attempts`、`delivery_sha256`、`review_independence`、`physical_quantities`、`scope`。
- **准入证书：** 一份 `admission-certificate` IR 文档，位于 `ArtifactStore` 中（profile `datafarm.training-admission/1.0.0`，生产者 `datafarm-training-certificate-compiler-v1`）。它绑定被准入的成员和媒体摘要、dense allowlist 与审计证据、数据集来源、数据集文件的摘要 manifest 和 waiver 策略，并由来自 `datafarm-training-admission-root-v1` 的 gate 回执签名。

## 工作原理

1. Agent 编写 verifier spec，验证一个被接受的渲染，并针对其变异来校准该 spec。
2. `verify_render.py` 对每个 episode 运行校准后的 spec，并写入回执和 HTML 报告。
3. 独立评审者（`pipeline_agent.py drive` 下的运行时评审）将解码视频与真实参考比较，并记录结论和证据。
4. `pipeline_agent.py verify` 对完成的 workspace 运行 `verify_delivery`。
5. 训练时，构建数据集，`issue_admission_certificate.py` 存储证据、编译证书并让 root 签名或拒绝。启动器调用 `verify_certificate`（见 [policy-training](policy-training.md)）。

## 仅 white 交付

当用户明确要求止于 white-mesh 阶段时，使用 `pipeline_agent.py prepare --stop-after white --target-count N ...`。输入锁记录该边界；默认为 `render`。已有的渲染任务不能通过改变交付类型来跳过渲染。如果现有输入锁没有 white-mesh 边界，保存用户的请求，并在新的交付 workspace 中绑定原始输入和现有输出；绝不要重写先前 run 的锁或成功状态。

White mesh 是模拟器输出的完整多视图视频，加上同一次执行中的实际状态和动作。它不声称真实照片外观，也不是训练准入证书。Provider 传输限制属于渲染 profile；white mesh 保留完整轨迹，动作和时钟都不会为适配 provider 长度而被改动。

`delivery.json` 使用 `datafarm.white-delivery/1`，绑定 `input_sha256`、`producer_id`、固定的 `target_count`、`episodes`、所有 `attempts`、`dataset` 和 `reading_guide`。

- `attempts` 保留 admitted、rejected 和 infrastructure failures，合格集合等于交付集合。数据集顺序等于 episode 顺序。
- `dataset` 使用 `datafarm.dataset-files/1`，列出 `episode_ids` 以及所有数据文件的路径和 sha256。数据可以位于外部路径，而 manifest 和评审记录位于交付 workspace 中。
- reading guide 解释实际状态与命令之间的差异、维度、顺序、单位、绝对或增量语义、时钟，以及一个可运行的读取示例。
- 每个 episode 的产物恰好是三个文件引用：`trajectory`、`video` 和 `requirements`。轨迹 manifest（`datafarm.white-trajectory/1`）绑定状态和动作文件及其 `state_schema`、`action_schema` 和 `clock`。视频 manifest（`datafarm.white-video-set/1`）的视图覆盖该 episode 的 `required_views`，每个流的每种介质都列在 `dataset` 中。
- 每个 episode 绑定一份独立执行的评审（`datafarm.white-review/1`）：`producer_id`、`reviewer_id`、`subjects`、`task`、`motion`、`alignment`、`joint_margin` 和 `visual` 结论、`execution`、`evidence`，以及针对所有已声明视图的可解码 `visual_evidence` 图像。`execution` 绑定评审者的 `actor_id`、`producer_id`、`role=reviewer` 和 transcript。不同的名称不能替代独立执行。逐帧解码、状态和动作比较、物理 judge 和关节检查由独立评审执行，并保存证据。照片风格不属于此评审。
- `pipeline_agent.py verify` 返回 `datafarm.white-delivery-verification/1`，其中 `rendering_certified` 和 `training_certified` 始终为 false。它验证文件绑定和评审证据的存在，而非物理或视觉正确性。

## 签发证书

```bash
python tools/issue_admission_certificate.py \
  --store runs/<run>/admission-store \
  --dataset-root <lerobot_home>/<repo_id> \
  --output runs/<run>/training-admission-certificate.json
```

- 退出码 0 表示已准入。该工具打印 `certificate_sha256`、`receipt_sha256`、证据来源及其摘要。
- 退出码 2 表示被拒绝。`failed_checks` 和 `evidence_notes` 说明原因，被拒绝的证书和回执保留在 store 中。
- 退出码 1 表示证据或用法错误。

证据默认值是来源中命名的 allowlist、该 allowlist 同级的 `semantic-audit.json` 和 `pi05-candidate-manifest.json`，以及来源中命名的 distribution 回执。可用 `--semantic-allowlist`、`--dense-audit`、`--candidate-manifest` 或 `--distribution-acceptance` 覆盖它们。`--output` 写入证书的 canonical 字节，其 SHA-256 即证书摘要。

数据集目录在认证后不得更改，因为每次启动都会重新哈希其文件。

```bash
python tools/launch_pi05_training.py train-command --spec <spec> \
  --admission-certificate runs/<run>/training-admission-certificate.json \
  --admission-store runs/<run>/admission-store
```

`--admission-certificate` 接受 64 位十六进制摘要（需要 `--admission-store`）或证书文件。如果文件位于 store 内的 `sha256/xx/<digest>.json` 下，则会推断出 store。

启动 manifest（`datafarm.pi05-train-launch/1.2.0`）记录 `admission.mode = certificate-verified`、验证摘要、数据集文件是否已验证，以及带逐项检查状态的合约审计。它替换掉的 manifest 会保留为 `launch-manifest.<sha12>.superseded.json`。

证书恰好准入一个已构建的数据集。其 waiver 策略为 `zero-waiver`，或 `explicit-waivers` 逐一指明每个批准的 waiver（sample、check、authorizer、reason）。被来源隐藏的 waiver（`allowed_semantic_waivers: []` 而证据却对某个 gate 豁免）会被拒绝。没有已验证证书的启动会被拒绝。

## 移交给训练

- 证书将训练输入绑定到它通过的独立检查，因此视频或配置被替换后不会复用一次通过。文本 allowlist 或手写的状态文件无法替代它。生产者不能充当独立验证者。
- 证书绑定成员、媒体摘要、allowlist、审计和数据集文件。模式意图、实际 reference payload 和真实风格评审由交付检查负责，不绑定进证书。缺少风格证据的证书不能自动升级：原始指标不被修改，授权也不回填。
- 数据构建检查证书和输入哈希，然后验证动作-图像时序、终态、裁剪与拼接，以及状态 schema。被拒绝或缺失的项保留在注册的分母中，替换轨迹有其自己的 ID 以及到其父候选的映射。报告同时说明总生产尝试数和最终准入数量。
- 一个 run 显式声明 `state_conditioning: none`，模型参数与数据一致。delta EEF 的参考帧、单位、时间步和 rollout 接口必须匹配。训练成功仅意味着步骤完成；应分别报告评估协议、真实与模拟边界以及部署限制。
- 训练受台账、GPU 授权和持久化检查约束。只有在 checkpoint、指标、命令、代码版本和证书位置都可验证之后才收尾。

## 失败行为

- 证据缺失时，verifier 返回 `abstain` 而非 `pass`。CLI 对 `fail` 和 `abstain` 都以 2 退出。
- `verify_delivery` 在第一条违规规则处抛出 `ContractError`（`ValueError`）。任何绑定文件中的一个字节变化都会以 "artifact changed" 失败。`pipeline_agent.py drive` 不接受未经验证交付的已完成任务。
- `verify_certificate` 抛出 `AdmissionError`，列出每个失败项。

## 限制

- **交付不要求 verifier 或校准回执。** Render verifier 通过指令把关渲染阶段：[render-batch skill](../../.agents/skills/render-batch/SKILL.md) 要求每个任务有校准后的 spec，且在 H3 路线上验收还需要 `evaluations/render-h3`。
- **交付检查不把关证书。** 准入读取 allowlist 和审计，不读取 `delivery.json`。
- **准入 root 在进程内运行。** 其独立性是一种组件身份（验证者 ID 不同于生产者，并从存储字节重新推导），而非独立执行。
- **声明的独立性未被验证。** 在 agent 运行时之外，它是一个经哈希校验的声明。
- **语义仍由评审者负责。** 交付检查绑定内容和成员关系。物理与视觉正确性来自它要求的评审，而它无法证明主机隔离了身份。

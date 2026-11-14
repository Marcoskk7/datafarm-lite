# 策略训练（pi0.5）

训练位于准入的下游。`tools/launch_pi05_training.py`（`src/datafarm/training/launch.py`）把一份从 `configs/training/template.json` 开始的 JSON
训练 spec 转换为启动清单，以及用于 vendored openpi 训练器（`vendor/trainer/`）的调度器命令。
它首先验证数据集、动作表示、状态条件模式、分布主张和准入绑定。除非每项检查都通过，
否则它不会写出任何可启动的内容。存在两个动作族：

- **EEF**：`absolute_eef`（`configs/training/template.json` 的起点，也是
  `tools/build_pi05_dataset.py --task <instruction> --expected-episodes <N>` 所构建的）、以及 `delta_eef_current_rpy_v1`（`delta_eef.py`）。
  构建器保留每个源帧；`--excise-recovery` 会丢弃故意的未对齐接近恢复绕行，因为其
  重访的关节状态会让无状态策略对同一观测得到两个未来。
- **关节（Joint）**（`joint_actions.py`）：`delta_joint_current_v1` 和 `absolute_joint_v1`。使用 `absolute_joint_v1` 时，
  模型预测绝对关节目标并忽略请求状态。这些模式在启动器中存在，但
  它们需要带有 `state_action_contract` 关节的数据集以及下面的关节源收据，且没有随附
  工具构建这样的数据集；你必须自己提供。

## 输入契约

对于双臂 PiPER 策略族，一个训练样本包含：

- 三个独立的 `224 x 224` RGB 观测：头部、左腕和
  右腕；
- 一个显式声明的状态条件模式 `none`；
- 一个按声明语义顺序、采用已准入时间约定的 14 维动作；
- 一个任务 prompt 和 50 的动作视界。

向 state 仅为 EEF 的数据集添加关节本体感知，需要新的、带版本的状态 schema 和
训练/推理一致性检查；不得静默追加。

把 state 和 action 补齐到模型宽度 32 是实现细节。
它不改变语义维度。像
T-layout carrier 这样的合成审查视频是传输产物；训练前必须解码为
三个相机张量。

PI0.5 可以通过把状态离散化为 256 个 bin 并放入语言
前缀来消费归一化状态。本启动器不使用该路径：每次启动都声明 `state_conditioning: none`，
且模型标志与 CLI 标志必须一致。任何其他值都会被拒绝。

## 为什么显式声明状态条件

在 PI0.5 tokenizer 中，`discrete_state_input=False` 会把 `state=None` 传给 tokenizer，而 PI0.5 没有连续后缀状态 token。因此数据集可以声明一个 14 维的 `observation.state`，由 loader 读取、归一化并补齐到 `(batch, 32)`，而模型只以图像和 prompt 为条件。在未提供覆盖时，模型配置默认 `discrete_state_input` 为 true，因此 false 值是项目特定的覆盖，而不是上游默认值。以这种方式训练的 checkpoint 不具备本体感知能力。

## 保证

1. **启动需要已验证的准入证书。**
   `train-command` 验证 `--admission-certificate`：存储的文档、由不同于生产者的
   verifier 给出的通过准入根收据且无拒绝性根收据、从存储证据重新推导
   准入集合，以及与本数据集 provenance、allowlist、members、repo、waivers 和文件的绑定。
   其他任何启动都会抛出 `AdmissionRequired`。
2. **状态条件模式是显式且一致的。** `state_conditioning` 必须为
   `none`，且命令会传递 `--model.no-discrete-state-input`。
3. **模型接口固定。** 命令总是传递 `--model.action-dim 32`、
   `--model.action-horizon 50` 和 `--model.pi05`。审计要求这些值、14 维
   原生 action，以及恰好三个视角 `cam_high`、`cam_left_wrist` 和
   `cam_right_wrist`。数据集必须暴露 14 维的 `observation.state` 和 `action`
   以及全部三个图像特征。
4. **数据集绑定到已接受的分布且完全生成。** spec
   必须使用 `datafarm.pi05-finetune/1.1.0`，并声明 `target_task` 和一个
   `required_distribution_claim`：
   - `broad_deployment` 要求 `training_scope: production_policy`；
   - `local_robustness` 仅用于探索，需要一个授权 ID。

   `distribution_acceptance` 必须与其 sha256 匹配，且数据集 provenance 必须指名
   该收据。provenance 还必须：
   - 为 `training_eligible`；
   - 固定一个语义 allowlist 及其摘要；
   - 把每个 episode 标记为 `image_source: generated`；
   - 与 spec 的 `allowed_semantic_waivers`、`repo_id` 和 `dataset_fps`（默认 30）匹配。
5. **动作表示被声明且与训练器配置匹配。** `action_representation` 是必需的，
   且必须与 `base_config` 后缀一致（`_delta_eef_current_rpy_v1`、
   `_delta_joint_current_v1`、`_absolute_joint_v1`，或无）。命令运行
   `src/datafarm/training/run_trainer.py`，并带 `--action-adapter delta-eef`、`joint` 或 `none`。关节 adapter 为每个
   Piper base config 注册两个关节 config，而不编辑训练器 checkout。
6. **关节动作具有精确、声明的语义。** 数据集存储绝对关节
   目标（`absolute_next_joint_current_gripper`，14 个字段 `left_joint_1..6,
   left_gripper, right_joint_1..6, right_gripper`，12 个手臂关节为弧度，夹爪单位一
   `m` [0, 0.105] 或 `normalized_0_1`）。
   - `delta_joint_current_v1` 从 12 个手臂关节中减去当前测量的关节状态，
     每个 chunk 使用一个锚点。
   - `absolute_joint_v1` 原样传递目标（形状仍会被验证）。
   - 两种模式下夹爪都保持绝对。
   - `model_representation` 必须与模式匹配。
7. **关节标签来自仿真器回读或经过审计的 drive target。** 数据集
   契约必须声明 `action_joint_t` 为：
   - 下一个保留帧的关节回读，或
   - 关节 *drive target*（用于没有回读的导出），其必须携带一个
     `joint_tracking_audit` 对象。

   夹爪必须是当前帧的专家命令，且字段顺序和单位必须
   等于启动契约。
8. **关节启动绑定精确的源证据。** spec 必须按 sha256 绑定：
   - 数据集 provenance；
   - 一个动作转换证书文件（及其存储）；
   - 一个终止关节回读清单，覆盖每个 episode，带精确的最终关节和
     未变更的源状态；
   - 一个独立的关节机器审计，唯一匹配数据集的 episode 和
     帧计数，并通过要求的精确检查。

   默认情况下，夹爪身份对照 EEF 基线来证明。只有当 spec 声明
   `eef_baseline: {status: not_applicable, reason}`、provenance 不指名任何 EEF 源、
   且审计把该检查记为不适用时，独立的命令重新推导才会取代它。
9. **训练器源码和 tokenizer 被固定。** `trainer_source_sha256` 和 PaliGemma tokenizer 哈希
    必须匹配，且必需的训练器文件必须存在。全局 `batch_size` 必须整除每个 GPU 候选数。运行使用 `--no-overwrite
    --no-resume`。

代码：`tools/launch_pi05_training.py`, `src/datafarm/training/`。

## 数据模型

- **训练 spec**（`datafarm.pi05-finetune/1.1.0`）。字段列在
  *配置*之下。
- **数据集**（LeRobot）：`meta/info.json`（`fps`、`total_episodes`、`total_frames`、
  `features`）和 `meta/lerobot-provenance.json`。provenance 持有 `repo_id`、
  `training_eligible`、`semantic_allowlist(_sha256)`、`episodes_detail[].image_source`、
  `allowed_semantic_waivers` 和 `distribution_acceptance_sha256`。关节数据集额外有
  `state_action_contract` 和 `joint_conversion`。
- **启动清单** `<run_manifest_dir>/launch-manifest.json`
  （`datafarm.pi05-train-launch/1.2.0`）：命令、契约审计（`checks`、
  `statuses`、`admission_mode`）、准入记录和数据集 provenance 摘要。被取代的
  清单会被保留。

## 工作原理

1. 从已准入的 episode 构建 LeRobot 数据集并签发证书
   （[delivery and admission](delivery-and-admission.md)）。
2. `preflight` 验证 spec 和数据集，配合 `--admission-certificate` 时还
   验证证书。
3. `norm-stats` 和 `loader-smoke` 生成归一化并测试数据 loader。
4. `train-command` 构建训练器 argv、解析准入、审计契约并
   写出启动清单。配合 `--execute` 时，它通过 `submit` 提交，后者按顺序尝试
   每个 GPU 数量候选（没有空闲时退出码为 2）。

## 入口点

```bash
python tools/launch_pi05_training.py preflight --spec <spec.json> --admission-certificate <digest> --admission-store <store>
python tools/launch_pi05_training.py norm-stats --spec <spec.json>
python tools/launch_pi05_training.py train-command --spec <spec.json> --admission-certificate <digest> --admission-store <store> --execute
```

## 配置

| 键 | 默认值 | 备注 |
|---|---|---|
| `state_conditioning` | 必需 | `none` |
| `action_representation` | 必需 | `absolute_eef`（模板）、`delta_eef_current_rpy_v1`、`absolute_joint_v1`、`delta_joint_current_v1` |
| `batch_size`, `num_train_steps` | 必需 | 代码不强制。 |
| `fsdp_devices`, `gpu_candidates` | `[fsdp_devices]` | 每个候选必须整除 `batch_size`。 |
| 在命令中固定 | — | action 维度 32，视界 50，最大 token 200；warmup 1000，峰值 LR 2.5e-5 在 30 000 步内衰减到 2.5e-6；AdamW（0.9, 0.95, 1e-8, wd 1e-10）；clip 1.0 |
| `dataset_fps`, `minimum_episodes` | 30, 1 | 数据集检查。 |
| `training_action_contract`, `joint_source_evidence`, `dataset_provenance_sha256`, `admission_certificate {path, sha256, store}` | — | 关节模式必需。 |

训练器、checkpoint 和 tokenizer 的路径是 spec 字段。部署范围的绑定
见 [deployment](../deployment.md)。

## 失败行为

- spec、数据集或证据漂移会在写出任何清单之前抛出 `ValueError` 或 `FileNotFoundError`。
  缺少证书会抛出 `AdmissionRequired`，并给出确切的
  补救方式。
- 契约审计失败会带出失败的检查名。
- 没有空闲的候选分配时，`submit` 退出码为 2，以便队列重试。其他
  调度器错误原样传递。

## 填写模板

`configs/training/template.json` 按原样不可运行。在 `preflight` 通过之前，你需要：

- `<task>`、`<org>/<dataset>` 和 `<authorization-id>` 占位符，以及 `TRAINER`、`TRAINING_PYTHON`、
  `OPENPI_CACHE`、`DATASETS` 和 `TRAINING_RUN` 的绑定；
- `terminal_home_contract.canonical_home_state_14d`，你的机器人的 14 个数字的 home 状态；
- `OPENPI_CACHE` 下的基础 checkpoint 目录和 tokenizer，其哈希必须等于 `paligemma_tokenizer_sha256`；
- `distribution_acceptance.path` 和 `.sha256`，绑定到一份收据（见下文）；
- 一个带 `meta/info.json`、终止于 home 状态的 parquet episode，以及 `meta/lerobot-provenance.json` 的数据集，后者持有
  `repo_id`、`training_eligible`、`semantic_allowlist`、`semantic_allowlist_sha256`、`episodes_detail`、
  `allowed_semantic_waivers`、`distribution_acceptance_sha256`、`training_scope` 和 `production_policy_eligible`。数据集
  构建器会写出这些。

`train-command` 额外需要 `--admission-certificate`。

## 分布接受收据

`distribution_acceptance` 指向一份 `datafarm-distribution-acceptance/1.0.0` JSON 文件，记录实际采集到的 setup 覆盖
（`src/datafarm/training/distribution.py::validate_distribution_acceptance_contract`）。由任务代码或
agent 编写；没有随附工具会做这件事。

| 字段 | 含义 |
|---|---|
| `schema_version` | `datafarm-distribution-acceptance/1.0.0` |
| `status` | `frozen` 或 `accepted` |
| `target_task` | 必须等于 spec 的 `target_task` |
| `claim` | `fixed_setup`、`local_robustness` 或 `broad_deployment`；必须等于 `required_distribution_claim` |
| `variable_objects` | 跨 episode 变化的物体名称的非空列表 |
| `frozen_denominator`, `accepted_count` | 正整数；它们必须相等 |
| `frozen_plan_sha256`, `cohort_manifest_sha256`, `coverage_authority_sha256` | plan、cohort 和覆盖证据的 SHA-256 绑定 |
| `unique_joint_layouts` | 必须等于分母 |
| `unique_relational_layouts` | 正数；`broad_deployment` 需要多于一个 |
| `required_world_span_xy_m` | 每个物体所需的 `[x, y]` 跨度（米） |
| `observed_per_object` | 每个物体的 `unique_positions`（多于一个）和 `span_xy_m`，至少达到所需跨度 |
| `required_image_span_px`, `observed_image_span_px` | 每个相机和物体在像素中的所需与观测 `[x, y]` 跨度；相机集合必须匹配 |

对于 `broad_deployment`，每个所需跨度必须严格为正。数据集 provenance 必须在 `distribution_acceptance_sha256` 中指名该收据的哈希。

## 局限性

- **只有视界被固定。** 批量大小和步数是普通的 spec 值。学习率衰减长度（30 000 步）独立于
  `num_train_steps` 固定。
- **证书不依赖交付验证。** 准入 profile 读取
  allowlist 和审计，而不是 `delivery.json`。
- **没有随附构建器写出关节数据集。** `tools/build_pi05_dataset.py` 写出 XYZ-RPY 契约，因此
  `absolute_joint_v1` 和 `delta_joint_current_v1` 需要你自己构建的数据集和收据。
- **drive-target 跟踪审计只检查形状。** `joint_tracking_audit` 必须
  是对象；其内容不在此处检查。由任务代码生成。
- **仅限生成图像。** 启动器拒绝任何图像源不是 generated 的数据集。

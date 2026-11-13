# 渲染契约

每个渲染请求都绑定到一份任务级*渲染契约*。agent 编写一份渲染
意图（模式、已授权的外观更改、真实参考图像、外观要求）
和一个五槽任务 overlay。`render_policy` 验证两者，并通过 overlay 无法替换的固定
prompt 模板展开它们。结果是单个编译后的契约文件。在
provider 请求离开进程之前，会对照该文件检查实际 payload：
prompt 文本、有序的真实图像字节以及源视频的存在。该
模块不发起网络调用。它证明的是*绑定*。它不证明图像
真的是照片，也不证明模型遵循了 prompt。

## 保证

1. **渲染不能在模式未解析时被编译。** 意图必须使用 schema
   `datafarm.render-intent/1`，`mode` 必须为 `sim2real` 或 `ood`，且 `run_id`、`task_id` 和
   `real_baseline` 必须是非空字符串。
2. **模式确认限定于本次 run、任务和模式。** `mode_confirmation` 必须
   重复相同的 `run_id`、`task_id` 和 `mode`，并带有非空的 `quote` 和 `source`。
   从其他 run 复制的确认会失败。
3. **模式与 OOD 轴一致，且轴是已确认的那些。** `ood` 至少需要
   `authorized_ood_axes` 中一个条目；`sim2real` 不需要。该列表必须与
   `mode_confirmation.authorized_ood_axes` 完全相等。每个轴需要一个非空名称（字符串，
   或带 `axis` 的 dict）。
4. **OOD 轴只能改变外观。** 每个轴的 JSON 文本（名称及任何其他
   字段）都会与 `PROTECTED_OOD_AXIS` 匹配。对机器人（robot、arm、gripper、
   end-effector）、相机（camera、viewpoint、camera/view layout）、物体（object count、
   number of objects、added/removed/extra objects、distractors）或运动（motion、trajectory、
   timing、action、pose）的匹配会拒绝该意图，同一模式中的中文术语也一样。
   这是对轴名称的词汇式、失败即拒绝的防护，不是语义检查。
5. **每个真实场景区域都被分类，且有三个始终必需。**
   `appearance_requirements` 必须恰好具有 `REFERENCE_AREAS` 的键（`robot_body`、
   `attached_cameras_and_cables`、`task_objects`、`table_and_background`、
   `photographic_character`），每个都带布尔 `required` 和非空 `description`。
   `robot_body`、`table_and_background` 和 `photographic_character` 必须为 `required: true`。
   `appearance_views` 必须是互不相同的非空名称。`appearance_invariants` 必须
   非空。
6. **每个真实参考都是可解码、哈希绑定的图像，且来源已声明。** 每个
   条目必须与其 `sha256` 匹配（`bound_file`，允许绝对路径），解码为 PNG、JPEG
   或 WEBP，并声明 `provenance.kind` 为 `real-episode-frame` 或 `real-photo`。`source-frame`
   类型及任何其他结构性类型都会被拒绝。`provenance.source` 本身也是
   哈希绑定的，episode 帧需要非负整数 `frame`。每个条目还
   声明 `scope`（`full-view` 或 `local-detail`）、`views`（`appearance_views` 的子集）、
   每个声明视角的界内整数 `[x, y, w, h]` 区域（位于 `view_regions` 中），
   以及 `coverage`（`REFERENCE_AREAS` 加 `full_scene` 的子集）。
7. **每个声明的视角都有一个全场景真实参考，且参考覆盖每个必需
   区域。** 对于 `appearance_views` 中的每个视角，至少有一个参考具有
   `scope: full-view`、指名该视角并列出 `full_scene`。`coverage` 的并集必须
   包含每个标记为 `required` 的区域。单个局部腕部纹理无法满足这一点。
8. **任务 overlay 恰好填充五个槽位，且不能触及模式或参考策略。**
   overlay 键必须等于 `VARIABLES`（`TASK_DESCRIPTION`、`SOURCE_VIDEO_ROLE`、
   `CAMERA_TIMING_CONTRACT`、`TASK_EVENT_CONTRACT`、`CONTINUITY_CONTRACT`），全部非空。
   `MODE`、参考映射、视角、基线、轴和不变量只来自
   意图。词汇式兜底（`CONFLICT`）会拒绝试图关闭
   真实参考的槽位文本（例如 “ignore real references”、“source-only”）。
9. **prompt 文本来自仓库模板。** 编译后的 prompt 是
   `prompts/render/common-v1.txt` 后接 `prompts/render/<mode>-v1.txt`，用
   `string.Template.substitute` 展开。未展开的 `${` 或 `{{` 会被拒绝。每个模板的路径和 sha256
   都会被记录。
10. **编译后的契约只有在其仍能重新编译为自身时才可用。** 加载时会
    重新验证意图（因此每个参考都会重新计算哈希），并检查 `intent_sha256` 和
    `prompt_sha256`。然后它用*当前*仓库模板重新编译，并要求
    字节相等，因此被编辑的模板、参考或意图会使其失效。如果
    设置了 `DATAFARM_RENDER_MODE`，它必须等于已确认的模式。
11. **Seedance 请求在持久化或发送之前会被检查。** payload 必须包含
    恰好一个文本条目，且等于编译后的 prompt。每个 `image_url` 都必须是内联的
    `data:image/...;base64,` URL；不透明 URL 会被拒绝，因为其字节无法
    验证。解码后的图像哈希必须以有序的真实参考哈希
    开头（缺失、重排或替换的图像会失败），且必须存在非空的 `video_url`。
    `create_once` 在写入 `task.json` 之前、在 POST 之前运行此检查。没有
    契约路径时，它会在任何调用之前抛出。
12. **恢复的 Seedance 任务只有在相同已验证策略下才复用其 provider ID。**
    存储的 `render_policy` 收据必须与当前契约文件哈希、意图和
    prompt 哈希以及真实图像前缀匹配。否则运行失败，而不是重新提交。
13. **H3 清单会全局和逐作业检查。** `reference_images` 文件必须
    哈希到有序真实参考（作为前缀）。清单 prompt 必须等于
    编译后的 prompt，或者当设置了 `prompt_lowering` 时等于 `lower_h3_prompt(contract, lowering)`。
    每个作业必须使用相同的参考列表。chunk-visibility 作业的 prompt 由其闭合 schema 的 `chunk_facts` 重新计算；
    任何其他作业必须复用清单 prompt。

代码：`src/datafarm/render/policy.py`, `src/datafarm/render/seedance/client.py`, `src/datafarm/render/seedance/batch.py`, `tools/render_policy.py`。

## 数据模型

- **渲染意图**（`datafarm.render-intent/1`）：`run_id`、`task_id`、`mode`、
  `mode_confirmation {run_id, task_id, mode, quote, source, authorized_ood_axes}`、
  `real_baseline`、`authorized_ood_axes`、`appearance_views`、`appearance_requirements`
  （五个区域，`{required, description}`）、`appearance_invariants`、`real_references`。
  模板 `configs/render/render-intent.template.json` 被有意设为
  不可编译（`mode: unresolved`、null）。
- **真实参考**：`path`、`sha256`、`provenance {kind, source {path, sha256},
  description, frame?}`, `scope`, `views`, `view_regions {view: [x, y, w, h]}`, `coverage`。相对路径相对意图所在目录解析。
- **Overlay**：恰好带有五个 `VARIABLES` 键的 JSON 对象。
- **编译后契约**（`datafarm.render-contract/1`）：`intent`、`intent_root`（绝对）、
  `overlay`、`prompt`、`prompt_sha256`、`templates [{path, sha256}]`、`intent_sha256`
  （对键排序后的意图的 `json_sha`）。
- **提交收据**（`datafarm.render-submission/1`，由两个检查返回）：
  `contract_sha256`、`intent_sha256`、`prompt_sha256`、`actual_image_sha256s`、
  `payload_sha256`、`provider`（`seedance` 或 `h3`），外加 `model`（Seedance）或
  `prompt_lowering` 和 `submitted_prompt_sha256`（H3）。Seedance 将其存于 `task.json`
  的 `contract.render_policy` 下。H3 worker 将其写入 `policy-receipt.json`。

## 请求中的参考顺序

Seedance 作业在 `real_references` 中持有真实场景图像的权威有序列表，并为每张图像重复 `role=real_scene_reference`，这些图像位于 `references` 中，`path` 和 `sha256` 按相同顺序逐项匹配。其后只能跟 `source_frame_zero`、`continuity_same_state` 或两者。payload 构建器把所有真实图像放在辅助图像之前，策略检查会把实际 payload 前缀与有序哈希比较，因此缺失、重排或替换的图像会在 POST 之前失败。多个作业使用各自的契约路径，且不得并发更改进程环境。请求证据存储哈希，而不是签名 URL 或凭据。

编译后的契约不能证明图像来自真实机器人：源视频和照片以及提取映射仍需要独立审查。查询现有任务 ID 不会发起新请求，也不构成对该记录的准入。

## 工作原理

1. agent 根据当前 episode 和用户限定范围的指令推导意图和 overlay
   （见 [`prompts/instructions/mode-and-style-contract.md`](../../prompts/instructions/mode-and-style-contract.md)）。
2. `tools/render_policy.py compile` 验证意图、检查 overlay、展开
   模板并写出契约。文件以独占模式打开，因此现有
   契约绝不会被覆盖。
3. Seedance：`batch.run_one` 构建 payload。`create_once` 调用 `check_seedance_payload`，传入作业的 `render_contract`（或 `DATAFARM_RENDER_CONTRACT`），
   然后持久化记录，然后 POST。
4. H3：`h3_package.py build` 写出清单和每 chunk 的 prompt，其
   `audit` 调用 `check_h3_payload`。worker 在渲染前重复 `audit`
   （见 [rendering](rendering.md)）。
5. 交付验证重新加载契约，并与存储的提交
   收据比较（见 [delivery and admission](delivery-and-admission.md)）。

## 入口点

```bash
python tools/render_policy.py compile --intent <task>/render-intent.json \
  --overlay <task>/render-overlay.json --output <task>/render-contract.json
python tools/render_policy.py check-seedance --contract <task>/render-contract.json --payload <body.json>
python tools/render_policy.py check-h3 --contract <task>/render-contract.json --payload <job-manifest.json>
```

每个命令都打印 `{"schema": ..., "valid": true}` 并以 0 退出，或把
`render-policy: <reason>` 打印到 stderr 并以 2 退出。

## 配置

| 键 | 默认值 | 效果 |
|---|---|---|
| `DATAFARM_RENDER_CONTRACT` (env) | 未设置 | `create_once` 在作业契约没有 `render_contract` 时使用的契约路径。未设置且缺失时抛出 `ContractError`。 |
| `DATAFARM_RENDER_MODE` (env) | 未设置 | 若设置，必须等于契约的模式，否则 `load_contract` 失败。它不能选择模式。 |
| `prompts/render/common-v1.txt`, `sim2real-v1.txt`, `ood-v1.txt` | 仓库文件 | 核心 prompt 正文。编辑其中一个会使所有基于早期文本编译的契约失效。 |
| `prompts/render/h3-caption-*.txt` | 仓库文件 | H3 lowering（`H3_LOWERINGS`）。 |

## 失败行为

- 所有规则违规都会抛出 `task_contract.ContractError`（一个 `ValueError`），消息
  指明规则。文件缺失会抛出 `FileNotFoundError`，来自 `Path.resolve(strict=True)`。
- `create_once` 在写 `task.json` 之前抛出，因此被拒绝的 payload 不会留下尝试
  记录，也不会发起 HTTP 调用。
- 模板、参考或意图在编译后发生变化的契约，会在
  之后每次加载时失败：恢复时、交付验证时以及 H3 worker 中。

## 局限性

- **只有词汇式防护。** `PROTECTED_OOD_AXIS` 和 `CONFLICT` 是词表。避开它们的改写
  （“manipulator colour”、“a cluttered tabletop”）会通过编译。一些
  无害措辞（“camera exposure”、“lighting timing”）会被拒绝。渲染 reviewer
  仍必须对照这些轴检查渲染内容。
- **跨层的轴词汇不一致。** `PROTECTED_OOD_AXIS` 拒绝提到 `distractors` 的轴。
  渲染 verifier spec 接受 `distractors` 作为受扰动的 OOD
  轴（`src/datafarm/verify/spec.py::OOD_AXES`，以及 `src/datafarm/verify/realism.py::BACKGROUND_AXES`）。
- **来源是声明而非证明。** 代码检查哈希、可解码性和区域
  边界。它无法分辨真实照片和被标为 `real-photo` 的渲染图像。
- **Seedance 源视频只检查存在性。** `check_seedance_payload` 不会
  对 `video_url` 目标做哈希。carrier 字节单独绑定：上传会被
  重新下载并哈希（`src/datafarm/render/seedance/upload_transport.py::exchange`），且配方固定
  `source_sha256`。
- **在 `batch.run_one` 中，carrier 上传发生在策略检查之前。** 当
  `create_once` 拒绝一个有问题的契约作业时，该作业的 carrier 已经发布到了 opt-in 上传主机。
- **真实参考前缀之后的图像不在此处检查。** 结构和连续性
  图像由 `src/datafarm/render/seedance/references.py::images` 验证，而不是由
  `render_policy` 验证。
- **H3 chunk facts 按原样信任。** `check_h3_payload` 从清单的 `chunk_facts` 重新计算每个 chunk prompt。
  它不会从 carrier 重新测量这些事实，也不检查 `chunk_facts_detector`。
- **模板由哈希保护，而非访问控制。** `prompts/render/` 不在
  演化控制器的保护列表中（`src/datafarm/evolution/controller.py::PROTECTED`）。
  编辑会被检测到，因为更早的契约停止加载；但没有任何机制阻止新契约
  针对被编辑的模板编译。
- **契约与其位置绑定。** 它存储绝对的 `intent_root`，因此移动
  run 目录会使其失效。
- **没有质量主张。** 有效的契约和 payload 并不说明生成的视频
  是否遵循 prompt。那是验证和审查的职责。

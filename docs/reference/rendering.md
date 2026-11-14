# 渲染：H3 control hybrid（默认）与 Seedance

渲染器将模拟的 white carrier 视频（动作、物体和时间已经固定）转换为摄影级多相机视频。默认渲染器是 **H3 control hybrid**：MiniMax-H3 Ref2VA，带有 H3-Fun ControlNet-Union 分支，由 carrier 的 gated soft-HED 边缘和一个 white inpaint source 驱动。它在平铺画布上渲染独立 chunk，每个 chunk 8 个 seed 由 VLM 选择，使用最小差异接缝和首 chunk 曝光锁定。第二渲染器是托管的 **Seedance** API。每个请求都绑定到 task 的 [render contract](render-contract.md)。agent 关于 canary 和评审的规则在 [`prompts/instructions/render.md`](../../prompts/instructions/render.md) 中。

## 两个渲染器共享的规则

渲染器只产生候选观测。它们不能决定动作、物理状态、相机、物体数量、接触、task 成功或训练 admission。一次成功的 provider 返回、可解码的 MP4、相等的帧数或逼真的画面都不是通过。

| 内容 | 最终权威 | provider 可否更改它 |
|---|---|---|
| 动作、状态、接触、task 事件 | 模拟 episode | 否 |
| 相机身份、内参、外参、每帧位姿、面板角色 | grounding 记录和 episode | 否 |
| 物体和机器人的数量、身份、几何、位姿、拓扑、遮挡 | 模拟 episode | 否 |
| 规范帧索引和动作映射 | 模拟 episode | 否 |
| 对材质、颜色、纹理、光照和 task 外部场景的允许更改 | render contract | 仅在声明的范围内 |
| 参考的用途和使用权 | grounding 记录和 render contract | 不得自行扩大 |
| 输出是否可接受 | verifier、独立评审、admission | provider 不得签署 |

**canonical frame** 是动作和状态的权威帧；其索引不随 provider 改变。**transport frame** 是为了满足 provider 的 FPS、时长或帧网格要求而编码的帧，必须能映射回 canonical frame。**recipe** 是 provider profile、prompt、reference bundle、帧和 chunk 计划、模型和验证版本的冻结组合。

以下是流程，由 agent 及其 reviewer 执行：

- **冻结输入。** 批次冻结分母、样本身份、训练和 held-out 划分以及 recipe。重复一个 task key 会恢复或返回原始尝试，绝不静默创建第二个付费 task。
- **profile 失败关闭。** profile 声明 provider、model、route、输入 FPS、帧或时长包络、帧网格、并发和资格范围。一个 provider 的限制不会被另一个继承。当 profile 缺失、超出包络或 recipe 未获资格时，停止而不是猜测。
- **给每个参考一个窄角色**，在 GPU 或 API 调用之前检查。scene plate 只覆盖 task 外部场景、桌面、曝光和光照。物体外观参考尽可能位置中立。机器人外观参考不得携带不兼容的完整手臂位姿。结构 guide 必须与相机、物体投影、机器人占用、交互状态和全局帧一致。连续性 guide 只来自同一 episode 的一个选定父片段，并绑定到其哈希。
- **保持帧精确。** 保存规范时钟、transport FPS 和每个输出帧的 canonical index、面板顺序/尺寸/裁剪/填充/缩放的可逆链条，以及每个 chunk 的 source range 和 keep range。每个 canonical index 恰好出现一次，在动作区域没有插值、删除、插入或交叉淡化。
- **仅在完整 episode canary 之后扩展。** 确定性地选择至少 8 个 setup，覆盖布局的两端和中间，每个都是带所有关键事件、终止状态和每个接缝的完整 episode。固定 recipe 必须至少通过 75%，且没有常见的失败检查覆盖半数或更多的 canary。将失败归因到 `temporal_control`、`geometry_reference`、`appearance_reference`、`continuation`、`provider` 或 `verifier`。prompt、reference、guide、分割、profile 或 verifier 的任何改变都会使 canary 失效。从多个 seed 中挑选最好的，或只运行第一个 chunk，都不是生产授权。
- **恢复，而非重新提交。** 保留请求摘要、task ID、model、seed、状态更新、原始响应、输出哈希、成本和错误类别。有 task ID 时只轮询和下载。创建不明确时，保留该模糊尝试并隔离它。遇到确定的不可重试 4xx 时，将输入标记为 blocked。原始输出绝不覆盖。
- **按顺序 gate：** transport；相机和面板；实体数量和身份；事件时序；夹爪、物体和容器拓扑；终止状态；外观（参考采纳和 source 保留是分开的硬 gate）；每个接缝处的连续性；来源、权利、成本和完整分母。高相关性或真实感分数不能覆盖实体、接触、阶段或终止状态中的硬错误。证据不足是 `abstain`，绝不是通过。
- **在正确的层级修复。** 对于孤立的随机拒绝，冻结已接受的样本和 chunk，只用确定性的新 seed 重绘涉事单元，并重跑不变的 verifier。对于系统性失败或低产出前缀，停止扩展，在最早的归属处修复，并重跑同一 canary。分母不变，失败样本及其成本被保留，完整通过不得来自放宽阈值、替换 carrier、更改动作或丢弃硬 setup。

## H3 control hybrid

| 位置 | 内容 |
|---|---|
| `src/datafarm/render/h3/` | 渲染和后处理工具；参见其 `README.md` 了解按工具列出的步骤表 |
| `src/datafarm/render/h3/BUNDLE-SCHEMA.md` | Task bundle 格式 |
| `vendor/videox_fun/` | 来自 upstream VideoX-Fun 的补丁，添加 `MiniMaxH3ControlPipeline` |
| `configs/render/h3-control-hybrid-v1.json` | 与 task 无关的默认 recipe，包括 reference policy |
| `configs/render/h3-ood-restyle-v1.json` | 对桌面材质和光色的 OOD 编辑 |
| `evaluations/render-h3/` | 冻结的 EVAL-v1（自动打分、独立盲评审说明）和 seed 选择 prompt |
| `src/datafarm/render/policy.py` | `lower_h3_prompt`, `chunk_visibility_sentences`, `check_h3_payload` |
| `prompts/render/h3-caption-*.txt` | H3 caption 风格 prompt 模板 |

### 流程

```text
Captured white attempt + the task's real episode
  │ datafarm.simulation.white_replay  (render-only: real robot colour, object shapes, scene dressing, dense image
  │   rate, wide wrist cameras; the state log must stay identical)  →  h3_carrier.py compose / check
  │ h3_episode_references.py build  (f0, robot-visible frame, robot / object / wrist sheets within the memory budget)
  ▼
White carrier (one per episode, T layout on a 1024x768 canvas with gutters)
  │ h3_enqueue.py --seed-offset 0..7 --per-chunk  →  h3_package.py build
  │   · compile the contract: h3_bundle.py → tools/render_policy.py compile (mode and axes only from bundle.mode_confirmations)
  │   · the last carrier frame held 48 frames past the end (chunk.tail_pad), cut back after assembly
  │   · independent chunks: 192 frames, advance 120, the first 72 frames are guidance
  │   · soft-HED control with a luma-step shadow gate; active repaint of the whole region
  │   · keep regions (optional): real pixels kept where the white shows a task state
  │   · chunk prompt = lower_h3_prompt(contract, h3-caption-chunkvis-notask) + facts measured on the white clip
  │   · references: a real scene frame matching the carrier's state, a robot reference keeping the real arm appearance
  │   · check_h3_payload runs when building and again before the worker renders
  ▼
h3_pool.py run (resident + elastic workers) or launch_control_worker.sh [memory_mode] per GPU → h3_control_worker.py
  ▼
h3_screen_prep.py composites → a VLM scores each chunk (evaluations/render-h3/prompts/seed-screen.txt) → choice.json
  (parts from several agents are merged with h3_screen_prep.py merge)
  ▼
h3_finalize.py select: h3_select.py --choice-file (the occupancy screen when the VLM gave no usable choice.json)
  → h3_assemble.py (min-diff seams, min_lead 56) → EVAL-v1 scoring
  → h3_exposure_lock.py (gain toward the first chunk, pixel positions untouched) → final-t3-<fps>fps.mp4
  → h3_wrist_crop.py when the episode has wide_wrist (wrist views cropped back to the white's own camera model)
  ▼
h3_score_and_pack.py --packages <pkg> (scores, writes <pkg>/pack) → h3_review_prep.py blind-review jobs
  → independent review per reviewer-instructions-v1.md (the only acceptance)
  ▼
h3_gate.py record/status: 2 prefixes → canary (≥ 75% accepted, no blocking kind in half of it) → the rest;
  h3_enqueue.py --gate refuses episodes the gate has not released
```

### Recipe 理由

| 做法 | 理由 |
|---|---|
| 使用 ControlNet hybrid，而非原生 Ref2VA | 原生 Ref2VA 复制参考图像的位姿。hybrid 使用 white model 的边缘结构作为 control 行，因此运动跟随模拟 |
| 带引导帧的独立 chunk，绝不链式 | 从上一个输出引导一个 chunk 会逐 chunk 累积伪影、暗角和偏色。独立 chunk 以最小差异硬切拼接，无交叉淡化 |
| 首 chunk 曝光增益锁定（过原点的比例增益） | 消除 chunk 之间的亮度跳变。直线拟合在静态腕部视角中会发散。不要朝真实相机的颜色校准，那会移动整幅画面 |
| 以 white model 作为 39 帧视频参考 | 抑制模型原本会虚构的内容，例如多余的夹爪或空夹爪下的物体。全部 192 帧会使 GPU 显存溢出 |
| 机器人参考保留真实手臂外观 | 存在 white 视频参考时，如果真实参考中看不到真实 link，模型会复制 white 手臂 |
| white 中的机器人 link 按真实机器人着色 | H3 复制 carrier：即使是真实机器人参考，苍白 simulator link 也会保持白色或银色 |
| 场景参考与 carrier 的 task 状态匹配 | 参考和 carrier 之间的状态差异会在 chunk 之间闪烁 |
| 对于离开视野的物体，不使用孤立物体图像 | 孤立物体图像会让物体在被收起后又在画面中出现 |
| 对于留在视野中的物体，使用物体近景 sheet | 腕部相机看到的物体比头部相机任何时候都更近；没有近景，它们会得到看起来像代理的物体 |
| 参考按编码面积预算（≤ 25 MP） | H3 以 2048 像素短边编码每张图像，因此 1280x360 的条带耗费约 15 MP；31.9 MP 会耗尽显存 |
| white 中仅渲染的物体形状和场景布置 | H3 保留 white 的形状和背景：圆柱代理保持为普通圆柱，昏暗或缺失的墙变成炭灰色墙 |
| 宽腕部 white，之后裁回（可选） | 宽视角给 H3 提供夹爪周围的场景上下文；裁剪恢复 white 自己的内参。代价：原始视角只是渲染面板的 0.65 并被放大回去，因此腕部细节下降（叠杯 canary：约为窄腕部渲染的拉普拉斯方差的一半）。当窄腕部视角使 H3 虚构墙或桌边缘时使用它，而不是默认使用 |
| 低于 20 fps 的 white 密集重渲染，绝不插值 | 运动插值会扭曲和拖拽快速物体；H3 会复制这种漂移 |
| 最后一帧 carrier 在结尾之后保持 48 帧 | 最后一个 chunk 的结尾是 H3 虚构运动或额外物体之处；填充会被重新裁掉 |
| 为模型未跟随的 task 状态保留真实像素 | 小的状态变化（显示屏亮起）会在错误的时间渲染；keep region 从真实像素复制它 |
| 逐 chunk 可见性 prompt，不写整 episode task 句子 | 整 episode 描述会让模型提前绘制未来状态。`-notask` 只保留 "the action follows Video 1" |
| 每 chunk 8 个 seed，并逐 chunk 做 VLM 筛查 | 像素指标无法捕捉内容错误，例如线缆、额外物体或 white 手臂。筛查只是选择；接受是独立盲评审 |
| OOD：跨 chunk 使用相同 seed，在输出端改变材质和光照 | 混合 seed 会让桌面颜色跳变。在输出端改变外观不触碰内容，而生成侧 OOD 可能在腕部视角回退成 white 桌面 |

### 准备 white carrier 和参考

渲染器复制 carrier 所显示的内容，因此大多数可见的渲染失败都在 white 中修复，而不是在 prompt 中。以下每一步的证据都来自对 2026-11 生产渲染（双臂 Piper 鼓、称苹果和叠杯；RealBot-01 舀麦片）的独立盲评审，其中每一步都将一次拒绝变为接受，或移除了 reviewer 指名的缺陷。

1. **真实参考。** 写 `tasks/<task>/episode-references.json` 并运行 `h3_episode_references.py build`。它从 task 自己的真实 episode 中、在 25 MP 预算内推导起始帧、真实机器人最可见的帧、机器人 sheet、物体近景 sheet（外观变化的物体带第二块 tile）以及叠放的腕部 sheet。机器人像素来自深色机器人启发式；对于浅色机器人或深色家具，先用 `h3_robot_masks.py` 从 task 已验证的运动学生成投影标签掩码并设置 `robot_masks`。对于 white-on-white 或分裂物体，给出 `object_boxes`。当物体在 task 期间离开视野时，关闭 `object_sheet`。
2. **Replay spec。** `h3_episode_references.py robot-colors` 为 white replay 写出 `@robot`（以及带掩码的 `@robot_links`）。在 simulator 代理与真实物体不同处添加仅渲染的物体形状，在 white 缺少真实墙、柜台或道具处添加仅渲染的 `extras`（simulator 照明昏暗的明亮真实墙需要 `emission`），为低于 20 fps 采集的 white 设置 `image_stride`，为宽腕部视角设置 `wrist_fov_factor` 0.65（仅当窄腕部视角使 H3 虚构房间边缘时；它会软化腕部细节）。用 `python -m datafarm.simulation.white_replay --check-spec` 验证。
3. **Replay。** 在能渲染的 host 上运行 `python -m datafarm.simulation.white_replay <attempt> <out> --spec <spec>`。当状态日志与尝试的不同，或请求的更改未被应用时，它拒绝成功。注意共面视觉部件（z-fighting）以及布置部件之间的接缝，H3 会把它们画成棍状。
4. **Carrier。** `h3_carrier.py compose --replay <out> --attempt <attempt> --out <carrier>`，然后 `h3_carrier.py check <carrier>`。在 bundle episode 上记录 `wide_wrist`（因子以及来自 replay 报告的腕部主点）。
5. **Keep regions**（仅当某 task 状态是模型在错误时间渲染的小细节时）：写一个 `h3.keep-regions/1` spec，将其 `states` 作为 `@states` 放入 replay 颜色，并将其命名为 episode 的 `keep_regions`。

replay 仅是渲染输入。它绝不取代尝试、其 judge 结论或其 admission，也不得更改动作、时序或物理。

### 用于新 task

bundle、参考图像、episode 列表、OOD 变体、recipe 副本和 seed 选择结果都是 task 的输出，由 agent 从当前真实 episode 推导并验证。

1. 确认模式。sim2real 或 OOD（及其轴）只能来自当前用户在范围内的指令。将确切措辞和来源写入 bundle 的 `mode_confirmations`。缺失时，`h3_bundle.py` 拒绝编译。
2. 创建 run 目录（`tasks/ recipes/ queue/ eval/realpool/`）并写 `tasks/<task>/bundle.json`：完整的 white carrier 和如上准备的参考、五个 overlay 槽位和真实基线。`h3_clean_references.py` 也可以生成无物体的 scene plate 和无线缆的 sheet。运行 `h3_bundle.py check <bundle> --run-dir <run>`；它在 carrier 低于 20 fps 时失败，并在头部视角没有 control 可见的墙或桌面接缝时警告。每个 task 从真实参考和 carrier 本身写一次文本字段（物体身份、颜色、尺寸和数量；阶段、事件和终止关系）；task 的每个 episode 随后复用该 bundle 文本。通用模板文本会丢失物体身份：用 "the objects follow the source video" 时，鼓被画成了普通圆柱。
3. 将 `configs/render/h3-control-hybrid-v1.json` 复制到 `<run>/recipes/<name>.json`，只更改 `name`、`bundle_variant` 以及有证据支撑的逐 task 开关（`prompt_lowering`、`chunk.event_aware`）。
4. 一次调用排队 8 个 seed，并在用户和 deployment 允许的 host（`<run>/queue/ALLOWED_HOSTS`）上启动 workers。第一个 seed 的 package 先构建，并填充其他 seed 复用的 HED 缓存；构建以 nice 10 运行并共享 `--build-slots`（默认 2）个 run 范围的槽位，因为每次构建运行 4 个 16 线程的 HED 进程。对于多个 episode，用 `h3_pool.py` 维持 workers：`resident` workers 保持加载（在 2026-11 canary 中，一个 resident worker 的模型加载约耗时 1000 秒），在 ticket 待处理时弹性 workers 会添加到 `max_workers` 为止。
5. 选择 seed、组装、打分、锁定、打包和评审。生产先运行 2 个完整 episode 前缀，然后固定运行 8 个 canary，使用固定分母并保留所有失败。`h3_gate.py init` 在第一次渲染前固定三个列表；用 `--gate` 排队，并在每个最终 package（或渲染失败）完成时 `record`。gate 只读取盲评审结论。当 seed-screen 会话失败或留下部分 `choice.json` 时，`h3_finalize.py select` 回退到占用筛查并记录原因；episode 仍进入盲评审。

```bash
export H3_RUN=/data/runs/<run> ; H=<datafarm>/src/datafarm/render/h3
python $H/h3_gate.py init --gate <name>-g1 --recipe <name> --task <task> --prefix <e1> <e2> \
  --canary <e3> <e4> <e5> <e6> <e7> <e8> <e9> <e10> --rest <e11> ...
python $H/h3_enqueue.py --recipe $H3_RUN/recipes/<name>.json --tasks <task> --gate <name>-g1 \
  --episodes <episode> --modes sim2real --seed-offset 0 1 2 3 4 5 6 7 --priority 001 --per-chunk
bash $H/launch_control_worker.sh full     # submit once per GPU, or keep a pool:
echo '{"schema": "h3.worker-pool/1", "resident": 1, "max_workers": 4}' > $H3_RUN/queue/pool.json
setsid nohup python $H/h3_pool.py run > $H3_RUN/queue/pool.out 2>&1 &   # touch $H3_RUN/queue/STOP to stop all
B=pkgs/<name>/<task>/<episode>-sim2real
cd $H3_RUN && python $H/h3_screen_prep.py composites --cands $B $B-s1 $B-s2 $B-s3 $B-s4 $B-s5 $B-s6 $B-s7 --out screen/<task>/<ep>
# the VLM writes screen/<task>/<ep>/choice.json per the seed-screen prompt
python $H/h3_finalize.py select --render-recipe <name> --task <task> --episode-tag <episode>-sim2real --name <name>v-vlm \
  --choice-file screen/<task>/<ep>/choice.json --seeds s0 s1 s2 s3 s4 s5 s6 s7
P=$H3_RUN/pkgs/exposure-lock-<name>v-vlm/<task>/<episode>-sim2real    # holds final-t3-<fps>fps.mp4
python $H/h3_score_and_pack.py --packages $P
python $H/h3_review_prep.py --subjects $P
# the independent reviewer writes reviews/<rid>/review/review.json
python $H/h3_gate.py record --gate <name>-g1 --episode <episode> --package $P   # or --failed "<reason>"
python $H/h3_gate.py status --gate <name>-g1      # stage, verdicts, the episodes released for rendering
```

单 package 命令：

```bash
H3_RUN=<run> python src/datafarm/render/h3/h3_package.py build --bundle <run>/tasks/<task>/bundle.json \
  --recipe <run>/recipes/<name>.json --episode <ep> --mode sim2real --out <run>/pkgs/<name>/<task>/<ep>-sim2real
python src/datafarm/render/h3/h3_package.py check <package>
python src/datafarm/render/h3/h3_paths.py show
python evaluations/render-h3/h3eval.py score --generated <final.mp4> --carrier <carrier.mp4> --out <score.json>
```

`h3_requeue_orphans.py` 将已死 scheduler workers 的 ticket 退回队列；在 local 模式下 `h3_pool.py` 为它启动的 workers 做这件事。`h3_pool.py status` 打印池状态（pending、running、存活 workers、最后错误）。

### OOD（桌面材质和光色）

参见 `configs/render/h3-ood-restyle-v1.json`。推荐路线在输出端改变外观，作用于已通过盲评审的 sim2real 结果。`h3_sam2_table_masks.py` 在生成的视频上按视角跟踪桌面（每 96 帧重新播种点）。`h3_ood_restyle.py` 减去从 white model 已知的物体，加上投影阴影，并在跟踪丢失时回退到从 white model 测得的桌面。桌面像素变成目标材质乘以生成亮度除以平滑后的桌面亮度，保留阴影、接触和噪声。光色只施加保持均值的色温增益。优先使用均匀材质（`--material-bgr`），因为腕部相机会移动，纹理无法跟随。

另一种做法是用 OOD 契约渲染 sim2real recipe，所有 chunk 使用相同 seed。带纹理的材质需要在模拟中为桌面贴纹理并重新导出 carrier。任何 OOD 都需要当前用户批准的轴。

### 依赖

- **GPU：** 每个 worker 占一个 GPU，对于 1024x768 画布且 192 帧 chunk 完全常驻，默认需要至少 172 GB 空闲显存（`H3_MIN_FREE_MB`）。
- **Python**（`H3_PYTHON`）：torch、diffusers、transformers、omegaconf、safetensors、opencv-python、numpy；系统 `ffmpeg` 和 `ffprobe`。打包、seed 选择和曝光锁定只需要 numpy 和 opencv。
- **外部资产**（`configs/external-assets.json`，doctor profile `h3-control`）：MiniMax-H3 Ref2VA 目录、H3-Fun ControlNet-Union 权重、MiniMax-H3 Turbo 4 步 LoRA、ControlNetHED annotator 权重、已应用 `videox-fun-datafarm.patch` 的 VideoX-Fun 仓库（参见 `vendor/videox_fun/README.md`）、ControlNet v1.1 annotator 源码、DINOv2-base（EVAL-v1）和 SAM2.1-hiera-large（仅 OOD）。
- **绑定**（优先环境变量，然后 `DATAFARM_<KEY>`，然后绑定文件；没有内置默认值，键或路径缺失即失败）：`H3_RUN`（或 `--run-dir`）、`H3_PYTHON`（默认：当前解释器）、`H3_MODELS`、`H3_VIDEOX_FUN`、`H3_TURBO_LORA`、`GPU_SCHEDULER`、`H3_EVAL_DINO`、`H3_SAM2`、`H3_HED_ANNOTATOR_REPO`。`python3 src/datafarm/render/h3/h3_paths.py show` 打印解析结果。
- **调度：** `launch_control_worker.sh` 调用 `$GPU_SCHEDULER submit`，host 来自 `<run>/queue/ALLOWED_HOSTS`。接口见 [deployment](../deployment.md#gpu-scheduling)。Local 模式（`H3_LOCAL_GPU=<index>`，或 `local_gpus` + `local_host`（在 `pool.json` 中））在没有 scheduler 的情况下在专用节点的 GPU 上运行同一个 worker；此时没有任何东西阻止其他用户的作业占用该 GPU，因此仅在 run 拥有的节点上使用它。

### 保证

1. **每个 package 两次通过 render-policy gate。** `build` 在所有 control 输入齐备之前隐藏 ticket，然后运行 `audit`。审计失败会把 ticket 放回 `.partial`。worker 在渲染前再次运行 `audit` 并写 `policy-receipt.json`。`audit` 调用 `check_h3_payload`，并要求 manifest schema `h3.control-hybrid-manifest/1`、`controlnet: true` 和 `reference_order: images-first`。每个 chunk 必须具有合法的 17n+5 长度（124–362 帧），且每个 control、inpaint 和 mask 输入必须是画布尺寸、sha256 匹配的文件。
2. **模式只来自 bundle 记录的确认。** `h3_bundle.py` 从 `bundle.mode_confirmations.<mode>` 构建渲染意图，没有确认则拒绝编译。
3. **每个 chunk prompt 是契约加封闭 schema 事实的确定性降级。** 对于 `h3-caption-chunkvis-*` 降级，builder 从 white 片段测量 `chunk_facts`（`head_absent_arms`、`empty_wrists`、`head_max_coloured_objects`、`wrist_large_objects`），写 `lower_h3_prompt(contract, lowering, chunk_facts)` 并记录 `chunk_facts_detector`。自由文本无法进入：schema 是封闭的，且 gate 会重新计算 prompt。
4. **浅色物体不会被报告为 "empty" 腕部视角。** `wrist-structure-v1` 在饱和像素超过视角的 0.4 %，或 `wrist_structure()` 至少为 0.75 时，将某个采样帧中的腕部视角计为显示有物体。`wrist_structure()` 是去除深色机器人、至多 4 条笔直房间接缝以及小于 12 px 的斑点后剩下的 Canny 边缘长度，以视角宽度为单位。只有当至多 10 % 的采样帧显示有物体时，视角才是 "empty"。
5. **control 边缘忽略 simulator 投射阴影。** soft-HED 图乘以 carrier 中强局部亮度阶跃的掩码（在 7 px 的 `window` 上、模糊灰阶图像的 max−min > `tau` 25，膨胀 5 px）。结果必须恰好有 `generated_frames` 帧。
6. **默认 recipe 被固定：** backend `control-hybrid`、canvas `t3-guttered-1024x768`、profile `control-turbo`、4 次 denoise 前向；chunk 为 192 帧、`advance` 120、`keep_head` 72、`tail_pad` 48；带 luma-step gate 的 soft-HED、`white_video_reference` 39；降级 `h3-caption-chunkvis-notask`、`min-diff` 接缝且 `min_lead` 56；8 个 seed（偏移 0–7）；首 chunk 曝光锁定且不做真实相机校准。模型路径是 `${H3_*}` 占位符。
7. **Deployment 路径失败关闭。** 缺失或不存在的值会引发 `MissingBinding`。
8. **能力演化不能改变 evaluator。** `evaluations/` 在演化控制器的受保护列表中。
9. **填充绝不进入输出。** 使用 `tail_pad` 时，画布 carrier 和 chunk 计划覆盖 episode 加上保留帧；`h3_assemble.py` 和 `h3_exposure_lock.py` 将输出裁回 `frames`，因此输出帧 *i* 就是 carrier 帧 *i*，且计数与 carrier 片段匹配。
10. **white replay 仅在轨迹相同时被接受。** `white_replay` 将 replay 的状态日志与尝试的比较（除 PhysX 接触报告外的每个字段，或 `log.npz` 的每个数组），当不同或请求的相机、颜色、stride、形状或布置更改未被应用时以 exit 2 退出。
11. **keep region 在审计之前打补丁。** `h3_package.py build` 在 episode 指定 `keep_regions` 时，为 chunk inpaint 视频和 repaint mask 打补丁，记录每个区域保留的帧，并在这些区域不出现在任何 white 帧中时停止；随后审计对打过补丁的输入做哈希。

代码：`src/datafarm/render/h3/`、`src/datafarm/render/policy.py`。

### 数据模型

- **Bundle** `<run>/tasks/<task>/bundle.json`：carrier、真实参考、五个 overlay 槽位、真实基线、`mode_confirmations`，以及一个扁平的 episode 列表。
- **Recipe** `<run>/recipes/<name>.json`（`datafarm.h3-render-recipe/1`）。代码使用的字段：`backend`、`canvas`、`worker_profile`、`denoise_forwards`、`chunk {frames, advance, keep_head, clip_frames, clip_offset, event_aware?}`、`seed_base`、`control {...}`、`prompt_lowering`、`assembly {seam, min_lead}`，可选的 `chunk_facts_wrist`。
- **Package**（`h3.package/1`）带 `job-manifest.json`（`h3.control-hybrid-manifest/1`）。每个 job 有 `chunk_id`、`chunk_index`、`seed`、`source_frame_range`、`generated_frames`、`white`、`control_video`、`inpaint_video`、`inpaint_mask`（带 `_sha256`）、`reference_images`、`prompt`、`chunk_facts` 和 `chunk_facts_detector`。package 还保存 `ticket.json`。worker 输出：`chunks/<chunk_id>/chunk-render.mp4`、`segment-manifest.json`、`policy-receipt.json`。
- **Seed 选择** `screen/<task>/<episode>/choice.json`（`h3.seed-screen/1`）：`chunks [{chunk, choice, scores}]`、`screener`。由 agent 而非代码写入。所选 package 的 `selection` 记录 `mode`（`screen` 或 `occupancy`）、选择，以及当占用筛查顶替时的 `fallback_reason`。
- **Gate** `gates/<name>.json`（`h3.render-gate/1`）：`recipe`、`task`、`prefix`、`canary`、`rest`（在 `init` 时固定）和 `records {episode: {package} | {failed}}`。结论从 `reviews/` 读取（在 `status` 时），绝不手工存储。

### 配置

| 键 | 默认值 | 效果 |
|---|---|---|
| recipe `chunk_facts_wrist` | `wrist-structure-v1` | 唯一的检测器；未知名称会退出 |
| recipe `chunk.event_aware` | 缺失 | `{before, min_advance}` 将 chunk 起点前移到手臂进入和退出事件之前 |
| recipe `chunk.tail_pad` | 48 | 分块前最后一帧 carrier 在结尾之后保持的帧数；组装后裁回 |
| bundle episode `wide_wrist` | 缺失 | 宽 FOV 腕部 white 的 `{factor, principal_points}`；最终会裁回 |
| bundle episode `keep_regions` | 缺失 | `h3.keep-regions/1` spec 路径；在 white 显示该状态处保留真实像素 |
| `h3_episode_references.py` spec `budget_mpix` | 25 | 每个 ticket 的编码参考面积 |
| recipe `seeds.count` / `seed_offsets` | 8 / 0–7 | 代码不读取；操作员将偏移传给 `h3_enqueue.py --seed-offset 0 1 … 7`。Seed = `seed_base` (1000) + 偏移 |
| `h3_enqueue.py --build-slots` | 环境变量 `H3_BUILD_SLOTS` 或 2 | 整个 run 范围内并发的 package 构建（`<run>/cache/build-slots`） |
| `<run>/queue/pool.json` | 缺失 | `h3_pool.py`：`resident`、`max_workers`、`resident_idle_min` (720)、`elastic_idle_min` (20)、`min_free_mb` (172000)、`profile`、可选的 `local_gpus` + `local_host`；每个周期重新读取 |
| `h3_enqueue.py --episodes` | 全部 | bundle 中的 episode ID |
| `build --hed-device` / `--hed-jobs` / `--hed-threads` | `cpu` / 4 / 16 | HED 运行的位置和方式 |
| `launch_control_worker.sh [memory_mode]` | `full` | worker 内存模式；`H3_IDLE_EXIT` 和 `H3_MAX_TICKETS` 约束其生命周期 |

### 失败行为

任何绑定、审计或形状失败都会在渲染之前停止 build 或 worker，失败 build 的 ticket 保持 `.partial`。worker 按 chunk 恢复：既有 output 又有 segment manifest 的 chunk 会被跳过。

### 限制

- **canary gate 仅通过 `--gate` 生效。** `h3_enqueue.py` 不带 `--gate` 时会渲染任何东西；[render-batch skill](../../.agents/skills/render-batch/SKILL.md) 要求生产使用 gate。75% 规则和 common-kind 规则只统计盲评审结论，因此 gate 的好坏取决于评审，且它不判断 canary episode 是否覆盖布局。
- **没有自动重渲染路径。** 由 agent 运行的 VLM 写 `choice.json`；没有代码调用 VLM，被标记的 chunk 不会被重渲染。没有可用的 choice 文件时，像素占用筛查挑选 seed。它会漏掉 VLM 能看到的内容错误（线缆、white 手臂），并假设 carrier 中是深色机器人。
- **默认 recipe 是起点，不是资格。** 新 task 需要它自己的完整 episode canary 和独立评审，OOD 不是自动的。
- **评审中要检查的伪影。** 阴影可能显得生硬或涂抹，伴有轻微偏色、暗角和错乱的小字。
- **reference policy 是建议，不是代码。** `references_policy` 中的文本不被强制执行；代码只强制执行哈希和顺序。
- **chunk facts 是启发式**（HSV 阈值、Canny、Hough）。一次漏检会产生错误的 prompt 句子。gate 检查 prompt 和 facts 是否一致，而不是 facts 是否正确。
- **每个后续 chunk 都失去一段引入。** 切点位于进入 chunk 内至少 `min_lead` 帧处，且至少在离开 chunk 结束前 4 帧，因此在默认分块下每个后续 chunk 失去其前 56–67 帧。
- **权重是外部的。** MiniMax-H3 和 turbo LoRA 是外部绑定（`NOTICE.md`）。

### 许可证

MiniMax-H3 LICENSE §V.3 禁止使用 MiniMax-H3 Works 或其 Outputs 来改进其他 AI 模型（MiniMax-H3 及其衍生除外），并且它有 Excluded Territories。用 H3 输出训练机器人策略是一种需要法律依据的用途。本文档不提供法律解释，且本仓库不授予对该模型或其输出的任何权利；在用 H3 渲染训练数据之前，请确认权利和地域限制。VideoX-Fun 和 ControlNet v1.1 HED annotator 是 Apache-2.0。

## Seedance

`src/datafarm/render/seedance/` 执行 edit 请求、带远程 SHA 验证的 source 上传、限流、task-ID 查询和下载，以及具有固定 source identity 的单次 create。

- **Route 与 profile。** 直接视频到视频的外观重写使用显式的 `edit` route。普通和高吞吐 endpoint 是分开的 profile；高吞吐 profile 的并发只影响调度，不继承任何质量资格。endpoint、model 或配额变化需要新的 profile。下文的 697 帧限制属于 Seedance，不限制 H3。
- **时钟。** Canonical frame `i` 就是 simulator frame `i`。相同的帧以 24 FPS 发送，之后用相同索引恢复 30 FPS 训练时钟。为适应 transport 网格而添加的帧只能复制 simulator 证明为静止的终止光栅。
- **分割与参考。** 长 episode 可以拆分为 provider 合法的阶段，这些阶段在 simulator 标注的非接触、已释放区域重叠；每个 canonical frame 恰好保留一次。在 simulator 事件处拆分是 task 代码。recipe 可以声明 `VIDEO 1`（同步的 white 布局）、阶段匹配的风格图像、阶段局部的结构图像，以及一个只约束相邻阶段之间连续性的上一输出锚点。多面板输出使用 T 布局：头部 1280×720 在上，腕部 640×360 在下。
- **异步 task。** seed 是实验性控制，不是字节精确的回放；返回的字节会被保存。一个没有 task ID 的近期 `submitting` 记录保持未解决，过了过期窗口后会被隔离。provider 成功、技术通过、语义通过和数据集 admission 分别计数。
- **修复。** 普通修复只重绘被拒绝的身份或阶段，然后经过同样的接缝和语义 gate。只有当 simulator 的相机位姿、机器人状态和每帧图像共同证明该面板在整个尾部静止时，才可通过复制切点之前的最后一帧来修复面板。使用 Seedance 输出训练另一个渲染器需要明确的书面授权和记录的权利依据。

```bash
python -m datafarm.render.seedance.batch --run <seedance-run> --phase canary --workers 8
```

### 保证

1. **一个 source 绝不会有两次 POST。** `create_once` 运行 `check_seedance_payload`、持久化 `{status: submitting}`，然后 POST。已存在的 `task.json` 会被返回而不重新发送，契约改变会引发异常。没有 provider ID 的记录（`create_rejected`、`ambiguous_no_id`、`ambiguous_transport`）以 "one-create limit" 停止作业。
2. **真实媒体上传 host 需选择启用。** carrier 只发往 `DATAFARM_SEEDANCE_UPLOAD_HOST` 或批量配置的 `upload_host`。没有默认值，且 host 必须是 `https://`。该检查在任何 rate slot 或尝试被消耗之前运行。每次上传限时 90 秒，会重新下载，且必须与本地 sha256 匹配。
3. **批量提交需要绑定到此批次的 canary 评审。** `--phase canary`（默认）只提交 `canary_ids`。`--phase all` 要求 `canary-review.json` 具有为真的 `continue_exploration`，且 `batch_sha256` 等于当前 `batch.json` 的 sha256。
4. **每个 source 帧都被保留，且不做重定时。** 使用 `clock`（来自 `batch.json`），source 行必须满足 `0 < frames <= transport_frames <= max_transport_frames`（受支持 profile 中为 697）、`transport_frames % 8 == 1`，且填充帧少于 8 帧。provider 输出必须恰好有 `transport_frames` 帧。`finalize` 裁掉填充，并写出 `render-24fps.mp4` 和 `render-30fps.mp4`，使用 `setpts=N/(fps*TB)`，因此输出帧 *i* 就是 source 帧 *i*。

代码：`src/datafarm/render/seedance/`。

### 数据模型与配置

一个 run 保存 `batch.json`（`model`、`jobs`、`canary_ids`、`upload_interval_seconds`、`create_interval_seconds`、`clock {transport_fps, canonical_fps, max_transport_frames}`、可选的 `upload_host`）、`canary-review.json`（`continue_exploration`、`batch_sha256`），以及每个 episode 的 `input-contract.json`、`upload.json`、`task.json`、`raw.mp4`、`result.json` 和 `failure.json`。`status-<phase>.json` 记录固定分母和失败。

| 键 | 默认值 | 效果 |
|---|---|---|
| `ARK_API_KEY`（env） | 必需 | 凭据，仅在 Authorization header 中发送 |
| `SEEDANCE_API_PROXY`（env） | 未设置 | API 和上传的代理；通过 stdin 配置而非 argv 传给 curl |
| `batch --workers` | 8（1–24） | 并发作业 |

`run_one` 重新对输入做哈希、上传 carrier（限流）、构建 payload（prompt、carrier `video_url`、按 `references.py::images` 检查的顺序内联的图像）、调用 `create_once` 并最多轮询 7200 秒。`finalize` 将结果标记为 `technical_complete_pending_visual_review` 和 `training_admitted: false`。

### 失败行为

worker 异常被写入 `episodes/<id>/failure.json`，批次继续。有 `failure.json` 而没有 `result.json` 的 episode 不会被自动重试。帧数不匹配会引发异常，`raw.mp4` 被保留。`poll_download` 在 provider 终止性失败以及成功但没有视频 URL 时引发异常。上传进行 6 次有界尝试并退避，每次都记录在 `upload-error.json` 中。

### 限制

- **adapter 中不会因反复失败而暂停。** `datafarm.render.seedance.batch` 记录失败并继续。停止逻辑位于 [batch supervisor](batch-supervisor.md)，因此要获得该行为需通过它运行。
- **上传先于 policy 检查。** carrier 在 `check_seedance_payload` 运行之前被上传，且上传的 carrier 在其过期（24 小时）之前是公开的。

## 比较各路线

| 维度 | H3 control hybrid | Seedance |
|---|---|---|
| 执行 | 本地 GPU workers | 远程异步 edit API |
| 时钟 | 24 FPS，profile 级帧限制 | 24 FPS transport，`8n+1`，697 帧 |
| source 权威 | 带结构控制的 white carrier | white 布局参考视频 |
| 参考风险 | 位姿、布局和实体投影污染 | 阶段参考可能重置状态或外观 |
| 连续性 | 带硬切接缝的独立 chunk | 带非接触接缝的重叠阶段 |
| 恢复键 | 本地 job 或 chunk 加上输出摘要 | 远程 task ID 加上不可变尝试 |
| 主要失败 | 额外实体、事件滞后、参考吸引、错误风格接受 | 帧数漂移、阶段重置、低运动腕部接缝、创建不明确 |

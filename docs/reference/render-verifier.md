# Render 验证器

本文档描述生成视频在被准入前如何由机器判定。每个任务都有一个 spec。
代码位于 `src/datafarm/verify/`，入口是 `tools/verify_render.py`，校准工具是 `tools/calibrate_render_verifier.py`。

## 1. 固定部分与任务部分

Render 层将模拟的 white carrier 重绘为目标域视频。判定有两个部分：

- **固定部分（代码，不随任务变化）：** 检查族、决策逻辑、持续性规则、fail-closed 行为和报告格式。
- **任务部分（数据，由 agent 编写）：** 一个 JSON spec，包含 Task IR 中每个角色的面板布局、颜色键和几何基元、状态中的物体名称、真实机器人参考以及 OOD 扰动轴。

新任务只需编写 spec，而不是新的 verifier。

## 2. 输入与权限

| 输入 | 权威用于 | 来源 |
|---|---|---|
| carrier（white-model 视频） | 几何、实体可见性、时序 | Episode IR |
| 模拟状态（物体位姿、相机 K/外参、夹爪命令） | 完整物体轮廓、事件时刻 | Episode IR |
| 真实机器人视频（相同布局） | 外观、相机曝光和闪烁行为 | Grounding IR |
| 外观参考图像 | 仅用于检查布局泄漏 | Render IR |
| 生成视频 | 被判定对象，无任何权威性 | provider |

## 3. 九个检查族

每个结果都是四种值之一：

- `pass`；
- `fail`；
- `abstain`：判定所需的权威性缺失或无法测量；**阻止准入**；
- `not_applicable`：客观上无法测量，例如移动的腕部相机没有静态背景；不阻止准入。

样本通过当且仅当所有硬检查为 pass 或 not_applicable。缺陷必须持续 `persist_samples`（默认 3 个采样帧）后才被判为失败。

1. **transport**：帧数、fps、宽高比、黑帧、sha256。
2. **structure**：
   - 对每个面板和每个采样帧，比较投影几何描述子的余弦（模糊灰度 + 梯度）；
   - 用相位相关估计全局平移（这可以捕捉挤压或错位）；
   - 面板边缘的黑边：生成图像的面板边缘出现 white model 没有的均匀近黑带（>= 4 layout px）并持续存在；这被判为面板裁剪或偏移错误。
3. **entity**（Task IR 每个角色一项）：
   - *missing*：white model 中可见的实例在生成视频中其附近没有该实体的像素；
   - *unsupported*：生成视频中出现实体大小的连通斑块，但其附近没有 white-model 实例、没有模拟投影轮廓、也没有其他实体；
   - *extent*：生成实体落在模拟投影轮廓之外的面积过大。当它接触其他实体或被面板边缘截断时，视为无法测量。
   - 生成侧分割首先学习**此视频自身**的实体外观（在 white model 授权的像素上拟合 CIELAB 模型；多色物体逐部分学习），然后加入色度下限。这样实体检查只判断几何和存在性，外观是否像真实机器人留给 realism。在 OOD 光照模式下只使用此自适应模型，不使用固定的目标颜色键。
   - 若远离任何模拟物体的背景中超过 2% 被分割为该实体（例如与任务物体同色的新桌面材质），该面板的 unsupported / extent 项判为 abstain：颜色不再能作为证据。
   - 子检查可以限制到某个面板，写作 `unsupported:head`。
4. **event**：
   - 对每个事件（夹爪打开/关闭，或 spec 给出的帧），比较所涉实体 ROI 内的运动信号；
   - 任务可以使用 `event_local_motion_min` 要求事件前后四帧内 white-model ROI 具有可测量的运动；如果运动只存在于窗口的其他部分，则不会用相关峰来声称此事件错位。默认 0 禁用此要求；
   - 搜索滞后，要求 |lag| <= 6 帧且相关 >= 0.3。
5. **temporal**：每个面板在全帧率下检查三件事（跳变和亮度阶跃先分别由两个视频的对比度归一化）：
   - 无法由 white model 解释的亮度阶跃：默认上限为 10/255；真实相机自身的阶跃统计只能提高它，绝不能降低；
   - 跳变；
   - 冻结。
6. **continuity**：在片段接缝处分别判断两类问题：
   - 光度跳变：与闪烁相同的阈值；
   - 结构跳变：先做增益/偏置匹配，然后要求变化同时超过局部运动的倍数和一个绝对下限。
   - 片段已渲染但未声明接缝时 abstain。
7. **background**（仅固定相机）：在 white model 不变化的像素上，生成图像也不应变化。
   - 物体附近排除阴影边距；
   - 在更宽的 "阴影可达范围" 内允许变暗；
   - 对每一帧，先计算排除移动物体和边距后仍可测量的静态像素比例；若低于 spec 的 `background_min_static_fraction` 则不判断该帧。默认 3%；静态背景很少的任务可以提高它。若整个片段中可测量的采样帧少于 30%，该面板返回 not applicable，不会从少量像素混合移动阴影中得出 pass 或 reject 结论；
   - 若移动物体的大片深阴影覆盖了大部分被判断像素，远离运动的静态背景几乎没有，且被判断像素的上四分位数保持在首帧曝光附近，则从该上四分位数取曝光，这样阴影不会被拟合为曝光下降。真正的全局曝光下降没有这样的上四分位数；
   - 不允许变亮，连通亮斑的面积单独检查；
   - 整体曝光漂移只是粗粒度防护（默认 60/255）。
8. **realism**：每个面板和每个区域（每个实体 + 背景）将六个特征与真实机器人画面比较：
   - 颜色、对比度、相对色度、纹理、锐度、噪声。
   - 每个特征除以固定容差（约一个最小可觉差），然后允许真实机器人自身前半/后半的波动。硬阈值为 6.0，3.0 及以上仅作参考警告。直接复制 white model 以及重新上色的版本仍会失败。
   - 在真实机器人视频中覆盖不足的区域记录为无法测量。
9. **reference_leak**：如果外观参考图像中的某个实体不在其 white-model 位置，却在第一生成帧中被填充，则判为布局泄漏。

## 4. sim2real 与 OOD

- **sim2real**：所有区域的所有特征都必须接近真实机器人。不强加固定光照风格：如果真实素材有硬阴影，渲染也应有。
- **OOD**：先绑定本任务明确授权、真实基线和真实图像；spec 只声明授权范围内的扰动轴（`lighting`、`background`、`table_material`、`distractors`、`camera_exposure`）。
  - 已授权的扰动轴不要求像素/特征等于真实基线；数值 gate 对 lighting 跳过颜色和对比度，对 background 轴跳过背景区域，但这不会跳过真实风格评审。超出授权的变化仍必须被拒绝。
  - 机器人和任务物体是不变实体，必须仍然看起来像真实机器人。
  - 几何、事件、时序和实体检查与 sim2real 完全相同。

## 5. 编写新任务 spec 的工作流（由 agent 执行）

将 `configs/render-verifier/template.json` 复制到任务 workspace；spec 是一个任务产物。

1. 从 Task IR 列出所有可见角色，并为每个角色写一个实体：
   - `carrier_keys` 在 white model 上分割；
   - `target_keys` 宽松覆盖目标域外观；
   - 如果真实参考物体是深色的，严格的 `target_keys` 会把仍可见的物体报告为 missing；可以设置单独的 `presence_keys`，仅用于 white-model 期望位置的存在性检查；新物体和体积检查仍使用 `target_keys`。启用前必须重放缺物体和同色加物体两个反例；仅通过缺物体案例不能确立实体数量准入。
   - 多色物体逐部分列出键，white-model 键和目标键按索引一一对应；
   - 存在状态时，还要写 `state_objects` 和几何基元。
2. 写面板、相机名称和原生分辨率，然后用投影叠加图目视确认投影对齐。
3. 绑定真实机器人参考（相同布局）和实际提交的真实外观图像的源/角色，并记录用户确认的模式和已授权扰动轴；模式未确认时先询问。用于验证的参考的存在不能替代生成请求携带真实图像这一证据。
4. 运行 `validate_spec`：
   - 每个 Task IR 角色都必须有一个实体检查；
   - 分段渲染必须声明接缝；
   - OOD 模式必须声明扰动轴；
   - 豁免必须说明理由。
5. **校准**：选一个人工批准的渲染作为正例并运行反事实集，要求正例通过且每一类反事实都被相应的检查族捕捉。反事实包括：
   - 移除物体、添加物体、放大 1.5 倍；
   - 时间偏移、冻结、闪烁；
   - 交换左右腕、水平挤压；
   - 背景重新打光、幻觉光斑；
   - 接缝帧跳变、重新上色、直接复制 white model。
6. 在整个 cohort 上运行，并抽查失败证据和部分通过的样本。发现误报时，修正通用规则并重新校准；不要针对单个样本调阈值。

## 6. 命令

```bash
python tools/verify_render.py --spec <task-workspace>/verifier-spec.json \
  --carrier white.mp4 --generated render.mp4 --real real-tlayout.mp4 \
  --state frame-aligned-robot-state.json --seams <frame> --out <dir>
python tools/calibrate_render_verifier.py --spec <task-workspace>/verifier-spec.json --carrier white.mp4 \
  --generated accepted.mp4 --real real-tlayout.mp4 --out <dir> [--waive robot_ghost="no robot entity in this spec"]
```

两个工具仅在 `pass` / `calibrated` 时以 0 退出，否则为 2。输出是 `receipt.json`（机器可读）和 `review.html`：每个失败或 abstain 的检查都带有 "white model | generated | overlay" 证据图像，时序证据显示前后帧。

## 7. 保证

1. **违反创作规则的 verifier spec 不能通过任何东西。** `validate_spec` 检查以下规则：
   - P0：面板唯一且位于布局内。
   - P1：每个 Task IR 角色都有一个带 `missing` 检查和 carrier/target 颜色键的实体。
   - P2：事件命名已知角色。
   - P3：分块渲染声明其接缝。
   - P4：realism 模式明确；`sim2real` 没有扰动轴；`ood` 声明来自 `OOD_AXES` 的轴。
   - P5：waiver 命名已知族并给出理由；阈值已知。

   任何违规都返回结论 `abstain` 并带 `spec_problems`。
2. **结论在固定检查族上 fail closed。** 这些族是 `transport`、`structure`、`entity`（存在、缺失、额外/unsupported、尺度）、`event`、`temporal`、`continuity`（接缝）、`background`、`realism` 和 `reference_leak`。任何硬 `fail` 使结论为 `fail`。否则任何硬 `abstain` 使其为 `abstain`。一个族只能通过有理由的 spec waiver 关闭，结果为 `not_applicable`。
3. **Realism 针对真实素材判断，绝不假设。** 没有真实片段时，`realism_vs_real_world` abstain。仅由 carrier 构成的渲染会失败。在 `ood` 模式下，只有已声明的轴会放宽特征。背景轴去掉背景区域，`lighting` 或 `camera_exposure` 去掉颜色和对比度特征。
4. **只有完整、干净的变异运行后 spec 才算已校准。** `calibrate_render_verifier.py` 验证被接受的渲染（阳性对照），然后注入 `counterfactual.SUITE` 中的 14 个缺陷。这些是实体删除/复制/缩放、时间偏移、冻结、闪烁、面板交换、挤压、重新打光漂移、光斑、接缝跳变、重新上色、white（carrier）复制以及机器人残影。`summarize` 仅当以下全部成立时才设置 `calibrated`：
   - 运行不是部分的（`--only` 运行是诊断）；
   - 阳性对照的结论为 `pass`；
   - 每个构造出的缺陷都在其预期检查中失败；
   - 每个无法构造的缺陷都有显式的 `--waive NAME=REASON`。

   命名未知缺陷的 waiver 会抛错，waiver 绝不豁免遗漏的缺陷。`calibration.json`（`datafarm.render-verifier-calibration/2`）记录 `spec_sha256` 和 `generated_sha256`。仅当 `calibrated` 为 true 时工具才以 0 退出。

代码：`src/datafarm/verify/`、`tools/verify_render.py`、`tools/calibrate_render_verifier.py`。

## 8. 数据模型

- **Verifier spec**（`datafarm.render-verifier-spec/1.0.0`）：布局和面板、实体
  （role、carrier/target 颜色键、检查、面板）、事件、`realism {mode,
  perturbed_axes}`、阈值、waiver、seam。从 `configs/render-verifier/template.json` 开始。
- **Verifier 回执**（`receipt.json`）：`verifier`（`datafarm-render-verifier-v2`）、`verdict`、`failed`、`abstained`、带来源的 `thresholds`、输入路径和 sha256、`checks[]`（`name`、`family`、`verdict`、`hard`、`reason`、`metrics`、`evidence`），以及 `episode_binding`。
- **校准**（`calibration.json`，`datafarm.render-verifier-calibration/2`）：`spec_id`、`spec_sha256`、`generated_sha256`、`positive_control_passes`、`caught`、`total`、`constructed`、`missed`、`not_constructible`、`unwaived_not_constructible`、`waivers`、`partial`、`calibrated`、`results`。

## 9. 配置

| Flag / 键 | 默认值 | 作用 |
|---|---|---|
| `verify_render.py --families` | 全部 | 限制检查族。受限制的运行仍可返回 `pass`。 |
| `--mode`、`--perturbed-axes` | 来自 spec | 为此 run 覆盖 realism 目标。 |
| `--single-chunk`、`--seams`、`--expected-frames` | spec | 用于 continuity 和 transport 的 episode 绑定。 |
| `calibrate --only` | 全部 | 诊断子集；始终 `partial`，永不 calibrated。 |
| `calibrate --waive NAME=REASON` | 无 | 接受一个无法构造的反事实。 |
| spec `waivers {family: reason}` | 无 | 将一个族变为 `not_applicable`。 |

## 10. 已知限制

- 实体分割基于颜色键和自适应外观模型；不使用学习式分割或跟踪。遮挡边界处的尺寸无法测量。
- 机器人的 "凭空出现" 不由颜色判断：黑色连杆、深色投影和深色墙壁无法用颜色区分。改用 `semantic_unsupported`（`semantic.py`，需要 GPU 和环境变量 `DATAFARM_DINOV2`）：
  - 原型从本视频 white-model 手臂区域的 DINOv2 特征学习；
  - 如果某处在语义上像手臂，而 white model 那里没有模拟内容（white-model 前景由时间中值背景和时间差分获得，可覆盖浅灰色连杆），则判为幻觉；
  - 如果手臂与背景的特征分布本身不可分，则判为 abstain。
  - "手臂残影" 反事实是校准的一部分。
- white model 自身的阴影跳变（模拟 shadow-map 伪影）也会使生成视频失败。这是真实的可见缺陷，但应在 Episode 或 white-model 采集阶段修复；报告不会把它标注为源自 white model。
- 硬 realism 阈值是默认值，不是逐任务值。为每个新任务运行 `tools/calibrate_render_verifier.py`。
- 没有语义级判断，例如动作是否真正完成或物体是否真正放置。这部分仍依赖终态实体检查和人工评审。
- **校准不覆盖 reference-leak 变异。** `counterfactual.SUITE` 没有 reference 布局泄漏缺陷。因此 `reference_layout_leak` 从不被校准。未传入参考图像时它为 `not_applicable`，且它只检查前五个采样帧，针对具有 `unsupported` 检查的实体。
- **Spec waiver 无法校准。** 在 spec 中豁免的族为 `not_applicable`。期望该族的反事实随后报告为 missed，校准失败。`--waive` 只覆盖无法构造的缺陷。
- **不附带任务 spec。** 只包含 `configs/render-verifier/template.json`；每个任务的 spec 是 run 的输出。

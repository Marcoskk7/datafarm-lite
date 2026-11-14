# 从标定 episode 进行 grounding

Grounding 把真实标定 episode 转换为仿真器和渲染器所消费的记录 G：仿真器使用的物理量、相机/尺度拟合、用于渲染的真实世界外观参考，以及可选的新建物体资产（见 [usdcraft](usdcraft.md)）。
随附代码不执行拟合本身。它固定 G 的*形式*（类型化的物理量账本、真实参考来源、源绑定的物体资产记录），并拒绝 G 不完整或绑定文件已变更的交付。拟合工作由 agent 按照 `.agents/skills/sim-grounding/SKILL.md` 完成。

## 保证

1. 每个账本条目都有唯一的非空 `name`，一个 `label`（取值为
   `measured | fitted | assumed | unknown`），以及字符串 `unit`（无量纲时为 `""`）。
2. `measured` 或 `fitted` 量至少需要一个证据引用。
3. 只有 `unknown` 量可以取 `null` 值；其他任何值必须是有限数字、
   列表或字符串。
4. `assumed` 或 `unknown` 量必须声明 `randomization` 或非空的
   `not_randomized_reason`。
5. 已声明的 `randomization`（任意 label）要么是至少两个值的 `choices`，要么是
   有限的 `low < high`；数值名义值必须位于 `[low, high]` 内。
6. grounding 记录文件必须带有 `schema: "datafarm.grounding/1"`；若给定了
   evidence binder，则应用于每个证据引用。
7. Grounding IR 文档必须包含 `payload.system_identification.physical_quantities`，
   由相同规则验证，每个证据条目为恰好
   `{kind, sha256, media_type}` 的 ArtifactRef。
8. 两个交付 verifier 都要求 `delivery.grounding` 绑定任务工作区内一个非空文件，
   其 SHA-256 匹配，并对其引用的每个证据文件重新计算哈希。
9. 渲染意图至少需要一个真实世界参考；每个参考是由哈希绑定的可解码 PNG/JPEG/WEBP，
   provenance 为 `real-episode-frame`（非负整数 `frame`）或
   `real-photo`，有一个绑定的 `source` 文件和一段描述；每个声明的外观视角都需要一个
   `full-view` 参考，覆盖 `full_scene`，且必需的外观区域必须被覆盖。
10. 除非每个真实参考的 `provenance.source.sha256` 都是
    锁定输入文件（`input.json`）之一，否则渲染交付会被拒绝。
11. 相机位姿和抓取随机化计划按 seed 确定、内容寻址且
    在整个 episode 内保持不变；逐帧相机抖动会被拒绝。

代码：`src/datafarm/core/grounding.py`, `src/datafarm/core/schema.py`, `src/datafarm/task/contract.py`, `src/datafarm/task/white_delivery.py`, `src/datafarm/render/policy.py`, `src/datafarm/simulation/randomization.py`。

## 数据模型

- **Grounding 记录**（`grounding.json`，由 `delivery.json` 以 `grounding: {path, sha256}` 绑定）：
  `{schema: "datafarm.grounding/1", physical_quantities: [entry...]}`。条目字段：`name`（str）、
  `label`、`unit`（str）、`value`（number | list | str | null）、`evidence`（`{path, sha256}` 列表，
  相对工作区）、`randomization`（`{low, high}` 或 `{choices: [...]}`）、`not_randomized_reason`
  （str）。`validate_quantities` 返回每个 label 的计数；两个 verifier 都在其验证结果中
  以 `physical_quantities` 报告该计数。
- **Grounding IR**（`IRKind.GROUNDING`、`src/datafarm/core/schema.py::REQUIRED_PAYLOAD_KEYS`）：
  payload 段为 `source`、`transfer_scope`、`camera_clock`、`reference_roles`、
  `system_identification`、`rights`；账本证据条目为 `{kind, sha256, media_type}`。
- **真实参考**（渲染意图中的 `intent.real_references[]`，`datafarm.render-intent/1`）：
  `path`、`sha256`、`provenance {kind, source {path, sha256}, frame?, description}`、
  `scope`（`full-view` | `local-detail`）、`views`、`view_regions {view: [x, y, w, h]}`、
  `coverage`（`REFERENCE_AREAS` 的子集，位于 `src/datafarm/render/policy.py`，加上 `full_scene`）。

## 工作原理

1. agent 阅读完整 episode 并遵循 sim-grounding skill：由机器人
   或已知物体尺寸得到度量尺度、相机内参/外参、桌面和物体布局，并通过
   多个时刻和视角的投影交叉检查；在第一次完整白模采集前对每个相机做小型仿真图像检查。
2. 它写出 `grounding.json`，列出仿真器使用的每个物理量，带上 label、
   measured/fitted 值对应的证据，以及不确定量的随机化或已说明的理由。
3. 它提取真实帧（或使用放入锁定输入中的用户提供真实照片）作为
   外观参考，并在渲染意图中记录其来源。
4. 如果缺少某个物体资产，它写出观察并运行 USDCraft 桥接
   （`plan`，然后 `run`）；导出的资产在仿真器导入、对比和接触探针
   经过独立审查之前保持未取得资格。
5. 交付验证（`verify_delivery` / `verify_white_delivery`）重新验证账本并
   重新计算其证据的哈希；渲染路径还会对照输入锁定重新验证参考来源。

## 入口点

```bash
# verify a finished task workspace (includes the grounding ledger)
python tools/pipeline_agent.py verify --work /data/task
```

## 失败行为

- 无效账本会抛出 `GroundingError`；verifier 将其重新抛出为 `ContractError`
  （“grounding record invalid: ...”）；IR 验证器抛出 `SchemaError`。不会进行任何修复。
- 证据或 grounding 文件变更会以 “artifact changed” 失败；交付必须重建。
- 超出输入锁定的参考来源会失败，并提示在 `prepare` 时纳入真实
  照片；该锁定无法事后扩展。

## 局限性

- **账本是声明式的。** 没有共享代码检查已声明的随机化是否在
  仿真器中实际应用，或某个 label 是否真实；证据绑定证明的是字节，而不是
  文件支持该值。正确性留给独立审查。
- **共享代码中实现的随机化**是
  `randomization.py` 的相机位姿和抓取计划（默认相机名匹配双臂装置）。**没有共享的质量、
  摩擦或材质随机化器**；对不确定量做随机化是账本中的声明要求。
- `choices` 不会与名义值核对；列表或字符串值从不做范围检查。
- **相机拟合和尺度交叉检查由 agent 按 skill 执行。** 没有随附的标定
  入口点，也没有 verifier 要求相机拟合证据。交付 verifier 不
  直接检查相机标定或关节限位占用。
- **参考帧**：没有共享工具生成 `real-episode-frame` 参考；`validate_intent`
  检查结构、图像解码、区域边界和来源绑定，但不会解码
  源视频来确认帧索引。渲染交付会对照输入锁定检查来源；
  白模交付没有真实参考检查。

# IR v0.2（A/B 对接面）

> 2026-11-13 初稿，2026-11-15 按 `86eeb36 refactor(kernel)` 和 [B 线接口设计](../reference/b-control-interfaces.md)（`b-control/0.1.0-draft`）对齐。v0.1 的 `BackendChoice`／探针证书、10 Hz 直采 mp4、`{path, sha256}` 引用已废弃。

这是两条线（USD 线 / 控制线）唯一的对接面。字段先求少而准，扩的时候按下面第 8 条走。

## 1. 组成

| 文档 | `schema` | 形态 | 生产者 |
| --- | --- | --- | --- |
| [AssetRecord](asset-record.md) | `datafarm.asset-record/1` | A 线普通 JSON | A |
| [Request](request.md) | `datafarm.request/1` | A 线普通 YAML/JSON（数据请求，只写意图） | A（1a） |
| [SceneLayout](scene-layout.md) | `datafarm.scene-layout/1` | A 线普通 JSON（布局输入） | A |
| [SceneGraph](scene-graph.md) | `datafarm.scene-graph/1` | A 线普通 JSON | A |
| [TaskContract](task-contract.md) | `datafarm.task-ir` | kernel `ArtifactDocument` | B |
| [Episode](episode.md) | `datafarm.episode-ir` | kernel `ArtifactDocument` | B |
| [RenderSpec](render-spec.md) | `datafarm.render-spec/1` | A 线普通 JSON（渲染请求） | A |

A 线三份是普通文档：有 `schema`，引用一律 ArtifactRef，但不套 kernel 信封。`TaskContract` 和 `Episode` 是 kernel 的 5 种 `IRKind` 之一（`src/datafarm/kernel/artifacts.py`），必须用 `ArtifactDocument` 信封，字段名和必需节不能自定。渲染产出不定新 schema：图像和内外参作为 ArtifactRef 记进 Episode 的 `frame_map` 与准入证据。

## 2. 位姿与坐标系

```json
{"frame_id": "world", "position_m": [0.45, 0.0, 0.78], "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0]}
```

- `frame_id` 必填，`world` 是场景根。Pose 表示 `T_frame_entity`，即实体局部系到 `frame_id` 的变换。
- 右手系、Z 向上、单位 m / rad。相机单独声明光学轴约定及其到 `frame_id` 的变换。
- 四元数顺序固定 **xyzw**（单位四元数）；比较旋转时 q 与 -q 等价。
- Isaac Lab 3.0（Newton/MJWarp 后端）的 root/body 位姿 API 原生即 **xyzw**，与 IR 一致，读写时**不做交换**。只有直接调用 USD/`Gf.Quat*`（实部在前，wxyz）的代码需要在该边界转换一次；IR 里不出现 `quat_wxyz`。
- 一个字段不能混装单位：revolute 关节 rad、prismatic 关节 m，必须在 `joint_types` 里分别声明。

## 3. 引用外部文件

一律用 `ArtifactRef`：

```json
{"kind": "asset.usd", "sha256": "…", "media_type": "model/vnd.usd"}
```

`src/datafarm/kernel/schema.py` 的 `validate_artifact` / `canonical_json_bytes` 会拒绝：

- 任何名为 `path`、`file`、`*_path`、`*_file`、`root` 的字段（要求改用 ArtifactRef）；
- 绝对路径（含 `C:\…`）、带凭据或带 query/fragment 的 URL；
- 凭据类字段（`token`、`secret`、`password` 等）。

所以 A 线文档里也不能写 `output.root` 这种字段。文件位置由部署侧 resolver 解析，IR 只带 `kind` + `sha256` + `media_type`。

资产记录里的 `physical_quantities` 原样复用 `datafarm.grounding/1`，校验函数是 `src/datafarm/grounding/quantities.py::validate_quantities`；传 `bind` 时证据必须是 ArtifactRef，不传则要求 `{path, sha256}` 形状。A 线文档一律传 `bind`。

## 4. 标识

`^[a-z0-9][a-z0-9._:-]{0,127}$`（与 kernel `IDENTIFIER_PATTERN` 一致）。语义分层用点号：

```text
object.plate        object.plate_raised_rim
object.cabinet      joint.drawer.slide
camera.head         robot.airbot
```

不要用 `plate_01` 这类纯序号做主键，序号在 B 加载后再分配。

## 5. kernel 信封（仅 TaskContract / Episode）

```json
{
  "schema": "datafarm.episode-ir",
  "schema_version": "1.0.0",
  "artifact_id": "episode.ep_0007",
  "created_at": "2026-11-15T02:00:00Z",
  "producer": "b.episode-recorder",
  "predecessors": [{"kind": "ir.scene-graph", "sha256": "…", "media_type": "application/json"}],
  "evidence": [{"kind": "report.action-replay", "sha256": "…", "media_type": "application/json"}],
  "payload": {}
}
```

顶层字段必须**恰好**是这 8 个（`created_at` 是 RFC3339 以 `Z` 结尾），`schema_version` 目前只接受 `1.0.0`。必需 payload 节见 `REQUIRED_PAYLOAD_KEYS`：

| `schema` | 必需 payload 节 |
| --- | --- |
| `datafarm.task-ir` | `demand` `roles` `stage_graph` `predicates` `forbidden_shortcuts` `skill_evidence` |
| `datafarm.episode-ir` | `setup` `actions` `state_trace` `contacts` `events` `frame_map` `replay` |

必需节必须齐全，额外字段允许，但同样受第 3 条约束。

## 6. 时间

- 控制频率 `control_dt_s = 1/120`，一个控制周期一次 Newton/MJWarp 步，内部 4 子步（见 B 线接口 §2.3）。子步状态不单独记录。
- `s[k]` 在第 k 个控制周期结束后采样，`s[0]` 是复位 + settle 后的初始状态。`a[k]` 作用在 `[k, k+1)`，因此 N 个动作对应 N+1 个状态。
- 相机是派生流：10 fps 即每 12 个控制步取一帧，只在 `frame_map` 里写明 `control_step` 与 `frame_index`，不重采样动作和状态。
- 一律存整数步号，时间由步号 × dt 推出，不存累加浮点时间。

## 7. 复用的 DataFarm schema（不要重造）

| 用途 | schema | 位置 |
| --- | --- | --- |
| 任务结构、阶段、判定条件映射 | `datafarm.task-ir`（profile `stage-graph/1`） | `src/datafarm/expert/task_ir.py` |
| 物理量账本 | `datafarm.grounding/1` | `src/datafarm/grounding/quantities.py` |
| 信封、ArtifactRef、规范 JSON、内容寻址 | — | `src/datafarm/kernel/` |

`datafarm.sim-qualification.certificate/1.0.0`（`src/datafarm/simulation/sim_qualification.py`）在本 MVP **不使用**：后端固定 Isaac Lab 3.0 + Newton/MJWarp，没有后端选择探针。

## 8. 版本规则

- 新增可选字段：版本号不变，validator 向后兼容。
- 删字段、改字段含义、加必填：新 schema id（`/1` → `/2`），旧文档保持可读。kernel 侧 `schema_version` 当前写死 `1.0.0`，要升只能改代码。
- 改任何一条都要两人同意后提交本目录；口头约定不算。

## 9. 落地

validator 放 `src/datafarm/ir/`，照 kernel 风格：模块级 `SCHEMA` 常量 + `validate()`，错误信息给人读。A 线文档的校验器除了调 `validate_quantities(bind=...)` 还要复用 kernel 的可移植性检查（`canonical_json_bytes`）。该目录已存在（AssetRecord、SceneGraph 校验器已实现）；其余文档的校验器尚未实现。

## 10. 文档

- [asset-record.md](asset-record.md)
- [request.md](request.md)
- [scene-layout.md](scene-layout.md)
- [scene-graph.md](scene-graph.md)
- [task-contract.md](task-contract.md)
- [episode.md](episode.md)
- [render-spec.md](render-spec.md)

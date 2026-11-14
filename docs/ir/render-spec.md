# RenderSpec — `datafarm.render-spec/1`

渲染请求：从一条 episode 的 `state_trace` 出发，指定视角、材质、光照和变体，产出图像。渲染器只读状态流，不回放物理，不动几何、碰撞、动作和接触。

A 线普通文档（不套 kernel 信封）。产出不回写新 schema：图像、内外参作为 ArtifactRef 记进 Episode 的 `payload.frame_map`，准入由 `datafarm.gate-receipt` 完成。

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema` | 是 | 固定 `datafarm.render-spec/1` |
| `episode` | 是 | ArtifactRef，被渲染的 `datafarm.episode-ir` |
| `engine` | 是 | 固定 `"isaac_rtx"`（Isaac Sim 6.2 / Replicator）。没有 `auto`、没有 `blender`、没有视频模型引擎 |
| `views` | 是 | 视角数组，见下 |
| `materials` | 是 | 材质覆盖数组；空数组表示保持资产原材质 |
| `lights` | 是 | 光照覆盖数组；空数组表示保持场景原光照 |
| `variants` | 是 | 要产出的组合数组，见下 |
| `output` | 是 | `{format: "mp4" / "png_sequence", quality}`；文件位置由 ArtifactStore 决定，本文件不带路径 |

`views[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `view_id` | 是 | 变体内唯一，如 `head`、`third_person` |
| `kind` | 是 | `physical`（SceneGraph 里声明的相机，复用其内外参）或 `virtual`（渲染时新建相机） |
| `camera_id` | 视情况 | `kind=physical` 必填，对应 SceneGraph `cameras[].camera_id` |
| `pose` / `intrinsics` | 视情况 | `kind=virtual` 必填：Pose 与 `{fx, fy, cx, cy, width, height}` |
| `resolution` | 是 | `[w, h]`，MVP 640×480 |
| `fps` | 是 | 从 episode 的控制步派生，MVP 10（每 12 个控制步一帧）；必须整除 120 |

`materials[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `slot` | 是 | 语义槽：`plate` / `drawer` / `table` / `robot`，对应 SceneGraph 的 `semantic_tag` |
| `mode` | 是 | `keep` / `override` |
| `params` | 视情况 | `override` 必填：`{base_color: [r,g,b], metallic: 0..1, roughness: 0..1}` |

`lights[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `light_id` | 是 | 如 `warm_key` |
| `mode` | 是 | `keep` / `override` |
| `type` | 视情况 | `dome` / `rect` / `sphere` |
| `intensity` / `color_temperature` / `pose` | 视情况 | `override` 时给出；`pose` 用 Pose |

`variants[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `variant_id` | 是 | 如 `head_ceramic_warm`，对应 Episode `frame_map[].variant_id` |
| `view` | 是 | `views` 中的 `view_id` |
| `material_set` | 是 | 生效的 material 槽列表 |
| `light_set` | 是 | 生效的 `light_id` 列表 |

## 示例（2 视角 × 2 材质 × 2 光照 = 8 个变体，列 2 个）

```json
{
  "schema": "datafarm.render-spec/1",
  "episode": {"kind": "episode-ir", "sha256": "…", "media_type": "application/json"},
  "engine": "isaac_rtx",
  "views": [
    {"view_id": "head", "kind": "physical", "camera_id": "camera.head", "resolution": [640, 480], "fps": 10},
    {"view_id": "third_person", "kind": "virtual",
     "pose": {"frame_id": "world", "position_m": [1.2, -0.8, 1.4], "quaternion_xyzw": [0.1, 0.3, 0.2, 0.9]},
     "intrinsics": {"fx": 320.0, "fy": 320.0, "cx": 320.0, "cy": 240.0, "width": 640, "height": 480},
     "resolution": [640, 480], "fps": 10}
  ],
  "materials": [
    {"slot": "plate", "mode": "override", "params": {"base_color": [0.9, 0.9, 0.88], "metallic": 0.0, "roughness": 0.35}},
    {"slot": "plate", "mode": "override", "params": {"base_color": [0.8, 0.8, 0.82], "metallic": 1.0, "roughness": 0.2}}
  ],
  "lights": [
    {"light_id": "warm_key", "mode": "override", "type": "dome", "intensity": 800.0, "color_temperature": 3200,
     "pose": {"frame_id": "world", "position_m": [0, 0, 3], "quaternion_xyzw": [0, 0, 0, 1]}},
    {"light_id": "cool_key", "mode": "override", "type": "dome", "intensity": 800.0, "color_temperature": 6500,
     "pose": {"frame_id": "world", "position_m": [0, 0, 3], "quaternion_xyzw": [0, 0, 0, 1]}}
  ],
  "variants": [
    {"variant_id": "head_ceramic_warm", "view": "head", "material_set": ["plate"], "light_set": ["warm_key"]},
    {"variant_id": "third_person_metal_cool", "view": "third_person", "material_set": ["plate"], "light_set": ["cool_key"]}
  ],
  "output": {"format": "mp4", "quality": 18}
}
```

## 约定

- **三路物理视角也由 A 渲染**。B 在 headless `newton_mjwarp` 下只产出状态流，不产出图像；`camera.head`、`camera.left_wrist`、`camera.right_wrist` 的每一帧都是 A 从 `state_trace` 回放渲染的，外参由每步刚体状态推导。
- 渲染可以把 `state_trace` 写成 USD time samples 再用 Replicator 出图，但必须保留原状态采样索引，不能把插值结果当成原始帧；需要插值时显式记录方法和限制。
- 一个物理 episode 派生多个材质/光照变体仍只算一条物理轨迹；变体不得改变几何、碰撞、动作或接触。
- 无效帧记 `missing` / `error`，不能复制上一帧冒充同步。
- 渲染完成前不得声称"真实图像"或 real-ready 数据：这是物理渲染，不是照片级重建。

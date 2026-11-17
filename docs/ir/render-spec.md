# RenderSpec `datafarm.render-spec/1` 与 RenderManifest `datafarm.render-manifest/1`

渲染请求从一条 episode 的 `state_trace` 出发，指定视角、材质、光照和变体，产出图像。渲染器只读状态流，不跑物理，也不改几何、碰撞、动作和接触。

两者都是 A 线普通 JSON（不套 kernel 信封），校验在 `src/datafarm/ir/render_spec.py`。RenderSpec 只有外观字段（视角、材质槽、灯光），几何、位姿、运动不是它的字段，所以任何变体在结构上都改不了它们。渲染产物记在 RenderManifest，其中 `frame_map` 条目与 Episode `payload.frame_map` 同构，由 B 合入 episode；准入仍由 `datafarm.gate-receipt` 完成。

## RenderSpec 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema` | 是 | 固定 `datafarm.render-spec/1` |
| `episode` | 是 | ArtifactRef，被渲染的 `datafarm.episode-ir` |
| `scene_graph` | 是 | ArtifactRef，该 episode 的 `datafarm.scene-graph/1`；物理相机和材质槽据此解析 |
| `engine` | 是 | 固定 `"isaac_rtx"`（Isaac Sim 6.2 RTX，经 Isaac Lab 3.0 相机）。没有 `auto`、`blender` 或视频模型引擎 |
| `views` | 是 | 非空视角数组，见下 |
| `materials` | 是 | 材质覆盖数组；可为空 |
| `lights` | 是 | 灯光数组；SceneGraph 不带灯光，所以每个变体至少要用一盏 |
| `variants` | 是 | 要产出的组合，非空，见下 |
| `output` | 是 | `{format: "mp4", codec: "h264", crf: 0..51}`；文件位置由 ArtifactStore 决定，本文件不带路径 |

`views[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `view_id` | 是 | spec 内唯一，如 `head`、`third_person` |
| `kind` | 是 | `physical`：SceneGraph 里声明的物理相机，内外参全部取自该相机；`virtual`：渲染时新建 |
| `camera_id` | 视情况 | `physical` 必填，对应 SceneGraph `cameras[].camera_id`；`physical` 不得再给 `pose` / `intrinsics` |
| `pose` / `intrinsics` | 视情况 | `virtual` 必填：世界系 Pose（OpenCV 光学系：z 前、x 右、y 下）与 `{fx, fy, cx, cy, width, height}` |
| `fps` | 是 | 必须整除 120；`physical` 必须等于相机的 `fps`。MVP 10，即每 12 个控制步一帧 |

分辨率由 `intrinsics.width/height` 决定，不另设字段。

`materials[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `material_id` | 是 | spec 内唯一，如 `plate.ceramic` |
| `slot` | 是 | SceneGraph 节点的 `semantic_tag`（`table` / `drawer` / `plate` / `background` / `robot`），且场景里必须有该标签的节点 |
| `params` | 是 | `{base_color: [r,g,b] ∈ [0,1], metallic: 0..1, roughness: 0..1}`，绑定为 OmniPBR 并覆盖该槽下所有节点的全部网格 |

保持资产原材质的写法是不把该槽放进变体的 `material_set`，所以没有 `mode` 字段。

`lights[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `light_id` | 是 | 如 `dome.warm` |
| `type` | 是 | `dome` / `distant` / `sphere` |
| `intensity` | 是 | > 0，USD `inputs:intensity` |
| `color_temperature` | 是 | 1000–15000 K，启用色温 |
| `pose` | 视情况 | `distant` / `sphere` 必填，世界系；`dome` 不得给 |
| `radius_m` | 视情况 | `sphere` 必填 |

`variants[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `variant_id` | 是 | 如 `head.m0.l1`，对应 `frame_map[].variant_id` |
| `view` | 是 | `views` 中的 `view_id` |
| `material_set` | 是 | 生效的 `material_id` 列表，可为空，同一槽最多一个 |
| `light_set` | 是 | 生效的 `light_id` 列表，非空 |

## RenderManifest 字段

一次渲染的产物清单，由 `tools/render_episode.py` 写出。

| 字段 | 说明 |
| --- | --- |
| `schema` | 固定 `datafarm.render-manifest/1` |
| `render_spec` / `episode` / `scene_graph` | 输入的 ArtifactRef |
| `engine` | `{name: "isaac_rtx", isaac_sim, isaac_lab}`：实际运行的版本 |
| `streams[]` | 每个变体一条：`{variant_id, view_id, camera_id, video, intrinsics, extrinsics, frame_count}`；`video` 是 `video/mp4`，`extrinsics` 是逐帧 `T_world_camera`（OpenCV 光学系）JSON |
| `frame_map[]` | `{camera_id, variant_id, frame_index, control_step, status, intrinsics, extrinsics, image}`，`status` 为 `ok` / `missing` / `error`，后两者带 `reason`。三个 ref 指向该条 stream 的文件 |
| `state_playback` | `{status: verified/failed/unverified, max_position_error_m, max_joint_error, tolerance_m, notes}`：回放后从仿真读回的位姿、关节与 `state_trace` 的最大偏差 |

虚拟视角的 `camera_id` 是 `view.<view_id>`。校验会检查每个变体都有对应的 stream，`frame_index` 从 0 起连续，`control_step` 依次为 0、120/fps、2·120/fps……

## 示例（2 视角 × 2 材质 × 2 光照 = 8 个变体，节选）

```json
{
  "schema": "datafarm.render-spec/1",
  "episode": {"kind": "episode-ir", "sha256": "…", "media_type": "application/json"},
  "scene_graph": {"kind": "ir.scene-graph", "sha256": "…", "media_type": "application/json"},
  "engine": "isaac_rtx",
  "views": [
    {"view_id": "head", "kind": "physical", "camera_id": "camera.head", "fps": 10},
    {"view_id": "third_person", "kind": "virtual", "fps": 10,
     "pose": {"frame_id": "world", "position_m": [1.2, -0.8, 1.4], "quaternion_xyzw": [0.1, 0.3, 0.2, 0.9]},
     "intrinsics": {"fx": 320.0, "fy": 320.0, "cx": 320.0, "cy": 240.0, "width": 640, "height": 480}}
  ],
  "materials": [
    {"material_id": "plate.ceramic", "slot": "plate",
     "params": {"base_color": [0.9, 0.9, 0.88], "metallic": 0.0, "roughness": 0.35}},
    {"material_id": "plate.steel", "slot": "plate",
     "params": {"base_color": [0.8, 0.8, 0.82], "metallic": 1.0, "roughness": 0.2}}
  ],
  "lights": [
    {"light_id": "dome.warm", "type": "dome", "intensity": 1000.0, "color_temperature": 3200},
    {"light_id": "dome.cool", "type": "dome", "intensity": 1000.0, "color_temperature": 6500}
  ],
  "variants": [
    {"variant_id": "head.m0.l0", "view": "head", "material_set": ["plate.ceramic"], "light_set": ["dome.warm"]},
    {"variant_id": "third_person.m1.l1", "view": "third_person", "material_set": ["plate.steel"], "light_set": ["dome.cool"]}
  ],
  "output": {"format": "mp4", "codec": "h264", "crf": 18}
}
```

## 约定

- **物理视角也由 A 渲染**。B 在 headless `newton_mjwarp` 下只产出状态流，不产出图像。单臂 MVP 的 `camera.head`、`camera.left_wrist` 的每一帧都由 A 从 `state_trace` 回放渲染（没有 `camera.right_wrist`）；腕部相机挂在机器人 link 上（SceneGraph `cameras[].link`），外参随每帧关节状态由 Isaac 的 link 位姿得出。
- 回放只写状态、不步进物理：每帧写入 `state_trace[k].robot` 的关节 q（含夹爪 mimic）、其余 `entities` 的根位姿与 `entities[node].joints`（抽屉等），然后只做渲染更新。只渲染 `control_step` 为 120/fps 整数倍的状态，不插值。
- 一条物理 episode 派生的多个材质/光照变体仍只算一条物理轨迹；变体不改变几何、碰撞、动作或接触。
- 无效帧记 `missing` / `error`，不复制上一帧冒充同步。
- 这是物理渲染，不是照片级重建：不得称为"真实图像"或 real-ready 数据。

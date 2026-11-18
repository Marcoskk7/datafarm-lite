# AssetRecord — `datafarm.asset-record/1`

一个资产（物体或机器人）检索到之后的完整记录：来源、许可、几何、关节、物理量。后面所有环节只认这份记录，不回查上游数据集。

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema` | 是 | 固定 `datafarm.asset-record/1` |
| `asset_id` | 是 | 点分标识，如 `object.plate_raised_rim`、`robot.airbot`、`object.cabinet_drawer` |
| `role` | 是 | `robot` / `task_object` / `receptacle` / `background` / `camera_rig` |
| `source` | 是 | 检索来源，见下 |
| `files` | 是 | `[{artifact: ArtifactRef, format}]`，`format` 取 `usd` / `urdf` / `mjcf` / `glb` / `obj` |
| `bbox_m` | 是 | 静止姿态下 AABB `[x, y, z]`，米 |
| `collision` | 是 | `{kind: "mesh" / "convex_hull" / "primitive", artifact?: ArtifactRef}`；没有单独碰撞网格时 `kind` 指主文件里的碰撞几何 |
| `joints` | 是 | 关节数组，刚体写 `[]`，见下 |
| `parts` | 否 | 分件碰撞盒数组，每个刚体（连杆）一条，见下；给场景按移动件扫掠关节行程，旧记录没有也能用 |
| `frames` | 是 | `{up_axis: "Z", root_frame_id, link_frame_ids: {...}}`；导入需要旋转或缩放时在 `notes` 写明 |
| `physical_quantities` | 是 | 复用 `datafarm.grounding/1` 的 physical_quantities 列表，见下 |
| `notes` | 否 | 人读备注：导入时的旋转、缩放、已知简化 |

`source`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `kind` | 是 | `simready` / `partnet_mobility` / `usdcraft` / `local` |
| `uri` | 是 | 下载地址或仓库内路径；不得带凭据或 query |
| `license` | 是 | SPDX 标识或许可名；无法确认写 `"unknown"` 并在 `notes` 说明 |
| `retrieved_at` | 是 | UTC ISO8601，`Z` 结尾 |
| `upstream_id` | 否 | 数据集内的资产 id / 版本 |

`joints[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 关节名，场景内唯一，如 `joint.drawer.slide` |
| `type` | 是 | `revolute` / `prismatic` / `fixed` |
| `axis` | 是 | 关节轴，单位向量（资产自身坐标系） |
| `limits` | 是 | `{lower, upper}`；revolute 用 rad，prismatic 用 m |
| `parent` / `child` | 是 | 连杆名 |
| `anchor_m` | 否 | 关节枢轴（关节系原点）在资产根系的坐标 `[x, y, z]`，米；revolute 绕它转动，prismatic 也带上 |
| `child_body` | 否 | 关节 `child` prim 所属刚体的 prim 路径（`prim_ref` 形式，如 `RootNode/Geometry/part_01_obj_00`），指向 `parts[].prim` |
| `mimic` | 否 | `{joint, multiplier, offset}`；G2 夹爪的从动指在这里，B 只观测不下发 |

`parts[i]`（分件碰撞盒，可选）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 刚体/连杆 prim 路径的叶名（如 `part_01_obj_00`） |
| `prim` | 否 | 刚体 prim 路径（`prim_ref` 形式，资产根相对、无前导 `/`，如 `RootNode/Geometry/part_01_obj_00`）；`joints[].child_body` 指向它 |
| `bbox_min_m` / `bbox_max_m` | 是 | 该刚体全部碰撞几何的轴对齐包围盒，资产根系（`frames.root_frame_id`）、米、静止姿态 |

- 坐标系与单位：`parts[]` 和 `anchor_m` 都在资产根系里，米；场景把它们当作节点位姿前（rest）的几何，
  节点绕世界 Z 的偏航由 SceneLayout 的 `yaw_deg` 加上去。关节初值 `joint_state` 再叠加在分件上。
- `parts[]` 由 `inspect_asset` 的碰撞体包围盒按刚体合并生成（同一刚体多个碰撞体取并集），每个刚体一条；
  叶名可能重复，按 `prim` 区分。
- `joints[].child` 是关节子 prim 的叶名，SimReady 里常是刚体下面的网格（`part_01_obj_00/part_01_mesh_00`
  → `part_01_mesh_00`），和分件叶名（`part_01_obj_00`）对不上，所以移动件靠 `child_body` 连到 `parts[].prim`；
  字段缺失时不按叶名猜，场景回退到整根系包围盒扫掠。
- 这是**可选**字段：旧记录没有 `parts[]` / `child_body` / `anchor_m` 仍然通过校验（按
  `datafarm.asset-record/1` 的版本规则第 8 条，新增可选字段不升版本）；场景扫掠在缺分件、缺 `child_body`
  或转动缺枢轴时回退到整根系包围盒。

`physical_quantities[i]`（字段和校验照搬 `src/datafarm/grounding/quantities.py::validate_quantities`）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | `mass`、`friction_static`、`drawer_damping` 等 |
| `label` | 是 | `measured` / `fitted` / `assumed` / `unknown` |
| `unit` | 是 | SI 单位，无量纲写 `""` |
| `value` | 视情况 | 只有 `unknown` 可为 null |
| `evidence` | 视情况 | `measured` / `fitted` 必填，ArtifactRef 数组 |
| `randomization` | 视情况 | `assumed` / `unknown` 必须给 `{low, high}` 或 `{choices: [...]}` |
| `not_randomized_reason` | 视情况 | 与 `randomization` 二选一 |

`assumed` / `unknown` 的量会被写进 TaskContract 的随机化清单，采样结果记入 `Episode.payload.setup`。

## 示例：带帮浅盘

```json
{
  "schema": "datafarm.asset-record/1",
  "asset_id": "object.plate_raised_rim",
  "role": "task_object",
  "source": {
    "kind": "simready",
    "uri": "https://huggingface.co/datasets/nvidia/simready-assets",
    "license": "unknown",
    "retrieved_at": "2026-11-15T02:00:00Z",
    "upstream_id": "simready/plate_raised_rim/v1"
  },
  "files": [
    {"artifact": {"kind": "asset.usd", "sha256": "…", "media_type": "model/vnd.usd"}, "format": "usd"}
  ],
  "bbox_m": [0.22, 0.22, 0.035],
  "collision": {"kind": "mesh"},
  "joints": [],
  "frames": {"up_axis": "Z", "root_frame_id": "object.plate_raised_rim", "link_frame_ids": {}},
  "physical_quantities": [
    {"name": "mass", "label": "assumed", "unit": "kg", "value": 0.35,
     "randomization": {"low": 0.2, "high": 0.5}},
    {"name": "friction_static", "label": "assumed", "unit": "", "value": 0.6,
     "randomization": {"low": 0.3, "high": 1.0}}
  ],
  "notes": "raised rim, graspable by parallel jaw; license not stated upstream"
}
```

## 示例：带抽屉的柜子（铰接）

```json
{
  "schema": "datafarm.asset-record/1",
  "asset_id": "object.cabinet_drawer",
  "role": "receptacle",
  "source": {"kind": "partnet_mobility", "uri": "https://partnet.cs.stanford.edu/",
             "license": "MIT", "retrieved_at": "2026-11-15T02:00:00Z", "upstream_id": "45261"},
  "files": [
    {"artifact": {"kind": "asset.usd", "sha256": "…", "media_type": "model/vnd.usd"}, "format": "usd"}
  ],
  "bbox_m": [0.6, 0.5, 0.8],
  "collision": {"kind": "convex_hull"},
  "joints": [
    {"name": "joint.drawer.slide", "type": "prismatic", "axis": [0, 1, 0],
     "limits": {"lower": 0.0, "upper": 0.42}, "parent": "cabinet_body_mesh", "child": "drawer_mesh",
     "anchor_m": [0.0, -0.2, 0.35], "child_body": "RootNode/Geometry/part_01_obj_00"}
  ],
  "parts": [
    {"name": "body_01_obj_00", "prim": "RootNode/Geometry/body_01_obj_00",
     "bbox_min_m": [-0.3, -0.25, 0.0], "bbox_max_m": [0.3, 0.25, 0.8]},
    {"name": "part_01_obj_00", "prim": "RootNode/Geometry/part_01_obj_00",
     "bbox_min_m": [-0.26, -0.24, 0.3], "bbox_max_m": [0.26, -0.02, 0.5]}
  ],
  "frames": {"up_axis": "Z", "root_frame_id": "object.cabinet_drawer", "link_frame_ids": {}},
  "physical_quantities": [
    {"name": "mass", "label": "assumed", "unit": "kg", "value": 12.0,
     "randomization": {"low": 8.0, "high": 20.0}},
    {"name": "drawer_damping", "label": "assumed", "unit": "", "value": 0.1,
     "randomization": {"low": 0.02, "high": 0.3}}
  ]
}
```

抽屉必须带 `prismatic`（或 `revolute`）关节；没有关节的柜子只能当背景，不能当 receptacle。MVP 要求抽屉**预先拉开**，初值写在 SceneGraph 的 `joint_state` 里。

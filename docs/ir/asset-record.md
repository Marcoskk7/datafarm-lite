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
| `collision` | 是 | `{kind: "mesh" / "convex_hull" / "primitive", artifact?: ArtifactRef}`；没有单独碰撞网格时 `kind` 指主文件里的碰撞几何。机器人额外带摆位标定，见下 |
| `joints` | 是 | 关节数组，刚体写 `[]`，见下 |
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
| `mimic` | 否 | `{joint, multiplier, offset}`；G2 夹爪的从动指在这里，B 只观测不下发 |

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

刚体还会带 `<body>.inertia_diagonal`（`kg*m^2`）和 `<body>.center_of_mass`（`m`）：来自资产作者的
MassAPI（老资产是 Isaac 的 `pxr:usd:physics_*`），刚体没有时按平行轴定理从碰撞体合成。两者都是
`assumed` + `not_randomized_reason`——作者数据没有任务证据，交付也不采样惯量。没有可用惯量的刚体
不写这两项，由引擎按几何估计。

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
     "limits": {"lower": 0.0, "upper": 0.42}, "parent": "cabinet_body", "child": "drawer"}
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

机器人（`role: "robot"`）的 `collision` 必须带摆位标定，否则 `plan` 拒绝把它放到支撑面上：

| 字段 | 含义 |
| --- | --- |
| `base_support_z_m` | 压在支撑面上的安装板下表面，基座坐标系 z |
| `base_footprint_min_m` / `base_footprint_max_m` | 安装板 xy 占地矩形 `[x, y]` |
| `base_mount` | `{"kind": "surface"}` 或 `{"kind": "edge_clamp", "axis": "+x/-x/+y/-y", "edge_plane_m", "overhang_m"}` |
| `base_bbox_min_m` / `base_bbox_max_m` | 基座刚体全部碰撞体的实测包围盒，只作证据 |

来源是 `configs/robot-mounts.json` 的显式标定，`prepare` 用实测包围盒复核后写入；不能让消费方用 `base_bbox_min_m` 推高度——AIRBOT 的 C 型夹在安装面以下约 10 cm，拿包围盒底面会把整机抬成悬空。

抽屉必须带 `prismatic`（或 `revolute`）关节；没有关节的柜子只能当背景，不能当 receptacle。MVP 要求抽屉**预先拉开**，初值写在 SceneGraph 的 `joint_state` 里。

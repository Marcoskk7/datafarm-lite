# Request — `datafarm.request/1`

数据请求：用户要什么数据。1a 把一句自然语言经一次结构化 LLM 调用写成 `request.yaml`，1b 的 coding agent 据此写 `datafarm.scene-layout/1`，后续阶段从这里读 episode 数、seed 分区、相机和渲染变体。

A 线普通文档（不套 kernel 信封），YAML 或 JSON 均可。它**只写意图**：不带坐标、位姿、资产 ID（SimReady 名、USD 路径）或主机路径；这些由 1b 检索、组装后写进 scene-layout / SceneGraph。校验器是 `src/datafarm/request/schema.py::validate_request`，1a 的结构化输出 schema 由 `request_json_schema()` 从同一个模型导出。

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema` | 是 | 固定 `datafarm.request/1` |
| `request_id` | 是 | 标识（见 [README §4](README.md#4-标识)），如 `plate-drawer-airbot-001` |
| `robot` | 是 | 机器人能力表 `ROBOT_CAMERAS` 里的键；目前只有 `airbot_single_g2`（单臂，左臂 + G2 夹爪） |
| `task` | 是 | 一句自然语言目标，如 `put the plate into the open drawer`；判定器与专家由后续阶段从它派生 |
| `scene` | 是 | 场景意图，见下 |
| `episodes` | 是 | 要交付的合格 episode 数，正整数 |
| `seeds` | 是 | `{dev: [first, last], holdout: [first, last]}`，见下 |
| `cameras` | 是 | 要记录的机器人相机 id，不重复，必须是该机器人装了的相机，见下 |
| `render` | 是 | `{engine, views, materials, lights}`，见下 |
| `dataset` | 是 | `{format: "hdf5"}`；目前只有 HDF5 |

`scene`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `setting` | 否 | 场景类型，snake_case，如 `kitchen` |
| `objects` | 是 | 物体数组，至少一个 `role=task_object`，见下 |
| `asset_sources` | 否 | 允许的资产来源，按偏好排序：`simready` / `partnet_mobility` / `usdcraft` / `local`（同 AssetRecord `source.kind`）；省略表示不限 |

`scene.objects[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `object_id` | 是 | 请求内唯一的标识，如 `object.plate`；1b 写 scene-layout 时沿用为 `node_id`。`floor` 保留 |
| `category` | 是 | 物体类别，snake_case 名词，如 `drawer_cabinet`；1b 用它检索资产 |
| `role` | 是 | `task_object` / `receptacle` / `background`（AssetRecord `role` 去掉 `robot`、`camera_rig`） |
| `constraints` | 否 | 结构化约束，见下；省略等于 `{}` |
| `count` | 否 | 相同实例个数，默认 1 |

`constraints`（字段都可选，不接受其他键，不接受自由文本）：

| 字段 | 说明 |
| --- | --- |
| `joint` | 物体必须具有的关节：`prismatic` / `revolute`（抽屉滑轨是 `prismatic`） |
| `joint_state` | 该关节初值：`open` / `closed`；必须同时给 `joint` |
| `shape` | 形状特征，snake_case，如 `raised_rim` |
| `support` | 放在哪个物体上：另一个 `object_id` 或 `floor`；对应 scene-layout 节点的 `on` |

`render`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `engine` | 是 | 固定 `"isaac_rtx"`，同 [RenderSpec](render-spec.md) |
| `views` | 是 | 视角 id 数组，不重复：机器人相机 id（必须也在 `cameras` 里）或虚拟视角名（如 `third_person`）；位姿由 RenderSpec 决定 |
| `materials` | 是 | 每个视角的材质变体数，≥1；1 表示保持资产原材质 |
| `lights` | 是 | 每个视角的光照变体数，≥1；1 表示保持场景原光照 |

变体总数 = `len(views) × materials × lights`，由 RenderSpec 展开成具体 `variants`。

## 约束

- **seed 分区**：`dev` 与 `holdout` 都是闭区间 `[first, last]`，`0 ≤ first ≤ last`，两区间不得相交（`[0, 99]` 与 `[100, 119]` 合法，`[0, 99]` 与 `[99, 120]` 不合法）。`dev` 给专家作者调试，`holdout` 只给冻结判定器验收；`holdout` 不得出现在作者能看到的任何上下文里。
- **相机能力**：`cameras` 必须是 `ROBOT_CAMERAS[robot]` 的子集。`airbot_single_g2` 只有 `head` 与 `left_wrist`，**没有 `right_wrist`**；请求双腕会被拒绝，而不是静默丢掉。能力表只在 `schema.py` 一处维护。
- **支撑关系**：`support` 引用的 `object_id` 必须存在，支撑链无环且最终落在 `floor` 或无 `support` 的物体上。
- `object_id` 唯一；至少一个 `task_object`。
- 类型严格：`episodes: "20"`、`materials: 2.0` 一律拒绝，不做隐式转换；未知字段一律拒绝。
- 复用 kernel 的可移植性检查（`canonical_json_bytes`）：不得出现绝对路径、`*_path` / `*_file` 字段、凭据字段或带 query 的 URL。
- JSON Schema 只覆盖结构；seed 不相交、引用存在、相机能力这类跨字段约束只在 `validate_request` 里检查。1a 拿到 LLM 输出后必须再过一遍 `validate_request`。

## 示例

`examples/request/plate-drawer-airbot.yaml`：

```yaml
schema: datafarm.request/1
request_id: plate-drawer-airbot-001
robot: airbot_single_g2
task: put the plate into the open drawer
scene:
  setting: kitchen
  objects:
    - object_id: object.table
      category: table
      role: background
      constraints: {support: floor}
    - object_id: object.cabinet
      category: drawer_cabinet
      role: receptacle
      constraints: {joint: prismatic, joint_state: open, support: floor}
    - object_id: object.plate
      category: plate
      role: task_object
      constraints: {shape: raised_rim, support: object.table}
  asset_sources: [simready, partnet_mobility]
episodes: 20
seeds:
  dev: [0, 99]
  holdout: [1000, 1019]
cameras: [head, left_wrist]
render:
  engine: isaac_rtx
  views: [head, third_person]
  materials: 2
  lights: 2
dataset: {format: hdf5}
```

## 约定

- 请求是意图，不是规格：`category: drawer_cabinet` 不指定哪个资产，`support: object.table` 不指定放在桌面哪里。具体资产与坐标由 1b 决定并留证据，不回写本文件。
- 1a 的 LLM 调用经 `datafarm.request.ports.StructuredLLM` 端口完成，厂商实现放在 `adapters/`；记录 `model`、`response_id` 与 token 数，但不把它们写进 request 本身。命令与默认值见 [request-drafting](../reference/request-drafting.md)。
- 新机器人、新相机先改 `ROBOT_CAMERAS`；新角色、新约束键按 [README §8](README.md#8-版本规则) 走：新增可选字段版本不变，改含义或加必填换 `/2`。

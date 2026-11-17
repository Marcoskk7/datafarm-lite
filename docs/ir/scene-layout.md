# SceneLayout — `datafarm.scene-layout/1`

场景布局说明：A 线普通文档，是 `tools/scene_assemble.py` 的输入。它只描述"放什么、放在哪、要够到哪"，
不描述资产本身的几何、碰撞和物理量；那些来自 SimReady 资产与 [AssetRecord](asset-record.md)。
`prepare` 读 `assets[]` 生成本地资产记录，`plan` 读其余节解位姿并做静态检查，`check` 再读 `probes[]`
和 `collision_groups` 在仿真里验证，最后产出 [SceneGraph](scene-graph.md)。

字段先少而准：布局只写水平位置、偏航角、支撑面和关节初值，高度由支撑面顶面与资产包围盒底面算出，
避免手填 z 造成悬空或穿模。改布局时在 `plan` 这一步迭代，不必启动仿真器。

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema` | 是 | 固定 `datafarm.scene-layout/1`；`resolve_nodes` 第一行校验 |
| `scene_id` | 是 | 如 `scene.plate_drawer_airbot`；`check` 写 SceneGraph 时用，必须符合 IR 标识符规则 |
| `assets` | 是 | 要检索并预处理的资产数组，见下；`prepare` 按此逐项处理 |
| `nodes` | 是 | 场景实例数组，见下；顺序即 `plan.json` 里 `nodes` 的顺序 |
| `robot` | 是 | 机器人挂载、TCP 与可达性参数，见下 |
| `reach_targets` | 是 | 可达性目标数组，见下；可以为空数组（此时可达性 trivially ok） |
| `probes` | 否 | 放置探针数组，仅 `check` 使用；缺省为空 |
| `collision_groups` | 是 | 接触判定分组，原样写进 SceneGraph；`check` 时校验，见下 |
| `cameras` | 否 | 相机数组，原样写进 SceneGraph 的 `cameras`；缺省为空 |

### `assets[i]`

对应一个 SimReady 资产或手动放置的资产。`prepare` 用 `--cache/<simready>/` 定位资产目录，
目录里必须有 `simready-fetch.json`；处理后 `asset_id` 成为 AssetRecord 的主键，也是 `nodes[].asset_id`
和 `robot.asset_id` 的取值来源。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `asset_id` | 是 | 资产标识，如 `object.plate`；同一文档内唯一 |
| `role` | 是 | `robot` / `task_object` / `receptacle` / `background` / `camera_rig`，写进 AssetRecord；`receptacle` 必须有 prismatic/revolute 关节 |
| `simready` | 是 | `--cache` 下的目录名，如 `dishware_plastic_bigPlate_a01`；目录里必须有 `simready-fetch.json`。机器人不写进 `assets[]`，由 `robot.package_asset` 单独处理 |
| `body` | 是 | `kinematic` / `rigid` / `articulation`，决定 Isaac Lab 用 `RigidObjectCfg`（kinematic 时 `kinematic_enabled=true`）还是 `ArticulationCfg` |
| `anchor_body` | `body=articulation` 时是 | 基座刚体在包装层里的 prim 路径，必须是相对资产根的 `prim_ref` 形式：无前导 `/`，每段只用字母、数字、下划线，如 `RootNode/Geometry/body_01_obj_00`；`prepare` 给它加固定关节锚到资产根 Xform |

`anchor_body` 的格式：`prim_ref` 就是 USD prim 路径去掉前导 `/`（见 `adapters/sim/usd_assets.prim_ref`），
所以 `RootNode/Geometry/body_01_obj_00` 是合法值，`/RootNode/Geometry/body_01_obj_00` 不是。
拒绝的情况：前导 `/`（kernel 的可移植性检查会把它当成宿主绝对路径）、`//`、结尾 `/`、空白、
`.` 或 `..` 段、以及 prim 名之外的字符（如 `-`、`:`、非 ASCII 字母）。前导 `/` 在 `prepare` 里表现为
`SchemaError: ... absolute filesystem path`；现在 `plan` 开头的 `layout.resolve_nodes` 会先逐项校验
`assets[]`（helper `layout.validate_assets`），凡请求 `assets[i]` 的 `anchor_body` 不合法就抛
`IRError: assets[i] (<asset_id>).anchor_body '<值>' must be a prim path relative to the asset root, like
'RootNode/Geometry/body_01_obj_00': no leading '/', no '.', '..' or empty segments`。
`anchor_body` 只在 `body=articulation` 时被 `prepare` 使用；其他 body 写了仍会校验格式，但被忽略，不报错。

`assets[]` 里不写 USD 路径、俯仰/翻滚角或物理参数：姿态只有偏航，高度由布局解出，
质量/摩擦/阻尼写在 AssetRecord 的 `physical_quantities` 里。

### `nodes[i]`

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `node_id` | 是 | 场景内唯一标识，如 `object.plate`；同时作为 Isaac Lab prim 名和 SceneGraph node id |
| `asset_id` | 是 | 必须出现在 `assets[]`，且 `prepare` 已为它产出 AssetRecord |
| `semantic_tag` | 是 | `table` / `drawer` / `plate` / `background` / `robot`；判定器和渲染按此查找 |
| `xy_m` | 是 | 世界系水平位置 `[x, y]`，m；高度不在这里给 |
| `yaw_deg` | 否 | 绕世界 +Z 的偏航角，度，默认 0；四元数固定 xyzw |
| `on` | 否 | 支撑节点 id 或 `"floor"`，默认 `"floor"`；决定 z 和占地检查的支撑面 |
| `z_m` | 否 | 显式覆盖解算出的高度，m；给了就不再按支撑面算，但仍按 `on` 决定子节点顶面和占地归属 |
| `joint_state` | 否 | `{关节名: 初值}`；关节名是上游 USD 里的名字（AssetRecord `joints[].name`），值必须在该关节 `limits` 内。非零初值还参与占地扫掠检查，见下 |
| `randomization` | 否 | `{position_jitter_m, yaw_range_deg}`，原样写进 SceneGraph；`yaw_range_deg` 是度且在 `[-180, 180]` |

非零 `joint_state` 的两条作用：驱动 `reach_targets[].joint` 的目标平移（见下），以及把该节点的占地
扫掠后再做重叠检查（`layout.joint_sweep_check`，见 `plan` 检查第 4 条）。AssetRecord 带 `parts[]`
（分件碰撞盒）和该关节的 `anchor_m` 时按移动件精确扫掠（`prismatic` 平移、`revolute` 绕枢轴旋转）；
缺分件或枢轴时回退到整根系包围盒平移，此时 `revolute` 无法界定转动，`plan` 会在
`footprint.joint_sweep.skipped` 里明确列出，而不是当成通过。

高度解算规则（`layout.resolve_nodes`）：

- `on="floor"`：基准面 `z=0`；否则先递归解出支撑节点，基准面取它的顶面。
- 支撑包围盒优先取 AssetRecord 的 `collision.bbox_*`（碰撞凸包，物理接触发生在那里），没有则取 `frames.bbox_*`，
  再没有才用 `bbox_m`。顶面高度 = 支撑节点 z + 该包围盒 `max_z`。
- 节点 z = 基准面 + `SUPPORT_CLEARANCE_M`（0.001 m）− 自身包围盒 `min_z`，结果取 6 位小数。
- 支撑依赖必须无环、指向已知节点；未知资产、未知节点、关节值越界都是 `IRError`。
- 同一 `node_id` 在 `nodes[]` 里重复出现时后者覆盖前者，不单独报错，作者要自己保证唯一。

### `robot`

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `node_id` | 是 | 机器人在场景内的 node id，如 `robot.airbot`；写进 SceneGraph |
| `asset_id` | 是 | 机器人 AssetRecord 的 id，`prepare` 用 `package_asset` 生成它 |
| `package_asset` | 是 | 机器人包 `delivery_manifest.json` 里的资产名（如 `airbot_single`）；`plan`/`check` 用它重新解析 URDF |
| `on` | 无 `base_pose` 时是 | 支撑节点 id 或 `"floor"` |
| `xy_m` | 无 `base_pose` 时是 | 世界系水平位置 `[x, y]`，m |
| `yaw_deg` | 否 | 偏航角，度，默认 0 |
| `base_pose` | 否 | 显式基座位姿；给了就原样使用，忽略 `on`/`xy_m`/`yaw_deg` |
| `arms` | 是 | 数组，MVP 写 `["left"]`，允许 `left`/`right` 的非空子集 |
| `gripper` | 是 | 固定 `"g2"` |
| `tcp_link` | 是 | 模型 TCP 所在 link；`robot-descriptor` 的 `model_semantics.tcp_candidates` 必须有该 link 的候选，否则报错 |
| `tcp_frame_id` | 是 | TCP 坐标系名，如 `tcp.left`；写进 SceneGraph `frames` |
| `tool_approach_axis` | 是 | 工具系里指向接近方向的轴，`x` / `y` / `z` |
| `limit_margin_rad` | 否 | IK 关节限位余量，rad，默认 0.05 |

无 `base_pose` 时，基座高度让基座刚体碰撞体底面贴在支撑顶面：`z = 支撑顶面 + SUPPORT_CLEARANCE_M − collision.base_bbox_min_m[2]`。
AssetRecord 没有 `collision.base_bbox_min_m` 时报错，不假设基座原点就是底面。

### `reach_targets[i]`

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 目标名，出现在 `plan.json` 的行和 diagnostics 里 |
| `node` | 是 | 目标写在其局部系的节点 id |
| `local_m` | 是 | 节点局部系下的位置 `[x, y, z]`，m |
| `joint` | 否 | 棱柱关节名；目标随该关节初值沿关节轴平移（抽屉内腔）。只接受 prismatic，其他类型报错 |
| `approach` | 否 | 世界系接近方向，代码会归一化；默认 `[0, 0, -1]`（自上而下） |
| `yaw_samples` | 否 | 绕接近轴尝试的转角数，默认 8；任一转角求得逆解即判可达 |

目标世界坐标 = 节点位姿 ×（`local_m` + 关节轴 × `joint_state[joint]`，缺省 0）。`joint` 写的是关节初值平移量，
不是当前测量值。

可达性用多起点阻尼最小二乘 IK，作用在 `tcp_link` 的模型 TCP 上，关节夹在 `[lower+margin, upper−margin]`；
不做碰撞感知规划，也不检查抓取姿态。"没解出来"只说明这些起点下局部未收敛，不能证明全局不可达。

### `probes[i]`（仅 `check`）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 探针名，写进 `check.json` 的 evidence |
| `asset_id` | 是 | 被传送、自由落下的资产；取 `nodes[]` 里第一个使用该资产的节点 |
| `node` | 是 | 目标局部系节点，同时作为期望接触的支撑节点 |
| `local_m` | 是 | `node` 局部系下的投放位置，m |
| `joint` | 否 | 同 `reach_targets[].joint`，投放点随棱柱关节初值平移 |
| `expect_rest_z_min_m` / `expect_rest_z_max_m` | 是 | 静止高度的接受区间，m（世界系） |

探针把物体传送到目标点，自由落体若干秒后检查：末速度低于阈值、静止高度在区间内、与 `node` 有接触。
用来证明"抽屉内腔装得下盘子且能托住"。

### `collision_groups`

`{group_id: {entities: [...], contact_with: [...], forbidden_with: [...]}}`，原样写进 SceneGraph。
`check` 时（`scene_graph.validate`）校验：三个列表都必须是已声明的 node id，`contact_with` 与
`forbidden_with` 不能有交集。`entities` 是本组主体，`contact_with` 是允许接触的对象，
`forbidden_with` 是违规接触的对象；判定器据此区分允许接触与违规接触。

### `cameras`

可选数组，原样进入 SceneGraph 的 `cameras`，由 `scene_graph.validate` 校验。每个相机的字段
（`camera_id`、`kind`、`mount`、`offset`、`intrinsics`、`optical_frame`、`distortion`、`fps`）见
[scene-graph.md](scene-graph.md)；`fps` 必须整除 120。布局本身不读相机。

## 单位与坐标系

- 长度 m，角度：`yaw_deg` / `yaw_range_deg` 用度，`limit_margin_rad` / `yaw_rad` 用弧度。
- 右手系、Z 向上；节点位姿是 `frame_id="world"` 的 `T_world_node`。
- 偏航四元数固定 xyzw：`[0, 0, sin(yaw/2), cos(yaw/2)]`，只绕 +Z。
- 支撑包围盒来自资产根系；碰撞凸包会比渲染网格外扩几毫米，布局按碰撞包围盒算支撑高度。

## `plan` 与 `check` 各验证什么

`plan`（纯 Python + numpy，不启动仿真器）：

1. 解出全部节点位姿，检查 schema、支撑无环、资产存在、关节初值在限位内。
2. 机器人基座位姿（显式 `base_pose` 或按基座碰撞体底面贴支撑面）。
3. 占地检查：每个非 floor 节点（以及由 `on`/`xy_m` 解出的机器人基座）的包围盒底面矩形按偏航旋转后，
   必须落在支撑节点顶面包围盒内；同一支撑面上的物体（floor 也算同一支撑面）两两不许相交。
   机器人给了显式 `base_pose` 时不参与占地检查。只适用于平放物体。
4. 关节初值扫掠（`layout.joint_sweep_check`，结果折进 `footprint`），两条路径：
   - **分件路径（优先）**：AssetRecord 有 `parts[]`、且该关节带 `child_body`（`parts[].prim` 的刚体 prim
     路径；`joints[].child` 本身是关节子 prim 的叶名，常是刚体下的网格，不能直接对叶名）时，
     只扫移动件。`prismatic` 把分件盒沿 `R_z(yaw)·axis·v` 平移，扫掠体取起点盒与终点盒的凸包（对凸盒精确）；
     `revolute` 若有 `joints[].anchor_m`，把分件三轴盒绕枢轴沿 `axis` 转 0→v，按 ≤5° 采样，
     取全部采样 8 个角的 XY 投影凸包（轴向不平行世界 Z 时投影是六边形，取凸包即保守外包；
     弦替代圆弧使覆盖内收不超过 `1e-3·r`）。这类关节记入 `checked` 并带 `method: "parts"`
     （prismatic 记 `travel_m`，revolute 记 `travel_rad`）。
   - **保守回退**：缺 `parts[]`、缺移动件或转动缺 `anchor_m` 时，`prismatic` 把整根系包围盒按
     `R_z(yaw)·axis·v` 平移；`revolute` 无法界定转动，一律记入 `footprint.joint_sweep.skipped`
     （回退输出与引入 `parts[]` 前逐字节一致，`checked` 里没有 `method`）。

   两条路径都只与同一支撑面（`on` 相同，floor 也算同一面）上的其他节点、以及自身支撑节点做 SAT 相交
   （边贴边不算），命中写一行 `object.cabinet drawer_joint at 0.35 m moves into object.table`
   （revolute 的单位写 `rad`）。都是 z 盲、不加余量，所以地面层的薄板仍可能被误报。
   这仍是 GPU 之前的运动学前置闸门，不替代 `check` 的穿透测量。
5. 逐个 `reach_targets` 求逆解，全部有解才 ok。
6. 写 `plan.json`：`spec_sha256`、`nodes`、`robot_base_pose`、`reachability`、`footprint`（含
   `xy_corners_m` 与 `joint_sweep: {ok, checked, skipped}`），外加 `ok`（可达性与占地的合取）。
   退出码 0 当且仅当 `ok=true`。

`check`（Isaac Lab 3.0 + Newton/MJWarp）：

1. 读 `plan.json`，`spec_sha256` 与当前布局不一致直接报错，要求重跑 `plan`。
2. 加载场景，显式写入 `joint_state` 并读回校验（`scene.reset()` 不会把它写回仿真）。
3. 静置检查根漂移与末速度，接触缓冲检查穿透，跑 `probes`。
4. 关节漂移分**主动**（`active_joints` 里列出的驱动关节）与**被动**（其余铰接关节）；机器人自身关节的静置漂移
   只记录（`settle.robot_joint_drift_rad`、`measured_joint_state`），不判定——它是驱动增益下的重力下垂，布局改不了。
   主动关节有严格上限 `--joint-tol-active`；被动关节没有固定上限，只要求静置后的测量值仍在关节 `limits` 内、
   且该节点没有新增接触对（`settle.new_contacts`），`--joint-tol-passive` 只决定何时报告"动了"。
   `check.json` 的 `settle.measured_joint_state` 是静置后按节点、按关节名的测量值，下游 episode 的初始状态
   取它；顶层的 `initial_joint_state` 只是声明的意图。
5. 用 `plan` 的 `nodes`、`robot_base_pose`、`reachability` 拼 SceneGraph 并过校验。
6. 退出码 0 当且仅当静置稳定、无穿透、探针全过、可达性 ok、占地 ok。`plan.json` 的 `ok` 不参与判断，
   `check` 分别读 `reachability.ok` 与 `footprint.ok`。

`plan` 的关节扫掠与 `check` 的关节规则是两层：前者是 GPU 之前的运动学包围盒闸门（分件盒平移/转动或整盒平移、z 盲），
后者判定仿真测量值；`plan` 通过不能替代 `check`，`check` 的被动关节规则也不能用来放宽扫掠。

## 示例（节选）

```json
{
  "schema": "datafarm.scene-layout/1",
  "scene_id": "scene.plate_drawer_airbot",
  "assets": [
    {"asset_id": "object.plate", "role": "task_object", "simready": "dishware_plastic_bigPlate_a01", "body": "rigid"}
  ],
  "nodes": [
    {"node_id": "object.table", "asset_id": "object.table_stainless", "semantic_tag": "table",
     "xy_m": [0.0, 0.0], "yaw_deg": 0, "on": "floor"},
    {"node_id": "object.plate", "asset_id": "object.plate", "semantic_tag": "plate",
     "xy_m": [0.52, -0.20], "yaw_deg": 0, "on": "object.table",
     "randomization": {"position_jitter_m": 0.03, "yaw_range_deg": [-180, 180]}}
  ],
  "robot": {
    "node_id": "robot.airbot", "asset_id": "robot.airbot_g2_left", "package_asset": "airbot_single",
    "on": "object.table", "xy_m": [0.84, -0.10], "yaw_deg": 0,
    "arms": ["left"], "gripper": "g2",
    "tcp_link": "left_end_link", "tcp_frame_id": "tcp.left",
    "tool_approach_axis": "x", "limit_margin_rad": 0.05
  },
  "reach_targets": [
    {"name": "plate_center_above", "node": "object.plate", "local_m": [0.0, 0.0, 0.10]},
    {"name": "drawer_center_release", "node": "object.cabinet", "joint": "joint_part_01_joint_00",
     "local_m": [0.015, 0.0, 0.82]}
  ],
  "probes": [
    {"name": "plate_in_drawer", "asset_id": "object.plate", "node": "object.cabinet",
     "joint": "joint_part_01_joint_00", "local_m": [0.015, 0.0, 0.80],
     "expect_rest_z_min_m": 0.74, "expect_rest_z_max_m": 0.80}
  ],
  "collision_groups": {
    "task_contact": {"entities": ["object.plate"],
                     "contact_with": ["robot.airbot", "object.cabinet", "object.table"], "forbidden_with": []}
  }
}
```

## 默认值

| 项 | 默认 |
| --- | --- |
| `nodes[].yaw_deg` | 0 |
| `nodes[].on` | `"floor"` |
| `reach_targets[].approach` | `[0, 0, -1]` |
| `reach_targets[].yaw_samples` | 8 |
| `robot.limit_margin_rad` | 0.05 |
| `SUPPORT_CLEARANCE_M` | 0.001 m |

## 请求符合性

`datafarm.scene.conformance.check_conformance(request, layout, records, rules)` 在 `plan` 之前做确定性检查，
确认布局忠实实现了 [Request](request.md)。它不碰几何和物理，只比对结构；能否摆下、够到仍由 `plan`/`check` 决定。

```python
report = check_conformance(request, layout, records, rules)  # rules = load_role_rules("configs/scene-roles.json")
report.ok        # 没有 problems
report.problems  # list[str]，一行一条
report.skipped   # 因缺 AssetRecord 而跳过的关节检查
report.complete  # 没有 skipped
```

- **节点映射**：`count=1` 时 `node_id == object_id`；`count>1` 时用 `<object_id>.<i>`（i 从 0 起）。
  每个请求物体恰好对应一个布局节点，布局里不允许请求之外的节点（机器人节点除外）。
- **角色**：布局 `assets[].role` 必须等于请求 `role`。
- **支撑**：`constraints.support`（缺省 `floor`）必须等于节点 `on`（缺省 `floor`）。
- **关节**：请求给了 `joint` 时，AssetRecord 必须有该类型的关节；`joint_state: open` / `closed`
  把关节位置按行程 `[lower, upper]` 从下限归一化，`open` 要求至少一个该类型关节 ≥ `open_min_fraction`，
  `closed` 要求至少一个 ≤ `closed_max_fraction`。没有传 AssetRecord 时这些检查记为 `skipped`，
  `complete=False`；`ok` 仍可能为真，调用方自己决定是否接受不完整报告。
- **机器人**：布局 `robot.package_asset`（规则表给出 `asset_id` 时也检查它）必须匹配请求 `robot`。
- **资产来源**：请求给了 `scene.asset_sources`、布局资产又带 `simready` 时，`simready` 必须在允许列表里。
- **角色规则**：见下表，取值在 `configs/scene-roles.json`。

| 角色 | 规则 |
| --- | --- |
| `task_object` | 至少 1 个 `reach_targets` 指向该节点；节点必须声明 `randomization`（`position_jitter_m` 或 `yaw_range_deg`） |
| `receptacle` | 至少 1 个 `reach_targets` 指向该节点；至少 1 个 `probes` 的 `node` 是该节点，且该 probe 的资产属于某个 `task_object` 节点 |
| `background` | 无额外规则（`reach_targets_min: 0`） |

规则表 schema 为 `datafarm.scene-roles/1`：`roles.<role>.reach_targets_min` / `require_probe` /
`probe_asset_role` / `require_randomization`，`joint_state.open_min_fraction` / `closed_max_fraction`，
`robots.<request robot>.package_asset`（可选 `asset_id`）。阈值都按关节行程从下限起算。

## 限制

- 节点只有偏航，不支持俯仰/翻滚；占地检查假设物体平放。
- 布局不检查手臂与柜体/桌子的碰撞，也不检查抓取姿态；这些留给 B 线规划和判定。
- 可达性只算运动学；"没找到解"不是全局不可达的证明。
- 布局不写资产物理参数，质量、摩擦、恢复系数、抽屉阻尼来自 AssetRecord，标为 `assumed`。

# SceneGraph — `datafarm.scene-graph/1`

后端无关的场景描述：放哪些资产、各自位姿、关节初值、相机、机器人，以及场景检查结果。Isaac Lab 3.0 从这里生成自己的场景（`SceneBinding`），不从 USD 层级或数组顺序推断语义。

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema` | 是 | 固定 `datafarm.scene-graph/1` |
| `scene_id` | 是 | 如 `scene.kitchen_plate_drawer` |
| `units` | 是 | 固定 `"m"` |
| `frame` | 是 | 固定 `"world"`，Z-up 右手系 |
| `asset_bundle` | 是 | ArtifactRef，整个场景的完整依赖集（所有 USD 及其引用） |
| `assets` | 是 | `[{asset_id, record: ArtifactRef}]`，指向 AssetRecord 文档 |
| `nodes` | 是 | 场景实例数组，见下 |
| `frames` | 是 | 命名坐标系 `[{frame_id, parent_frame_id, pose}]`；无环、无悬空 parent |
| `cameras` | 是 | 相机数组，见下 |
| `robot` | 是 | 机器人挂载、可控关节和可达性，见下 |
| `collision_groups` | 是 | `{group_id: {entities: [...], contact_with: [...], forbidden_with: [...]}}`；判定器据此区分允许接触与违规接触 |
| `physics_parameter_ledger` | 是 | ArtifactRef，本次场景实例化实际使用的物理量（含随机化采样结果） |
| `runtime_profile` | 是 | ArtifactRef，控制器、求解器、dt、驱动与接触配置的不可变快照 |
| `backend` | 是 | `{name: "isaaclab", version: "3.0.0", physics: "newton_mjwarp"}` |
| `checks` | 是 | 场景检查结果，见下 |
| `randomization` | 否 | `{seed, sampled: {quantity: value}}` |

`nodes[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `node_id` | 是 | 场景内唯一，如 `object.plate` |
| `asset_id` | 是 | 对应 `assets` 的 asset_id |
| `parent` | 否 | 挂到另一个 node（如抽屉里的盘子）；null 表示直接挂 world |
| `pose` | 是 | Pose，相对 parent |
| `joint_state` | 否 | `{关节名: 值}`，铰接资产的**声明意图**（预先拉开的抽屉写 `{"joint.drawer.slide": 0.42}`）；不是物理事实。静置后的测量值在 `check.json` 的 `settle.measured_joint_state`，并成为 episode 的 `setup.initial_state` / `setup.articulations` |
| `semantic_tag` | 是 | 小写类别标识符（`^[a-z0-9][a-z0-9._:-]{0,127}$`），如 `table` / `block` / `microwave_oven`；`robot` 保留给机器人节点。渲染按它分组材质，不按名字前缀 |
| `randomization` | 否 | `{position_jitter_m, yaw_range_deg}`；yaw 用度，`[-180, 180]` |

`semantic_tag` 是开放词汇表：任何合法标识符都可以，新增类别不需要改代码。旧文档里的 `table` / `drawer` / `plate` / `background` 仍然合法，其中 `background` 不再承担"其他"的兜底语义。`robot` 是唯一保留值，只能用在 `robot.asset_id` 对应的节点上。

`cameras[i]`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `camera_id` | 是 | 单臂 MVP：`camera.head` / `camera.left_wrist`；`camera.right_wrist` 只在双臂模型里出现 |
| `kind` | 是 | `physical`（绑在机器人 link 上）或 `virtual`（渲染时自由加） |
| `mount` | 是 | 挂载的 node 或 frame id；腕部相机填机器人 node |
| `link` | 否 | 机器人 link 名，如 `left_end_link`。给出时 `mount` 必须是机器人 node，相机随该 link 运动 |
| `offset` | 是 | 光学坐标系相对挂载点的 Pose：给了 `link` 时 `frame_id` 填 link，否则填 mount；轴向按 `optical_frame.convention` |
| `intrinsics` | 是 | `{fx, fy, cx, cy, width, height}`，像素 |
| `optical_frame` | 是 | `{convention: "opencv" / "ros" / "opengl"}`：opencv 与 ros 同为 z 前、x 右、y 下；opengl 为 -z 前、y 上 |
| `distortion` | 是 | `{model: "none" / "radtan", coefficients: [...]}`；MVP 用 `none` |
| `fps` | 是 | 采样帧率，MVP 10；必须整除控制频率 120 |

单臂 MVP（AIRBOT G2 左臂）只有两路物理相机，另加渲染用的虚拟视角 `third_person`（640x480，只在 RenderSpec 里声明，不进 SceneGraph）：

| `camera_id` | 来源 | 分辨率 |
| --- | --- | --- |
| `camera.head` | ZED 2i 左目，`airbot_dual_description/config/layout.yaml` 的 `env_camera`；内参取 `calib/zed2i_39532546_intrinsics.json` 的 HD720（1280x720，fx 521.543）减半 | 640x360，fx 260.77 |
| `camera.left_wrist` | D435i 彩色，挂 `left_end_link`；外参 `calib/wrist_d435i_handeye.json` 的 `T_flange_from_camera`，内参 `calib/wrist_d435i_intrinsics.json`；URDF `airbot_g2_left.urdf` 的 `left_camera_link` 只是相机外壳网格 | 640x480，fx 607.07 |

单臂 URDF 没有右腕相机，也没有 head/ZED link。`right_camera_link` 只存在于双臂模型 `airbot_dual.urdf`，且 `layout.yaml` 里右腕暂时复用左腕手眼标定；上双臂前不得声明 `camera.right_wrist`。

`robot`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `asset_id` | 是 | 机器人 AssetRecord |
| `base_pose` | 是 | Pose |
| `arms` | 是 | MVP 只有 `["left"]`（单臂 AIRBOT + G2）；字段保持数组，以后加 `"right"` 不升版本 |
| `gripper` | 是 | `"g2"` |
| `controlled_joints` | 是 | B 下发的关节名；mimic 从关节只观测 |
| `tcp` | 是 | `{frame_id, link, T_link_tcp: Pose}`；禁止用 link 原点临时顶替 |
| `reachability` | 是 | `{checked, ok, notes}`；任务物体和抽屉内腔都必须可达 |

MVP 复用已验收资产 `assets/robot_assets_isaaclab3_validated_20261113/validated_usd/airbot_single`，URDF 是关节限位、运动链和夹爪几何的权威来源，SceneGraph 不重复手抄。

`checks`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `settle` | 是 | `{ran, steps, max_drift_m, stable}`；加载场景后空跑若干控制步，刚体位移不超过阈值 |
| `penetration` | 是 | `{ran, max_depth_m, ok}` |
| `reachability` | 是 | 与 `robot.reachability` 同构 |

可达性判断只能证明"某些目标位姿可达"，单次局部 IK 不收敛不能证明全局不可达，结论要写进 `notes`。

## 示例（节选）

```json
{
  "schema": "datafarm.scene-graph/1",
  "scene_id": "scene.kitchen_plate_drawer",
  "units": "m",
  "frame": "world",
  "asset_bundle": {"kind": "scene.bundle", "sha256": "…", "media_type": "application/json"},
  "assets": [
    {"asset_id": "object.plate_raised_rim",
     "record": {"kind": "ir.asset-record", "sha256": "…", "media_type": "application/json"}},
    {"asset_id": "object.cabinet_drawer",
     "record": {"kind": "ir.asset-record", "sha256": "…", "media_type": "application/json"}}
  ],
  "nodes": [
    {"node_id": "object.table", "asset_id": "object.table_simple", "semantic_tag": "table",
     "pose": {"frame_id": "world", "position_m": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]}},
    {"node_id": "object.cabinet", "asset_id": "object.cabinet_drawer", "semantic_tag": "drawer",
     "pose": {"frame_id": "world", "position_m": [0.45, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]},
     "joint_state": {"joint.drawer.slide": 0.42}},
    {"node_id": "object.plate", "asset_id": "object.plate_raised_rim", "semantic_tag": "plate",
     "pose": {"frame_id": "world", "position_m": [0.0, -0.25, 0.78], "quaternion_xyzw": [0, 0, 0, 1]},
     "randomization": {"position_jitter_m": 0.05, "yaw_range_deg": [-180, 180]}}
  ],
  "frames": [
    {"frame_id": "world", "parent_frame_id": null,
     "pose": {"frame_id": "world", "position_m": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]}}
  ],
  "cameras": [
    {"camera_id": "camera.left_wrist", "kind": "physical", "mount": "robot.airbot", "link": "left_end_link",
     "offset": {"frame_id": "left_end_link", "position_m": [-0.1358, 0.0332, 0.0936],
                "quaternion_xyzw": [-0.5881, 0.5837, -0.3964, 0.3954]},
     "intrinsics": {"fx": 607.07, "fy": 606.77, "cx": 322.67, "cy": 253.67, "width": 640, "height": 480},
     "optical_frame": {"convention": "opencv"},
     "distortion": {"model": "none", "coefficients": []},
     "fps": 10}
  ],
  "robot": {
    "asset_id": "robot.airbot", "arms": ["left"], "gripper": "g2",
    "base_pose": {"frame_id": "world", "position_m": [0, 0, 0.78], "quaternion_xyzw": [0, 0, 0, 1]},
    "controlled_joints": ["left_joint1", "left_joint6", "left_g2_joint"],
    "tcp": {"frame_id": "tcp.left", "link": "left_link6",
            "T_link_tcp": {"frame_id": "left_link6", "position_m": [0, 0, 0.1266], "quaternion_xyzw": [0, 0, 0, 1]}},
    "reachability": {"checked": true, "ok": true, "notes": "targets within 647 mm; local IK only"}
  },
  "collision_groups": {
    "robot_self": {"entities": ["robot.airbot"], "contact_with": [], "forbidden_with": ["robot.airbot"]},
    "task_contact": {"entities": ["object.plate"], "contact_with": ["robot.airbot", "object.cabinet", "object.table"],
                     "forbidden_with": []}
  },
  "physics_parameter_ledger": {"kind": "ir.physics-ledger", "sha256": "…", "media_type": "application/json"},
  "runtime_profile": {"kind": "sim.runtime-profile", "sha256": "…", "media_type": "application/json"},
  "backend": {"name": "isaaclab", "version": "3.0.0", "physics": "newton_mjwarp"},
  "checks": {
    "settle": {"ran": true, "steps": 240, "max_drift_m": 0.0005, "stable": true},
    "penetration": {"ran": true, "max_depth_m": 0.0, "ok": true},
    "reachability": {"checked": true, "ok": true, "notes": ""}
  }
}
```

## 后端

固定 Isaac Lab 3.0（Isaac Sim 6.2）+ `newton_mjwarp`。没有后端选择、没有能力矩阵、没有探针证书，也没有 SAPIEN 回退路径。渲染用 Isaac RTX / Replicator 6.2 的 API。

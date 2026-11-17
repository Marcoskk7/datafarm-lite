# Episode — `datafarm.episode-ir`

一条已执行、已判定、可重放的 episode。kernel `ArtifactDocument`，`payload` 的七个键由 `REQUIRED_PAYLOAD_KEYS` 固定（`src/datafarm/kernel/schema.py`），内容语义取自 [B 线接口 §6](../reference/b-control-interfaces.md)。这是数据集的原子单位。

## 信封

| 字段 | 值 |
| --- | --- |
| `schema` | `datafarm.episode-ir` |
| `schema_version` | `1.0.0` |
| `artifact_id` | `episode.ep_0007` |
| `producer` | `b.episode-recorder` |
| `predecessors` | TaskContract、SceneGraph、AssetRecord、expert 源码 |
| `evidence` | 判定器复核记录、状态回放误差报告、渲染变体 |

## payload

| 键 | 内容 |
| --- | --- |
| `setup` | 场景 / 机器人 / runtime profile / 合同 / judge / expert 的 ArtifactRef，引擎版本，seed 与分区，实际初始状态与随机化采样，`control_dt_s`，关节顺序与类型，能力快照，judge 结果 |
| `actions` | 逐控制周期一条：`control_step`、`requested_joint_targets` / `applied_joint_targets`（`{joint: 值}`）、`application`、`command` |
| `state_trace` | N+1 个实测状态，每条字段见下；这是 CaP 反馈、Judge 输入和渲染回放共用的唯一状态形状 |
| `contacts` | 每个状态一条接触样本，与 `state_trace[k].contacts` 逐字节相同 |
| `events` | 原语开始/结束、阶段变化、错误、judge、停止与恢复 |
| `frame_map` | 每帧的 `camera_id`、`variant_id`、`control_step`、内外参引用、图像 ArtifactRef |
| `replay` | 状态回放与动作回放的状态、误差报告 ArtifactRef |

录制器（`measured-scene-episode/1`，见 [recorded-episode](../reference/recorded-episode.md)）另写 `profile`、`termination`、`judgement`。
`setup` / `events` / `replay` 的字段表仍是目标设计，录制器目前写的是其中一部分（来源 ref、`control_dt_s`、`node_ids`、初始化记录）；
`actions`、`state_trace`、`contacts` 已与录制器一致。

### setup

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `scene_graph` / `task_contract` / `expert` / `judge` / `runtime_profile` / `scene_bundle` | 是 | 全部 ArtifactRef |
| `backend` | 是 | `{name: "isaaclab", version: "3.0.0", physics: "newton_mjwarp"}` |
| `seed` / `seed_partition` | 是 | 实际 seed 与所属分区（`dev` / `holdout`） |
| `sampled_randomization` | 是 | 本次实际采样的物理量与位姿抖动值 |
| `initial_state` | 是 | `s[0]` 的完整快照摘要，重放从这里恢复 |
| `control_dt_s` / `solver_substeps` | 是 | 1/120 与 4 |
| `joint_names` / `joint_types` / `control_names` | 是 | revolute 用 rad、prismatic 用 m；J 是观测关节数，U 是控制通道数，两者通常不等 |
| `judge_result` | 是 | `{outcome: success/failure/error/aborted, success: bool/null, terminated, truncated, reason_code, criteria: {criterion_id: bool}, evidence_steps, judge_artifact}` |

### actions

每个控制周期一条记录，请求目标与执行器实际读回的目标都要写；只存 `move_tcp` 参数不算记录。

```json
{"control_step": 0, "requested_joint_targets": {"left_joint1": 0.0, "left_g2_joint": 0.02},
 "applied_joint_targets": {"left_joint1": 0.0, "left_g2_joint": 0.02}, "application": "actuator_readback",
 "command": {"call_id": "call.0003", "primitive": "tcp"}}
```

`control_step` 从 0 连续；N 条动作必须对应 N+1 个状态。`applied_joint_targets` 覆盖机器人全部关节的执行器目标读回。

### state_trace

一条 state 的校验器是 `src/datafarm/ir/episode_state.py::validate_state`，录制器（`expert/episode.py`）、CaP 反馈
（`cap/state_feedback.py`）、Judge（`judge/protocol.py`）和渲染回放（`adapters/render/isaac/playback.py`）都用它，
各层只能取子集或整条转发，不能改字段名。字段必须恰好是下面这些（`robot.tcp` / `robot.gripper` 按能力可缺）。

| 字段 | 说明 |
| --- | --- |
| `control_step` | 整数步号；`s[k]` 在第 k 个周期结束后采样 |
| `time_s` | `control_step × control_dt_s` |
| `units` / `frame_id` | 固定 `{length: m, angle: rad, time: s}` / `world` |
| `entities` | `{node_id: 实体}`，覆盖 SceneGraph 的**全部**节点（含机器人根） |
| `robot` | 机器人实测段，等于 `IsaacRobotAPI.observe()` 的输出 |
| `contacts` | 本周期末的接触样本，见 contacts |

`entities[node_id]`：

| 字段 | 说明 |
| --- | --- |
| `availability` | 固定 `available`；读不到的实体整条 state 不入轨迹，原始读数另存为失败证据 |
| `asset_id` | 该节点的 AssetRecord id |
| `position_m` / `quaternion_xyzw` | 根位姿，world 系，四元数 xyzw 单位长度 |
| `linear_velocity_m_s` / `angular_velocity_rad_s` | 根线速度 / 角速度，world 系 |
| `joints` | `[{name, type: revolute/prismatic, unit: rad/m, q, dq}]`，按源资产的非固定关节；抽屉开度就在这里 |
| `links` | `{link 名: {position_m, quaternion_xyzw}}`，各刚体 link 的世界位姿 |

`robot`：

| 字段 | 说明 |
| --- | --- |
| `joint_names` / `q` / `dq` | 运行时关节顺序与对应实测位置 / 速度；revolute 用 rad、prismatic 用 m |
| `tcp` | 命名 TCP 的 Pose（world），绑定没有 TCP 时缺省 |
| `gripper` | `{width_m, width_range_m, state_source, verification, within_defined_range}`，绑定没有夹爪时缺省；越界读数保留并以 `within_defined_range=false` 标出 |

没有 `valid` 字段：非有限或不完整的读数不进入 `state_trace`（录制失败并另存原始读数），轨迹里的每一条都已通过校验。

### contacts

每个 state 一条，`payload.contacts[k] == state_trace[k].contacts`。

| 字段 | 说明 |
| --- | --- |
| `control_step` | 等于所属 state 的步号 |
| `sampling` | 固定 `cycle_end_snapshot`；不宣称子步完备 |
| `availability` | `available` / `unavailable` |
| `rows` | 可用时 `[{a, b, body_a, body_b, dist_m}]`（`a`/`b` 是 node_id 或 null，USD body 标签加 `usd:` 前缀）；不可用时必须是 `null`，`[]` 才表示查询有效且零接触 |
| `coverage` | `unknown` / `complete` / `truncated` |
| `force_availability` / `normal_availability` / `reason_code` | 可选；力和法向未支持时写 `unavailable`，不补 0 |

### frame_map

三路物理相机（`camera.head`、`camera.left_wrist`、`camera.right_wrist`）的画面由 A 线渲染器从 `state_trace` 生成，B 在 headless Newton 下只录状态。10 fps = 每 12 个控制步一帧。

```json
{"camera_id": "camera.head", "variant_id": "base", "frame_index": 0, "control_step": 0,
 "intrinsics": {"kind": "camera.intrinsics", "sha256": "…", "media_type": "application/json"},
 "extrinsics": {"kind": "camera.extrinsics", "sha256": "…", "media_type": "application/json"},
 "image": {"kind": "image.mp4", "sha256": "…", "media_type": "video/mp4"}}
```

没有相机时 `frame_map` 为 `[]`，不得据此签收三视角数据。

### replay

| 字段 | 说明 |
| --- | --- |
| `state_playback` | `verified` / `failed` / `unverified`，A 线状态回放（渲染用），只证明位姿与图像同步 |
| `action_replay` | `verified` / `failed` / `unverified`，B 线动作重放，重新施加 `applied` 输入 |
| `action_replay_report` | ArtifactRef，含逐实体最大位置误差、TCP 误差、旋转/关节/接触时序差异 |
| `tolerance_m` | 从 TaskContract 抄来的 0.01，不在这里重新定义 |

缺步、未比较实体、不同资产或只比末帧都不能判定 `verified`。超限时保留原始轨迹和重放轨迹，不改原动作过门。

## 与旧版 IR 的差别

- 不再有 `episode/1` 那种 `observations/*.mp4` + `actions.npy` 的平铺布局；流位置由 ArtifactRef 记录，HDF5 导出（B 线）另行打包。
- 不再有 10 Hz 动作；动作和状态是 120 Hz 控制步，图像是派生流。
- `admission` 不在 episode 里：准入是独立的 `datafarm.gate-receipt`，以本 episode 的 `document_sha256` 作 `subject_sha256`。

## 示例（节选）

```json
{
  "schema": "datafarm.episode-ir",
  "schema_version": "1.0.0",
  "artifact_id": "episode.ep_0007",
  "created_at": "2026-11-15T03:00:00Z",
  "producer": "b.episode-recorder",
  "predecessors": [
    {"kind": "task-ir", "sha256": "…", "media_type": "application/json"},
    {"kind": "ir.scene-graph", "sha256": "…", "media_type": "application/json"}
  ],
  "evidence": [
    {"kind": "report.action-replay", "sha256": "…", "media_type": "application/json"}
  ],
  "payload": {
    "setup": {
      "scene_graph": {"kind": "ir.scene-graph", "sha256": "…", "media_type": "application/json"},
      "task_contract": {"kind": "task-ir", "sha256": "…", "media_type": "application/json"},
      "expert": {"kind": "source.expert", "sha256": "…", "media_type": "text/x-python"},
      "judge": {"kind": "source.judge", "sha256": "…", "media_type": "text/x-python"},
      "runtime_profile": {"kind": "sim.runtime-profile", "sha256": "…", "media_type": "application/json"},
      "scene_bundle": {"kind": "scene.bundle", "sha256": "…", "media_type": "application/json"},
      "backend": {"name": "isaaclab", "version": "3.0.0", "physics": "newton_mjwarp"},
      "seed": 1003,
      "seed_partition": "holdout",
      "sampled_randomization": {"mass": 0.31, "friction_static": 0.72, "drawer_damping": 0.11, "plate_yaw_deg": 37.0},
      "initial_state": {"control_step": 0, "q": [0.0, 0.02]},
      "control_dt_s": 0.008333333333333333,
      "solver_substeps": 4,
      "joint_names": ["left_joint1", "left_g2_joint"],
      "joint_types": ["revolute", "prismatic"],
      "control_names": ["left_joint1", "left_g2_joint"],
      "judge_result": {"outcome": "success", "success": true, "terminated": true, "truncated": false,
                       "reason_code": "GOAL_REACHED",
                       "criteria": {"plate_inside_drawer": true, "plate_at_rest": true},
                       "evidence_steps": [180, 192],
                       "judge_artifact": {"kind": "source.judge", "sha256": "…", "media_type": "text/x-python"}}
    },
    "actions": [
      {"control_step": 0, "requested_joint_targets": {"left_joint1": 0.0, "left_g2_joint": 0.02},
       "applied_joint_targets": {"left_joint1": 0.0, "left_g2_joint": 0.02}, "application": "actuator_readback",
       "command": {"call_id": "call.0003", "primitive": "tcp"}}
    ],
    "state_trace": [
      {"control_step": 0, "time_s": 0.0, "units": {"length": "m", "angle": "rad", "time": "s"}, "frame_id": "world",
       "entities": {
         "object.plate": {"availability": "available", "asset_id": "object.plate_raised_rim",
                          "position_m": [0.5, -0.25, 0.78], "quaternion_xyzw": [0, 0, 0, 1],
                          "linear_velocity_m_s": [0, 0, 0], "angular_velocity_rad_s": [0, 0, 0],
                          "joints": [], "links": {"plate": {"position_m": [0.5, -0.25, 0.78], "quaternion_xyzw": [0, 0, 0, 1]}}},
         "object.cabinet": {"availability": "available", "asset_id": "object.cabinet_drawer",
                            "position_m": [0.8, 0.0, 0.0], "quaternion_xyzw": [0, 0, 0, 1],
                            "linear_velocity_m_s": [0, 0, 0], "angular_velocity_rad_s": [0, 0, 0],
                            "joints": [{"name": "joint.drawer.slide", "type": "prismatic", "unit": "m", "q": 0.42, "dq": 0.0}],
                            "links": {}}},
       "robot": {"joint_names": ["left_joint1", "left_g2_joint"], "q": [0.0, 0.02], "dq": [0.0, 0.0],
                 "tcp": {"frame_id": "world", "position_m": [0.5, 0.0, 0.8], "quaternion_xyzw": [0, 0, 0, 1]},
                 "gripper": {"width_m": 0.06, "width_range_m": [0.0, 0.08], "state_source": "measured_finger_joints",
                             "verification": "model_geometry_only", "within_defined_range": true}},
       "contacts": {"control_step": 0, "sampling": "cycle_end_snapshot", "availability": "available",
                    "rows": [], "coverage": "unknown"}}
    ],
    "contacts": [
      {"control_step": 0, "sampling": "cycle_end_snapshot", "availability": "available", "rows": [], "coverage": "unknown"}
    ],
    "events": [
      {"control_step": 0, "event": "primitive_started", "call_id": "call.0003", "api": "move_tcp"}
    ],
    "frame_map": [
      {"camera_id": "camera.head", "variant_id": "base", "frame_index": 0, "control_step": 0,
       "intrinsics": {"kind": "camera.intrinsics", "sha256": "…", "media_type": "application/json"},
       "extrinsics": {"kind": "camera.extrinsics", "sha256": "…", "media_type": "application/json"},
       "image": {"kind": "image.mp4", "sha256": "…", "media_type": "video/mp4"}}
    ],
    "replay": {"state_playback": "verified", "action_replay": "verified",
               "action_replay_report": {"kind": "report.action-replay", "sha256": "…", "media_type": "application/json"},
               "tolerance_m": 0.01}
  }
}
```

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
| `actions` | 逐控制周期的 `requested` / `applied` 目标、`interval_steps`、控制模式、原语 `call_id` |
| `state_trace` | N+1 个整场快照：每个实体的根位姿、链接与关节，机器人关节段，接触快照 |
| `contacts` | `availability` + 接触记录 |
| `events` | 原语开始/结束、阶段变化、错误、judge、停止与恢复 |
| `frame_map` | 每帧的 `camera_id`、`variant_id`、`control_step`、内外参引用、图像 ArtifactRef |
| `replay` | 状态回放与动作回放的状态、误差报告 ArtifactRef |

### setup

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `scene_graph` / `task_contract` / `expert` / `judge` / `runtime_profile` / `scene_bundle` | 是 | 全部 ArtifactRef |
| `backend` | 是 | `{name: "isaaclab", version: "3.0.0", physics: "newton_mjwarp"}` |
| `seed` / `seed_partition` | 是 | 实际 seed 与所属分区（`dev` / `holdout`） |
| `sampled_randomization` | 是 | 本次实际采样的物理量与位姿抖动值 |
| `initial_state` | 是 | **测量值**：`check` 静置后的 `s[0]` 完整快照摘要（关节、TCP、实体位姿），重放从这里恢复 |
| `control_dt_s` / `solver_substeps` | 是 | 1/120 与 4 |
| `node_ids` | 是 | 本 episode 覆盖的场景节点；`state_trace[k].entities` 必须与之逐一对应 |
| `judge_result` | 是 | `{outcome: success/failure/error/aborted, success: bool/null, terminated, truncated, reason_code, criteria: {criterion_id: bool}, evidence_steps, judge_artifact}` |

布局/SceneGraph 里的 `joint_state` 只是**声明意图**，不是物理事实。物理闸门（`tools/scene_assemble.py check`）写入意图、静置后读出测量值并导出；`setup.initial_state` 和 `state_trace` 里的关节值都取测量值，episode 从测量态开始重放。被动（非驱动）关节在静置中可以动：只要测量值仍在关节限位内、且该节点没有新增接触对，就允许并记为测量初始态；被驱动或保持的关节仍受漂移上限约束。

### actions

每个控制周期一条记录，`requested` 与 `applied` 都要写；只存 `move_tcp` 参数不算记录。

```json
{"call_id": "call.0003", "interval_steps": [0, 1], "mode": "joint_position_target",
 "control_names": ["left_joint1", "left_g2_joint"], "requested": [0.0, 0.02], "applied": [0.0, 0.02]}
```

MVP 只有 `joint_position_target`。N 条动作必须对应 N+1 个状态；`mode` 出现新值必须先改 schema。

### state_trace

每个 `state_trace[k]` 是第 k 个控制周期末的整场快照。字段集合由 `src/datafarm/ir/episode_state.py` 固定，多一个少一个都拒绝；消费方按字段名读取，不按位置。

| 字段 | 说明 |
| --- | --- |
| `control_step` / `time_s` | 步号与时间；`time_s == control_step * control_dt_s` |
| `units` / `frame_id` | 固定 `{length: m, angle: rad, time: s}` 与 `world` |
| `entities` | `{node_id: 实体快照}`，必须覆盖场景全部 `node_id` |
| `robot` | 机器人观测段，见下 |
| `contacts` | 本周期末的接触快照，见下 |

实体快照：

| 字段 | 说明 |
| --- | --- |
| `availability` | 只允许 `available`；不可用的实体不进 episode |
| `asset_id` | 对应 SceneGraph 的 `asset_id` |
| `position_m` / `quaternion_xyzw` | **实体根位姿**，世界系，米 / 单位四元数 xyzw |
| `linear_velocity_m_s` / `angular_velocity_rad_s` | 根速度，世界系 |
| `joints` | `[{name, type, unit, q, dq}]`；`revolute` 配 `rad`、`prismatic` 配 `m`，同一实体内名字不能重复 |
| `links` | `{link_name: {position_m, quaternion_xyzw}}`，各刚体（或 articulation 的 body）的世界位姿 |

`links` 是刚体原点，**不是**实体根位姿：桌子的实测刚体原点可能比场景根低近 0.8 m，回放只拿 `position_m` 当根位姿，把 `links` 当根位姿会把整张桌子搬走。

机器人段：

| 字段 | 说明 |
| --- | --- |
| `joint_names` / `q` / `dq` | 同一顺序的观测关节；名字唯一、长度一致 |
| `tcp` | 可选，`{frame_id: world, position_m, quaternion_xyzw}` |
| `gripper` | 可选，`{width_m, width_range_m, state_source, verification, within_defined_range}`；开度由实测手指关节反算，不是名义值 |

关节名只在一台实体（或机器人）内唯一：两个实体可以有同名关节。消费方按 `node_id + joint_name` 绑定，不能用一个全局扁平字典。

### contacts

| 字段 | 说明 |
| --- | --- |
| `availability` | `available` / `unavailable`；`unavailable` 时 `rows` 必须是 `null`，不能拿空列表冒充零接触 |
| `rows` | `[{a, b, body_a, body_b, dist_m}]`；`a`/`b` 是 node_id 或 `null`（world/ground），`body_a`/`body_b` 是 `usd:/World/...` 形式的场景内标识，不是磁盘路径 |
| `coverage` | `unknown` / `complete` / `truncated`；`complete` 只在求解器计数 `nacon <= naconmax` 时成立，不宣称子步完备 |
| `sampling` / `control_step` | 固定 `cycle_end_snapshot`，且等于同一状态的 `control_step` |
| `force_availability` / `normal_availability` | 只允许 `unavailable`：本线不测力与法向 |
| `reason_code` | 可选，接触不可用时的原因码 |

### frame_map

单臂 MVP 的两路物理相机（`camera.head`、`camera.left_wrist`，见 [SceneGraph](scene-graph.md)）和虚拟视角的画面由 A 线渲染器从 `state_trace` 生成，B 在 headless Newton 下只录状态。10 fps = 每 12 个控制步一帧。

```json
{"camera_id": "camera.head", "variant_id": "base", "frame_index": 0, "control_step": 0,
 "intrinsics": {"kind": "camera.intrinsics", "sha256": "…", "media_type": "application/json"},
 "extrinsics": {"kind": "camera.extrinsics", "sha256": "…", "media_type": "application/json"},
 "image": {"kind": "image.mp4", "sha256": "…", "media_type": "video/mp4"}}
```

没有相机时 `frame_map` 为 `[]`，不得据此签收多视角数据。

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
      "node_ids": ["object.table", "object.plate", "robot.airbot"],
      "judge_result": {"outcome": "success", "success": true, "terminated": true, "truncated": false,
                       "reason_code": "GOAL_REACHED",
                       "criteria": {"plate_inside_drawer": true, "plate_at_rest": true},
                       "evidence_steps": [180, 192],
                       "judge_artifact": {"kind": "source.judge", "sha256": "…", "media_type": "text/x-python"}}
    },
    "actions": [
      {"call_id": "call.0003", "interval_steps": [0, 1], "mode": "joint_position_target",
       "control_names": ["left_joint1", "left_g2_joint"], "requested": [0.0, 0.02], "applied": [0.0, 0.02]}
    ],
    "state_trace": [
      {"control_step": 0, "time_s": 0.0, "units": {"length": "m", "angle": "rad", "time": "s"},
       "frame_id": "world",
       "entities": {"object.plate": {"availability": "available", "asset_id": "object.plate",
                                     "position_m": [0.5, -0.25, 0.78], "quaternion_xyzw": [0, 0, 0, 1],
                                     "linear_velocity_m_s": [0, 0, 0], "angular_velocity_rad_s": [0, 0, 0],
                                     "joints": [],
                                     "links": {"body": {"position_m": [0.5, -0.25, 0.78],
                                                        "quaternion_xyzw": [0, 0, 0, 1]}}}},
       "robot": {"joint_names": ["left_joint1", "left_g2_joint"], "q": [0.0, 0.02], "dq": [0.0, 0.0],
                 "tcp": {"frame_id": "world", "position_m": [0.5, 0.0, 0.8], "quaternion_xyzw": [0, 0, 0, 1]}},
       "contacts": {"availability": "available", "rows": [], "coverage": "complete",
                    "sampling": "cycle_end_snapshot", "control_step": 0,
                    "force_availability": "unavailable", "normal_availability": "unavailable"}}
    ],
    "contacts": {"availability": "available", "sampling": "cycle_end_snapshot", "records": []},
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

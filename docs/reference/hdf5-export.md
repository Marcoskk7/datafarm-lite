# HDF5 export `datafarm.episode-hdf5/1`

> **临时补丁（tmp(B)），待 B 线同学确认。** 仓库此前没有 HDF5 导出器，集成链
> （request → scene → expert → recorded Episode → judge → render → HDF5 → admission）卡在 HDF5 这一步，
> 所以先按 [B 线接口 §8](b-control-interfaces.md) 的最小约定落一个只打包、不做任何数值处理的导出器。
> 它是过渡实现，B 线接管数据集包后应当删除或迁移，而不是并行保留两个入口。

实现：`src/datafarm/expert/hdf5_export.py`（`export_episode` / `read_episode` / `validate_hdf5`）与
`tools/export_episode_hdf5.py`（CLI）。输入是本仓库录制器的 `measured-scene-episode/1` payload，
导出前先过 `datafarm.expert.episode.validate_episode`。

## 布局

一个 episode 一个文件，`/meta` 一次写清身份与控制约定：

```text
/meta                               attrs: schema="datafarm.episode-hdf5/1", profile, episode_id,
                                    task_id, task_revision, attempt_id, control_dt_s, control_mode,
                                    real_ready=false, frames, robot_node_id, joint_types_source,
                                    steps_states, steps_actions, source_episode_sha256,
                                    judge_result_json, termination_json, provenance_json
                                    datasets: joint_names[J], joint_types[J], control_names[U]  UTF-8
/state/control_step                 [N+1] int64，从 0 连续
/state/robots/<robot_id>/q          [N+1,J] float64   （robot_id 见下）
/state/robots/<robot_id>/dq         [N+1,J] float64
/state/robots/<robot_id>/tcp_pose_xyzw    [N+1,7] float64，x,y,z,qx,qy,qz,qw（状态里有 tcp 才写）
/state/robots/<robot_id>/gripper_width_m  [N+1,1] float64（状态里有 gripper 才写）
/state/entities/<node_id>/pose_xyzw       [N+1,7] float64，x,y,z,qx,qy,qz,qw
/state/entities/<node_id>/linear_velocity_m_s   [N+1,3] float64
/state/entities/<node_id>/angular_velocity_rad_s [N+1,3] float64
/state/entities/<node_id>/joints/<joint>/q,dq    [N+1,1] float64；组属性 type/unit
/state/entities/<node_id>           attr: asset_id
/action/interval_steps              [N,2] int64，第 k 行 [k, k+1]
/action/robots/<robot_id>/requested [N,U] float64，按 control_names 顺序；未请求的位置写 0.0，不可解读
/action/robots/<robot_id>/requested_mask [N,U] bool，该周期是否请求了该通道
/action/robots/<robot_id>/applied   [N,U] float64，按 control_names 顺序
/action/call_id                     [N] UTF-8，`command.call_id`，缺省为 ""
/contacts/records                   [N+1] UTF-8 JSON（与 state_trace[k].contacts 逐字节一致）
/events/records                     [E] UTF-8 JSON
/frames/<camera_id>/<variant_id>/control_step      [F] int64
/frames/<camera_id>/<variant_id>/frame_index       [F] int64
/frames/<camera_id>/<variant_id>/status            [F] UTF-8（ok/missing/error）
/frames/<camera_id>/<variant_id>/video_ref_json    [F] UTF-8 ArtifactRef JSON 或 "null"
/frames/<camera_id>/<variant_id>/intrinsics_ref_json [F]、.../extrinsics_ref_json [F]
/frames                             attrs: render_spec_json, state_playback_json
```

- `robot_id`：关节名与 `state_trace[k].robot.joint_names` 完全一致的那个 entity 节点；没有这种节点时用
  `robot`。实际取值记在 `meta.robot_node_id`，也可由 `provenance.robot_node_id` 显式指定。
- `joint_types`：`robot` 段没有类型字段。优先 `provenance.robot_joint_types`（`{joint: type}` 或与
  `joint_names` 等长的列表），其次同名 entity 的关节类型；两者都没有时逐关节写 `unknown`，
  `meta.joint_types_source` 分别记 `provenance` / `state_trace_entity` / `unknown`。
- `control_names`：全部 action 的 `applied_joint_targets` 键的**排序**列表。逐行通道集合必须一致，
  applied 缺通道直接失败，绝不补值。`requested_joint_targets` 只含该周期实际下发的通道（lab30 实录：
  `move_tcp` 只请求 6 个手臂关节，`move_gripper` 只请求夹爪主关节），必须是 applied 通道的子集；
  未请求的位置由 `requested_mask=false` 标出，`requested` 对应值无意义。
- `J ≠ U` 是常态：J 是观测关节数，U 是实际控制通道数，两者不互相推导。
- 数组是录制值的 float64/int64 原样拷贝：不重采样、不取整、不做插值；N 条动作对应 N+1 个状态。
- `contacts.rows` 的 `null`（不可用）与 `[]`（可用且零接触）在 JSON 记录里原样保留，不互相替换。
- `/meta` 的 JSON 字段（judge/termination/provenance）用 kernel 规范 JSON（键序稳定、拒绝非有限浮点与宿主路径）。
  `judge_result_json` 在没有 Judge 结果时是 JSON 字符串 `"unverified"`；`real_ready` 恒为 `false`，
  仿真产出不是 real-ready 数据。
- 没给渲染清单时 `/frames` 不存在，`meta.frames="not_rendered"`；给了清单时 `meta.frames="rendered"`，
  `/frames` 只存引用（ArtifactRef），不存像素。

## CLI

```bash
python tools/export_episode_hdf5.py export --store WORK/store \
    --episode-ref '{"episode_ref": {"kind": "episode-ir", "sha256": "…", "media_type": "application/json"}}' \
    --out WORK/episode.h5 [--judge verdict.json] [--render-manifest-ref '{"kind": "render-manifest", …}']

python tools/export_episode_hdf5.py validate --file WORK/episode.h5 [--store WORK/store --episode-ref …]
```

两个子命令都打印 JSON；`export` 退出码 0 表示写完，`validate` 退出码 0 只在 problems 为空时。
`--store` 与 `--episode-ref` 成对给出时，校验会逐数组与 payload 比对（float64 逐位相等，不是近似比较）。

## 已知限制

- 只支持 `joint_position_target` 控制模式；其它模式必须先在 §6/§8 定义导出约定。
- 一个文件一个 episode。§8 允许逻辑组跨多个文件，多成员 manifest 与 schema 版本演进属于 B 线的数据集包，
  本模块不实现；接入前请以 B 线确认的布局为准。
- 导出器不判定任务成功、不签收数据：它保留 `termination` 与 judge 输入，不产生准入结论。

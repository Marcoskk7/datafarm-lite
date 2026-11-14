# TaskContract — `datafarm.task-ir`

把任务、判定器、种子和验收规则绑成一个合同。整份是 kernel `ArtifactDocument`：`payload` 的六个核心键就是 DataFarm Task IR（profile `stage-graph/1`，校验器 `src/datafarm/expert/task_ir.py::validate_stage_graph`），本 MVP 再补五个 A/B 约定用的键。

## 信封

| 字段 | 值 |
| --- | --- |
| `schema` | `datafarm.task-ir` |
| `schema_version` | `1.0.0` |
| `artifact_id` | `task.plate_into_drawer` |
| `producer` | `b.task-contract-author` |
| `predecessors` | SceneGraph、AssetRecord 的 ArtifactRef |
| `evidence` | 判定器源码、负向对照记录、阈值依据 |

## payload

### 核心节（必需，含义见 `task_ir.py` 模块文档）

| 键 | 说明 |
| --- | --- |
| `demand` | 任务要求原文 |
| `roles` | `{role: {binding, kind}}`，binding 用第 4 节点分标识 |
| `stage_graph` | `{stages: [...], sync: [...]}`；`stage.arm` 取 `left` / `right` / `both` / `either`，MVP 用 `left` |
| `predicates` | `{id: {kind, expr, tolerance, judge_criterion}}`；`terminal` / `transient` 必须写 `judge_criterion` |
| `forbidden_shortcuts` | 禁止的取巧，如"直接写位姿把盘子放进抽屉" |
| `skill_evidence` | `[{skill, evidence}]` |

### 扩展节（本 MVP 新增，可选）

| 键 | 说明 |
| --- | --- |
| `judge` | `{artifact: ArtifactRef, criteria: [criterion_id]}`；`criteria` 必须与 Task IR 里 terminal / transient 谓词的 `judge_criterion` 一一对应（`validate_stage_graph(judge_criteria=...)` 会检查） |
| `seeds` | `{dev: [start, end], holdout: [start, end]}`，闭区间，两段不重叠 |
| `protocol` | `{episodes, holdout_pass_ratio, max_attempts_per_seed}`；MVP 为 20、0.8、3 |
| `randomization` | `[{quantity, spec}]`，从 AssetRecord 账本里 `assumed` / `unknown` 的量聚合 |
| `replay` | `{tolerance_m, entities: [node_id]}`；MVP 容差 0.01，实体写清哪些参与签收 |

`judge` 在专家开发前冻结，之后任何判据、阈值、场景物理配置的变化都产生新 `artifact_id`，不原地改。

## 示例

```json
{
  "schema": "datafarm.task-ir",
  "schema_version": "1.0.0",
  "artifact_id": "task.plate_into_drawer",
  "created_at": "2026-11-15T02:00:00Z",
  "producer": "b.task-contract-author",
  "predecessors": [
    {"kind": "ir.scene-graph", "sha256": "…", "media_type": "application/json"}
  ],
  "evidence": [
    {"kind": "source.judge", "sha256": "…", "media_type": "text/x-python"}
  ],
  "payload": {
    "demand": "put the plate into the open drawer",
    "roles": {
      "task.object": {"binding": "object.plate_raised_rim", "kind": "task_object"},
      "task.receptacle": {"binding": "object.cabinet_drawer", "kind": "receptacle"},
      "task.robot": {"binding": "robot.airbot", "kind": "robot"}
    },
    "stage_graph": {
      "stages": [
        {"id": "approach", "skill": "move_tcp", "arm": "left", "role": "task.object",
         "frame": "tcp.left", "exit": ["eef_near_plate"], "guard": [], "recovery": "home"},
        {"id": "grasp", "skill": "set_gripper", "arm": "left", "role": "task.object",
         "frame": "tcp.left", "exit": ["gripper_closed_on_rim"], "guard": [], "recovery": "open"},
        {"id": "transport", "skill": "move_tcp", "arm": "left", "role": "task.receptacle",
         "frame": "tcp.left", "exit": ["plate_over_drawer"], "guard": ["no_forbidden_contact"], "recovery": "retreat"},
        {"id": "release", "skill": "set_gripper", "arm": "left", "role": "task.receptacle",
         "frame": "tcp.left", "exit": ["gripper_open"], "guard": [], "recovery": "retreat"}
      ],
      "sync": []
    },
    "predicates": {
      "eef_near_plate": {"kind": "exit", "expr": "distance(tcp.left, object.plate) < 0.03", "tolerance": 0.03},
      "gripper_closed_on_rim": {"kind": "exit", "expr": "gripper width stops changing while plate is lifted", "tolerance": 0.002},
      "plate_over_drawer": {"kind": "exit", "expr": "plate center xy inside drawer inner bounds", "tolerance": 0.005},
      "gripper_open": {"kind": "exit", "expr": "gripper width back to the released opening", "tolerance": 0.002},
      "plate_inside_drawer": {"kind": "terminal", "expr": "whole plate geometry inside drawer inner volume", "tolerance": 0.005, "judge_criterion": "plate_inside_drawer"},
      "plate_at_rest": {"kind": "terminal", "expr": "plate speed below threshold for the hold window", "tolerance": 0.01, "judge_criterion": "plate_at_rest"},
      "no_forbidden_contact": {"kind": "guard", "expr": "no contact outside collision_groups.task_contact", "tolerance": 0}
    },
    "forbidden_shortcuts": ["set the plate pose directly", "open the drawer by script during the episode"],
    "skill_evidence": [{"skill": "move_tcp", "evidence": "b-control interfaces §4.2"}],
    "judge": {"artifact": {"kind": "source.judge", "sha256": "…", "media_type": "text/x-python"},
              "criteria": ["plate_inside_drawer", "plate_at_rest"]},
    "seeds": {"dev": [0, 99], "holdout": [1000, 1019]},
    "protocol": {"episodes": 20, "holdout_pass_ratio": 0.8, "max_attempts_per_seed": 3},
    "randomization": [
      {"quantity": "plate_pose", "spec": {"position_jitter_m": 0.05, "yaw_range_deg": [-180, 180]}},
      {"quantity": "mass", "spec": {"low": 0.2, "high": 0.5}},
      {"quantity": "friction_static", "spec": {"low": 0.3, "high": 1.0}},
      {"quantity": "drawer_damping", "spec": {"low": 0.02, "high": 0.3}}
    ],
    "replay": {"tolerance_m": 0.01, "entities": ["object.plate", "object.cabinet"]}
  }
}
```

## 与 B 线的对应

| 我们的概念 | Task IR / B 线字段 |
| --- | --- |
| 任务描述 | `demand` |
| 物体 / 机器人角色 | `roles` |
| 双臂分工、阶段顺序 | `stage_graph.stages`（`skill`、`arm`、`exit`、`guard`、`recovery`） |
| 判定条件 | `predicates`（terminal / transient 必须写 `judge_criterion`） |
| 禁止的取巧 | `forbidden_shortcuts` |
| 可用技能与证据 | `skill_evidence` |
| 阈值来源 | `predicates[*].tolerance`，具体依据写在 `evidence` 里，不写进阈值字符串 |

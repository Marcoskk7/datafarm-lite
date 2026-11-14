# h3.task-bundle/1 — H3 control-hybrid 渲染器的每任务输入 bundle

每个任务一个文件：`<run>/tasks/<task>/bundle.json`（参考变体为 `bundle-<variant>.json`）。bundle 是 agent 为当前任务派生的 TASK ARTIFACT（第二层）；它被 `h3_bundle.py`（契约编译 + 检查）、H3 包构建器、evaluator 与 review packer 消费。所有路径都是绝对的部署路径。bundle 是只读输入；绝不要复制大型媒体，用路径引用它们（sha256 由工具计算）。

渲染模式与任何 OOD 轴并非由 harness 提供。`run_id` 与 `mode_confirmations` 记录当前用户作用域内的指令（逐字引述 + 给出它的位置）；`h3_bundle.py` 将它们复制进渲染意图，并在缺失时停止。来自另一次 run 或另一位用户的引述不是本次的授权。

```json
{
  "schema": "h3.task-bundle/1",
  "task": "my-task",                         // short id, also the directory name
  "run_id": "RUN-...",                       // the run that owns this render (required)
  "task_id": "my-task-h3-control-hybrid",    // optional; default "<task>-h3-control-hybrid"
  "mode_confirmations": {                    // required; only the modes the current user confirmed
    "sim2real": {"quote": "<user words>", "source": "<where/when the user said it>"},
    "ood": {"quote": "<user words>", "source": "...",
            "authorized_ood_axes": ["<axis 1 exactly as authorized>", "..."]}
  },
  "layout": "t3",                            // carrier layout: 1280x1080, head (0,0,1280,720),
                                             //   left_wrist (0,720,640,360), right_wrist (640,720,640,360)
  "carrier_fps": 24,                         // native fps of the carrier files below (24 or 30)
  "appearance_views": ["head", "left_wrist", "right_wrist"],
  "episodes": [EPISODE, ...],
  "real_references": [REFERENCE, ...],       // default real refs (>=1 full-view full_scene per view)
  "per_episode_references": {"<episode id>": [REFERENCE, ...]},   // optional override
  "overlay": {                                // exactly these 5 slots (render-policy VARIABLES)
    "TASK_DESCRIPTION": "...", "SOURCE_VIDEO_ROLE": "...", "CAMERA_TIMING_CONTRACT": "...",
    "TASK_EVENT_CONTRACT": "...", "CONTINUITY_CONTRACT": "..."
  },
  "real_baseline": "one paragraph describing the real scene appearance",
  "appearance_invariants": ["...", "..."],
  "appearance_requirements": {               // exactly these 5 areas; robot_body,
    "robot_body": {"required": true, "description": "..."},          // table_and_background and
    "attached_cameras_and_cables": {"required": true|false, "description": "..."},  // photographic_character
    "task_objects": {"required": true|false, "description": "..."},   // must be required=true
    "table_and_background": {"required": true, "description": "..."},
    "photographic_character": {"required": true, "description": "..."}
  },
  "ood_variants": [                          // >= 2 when ood is confirmed; only the authorized axes may change
    {"name": "slate-laminate-cool", "tabletop": "matte slate-gray laminate", "lighting": "cool 6500K daylight"}
  ],
  "realpool": {"sources": [                  // real camera frames for realism metrics, per view
    {"video": "/abs/real/cam_high.mp4", "view": "head", "stride": 15},
    {"image": "/abs/real-tlayout.png", "view": "head", "region": [0, 0, 1280, 720]}
  ]},
  "notes": "anything the renderer/evaluator must know (e.g. render window, padding, fps relabel)"
}
```

EPISODE：
```json
{"id": "episode-000",
 "carrier": "/abs/white-authority-3view-<fps>fps.mp4",   // canonical 1280x1080 T-layout white video, >= 20 fps
 "frame_range": [100, 900] | null,        // half-open window of carrier frames to render (null = all)
 "wide_wrist": {"factor": 0.65,           // optional: the carrier's wrist views are a wide-FOV white replay;
                "principal_points": {"left": [cx, cy], "right": [cx, cy]}},  // 1280x720 wrist pixels; cropped back
 "keep_regions": "/abs/keep.json",        // optional: h3.keep-regions/1 spec (h3_keep_regions.py)
 "notes": "..."}
```

REFERENCE（render-policy 真实参考；sha256 由工具添加）：
```json
{"path": "/abs/real-3view.png",
 "provenance": {"kind": "real-episode-frame" | "real-photo", "source": "/abs/real/source.mp4 or image",
                "frame": 240, "description": "real episode X frame 240, cam_high|left|right tiled"},
 "scope": "full-view" | "local-detail",
 "views": ["head", "left_wrist", "right_wrist"],
 "view_regions": {"head": [0, 0, 1280, 720], "left_wrist": [0, 720, 640, 360], "right_wrist": [640, 720, 640, 360]},
 "coverage": ["full_scene", "robot_body", "attached_cameras_and_cables", "task_objects",
              "table_and_background", "photographic_character"]}
```

规则
- 真实参考必须是真实的相机帧（绝不是白模、仿真或生成帧）。每个声明的视角都需要一个具有 `full_scene` 覆盖的全视角参考；所有必需区域都必须被覆盖。
- overlay 文本以 provider 中立的方式陈述物体身份/数量、阶段、事件与终止关系；它不提及 provider、IMAGE 编号或 chunk 窗口。CAMERA_TIMING_CONTRACT 描述 T 布局（头部在上、左腕左下、右腕右下）、24 fps 播放，以及输出帧 i 对应源帧 i。
- 用以下命令检查：`python src/datafarm/render/h3/h3_bundle.py check <run>/tasks/<task>/bundle.json --run-dir <run>`（必须不打印任何问题）。
- 默认配方的参考策略（configs/render/h3-control-hybrid-v1.json、docs/reference/rendering.md）：一张在渲染开始时任务状态与 carrier 匹配的真实场景帧 + 一张保留真实机械臂连杆的真实机器人图；物体特写图仅用于一直留在视野中的物体（被收起的物体则不需要）。`h3_episode_references.py` 在 25 MP 内存预算内派生生产集（起始帧、机器人可见帧、机器人 / 物体 / 腕部图）；`h3_clean_references.py` 派生无物体的底板与无线缆的图。
- `h3_bundle.py check` 在 carrier 低于 20 fps 或 carrier 帧率与 `carrier_fps` 不一致时失败，校验 `wide_wrist` 与 `keep_regions`，并在头部视角在 control gate 之上没有墙/桌接缝时告警。

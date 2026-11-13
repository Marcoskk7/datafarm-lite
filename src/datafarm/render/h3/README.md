# H3 control hybrid harness

pipeline 的生产 H3 渲染路线：MiniMax-H3 Ref2VA + H3-Fun ControlNet-Union（soft-HED）+ white inpaint、
独立的 192 帧 chunk、Sol-H3 turbo 4 步 LoRA、8 个 seed + 每 chunk 的 VLM seed 筛选、min-diff 接缝、
首 chunk 曝光增益锁定。流程、理由、证据和限制：[docs/reference/rendering.md](../../../../docs/reference/rendering.md)。
默认 recipe：`configs/render/h3-control-hybrid-v1.json`；OOD：`configs/render/h3-ood-restyle-v1.json`；
评估器：`evaluations/render-h3/`；VideoX-Fun 改动：`vendor/videox_fun/`。

这些工具用 `H3_PYTHON` 按路径运行，该解释器不必安装本包；`h3_paths.py` 负责定位 checkout。

| 步骤 | 工具 |
|---|---|
| 白模 attempt 的仅渲染回放（机器人颜色、形状、着装、dense rate、宽腕） | `python -m datafarm.simulation.white_replay` |
| 组合并检查 T 形布局的白色 carrier | `h3_carrier.py` |
| 来自任务 episode 的真实参考（生产集、内存预算、机器人颜色） | `h3_episode_references.py` |
| 来自任务运动学的投影机器人掩码（浅色机器人、深色家具） | `h3_robot_masks.py` |
| 为某个任务状态保留真实像素（在此验证，由 builder 应用） | `h3_keep_regions.py` |
| 验证一个 bundle，编译其契约 | `h3_bundle.py` |
| 从 episode 清理真实参考（可选） | `h3_clean_references.py` |
| 构建一个 package | `h3_package.py`（通常经由 `h3_enqueue.py`） |
| 将 package 或 chunk 入队 | `h3_enqueue.py` |
| 渲染 | `launch_control_worker.sh` -> `h3_control_worker.py`；用 `h3_pool.py` 维持常驻 + 弹性 worker |
| 把死亡 worker 的 ticket 退回队列 | `h3_requeue_orphans.py` |
| seed 筛选合成图、合并答案 | `h3_screen_prep.py` |
| seed 选择（VLM 筛选，或以占用率筛选作为回退）、组装、曝光锁定、宽腕裁剪还原、评分 | `h3_finalize.py`（`h3_select.py`、`h3_assemble.py`、`h3_exposure_lock.py`、`h3_wrist_crop.py`） |
| 生产 gate：2 个 prefix，然后 canary，然后其余（`h3_enqueue.py --gate`） | `h3_gate.py` |
| 评分并写出 review pack | `h3_score_and_pack.py` |
| 盲审作业 | `h3_review_prep.py` |
| 对已接受渲染做 OOD 编辑 | `h3_sam2_table_masks.py`、`h3_ood_restyle.py` |

部署绑定（缺失时 fail closed；`h3_paths.py`）：

| 值 | 来源 |
|---|---|
| 运行目录（tasks/、recipes/、pkgs/、queue/、cache/、screen/、reviews/、eval/realpool/） | `--run-dir` 或环境变量 `H3_RUN` |
| `H3_PYTHON` | 带 torch/diffusers/VideoX-Fun 依赖的解释器；默认：当前解释器 |
| `H3_MODELS` | 包含 `MiniMax-H3/Ref2VA` 和 `MiniMax-H3-Fun-Controlnet-Union/` |
| `H3_VIDEOX_FUN` | 已应用 `vendor/videox_fun/videox-fun-datafarm.patch` 的 VideoX-Fun checkout（pipeline、HED 工具、`models/annotators/ControlNetHED.pth`） |
| `H3_TURBO_LORA` | `minimax_h3_ref2v_turbo_4step_v0.1_bf16.safetensors`（turbo profile） |
| `GPU_SCHEDULER` | 调度器可执行文件（`submit`/`leases`）；主机来自 `<run>/queue/ALLOWED_HOSTS`；本地模式不使用（`H3_LOCAL_GPU`、`pool.json` 的 `local_gpus`） |
| `H3_EVAL_DINO` | 作为 `--dino` 传给冻结 EVAL-v1 评分器的 DINOv2-base 目录 |
| `H3_SAM2` | `sam2.1-hiera-large` 目录（仅 OOD 工具） |
| `H3_HED_ANNOTATOR_REPO` | 补丁后 checkout 中 soft-HED 工具 `<checkout>/tools/prepare_hed_control.py` 的 ControlNet v1.1 annotator 源码 |

每个键依次从环境、`DATAFARM_<KEY>`、运行时绑定文件的显式条目读取。
`python3 h3_paths.py show` 打印解析结果。

任务 bundle、参考、recipe 和筛选选择是运行目录中的任务产物；这里没有任何东西是
任务注册表或授权。模式和 OOD 维度只来自 bundle 的 `mode_confirmations`。

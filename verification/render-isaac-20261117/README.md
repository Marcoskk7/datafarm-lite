# Isaac RTX 渲染链路验证（2026-11-17）

在 lab30（RTX 4090，Isaac Sim 6.2 / Isaac Lab 3.0）上用同一个场景把一条 episode 渲染成 12 个 mp4，证明
`RenderSpec → 回放 state_trace → RTX 出图 → RenderManifest` 这条链是通的。

**这条 episode 是合成的**（`tools/synthetic_episode.py`）：只有 `state_trace`，不跑物理，`setup.synthetic=true`、
`judge_result.outcome=aborted`。它验证渲染链路，不是执行结果，不能进数据集。画面里盘子停在初始位置，因为合成轨迹里
没有任何抓取动作。

## 复现

```bash
ssh lab30
cd /opt/data/private/isaaclab/render/repo
source ~/isaaclab30_env.sh
S=examples/scene/plate-drawer-airbot.json
W=/opt/data/private/isaaclab/render/scene-work

python tools/scene_assemble.py prepare --spec $S --cache /opt/data/private/isaaclab/simready-cache \
  --robot-package /opt/data/private/isaaclab/a-scene/runtime/robot_assets_isaaclab3_validated_20261113 --work $W
python tools/scene_assemble.py plan  --spec $S --work $W
python tools/scene_assemble.py check --spec $S --work $W --headless

python tools/synthetic_episode.py --work $W --out $W/episode.json --steps 240 --drawer 0.25
python tools/render_episode.py spec   --work $W --episode $W/episode.json --out $W/render-spec.json
python tools/render_episode.py render --work $W --spec $W/render-spec.json --out $W/render-out.json --headless
```

`spec` 是纯 CPU；`render` 必须在 Isaac Lab 的 Python 里跑，耗约 30 秒（其中约 14 秒是启动 Kit）。

## 本轮结果

| 项 | 值 |
|---|---|
| 视角 | `head`、`left_wrist`（SceneGraph 物理相机）、`third_person`（虚拟相机，look-at 生成） |
| 外观组合 | 2 组材质（m0 原材质 / m1 覆盖盘子、桌子、抽屉）× 2 组灯光（l0 中性 / l1 暖光） |
| 变体 | 3 × 2 × 2 = 12 个 mp4，每个 640×360 / 10 fps / 21 帧 |
| 帧状态 | 252 帧全部 `ok`，0 `missing`、0 `error` |
| 回放校验 | `verified`：位置最大误差 2.5e-8 m，关节 5.7e-8，相机摆放 1e-6 m |
| RenderSpec | `sha256:1948a413945a55b91949010da882b01f1efb83cb22a2d4cc09392439f5e14a72` |
| RenderManifest | `sha256:b9366271184d2b9068390f9acc0cf6da1c5452968829e1af485106c48b6ff187` |

视频不进仓库（每批约 1.3 MB，且是合成产物），需要看图时按上面的命令重跑。

## 本轮修掉的两个 Isaac 渲染问题

都在 `src/datafarm/adapters/render/isaac/playback.py`：

1. **相机第一次的位置写不进去。** 相机建好后、首次 `sim.render()` 之前写 `set_world_poses`，位姿会丢，
   三个视角的第 0 帧都停在原点。改为 `after_reset` 末尾先 `sim.render()`，再对每个相机
   `update(0.0, force_recompute=True)`。
2. **近裁剪 1 cm 会切进相机自己的外壳。** 腕部相机被支架和镜头罩挡住，画面只剩地面和天空。
   改为 `NEAR_CLIP_M = 0.04`（4 cm）、`FAR_CLIP_M = 1e6`，写进 `PinholeCameraCfg.from_intrinsic_matrix`。

## 文件

| 文件 | 内容 |
|---|---|
| `render-spec.json` | 本轮 RenderSpec 全文（3 视角、4 个材质、4 个灯光、12 个变体） |
| `render-manifest.json` | 本轮 RenderManifest 全文（12 条流、252 条帧映射、内外参引用、回放校验） |
| `render-out.json` | 命令输出：清单引用、视频目录 |
| `render.out` | 渲染器日志（含 Kit 启动与每组的耗时；`*.log` 被 .gitignore 排除，所以叫 `.out`） |
| `views-m0-l0.png` | 原材质 + 中性光下三个视角的第 0 / 10 / 20 帧 |

## 没验证的

- **没有真实 episode。** B 线的 `state_trace` 还没接进来；`setup.joint_names`、`q`、`entities`、
  `articulations`、`control_step` 的格式只按文档约定实现，没和真实 episode 对过。
- **腕部相机外参没做视觉核对。** 参数从 AIRBOT 标定换算得出，合成轨迹里腕部视角对不准盘子也没有抓取动作，
  所以看不到"应该看到什么"的对照。
- **`camera.right_wrist` 还没参数**，示例场景只有 head 和 left_wrist。
- **没有接准入。** 渲染产物没有写进 `datafarm.gate-receipt`，也没有进 HDF5 导出。

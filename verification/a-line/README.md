# A 线验证流程（2026-11-19 复跑）

一条命令跑完全链路，每一关都有可复核的证据：`tools/verify_a_line.py`。

```bash
ssh lab30
cd /opt/data/private/isaaclab/a-scene/repo
source ~/isaaclab30_env.sh
python tools/verify_a_line.py \
  --spec examples/scene/plate-drawer-airbot.json \
  --cache /opt/data/private/isaaclab/simready-cache \
  --robot-package /opt/data/private/isaaclab/a-scene/runtime/robot_assets_isaaclab3_validated_20261113 \
  --work /opt/data/private/isaaclab/a-scene/work-verify-mounts
```

退出码 0 = 所有跑过的关都过。报告写进 `WORK/verify-a-line.json`，每关的证据（可达性解、静置漂移、穿透深度、探针落点、SceneGraph 摘要）都在里面。`--offline` 只跳过联网检索那一关，其余全在本地；`--skip-tests` 跳过 CPU 测试。

## 关卡

| 关 | 证明什么 | 失败意味着 |
|---|---|---|
| `env` | 解释器有 numpy、pxr、coacd、isaaclab | 用错了 venv（要用 `~/IsaacLab30/.venv`） |
| `cache` | 每个 SimReady 资产都存在，且按 `simready-fetch.json` 重新哈希全部文件一致 | 缓存被改动或文件缺失，先 `fetch` 修复 |
| `index` | 数据集索引可答，`probe` 能报出每个资产的关节数与许可证 | 网络或端点不通；抽屉资产必须报出关节 |
| `prepare` | 碰撞包装层、AssetRecord 生成，且能从 store 重新加载 | pxr/coacd 版本、或资产结构与预期不符 |
| `plan` | 布局解算、占地检查、IK 可达性（纯 numpy，几秒） | 布局越界/重叠，或目标点不可达（改 spec 重跑，不动仿真） |
| `check` | Isaac Lab 3.0 下写入关节初值、静置、穿透、抽屉落盘探针 | 物理或场景配置问题，日志在 `WORK/check.log` |
| `graph` | SceneGraph 过 IR 校验器，且 checks 自洽 | 生成逻辑与 IR 定义不一致 |
| `source` | 跑完全部阶段后源资产字节未变 | 有代码在写回缓存，属于来源绑定被破坏 |
| `tests` | `tests/test_scene_layout.py` + `tests/test_simready_fetch.py` | 布局/检索逻辑回归 |

## 场景是怎么进去的

三步，各管一段，互不依赖仿真：

1. **`prepare`**（要 pxr + coacd）：读缓存里的原始 USD，选中 Newton 物理变体（写 session layer，不改源文件），把碰撞网格换成 CoACD 凸包，写 `WORK/collision/<asset>.usda` 包装层；铰接资产在基座刚体上加固定关节锚到世界。产出 AssetRecord（包围盒、铰接、关节限位、质量、物理材质都被记录；假设值标 `assumed` 并带随机化区间）。
2. **`plan`**（只要 numpy）：按 `on` 关系把节点放到支撑面顶面之上；机器人按 `configs/robot-mounts.json` 的标定摆位——AIRBOT 是桌沿夹装（`mount.edge`/`along_m` 推导 xy 与偏航），安装板底面贴桌面，夹具五金挂在桌沿外；占地检查用按偏航旋转的矩形并允许标定的板宽越界；对每个目标点多起点 IK。几秒钟出结果，是布局迭代的工作面。
3. **`check`**（要 GPU）：Isaac Lab 3.0 + Newton 加载场景，显式写 `joint_state`（`scene.reset()` 在 Newton 下不回写），静置 2 s 查漂移与穿透，再把盘子投到打开的抽屉里看落点，最后写 SceneGraph。

## 检索怎么用

```bash
python tools/simready.py --endpoint https://hf-mirror.com search cabinet --require-joint prismatic
python tools/simready.py --endpoint https://hf-mirror.com probe fixture_cabinet_b01
python tools/simready.py --endpoint https://hf-mirror.com fetch fixture_cabinet_b01 --dest /opt/data/private/isaaclab/simready-cache
```

lab30 访问不到 `huggingface.co`，必须给 `--endpoint https://hf-mirror.com`（或设 `HF_ENDPOINT`）。`fetch` 先把分支解析成 commit sha 再下载，逐文件按 BOM 校验 sha256，写 `simready-fetch.json`（license、revision、BOM 摘要、取回时间、每个文件的 sha256 与大小）；重复执行只会补缺失或损坏的文件。

**缓存必须保持原始字节。** 任何修补都写在 `WORK/collision/` 的包装层里。`source` 关就是为了抓这一点：它在本轮最后一次证明缓存与 manifest 逐字节一致。

## 本轮场景（2026-11-19）

`examples/scene/plate-drawer-airbot.json` 已按桌沿夹装重排：AIRBOT 夹在不锈钢桌（1.8×0.70 m）的 +x 短边（`mount: {edge: "+x", along_m: -0.20}`，基座 `[0.788, -0.20, 0.875]`，偏航 −180°），盘子在桌面 `[0.60, -0.10]`，抽屉柜换成更浅的 `cabinet_file_d04`（0.375×0.529×0.877 m，上抽屉行程 0.269 m，预开 0.268 m）放在桌子南侧 `[0.96, -0.63]`、抽屉朝 −x 拉出。

实测：可达 10/10；静置漂移 1.1 mm、机械臂关节重力下沉 0.0127 rad（上限 0.02）；穿透 0.18 mm；机械臂安装板与桌面分离 5.9 mm（不再悬空）；盘子落入抽屉 z 0.4723 m（区间 0.45–0.52）。

机器人基座的 C 型夹五金碰撞在运行时被关掉了：包里的安装板碰撞是 `convexHull`，C 型夹的开口被填实成一块 0.19×0.28×0.118 m 实体，板贴在桌面上会整块穿进桌面 84.6 mm 并弹飞旁边的盘子。`prepare` 因此写一层包装 USD，把 `Geometry/left_base_link/mount_plate_link` 置 `active = false`，视觉不变；记录见 Record 的 `collision.artifact`。

## 本轮证据

见 `verification/a-line/`：`verify-a-line.json`（全部关卡与证据）、`verify.out`（终端输出）、`summary.json`（摘要）。

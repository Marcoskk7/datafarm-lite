# A 线：SimReady 资产检索与场景组装

把 NVIDIA SimReady 资产（Hugging Face `nvidia/simready-assets`）拉下来，结合机器人包和布局说明，产出 `ir.asset-record` 与 `ir.scene-graph`（字段见 [asset-record](../ir/asset-record.md)、[scene-graph](../ir/scene-graph.md)）。仿真固定为 Isaac Lab 3.0 + Newton/MJWarp。

| 代码 | 作用 |
|---|---|
| `src/datafarm/adapters/assets/simready.py`, `tools/simready.py` | 检索索引、探测资产、下载并按 BOM 校验 sha256；固定数据集 commit |
| `src/datafarm/adapters/sim/usd_assets.py` | USD 检查（包围盒、刚体、关节、质量、材质），CoACD 凸包碰撞包装层，铰接资产的世界锚定，机器人基座碰撞包围盒 |
| `src/datafarm/scene/asset_records.py` | 由检查结果生成 AssetRecord；物理量标 `assumed` 并给随机化区间 |
| `src/datafarm/scene/layout.py`, `kinematics.py` | 按 `on` 关系解位姿、占地检查、多起点 IK 可达性、生成 SceneGraph |
| `src/datafarm/adapters/sim/isaac_scene.py` | Isaac Lab 场景配置、写入初始关节状态、静置与穿透检查、放置探针 |
| `src/datafarm/ir/` | AssetRecord / SceneGraph 校验器 |
| `tools/scene_assemble.py` | `prepare` → `plan` → `check` 三步入口 |

## 检索与拉取

```bash
python tools/simready.py --endpoint https://hf-mirror.com search cabinet --require-joint prismatic
python tools/simready.py probe fixture_cabinet_b01
python tools/simready.py fetch fixture_cabinet_b01 --dest /data/simready-cache
```

端点默认取 `$HF_ENDPOINT`，否则用 `huggingface.co`。PlatforMAX 节点访问不了 `huggingface.co`，要用 `hf-mirror.com`。`fetch` 先把分支解析成 commit sha 再下载，每个文件都按 `com.nvidia.simready.packaging.bom.json` 校验，写 `simready-fetch.json`（含 license、commit、BOM sha256、取回时间）。默认跳过贴图预览缩略图，加 `--all-files` 才下载。可选资产的盘点见 [simready-inventory](../simready-inventory.md)。

## 组装

```bash
python tools/scene_assemble.py prepare --spec examples/scene/plate-drawer-airbot.json \
    --cache /data/simready-cache --robot-package /data/robot_assets_isaaclab3_validated_20261113 --work WORK
python tools/scene_assemble.py plan  --spec ... --work WORK          # 只需 numpy；改布局时在这一步迭代
python tools/scene_assemble.py check --spec ... --work WORK --headless  # Isaac Lab 3.0 Python
```

- `prepare`（需要 pxr + coacd）：给每个 SimReady 资产写碰撞包装层。上游碰撞网格换成 CoACD 凸包（参数见 `DEFAULT_COACD`，结果按网格内容和参数缓存），原网格上的质量移到刚体上，Newton variant 选中。铰接资产在 `anchor_body` 上加固定关节锚到世界。AssetRecord 的 `collision.bbox_*` 记录凸包的包围盒；机器人记录 `collision.base_bbox_*`，即基座刚体碰撞体的包围盒。
- `plan`：节点按支撑面顶面（有凸包包围盒时用凸包）加 `SUPPORT_CLEARANCE_M` 放置；机器人让基座碰撞体底面贴到支撑面上。占地检查用按偏航旋转的包围盒底面矩形：越出支撑面，或与同一支撑面上的其他东西重叠，都判失败。可达性对每个目标用多起点阻尼最小二乘 IK，作用在模型 TCP 上，自上而下接近，留关节限位余量；不做碰撞感知规划。
- `check`：加载场景，显式写入 `joint_state`（Isaac Lab 3.0 + Newton 下 `scene.reset()` 不会把它写回仿真；写入后读回值和目标不一致就报错），然后静置并检查穿透，再把物体放进打开的抽屉做探针，最后写出 SceneGraph。铰接家具关闭自碰撞：相邻抽屉的凸包互相外扩，会在初始时互相顶住。

退出码为 0 当且仅当静置、穿透、探针、可达性、占地全部通过。`WORK/store/` 是内容寻址的证据；主机路径只出现在 `prepared.json`，不会进入 IR。

## 一条命令验证全链路

`tools/verify_a_line.py` 把上面三段和检索、校验串起来，逐关给结论，报告写进 `WORK/verify-a-line.json`：

```bash
python tools/verify_a_line.py --spec examples/scene/plate-drawer-airbot.json \
    --cache /data/simready-cache --robot-package /data/robot_assets_isaaclab3_validated_20261113 \
    --tests-python /path/to/pytest-venv/bin/python --work WORK
```

关卡：`env`（pxr/coacd/isaaclab）→ `cache`（按 manifest 重哈希）→ `index`（联网检索与 probe，`--offline` 跳过）→ `prepare` → `plan` → `check` → `graph`（SceneGraph 过 IR 校验器）→ `source`（跑完后源资产字节未变）→ `tests`。Isaac venv 里通常没有 pytest，`--tests-python` 可指向另一个解释器。本机 lab30 的完整记录见 `verification/a-line/`。

**缓存必须保持原始字节。** 变体选择写在 session layer，碰撞与修补只进 `WORK/collision/`；`source` 关就是为抓写回缓存这类来源绑定破坏。

## 限制

- 物理量（质量、摩擦、恢复系数、抽屉阻尼）是 SimReady 作者值或假设值，标为 `assumed`，不是测量值。
- 可达性只算运动学，不检查手臂和柜体或桌子的碰撞，也不检查抓取姿态是否可行；这些交给 B 线的规划与判定。
- 占地检查只考虑平放物体（只有偏航），也不检查放在地面上的东西之间是否重叠。
- 机器人包许可证未知，记录为 `unknown`。

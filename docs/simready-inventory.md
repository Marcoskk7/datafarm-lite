# SimReady 资产盘点（A 线第 1 天）

> 2026-11-15。目标：核实 `docs/mvp-design.md` §7 风险 2「SimReady 里有没有带关节的抽屉柜」。
> 结论：**有**，而且带 Newton payload（对上固定的 Isaac Lab 3.0 + Newton/MJWarp 后端）。
> 下文所有关节、质量、摩擦、许可数字都来自实际拉取的资产文件，不是记忆或文档转述。

## 1. 在哪、怎么拿

| 项 | 值 |
| --- | --- |
| 数据集 | Hugging Face `nvidia/simready-assets`（2026-10-21 随 SimReady Central 发布，免登录） |
| 索引 | `github.com/NVIDIA-Omniverse/simready-central`（`submissions/` 下 21 个 OEM 命名空间） |
| API | `GET https://huggingface.co/api/datasets/nvidia/simready-assets/tree/main?recursive=true`（分页，每页 1000） |
| 规模 | 顶层资产目录 **2677** 个（本次 API 可枚举到 80000 条路径）；SimReady Central 对外口径 2000+ |
| 大类 | `food` 538、`kitchenware` 179、`box` 154、`handTool` 97、`cabinet` 23、`dishware` 22 |
| 网络 | 本机直连 `huggingface.co` 超时，`hf-mirror.com` 可用；下载器必须支持可配置 endpoint |

每个资产有自己的 `com.nvidia.simready.packaging.json`：`package_id` 带版本与时间戳，
`license` 字段、`content_hash`／`package_hash`（sha256）。
`.metadata/com.nvidia.simready.packaging.bom.json` 逐文件给 `relative_path` + `size` + `sha256`，
正好是 AssetRecord 需要的 `ArtifactRef` 证据，不需要自己算。

## 2. 单资产目录结构

```text
<asset>/
  com.nvidia.simready.packaging.json      # package_id / license / hashes
  .metadata/
    LICENSE.txt
    com.nvidia.simready.packaging.bom.json
    com.nvidia.simready.conformance.Package@1.0.0.json   # profile + 逐项 FE 通过情况
    com.nvidia.simready.root_usds.json                   # 入口 USD 路径
  simready_usd/
    sm_<asset>_01.usd                     # 根 USD（USDC 二进制）
    payloads/
      base.usda  geometries.usd  materials.usda  physics.usda
      instances.usda  instances_mujoco.usda  instances_newton.usda  instances_physx.usda
      PhysX/{enabled,disabled}.usda       # 三个引擎各一套 override
      MuJoCo/{enabled,disabled}.usda
      Newton/{enabled,disabled}.usda      # ← 我们的后端直接吃这一套
    Textures/  source_assets/  .thumbs/
```

- profile 为 `Prop-Robotics-Neutral`／验证记录写 `Robotics-Prop`，`metersPerUnit = 1`、`upAxis = Z`。
- 抽查的 2677 个目录里 **2541** 个带 `payloads/Newton`（缺的是早期格式资产，如 `dishware_bowl_a01`）。
  候选清单里的资产逐个确认过 `payloads/Newton/enabled.usda` 返回 200。

## 3. MVP 候选

| 角色 | 资产 | 依据 | 备注 |
| --- | --- | --- | --- |
| receptacle | `fixture_cabinet_b01` | 2 个 prismatic 抽屉，wood 台面，`props_endeavor_kitchen` 套件 | **首选**，缩略图确认是厨房抽屉柜 |
| 备选 receptacle | `fixture_cabinet_a01` / `_d01` | 2／3 个 revolute 柜门 | 门不是抽屉，只能当背景或换成开门任务 |
| 备选 receptacle | `cabinet_file_b01` / `_d01` | 6 个 prismatic 抽屉，行程 0.29 m | 办公文件柜，厨房场景里不自然 |
| 备选 receptacle | `cabinet_for_drugs_a01` | 4 抽屉（0.6 m）+ 4 滑板（0.58 m） | 行程最长，柜体是金属药柜 |
| task_object | `kitchenware_bowl_f01` | 宽口浅碗，直壁带沿 | 抓到沿的概率高，首选 |
| task_object | `kitchenware_bowl_k01` | 木碗，浅 | 备选 |
| task_object | `dishware_plastic_bigPlate_a01` | 方盘带四边沿，0.24 m | 方沿最好夹，但碰撞代理是单块薄板，需重做碰撞 |
| table / background | `table_metal_a02`、`table_metal_stainless_a03` | 不锈钢工作台，带下层板，0 关节 | 厨房台面 |
| 整场景（可选） | `facilitySystem_kitchenette_module_a01` | 冰箱 + 水槽 + 抽屉 + 3 门，17 个关节 | 1.4 GB，v1 先不用 |

抽到的缩略图（`simready_usd/.thumbs/256x256/`）已确认：b01 是白柜体 + 木台面 + 两抽屉 + 两层开放格；
bowl_f01 是宽浅陶瓷碗；bigPlate_a01 是蓝色方形浅盘；table_metal_stainless_a03 是不锈钢双层工作台。

## 4. 抽屉关节证据（`simready_usd/payloads/Physics/physics.usda`）

| 资产 | 关节 | 轴 | 行程 | 刚体质量 |
| --- | --- | --- | --- | --- |
| `fixture_cabinet_b01` | `joint_part_01_joint_00`、`joint_part_02_joint_00` | X | `[0, 0.46218]` m | body 36.191 kg |
| `fixture_cabinet_a01` | `joint_part_01/02_joint_00` | Y (revolute) | `[-90, 0]`° | 20.168 / 2.537 / 2.537 kg |
| `cabinet_file_a01` | `joint_drawer_01..06` | X | 0.2945 m | 1.242 / 5.17 / 5.243 kg 等 |
| `cabinet_for_drugs_a01` | `joint_drawer_01..04`、`joint_slide_01..04` | X | 0.6 m / 0.58 m | 15.0 kg 柜体 |
| `equipment_rolling_tool_cabinet_a01` | 5 个 `joint_drawer_*` + 5 个 `joint_rail_*` + 轮组 | X / Z | 0.20–0.26 m | 25.699 kg |

`fixture_cabinet_b01` 的根 `RootNode` 带 `PhysicsArticulationRootAPI`，抽屉是独立 rigid body，
`physics:body0 = body_01_mesh_00`、`body1 = part_0N_mesh_00`，可直接当 Articulation 导入；
抽屉的 `joint_state` 初值（预先拉开）写进 SceneGraph 即可，不需要改资产。

## 5. 与 AssetRecord 的映射，以及缺口

| AssetRecord 字段 | SimReady 提供 | 缺口 |
| --- | --- | --- |
| `source.uri` / `license` / `upstream_id` | `packaging.json` 的 `package_id`、`license`（短名单全是 `CC-BY-4.0`） | 无 |
| `files[].artifact.sha256` | `bom.json` 逐文件 sha256 | 无 |
| `joints[]` | `physics.usda` 的 `PhysicsPrismaticJoint` / `PhysicsRevoluteJoint`：`axis`、`lowerLimit`、`upperLimit`、`body0/body1`、`localPos0` | 无 |
| `collision.kind` | 每个资产带碰撞几何，但只给 `mesh` 代理 | 已解决：准备阶段换成 CoACD 凸包（`collision.kind = mesh`，凸包写进包装层），方盘的薄板代理不再使用 |
| `bbox_m` | **不提供**。`base.usda` 的 `extent` 是子网格局部范围，直接并集算出来是错的 | 已解决：准备阶段用 `BBoxCache` 现算，记为资产根坐标系下的包围盒 |
| `physical_quantities.mass` | 只给`密度`（`physics:density`：陶瓷 2600、塑料 950、木 700、金属 8000），柜体额外给显式 mass + 惯量 | 按密度 × 碰撞体积推，账本记 `assumed`（作者值，不是实测也不是拟合） |
| `physical_quantities.friction_static` | 有（柜体 0.4–0.5、碗 0.35、盘 0.4） | 无 |
| restitution | 有，但塑料盘 0.4 偏高，落桌会弹 | 记为 `assumed`；随机化清单里应优先收窄这一项 |

## 6. 第 1 天待核：已核（2026-11-15，lab30 / Isaac Lab 3.0 + Newton）

1. **是不是 2 自由度 Articulation、行程够不够**：是。导入后识别为 articulation，两个 prismatic 关节，写入 `joint_state` 后读回 `[0.40, 0.00]` 与目标一致。`plan`/`check` 都用 0.40 m 开度，没到极限 0.46218 m。
2. **抽屉内腔净尺寸够不够放盘子**：够。把盘子投放到打开的抽屉里，落点 z = 0.759 m（抽屉内底），X/Y 落在抽屉中心 ±1 mm 内，只与柜体接触。
3. **台面高度与 AIRBOT 可达性**：实测桌面包围盒顶面 z = 0.80421 m（含支撑间隙），柜体整体 0.9285 m 高。机器人基座碰撞体底面贴桌面后，盘子与抽屉内部 10 个目标点全部求得逆解，最大位置误差 0.8 mm。
4. **方盘碰撞代理要不要重做**：已重做。上游的薄板代理不再使用，改为 CoACD 凸包（质量随之从网格移到刚体，盘 0.186 kg）；凸包包围盒也用来定支撑高度。
5. **G2 夹爪开口 vs 盘沿直径**：**未核**。夹爪几何映射只有模型级推导，没有实测标定，按 B 线的说法夹爪能力保持关闭。

细节与证据见 [scene-assembly](reference/scene-assembly.md) 与 `verification/a-line/`。

## 7. 许可与体积

- 短名单全部 `CC-BY-4.0`：可商用，需署名。AssetRecord 里逐条记 `license` 与 `retrieved_at`。
- 体积（含 `Textures/` 与 `source_assets/` 的重复文件计数）：碗 11 MB、方盘 51 MB、
  不锈钢台 64 MB、`fixture_cabinet_b01` 351 MB、`facilitySystem_kitchenette_module_a01` 1.45 GB。
  磁盘上实际约一半（两处同一份贴图）。
- 下载只能在有网的地方做：本机 `huggingface.co` 不通，用 `hf-mirror.com` 或直接在 GPU 机器上拉。

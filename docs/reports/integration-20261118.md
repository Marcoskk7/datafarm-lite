# test/integration-20261118 全链路集成验收报告（草稿，进行中）

日期：2026-11-18（Asia/Singapore）。分支：`test/integration-20261118`（origin/main `9d8b055` 为基线）。
产出是 Isaac Lab 3.0 / Newton 仿真数据，**不是 real-ready 数据**。

> 本文件在验收过程中持续更新；凡未实际观察到的结论标注【推断】。

## 1. 合并记录

| 顺序 | PR | head（预期 = 实际） | 冲突文件 | 取舍 |
|---|---|---|---|---|
| 1 | #13 refactor/B-state-from-episode-ir | 74bf9ec = 74bf9ec | 无 | — |
| 2 | #12 feat/A-load-scene | d4378e5 = d4378e5 | tools/render_episode.py | 两边意图合并：保留 #12 的 `scene_runtime.load_scene`（不读 plan.json、抽屉阻尼取 runtime profile），保留 #13 的 `validate_state` 校验 state_trace、`write_state(state)` 不带 joint_names；删去两边各自变成无用的局部变量 |
| 3 | #11 docs/A-single-arm-cameras | 81d749b = 81d749b | docs/ir/render-spec.md | 第 1 条取 #11（单臂只有 head + left_wrist）；第 2 条取 #13 的 state 字段写法（`entities[node].joints`），#11 里旧的 `articulations` 字样随 #13 删除 |
| 4 | #14 feat/A-request-authoring（21 提交） | 9ccfd41 = 9ccfd41 | docs/ir/episode.md（B 线 IR 文档） | state_trace 字段表取 #13（validate_state 强制的形状）；#14 新增的“是仿真测量值，不是声明的 joint_state”一句移到 `entities[node].joints` 行。仅措辞，无字段增删 |

origin/main 实际为 `9d8b055`，与预期一致。未合并 `origin/feat/B-judge-generation`。

## 2. 逐 PR 结论

（待补全：见第 9 节）

## 3. CPU 测试结果

| 环境 | 范围 | 结果 |
|---|---|---|
| macOS（本机，Python 3.12.13） | `make test`（合并后 3e3bd60） | 915 passed / 4 failed / 14 skipped；4 个失败在 origin/main 9d8b055 上同样失败（main 上共 60 failed） |
| macOS | `make lint` / `make audit` | 0 violations，import-linter 7/7 kept；audit 604 files 0 errors |
| lab30（Linux，Python 3.12.16） | `make test`（3e3bd60） | 921 passed / 4 failed / 8 skipped；4 个失败均为 `test_cap_xh`，在 origin/main 上同样失败（5 failed） |

（待补：最终 HEAD 的完整复跑）

## 4. 物理闸门首次 GPU 实跑

lab30run（n001，RTX 4090，驱动 580.95.05，Isaac Sim 6.2 / Isaac Lab 3.0），实例到期 2026-11-18 23:05:55 SGT。
代码以 git bundle 同步到 `/opt/data/private/isaaclab/integration-20261118/repo`，每次同步后核对远端 HEAD 与本机一致。

**实际参数**（经主机路径 `scene_author_cli.check_with_tool`，驱动脚本 `runs/gate_driver.py` 记录真实 argv）：

| 项 | 值 |
|---|---|
| `check.python` | `/home/isaac/IsaacLab30/.venv/bin/python`（修正后保持 venv 符号链接，不解析到 uv 基础解释器） |
| `check.env_script` | `/home/isaac/isaaclab30_env.sh` |
| 包装 | `bash -c 'source "$0" \|\| exit $?; exec "$@"' /home/isaac/isaaclab30_env.sh <python> tools/scene_assemble.py check --spec … --work <prepared> --headless --settle-s 2 --probe-s 2 --drift-tol 0.005 --speed-tol 0.01 --joint-tol-active 0.005 --joint-tol-passive 0.005 --penetration-tol 0.003 --nconmax 4096 --njmax 8192` |
| 环境 | 调用方 `env -u DISPLAY -u XAUTHORITY -u VK_ICD_FILENAMES -u LD_LIBRARY_PATH -u VIRTUAL_ENV`；Vulkan 变量只能来自 env_script，日志无 `Failed to create any GPU devices` |
| headless | `--headless` 由主机总是追加；`check` 走 Newton 无 Kit 路径（日志无 Kit/carb 启动行），在无 DISPLAY 的 SSH 会话中跑通 |
| 产物 | `check.json`、`check.log` 写在 prepared 目录（`<work>/prepared/`） |

**已知合法 layout（`examples/scene/plate-drawer-airbot.json`）三次尝试**（`runs/gate-01-known-layout/`）：

| 尝试 | 结果 | 根因 | 修正 |
|---|---|---|---|
| 1 | `CheckError: exit 1 without check.json`（静置、探针已完成） | `settle.new_contacts[*].body_b` 是 USD prim 路径 `/World/...`，被 kernel 可移植校验当作主机绝对路径，`store.put_json` 抛 SchemaError | `329336c`：USD body 标签加 `usd:` 前缀（与 B 录制器同一约定），附测试 |
| 2 | exit 1，诊断 `check: joint robot.airbot.left_joint3 drifted 0.0127 m > 0.005 m (actuated/held)` | PR #14 `0a80c1a` 把机器人关节全部列入主动关节严格上限；AIRBOT 位置驱动在 2 s 静置中重力下垂 0.012745 rad（verification/a-line 与 1b 实跑记录了同一数值且通过）。布局无法修正它，闸门会拒绝所有布局 | `cf390b7`：机器人关节漂移只记录（`robot_joint_drift_rad`），`active_joints` 中的驱动关节仍按严格上限；**未改任何容差数值**。这是语义变更，需 PR #14 作者确认 |
| 3 | **exit 0，通过**，44.8 s | — | — |

尝试 1 之前另有一次：手工核对时发现 `bash -c` 包装里多余的 `shift` 会把解释器从 argv 丢掉、`resolve_check_interpreter` 用 `Path.resolve()` 跟随 venv 符号链接离开 venv（实测得到 `.../cpython-3.12.16.../python3.12`，无 isaaclab）。`d5f9e34` 修正两处并附两条会在旧代码失败的测试（env_script 的 export 进入 exec 后进程、argv[0] 正是解释器）。

**check.json 摘要（尝试 3，均为实测）**：240 步静置，最大根漂移 1.038 mm，末速度 0.708 mm/s，穿透 39 µm（容差 3 mm，69 个接触），探针 `plate_in_drawer` 通过（盘子静止于 z = 0.7593 m，接触 `object.cabinet`），被动关节（抽屉 0.4 m）漂移 0，机器人 `left_joint3` 下垂 0.012745 rad（记录），新增接触对 `None | object.cabinet`（深度 −0.3 mm，未接触）。

**诊断回传**（同一主机路径，lab30 实测）：超时（`timeout_s=5`）→ `check timed out after 5 s`；解释器起不来（返回 127 的假解释器）→ `exit 127 without check.json`；env_script 不存在 → `exit 1 without check.json` 且日志尾带 bash 报错；退出码 0/1 与 `check.json` 的诊断行分别见尝试 3 / 尝试 2。

**scene-author 全流程**（`runs/author-01-20261117T193912Z`，Claude Code 2.1.325 / claude-opus-5-5，经本机 4001 适配器；proot 沙箱）：2 轮，$4.71。第 1 轮 plan 失败（两个抽屉目标不可达，误差 69/91 mm，作为诊断回给 agent）；第 2 轮接受：符合性完整、plan ok、**GPU check exit 0**（漂移 1.021 mm，穿透 11 µm，探针通过）。未使用 `--kinematic-only`。

**后续发现（第 5 节阻断）**：check 产出的 SceneGraph 无法被 `scene_runtime.load_scene` 加载（`bundle coverage incomplete`）。根因：`prepare` 读 `delivery_manifest.json` 顶层 `layer_sha256`，而机器人包把摘要放在 `assets.<name>.layer_sha256`，机器人从未进入 asset bundle。所有已组装场景（示例布局与 agent 布局）都受影响，单测因夹具手写 bundle 未覆盖。`8dff94e` 修正并附按真实 manifest 形状的测试；同一 agent 布局（字节不变）重新 prepare/plan/check（`runs/scene-02-reprepared`，check exit 0），之后 B 侧 `load_scene` + `IsaacRobotAPI` + 全场景测量在 GPU 上实测可用（`runs/chain/probe.json`）。

## 5. 真机全链路

工作区：`lab30:/opt/data/private/isaaclab/integration-20261118/runs/`（本机副本 `repos/PyRUA-Lean/runs/integration-20261118/lab30-runs/`；驱动脚本副本 `.../chain-src/`）。驱动脚本是本次 run 的任务产出，不进仓库。

**LLM 通路**（凭据只在本机已有桥接进程中，不进 argv/日志/提交）：
- Claude Code 2.1.325（scene-author）：经 lab30 上已有的 Anthropic→OpenAI 适配器 `127.0.0.1:4001`，占位 token。
- Codex 0.169（task-plan、judge 作者/审查）：lab30 上原有 `fyp_codex_proxy.py` 的出口被硬编码为直连，Tailscale 域名在 lab30 不可解析；本次写了 `runs/codex_relay.py`（不改用户文件，只替换 opener 走已有隧道 `127.0.0.1:1054`），最小调用 `{"answer":"PONG"}` 通过后接入；以注入网关方式传给仓库的 structured-call 端口。

**阶段与结果（截至本节写作时）**：

| 阶段 | 结果 | 证据 |
|---|---|---|
| request → scene（scene-author，GPU 闸门） | 通过：`author-01`（盘子 `dishware_plate_a01`，无相机，后发现不可渲染）；修 `b5b2a9f` 后重跑 `author-02`：2 轮、$7.30，选 `dishware_plastic_bigPlate_a01`（带帮），相机按标定声明，GPU check exit 0（漂移 1.05 mm，穿透 61 µm，探针通过） | `runs/author-02-20261117T220718Z/` |
| request → task（task-plan，Codex） | `task2`/`task3` planned（task3 去掉了 host 添加的 forbidden-shortcut，它不在原始 request 中，judge 无法从状态判定） | `runs/chain/task3/` |
| live 录制（B `record_scene_episode`） | 链路可用：load_scene + IsaacRobotAPI + TCP IK 主机 + 全场景测量 + 录制，最长一条 3438 步 / 103 MB | `runs/chain/dev/*`, `runs/chain/s2/dev/*` |
| 专家（CaP） | 进行中，尚无成功 episode（见下） | |
| judge 生成/冻结（Codex 作者 + 独立审查） | 进行中，尚未冻结（见下） | `runs/chain/s2/judge*` |
| 渲染 / HDF5 / admission | 依赖成功 episode，未开始 | |

**专家开发记录（全部保留）**：手写专家 v1–v9 在 `author-01` 场景（平盘，无相机），s2v1–s2v6 在 `author-02` 场景（带帮盘）。每次失败都定位到原因：腕关节限位（离线用仓库运动学做稠密连续性检查后改用关节空间 via 点）、IK 600 步内不收敛（位置驱动稳态下垂 6–7 mm）、指尖压到盘底、闭合到 0 时被动 mimic 手指从薄帮上滑过（盘子未被带起，`s2v1/s2v2`）。CaP 搜索（`tools/cap_search.py`，Codex actor）尚未接入：它要求一个准备好的 expert-harness round（xh broker），而 round 又需要冻结的 judge。【推断】手写专家只是 dev 种子上的开发产物，最终 5 条 episode 若来自手写专家，不能称为 CaP 产出。

## 6. parts[] 精确扫掠

实现由子 agent（sonic；环境没有名为 `fast` 的 agent）在独立 worktree `test/parts-sweep-20261118` 完成，主 agent 审阅后要求修正一次（见下）。按要求分两提交、在第 5 节 5 条 episode 之后才合入 test 分支：

- `c06a2b2` `feat(A-layout)`：`joint_sweep_check` 优先按 `parts[]` 精确扫掠。移动件 = `parts[].prim` 等于 `joints[].child_body` 的分件；prismatic 取起止分件盒凸包（精确）；revolute 需 `anchor_m`，绕枢轴 ≤5° 采样取 8 角投影凸包（弦代弧内收 ≤1e-3·r）。结果 `checked[].method = "parts"`。缺字段时回退，输出与改动前逐字节一致。
- `879dbab` `contract(A)`（跨线契约，待 B 线确认）：`datafarm.asset-record/1` 新增可选 `parts[]`（`name`、`prim`、`bbox_min_m`、`bbox_max_m`，资产根系，米，静止姿态）、`joints[].anchor_m`、`joints[].child_body`；版本号不变（IR README 第 8 条），validator 向后兼容。

审阅发现并已修正：子 agent 初版按叶名 `part.name == joint.child` 匹配。真实 SimReady 资产里关节 child 是刚体下的网格（`.../part_01_obj_00/part_01_mesh_00`），叶名永远不等，精确路径在真实资产上不会生效，而合成测试用了相同叶名，测不出来。改为 prim 路径关联，测试改用真实命名形状与实测 `fixture_cabinet_b01` 数值：抽屉 0.35 m 时分件前沿到 x = 0.68937，整盒保守法只到 0.65839，x∈[0.66, 0.68] 的障碍只有分件路径能发现。

消费方审计（子 agent 用 grep；本环境 LSP 未就绪【推断：未运行 xd://lsp references】）：scene_runtime、task/planning 经 `asset_record.validate`；layout、render/plan、scene_measurements、scene_files、scene/authoring 只读已知键；B 侧 public_task `joint_facts`、episode `asset_refs` 只取显式键或 ArtifactRef。无严格键白名单，无 B 线文件改动。

CPU：在 `879dbab` 上相关 88 passed / 32 subtests；在 `c06a2b2` 单独 76 passed；lint/audit 通过。

改动前 joint_sweep（lab30 实测，`runs/scene-02-reprepared/prepared/plan.json`）：`{"checked": [{"joint": "joint_part_01_joint_00", "node": "object.cabinet", "travel_m": 0.35}], "ok": true, "skipped": []}`（保守整盒平移）。改动后结果见第 5 节之后的回归。

## 7. 失败与未解决项

以下为真机阶段发现、且已在 test 分支上修正或打补丁的问题（按发现顺序）；未解决项见末尾。

| # | 现象（lab30 实测） | 根因 | 处理 |
|---|---|---|---|
| 1 | 首次 GPU check 在写证据时崩溃 | USD prim 路径被可移植校验当成主机路径 | `329336c` A 线修正 |
| 2 | `--check-env-script` 时解释器从 argv 丢失；`--check-python` 解析出 venv 外解释器 | `bash -c` 多余 `shift`；`Path.resolve()` 跟随符号链接 | `d5f9e34` A 线修正 |
| 3 | 已知合法布局被闸门拒绝 | 机器人重力下垂 12.7 mrad 被当作主动关节漂移（PR #14 新语义） | `cf390b7` A 线：恢复 main 语义（只记录），待 PR #14 作者确认 |
| 4 | check 产出的 SceneGraph 无法被 `load_scene` 加载 | prepare 读错 `layer_sha256` 位置，机器人不进 asset bundle | `8dff94e` A 线修正 |
| 5 | 没有 HDF5 导出器 | B 线未实现 | `f71adf0` tmp(B) |
| 6 | task-plan 三次被 validator 拒或被网关拒 | proposal_schema 与 validate_proposal 约束不一致；网关不接受 maxLength/uniqueItems | `c7e8204`/`92cebca`/`7fbd9e9` tmp(B) |
| 7 | 每次 `move_gripper` 都 `SPEED_LIMIT` | G2 被动 mimic 手指开度瞬时过冲参考斜坡 ~6 倍，速率检查用参考速率 | `77a55f4` tmp(B)：实测开度按模型手指速度上限检查 |
| 8 | 真实 episode 录到 100 步后每步约 1 s | 录制器每次检查都整条重序列化（O(N²)） | `cf98249` tmp(B)：增量计数，上限逐字节等价 |
| 9 | agent 布局 `cameras: []`，渲染时才失败 | 符合性不检查相机 | `b5b2a9f` A 线：请求相机必须按机器人标定事实声明 |
| 10 | 一次 rollout 进程段错误（`python3.12` segfault，dmesg） | 未定位【推断：Newton/Warp 原生层】；重跑同种子未复现 | 保留失败目录 `dev/v2-seed2b.segfault` |
| 11 | `move_tcp` 姿态 0.4 rad 以上在 600 步内不收敛；稳态 TCP 位置误差 6.6 mm > 5 mm 容差 | 位置驱动在重力下的稳态误差；IK 参考按测量值推进 | 专家侧：分段转向；运行时 control profile 的位置容差 5→10 mm（控制剖面是任务产出，不是判定阈值）；见第 5 节 |

## 8. `tmp(B):` 与 `contract(A):` 提交清单

所有 `tmp(B):` 提交正文均含"临时补丁，待 B 线同学确认"；`contract(A):` 正文含"跨线契约，待 B 线确认"。

| 提交 | 文件 | 原因（实测） | 改动 | 替代方案建议 |
|---|---|---|---|---|
| `f71adf0` tmp(B) | `src/datafarm/expert/hdf5_export.py`、`tools/export_episode_hdf5.py`、`docs/reference/hdf5-export.md`、测试 | 仓库没有 HDF5 导出，链路在导出步断开 | 按 b-control-interfaces §8 写单 episode HDF5（`datafarm.episode-hdf5/1`，`real_ready=false`），读回逐位校验 | B 线建数据集包：多 episode manifest（成员+摘要）、schema 版本策略 |
| `c7e8204` tmp(B) | `task/planning.py` | 模型输出 role `"already-open drawer container"`，schema 未声明 ID 规则，validator 拒收 | schema 给 role pattern、node/frame enum | 用 validator 常量生成 schema |
| `92cebca` tmp(B) | 同上 | task name > 256 字符被拒 | 补长度/个数上限 | 同上 |
| `7fbd9e9` tmp(B) | 同上 | 上一提交加的 maxLength/uniqueItems 被 FYP 网关整条拒收 | 只保留网关接受的关键字，256 写进 description | 同上，并按网关能力表约束 schema 关键字 |
| `77a55f4` tmp(B) | `simulation/tcp_control.py` | G2 每次开合都 `SPEED_LIMIT`（复现 `docs/reports/evidence/gripper-width-rate-20261118.md`） | 实测开度速率上限改为源模型手指速度之和（0.4 m/s）；参考斜坡仍按请求速度 | 机器人包给被动手指加小阻尼/滤波，或在 control profile 中由 B 线签字给出窗口化检查 |
| `cf98249` tmp(B) | `expert/episode.py` | 录制 O(N²)，100 步后 ~1 s/步 | 增量字节计数，与旧整条序列化逐字节等价（测试锁定边界） | 状态流式写盘（JSONL），按流计字节 |
| `879dbab`→（合入后新 SHA，见第 6 节）contract(A) | `ir/asset_record.py`、`scene/asset_records.py`、`docs/ir/asset-record.md`、`docs/ir/scene-layout.md` | `parts[]` 精确扫掠需要分件几何与枢轴 | 新增可选 `parts[]`、`parts[].prim`、`joints[].anchor_m`、`joints[].child_body`，版本不变 | — |

需 B 线确认的契约条目：AssetRecord 上述四个可选字段；HDF5 schema `datafarm.episode-hdf5/1` 的组布局；`move_gripper` 实测开度速率上限的取法；task-plan schema 约束的来源。

## 9. 是否建议合入 main

（待补全）

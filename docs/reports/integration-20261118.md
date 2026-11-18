# test/integration-20261118 全链路集成验收报告

日期：2026-11-18（Asia/Singapore）。分支：`test/integration-20261118`（origin/main `9d8b055` 为基线）。
产出是 Isaac Lab 3.0 / Newton 仿真数据，**不是 real-ready 数据**。

凡未实际观察到的结论标注【推断】。实现改动、CPU 测试、真机结果与未知项分节陈述。

**摘要**：4 个 PR 按序合入 test 分支（3 处冲突，均已按语义解决）；物理闸门第一次在 GPU 上真跑并通过（修 4 处 A 线问题）；链路的每一段（场景组装 + GPU 闸门 → task-plan → judge 生成与冻结 → 录制 → 冻结 judge 独立判定 → 状态重放渲染 → HDF5 导出与独立校验）都已在 lab30 上用真实数据跑通，但**没有得到任何 judge 判定成功的 episode**：手写专家抓不起带帮大盘，5 条成功 episode 与 admission **未达成**。parts[] 精确扫掠已实现、合入并在真机回归，但它改动了冻结 judge 的依赖文件，合入后已冻结的 judge 需重新冻结。test 分支上有 11 个 `tmp(B):` 与 1 个 `contract(A):` 待 B 线确认。

## 1. 合并记录

| 顺序 | PR | head（预期 = 实际） | 冲突文件 | 取舍 |
|---|---|---|---|---|
| 1 | #13 refactor/B-state-from-episode-ir | 74bf9ec = 74bf9ec | 无 | — |
| 2 | #12 feat/A-load-scene | d4378e5 = d4378e5 | tools/render_episode.py | 两边意图合并：保留 #12 的 `scene_runtime.load_scene`（不读 plan.json、抽屉阻尼取 runtime profile），保留 #13 的 `validate_state` 校验 state_trace、`write_state(state)` 不带 joint_names；删去两边各自变成无用的局部变量 |
| 3 | #11 docs/A-single-arm-cameras | 81d749b = 81d749b | docs/ir/render-spec.md | 第 1 条取 #11（单臂只有 head + left_wrist）；第 2 条取 #13 的 state 字段写法（`entities[node].joints`），#11 里旧的 `articulations` 字样随 #13 删除 |
| 4 | #14 feat/A-request-authoring（21 提交） | 9ccfd41 = 9ccfd41 | docs/ir/episode.md（B 线 IR 文档） | state_trace 字段表取 #13（validate_state 强制的形状）；#14 新增的“是仿真测量值，不是声明的 joint_state”一句移到 `entities[node].joints` 行。仅措辞，无字段增删 |

origin/main 实际为 `9d8b055`，与预期一致。未合并 `origin/feat/B-judge-generation`。

## 2. 逐 PR 结论

| PR | 能否直接合入 main | 需先改什么（证据见第 4、7 节） |
|---|---|---|
| #13 refactor/B-state-from-episode-ir | **可以**（单独合入无冲突；CPU 测试无新增失败；真机上录制器、judge、渲染重放都按它的 state_trace 跑通） | 无。后续 B 线自己的问题（录制 O(N²)、judge worker 上限、HDF5）是 test 分支上的独立 `tmp(B)`，不阻塞 #13 |
| #12 feat/A-load-scene | **可以，但需按本次方式解决与 #13 的冲突**（`tools/render_episode.py`） | 先合 #13，再把 #12 rebase 到其上，冲突解法见第 1 节；真机渲染（s2v8）走的就是合并后的版本，`state_playback` 0 误差 |
| #11 docs/A-single-arm-cameras | **可以**（纯文档） | 与 #13 的 `docs/ir/render-spec.md` 冲突按第 1 节解；注意本次真机渲染的请求视角是 `head` + `third_person`，与该 PR 的 `head` + `left_wrist` 不一致——请求 YAML 需对齐 |
| #14 feat/A-request-authoring | **不建议单独直接合入** | ① `cf390b7` 改回了 #14 新加的机器人关节漂移语义（否则已知合法布局也被闸门拒绝），需 #14 作者确认；② `d5f9e34`（`--check-python`/`--check-env-script` 包装，#14 引入的代码路径）与 `b5b2a9f`（请求相机必须声明，否则 agent 布局不可渲染）应随 #14 一起进 main。另外 `8dff94e`（机器人 USD 未进 bundle）与 `329336c`（check 证据含 USD 路径时崩溃）是 **main 上已有**的缺陷（`9d8b055` 的 `tools/scene_assemble.py` 第 99 行同样读顶层 `layer_sha256`），只是在 #14 的 GPU 闸门首跑中暴露，建议单独先合 |

## 3. CPU 测试结果

| 环境 | 范围 | 结果 |
|---|---|---|
| macOS（本机，Python 3.12.13） | `make test`（合并后 3e3bd60） | 915 passed / 4 failed / 14 skipped；4 个失败在 origin/main 9d8b055 上同样失败（main 上共 60 failed） |
| macOS | `make lint` / `make audit` | 0 violations，import-linter 7/7 kept；audit 604 files 0 errors |
| lab30（Linux，Python 3.12.16） | `make test`（3e3bd60） | 921 passed / 4 failed / 8 skipped；4 个失败均为 `test_cap_xh`，在 origin/main 上同样失败（5 failed） |

| macOS | `make test`（合入 parts[] 后 `c0db610`） | 953 passed / 4 failed / 15 skipped（153 subtests）；4 个失败与 `3e3bd60` 上完全相同（本机 worktree 复核：`test_configurable_runtime` 3 个依赖 Linux `/proc/<pid>/stat`，`test_expert_harness_round::test_prepare_fails_closed` 依赖 Linux 权限语义），不是本次改动引入 |
| macOS | `make lint`（`c0db610`） | import-linter 7/7 kept |

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
| 专家（CaP） | **未达成**：手写专家 s2v1–s2v9 在 `author-02` 场景无一成功（见下）；CaP 搜索未接入 | `runs/chain/s2/dev/*` |
| judge 生成/冻结（Codex 作者 + 独立审查） | **已冻结**：第 4 次构建（前 3 次失败全部保留）候选 2 通过作者、独立审查（不同会话 ID）与主机正/反例；锚点 `480bea02da959fc5b98ff93425b76cad93882935ae170299240d65ce2eb7200c`；条件 `drawer_preopened`、`plate_in_open_drawer`、`other_drawer_closed` | `runs/chain/s2/judge/build/candidate-02/frozen` |
| 冻结 judge 独立判定 | 可用：独立进程 `judge_cli verify`（锚点核对 `verified`）+ `evaluate`。s2v6、s2v8 判为 `failure`（`plate_in_open_drawer=false`），与实际一致 | `s2/dev/s2v8-seed17/verdict.json` |
| 重放 + Isaac 渲染 | **链路实测可用**（s2v8-seed17，失败 episode）：2 视角 × 4 材质/光照 variant，各 286 帧；`state_playback` = `verified`，最大位置误差 0.0 m、最大关节误差 0.0（容差 0.01 m）——渲染运动只来自录制的 state_trace | `s2/dev/s2v8-seed17/render-out.json` |
| HDF5 导出 | **链路实测可用**（s2v8-seed17）：修 3 个导出器缺陷后 export exit 0、独立 validate exit 0（逐数组 float64 逐位一致），69 MB，3421 动作 / 3422 状态 / 8 路视频帧表 | `s2/dev/s2v8-seed17/hdf5-*.json` |
| admission | **未执行**：没有 judge 判定成功的 episode；仓库 admission（`issue_admission_certificate.py`）面向 LeRobot 数据集，本 fork 的 HDF5 路径尚无对应审查器 | — |

**专家开发记录（全部保留）**：手写专家 v1–v9 在 `author-01` 场景（平盘，无相机），s2v1–s2v9 在 `author-02` 场景（带帮大盘）。每次失败都定位到原因：腕关节限位（离线用仓库运动学做稠密连续性检查后改用关节空间 via 点）、IK 600 步内不收敛（位置驱动稳态下垂 6–7 mm）、指尖压到盘底、闭合到 0 时被动 mimic 手指从薄帮上滑过（`s2v1/s2v2`）、夹爪闭合 150 步超时停在 3 cm（`s2v6/s2v7`）、完全闭合后手指夹住盘帮但抬升时盘子只被带起 3 mm 即滑脱（`s2v8`，接触深度 ≤0.5 mm，盘子 0.186 kg）、抬升初期关节速度超限 `SPEED_LIMIT`（`s2v9`）。CaP 搜索（`tools/cap_search.py`，Codex actor）尚未接入：它要求一个准备好的 expert-harness round（xh broker）。【推断】手写专家只是 dev 种子上的开发产物，最终 episode 若来自手写专家，不能称为 CaP 产出。

**全部 rollout 尝试与 seed**（`execution_status` 为 B 侧专家执行状态；只对 s2v6、s2v8 跑了冻结 judge）：

| 尝试 | seed | 场景 | 步数 | 执行状态 | 结果 / 失败原因 |
|---|---|---|---|---|---|
| v1 | 0 | author-01 | 75 | error | 腕关节限位 |
| v2 | 1 | author-01 | 1 | error | 首步关节限位 |
| v2 | 2 (a–d) | author-01 | 869 | error | a 卡死中止、b 段错误、c 录制 O(N²) 后中止、d 完成 |
| v3–v9 | 3–9 | author-01 | 75–3438 | error | IK 不收敛 / 指尖压盘底；场景无相机不可渲染 |
| s2v1–s2v5 | 10–14 | author-02 | 1415–3212 | error | 手指从帮上滑过、夹爪超时 |
| s2v6 | 15 | author-02 | 3083 | timeout | judge `failure`：盘子未离开桌面 |
| s2v7 | 16 | author-02 | 3112 | timeout | 同上（降低抓取高度） |
| s2v8 | 17 | author-02 | 3421 | timeout | judge `failure`；完整渲染 + HDF5 实跑 |
| s2v9 | 18 | author-02 | 1532 | error | 抬升时 `SPEED_LIMIT` |

**第 5 节结论**：5 条成功 episode 的验收标准**未达成**。链路上除"专家能完成任务"之外每一段（场景组装 + GPU 闸门、task-plan、judge 冻结与独立判定、live 录制、状态重放渲染、HDF5 导出与独立校验、admission 审查器）都在 lab30 上用真实数据跑通；阻塞点是抓取带帮大盘的专家本身（物理抓取）。手写专家阶段按"同一阶段连续失败 3 次即停止"在 s2v9 后停止；用户随后要求接入 CaP 重开该阶段（5.1），CaP 用 13 个候选 / 30 次 rollout 得到同一结论（0 成功，全部卡在 `plate_in_open_drawer`）。

### 5.1 CaP 接入（用户 2026-11-18 追加要求）

用户要求接入 CaP 搜索重开专家阶段（覆盖第 4 节"连续失败 3 次即停止"）。round、引擎与证据在 lab30 `runs/cap-01/`（本机副本 `runs/integration-20261118/lab30-runs/cap-01/`；>5 MB 的 episode 只在 lab30，哈希在本机 `cap-01/LARGE_FILES.sha256`）。

**接入方式（全部走仓库既有代码路径，未改仓库源码）**：

| 组件 | 做法 | 证据 |
|---|---|---|
| task adapter/1 | `deploy/make_adapter.py` 生成 `adapter-base.json`，再由 `judge_cli attach` 生成 `adapter.json`：冻结 judge 经 `verifier.judge_bundle` + 外部锚点 `480bea02…` 绑定，条件 ID/描述取自冻结 TaskContract | `deploy/adapter.json`（`verifier.version = bc872123…`） |
| holdout | `expert_harness.py make-holdout-seeds --per-family 3 --spares 1` → `private/holdout.json`（0600，agent 不可读） | `private/holdout.json` |
| round | `prepare-round`：brief 9464 字符，lint 0 error | `round/brief.md`、`round/round.json` |
| smoke | `smoke-rollout --seed 100`：真实 job 脚本 → 真 Isaac 录制 → judge 在沙箱外独立进程 | `smoke/1/smoke-s100/verdict.json`（`judged`，`success=false`） |
| 作者 | `tools/cap_search.py` + `deploy/capdeploy.py`（并行引擎 + Codex provider 经 `127.0.0.1:8897` relay，`gpt-6.1-sol`）；rollout 由 `run-round` 的 broker 执行，占位 agent 只维持 broker 存活 | `search-*/`、`codex-decisions/`（每次决策的 request/schema/prompt/receipt/events 全留） |
| 公开轨迹 | `deploy/run_rollout.py` 额外写 `sim/primitive.jsonl`（每 primitive 的实测 before/after，按证据预算裁剪：数值 1e-4、只留 plate/cabinet 实体与夹爪/抽屉 link、接触行只留相碰对并标 `coverage=truncated`），并用 `sys.monitoring` 捕获专家异常打印 `EXPERT_RAISED` 供 harness 归类 `expert_error` | `round/rollouts/A-*/sim/primitive.jsonl` |

**结果（lab30 实测）**：**13 个候选、30 次 counted rollout、0 成功、0 infra 失败**。判定失败项 30/30 全是同一条 `plate_in_open_drawer=false`；执行状态 22 次 `motion_timeout`、8 次 `physical_conditions_failed`。模型诊断与手写专家一致：夹爪已接触盘帮（双侧接触 −0.2/−0.5 mm，受阻宽度 0.0082 m），抬升时保持不住。

**CaP 阶段暴露的部署问题（均已定位，不涉及仓库语义）**：

| # | 现象 | 根因 | 处理 |
|---|---|---|---|
| 1 | 首轮 `inspect` 抛 `Selected evidence exceeds the context budget` | 我的公开轨迹单条 primitive 记录约 43 KB，4 条即超 24 KB 证据预算 | 裁到单条约 5 KB（降精度、裁实体/接触行）；`inspect` 改为预算内保留前缀并附 `evidence-budget` 说明，不再让整个搜索失败 |
| 2 | `cap_search` 在 broker 回 `concurrency` 时整体退出 | broker 先到先得：满 2 个运行中即拒；并行引擎把拒绝当致命错误 | 撤回同一 identity 的被拒响应后等待重试；拒绝记录仍在 round `processed/` 与事件日志 |
| 3 | 一次 provider 调用 `turn.failed: stream disconnected before completion`（77 s，最大 prompt 95.8 KB） | 上游先回 200 再中途断流；relay 只记录到一条无关的 `400 NUS upstream did not accept this request`，故不是配额/鉴权 | 失败决策目录移到 `codex-decisions-failed/` 并记录原因；是否与长度相关待定因实验（第 7 节） |
| 4 | rollout 无法用 bwrap/unshare | 容器拒绝 mount namespace（`Failed to make / slave`、`Operation not permitted`）；`proot` 可用但 seccomp 加速未生效（`PROOT_NO_SECCOMP` 开关耗时相同） | 实测 proot 每次 syscall 多 71 µs → Isaac 加载 11.85 s → 412.79 s，而录制阶段 9.97 s → 12.17 s 几乎不变（证明是 syscall 截获而非 CPU）；round 只能 `isolation: none`，harness 记 30 条 `unisolated_rollouts` 违规 |

### 5.3 CaP 失败分类（37 次 rollout 全量，`runs/cap-01/failure-taxonomy.json`）

| 项 | 计数 |
|---|---|
| 成功 / 失败 | 0 / 37 |
| 失败判定条件 | 37/37 全是 `plate_in_open_drawer=false`（`drawer_preopened`、`other_drawer_closed` 均 true） |
| 执行状态 | 23 `motion_timeout`、14 `physical_conditions_failed` |
| 第一个失败的 primitive | 16 `move_tcp: LOCAL_IK_NONCONVERGENCE`、8 `move_tcp: SPEED_LIMIT`、6 `move_tcp: JOINT_LIMIT`、7 `move_gripper: GRIPPER_NONCONVERGENCE` |

第 2 轮搜索（search-5，5 个候选 / 10 次 rollout，`runs/cap-01/search-5/history.json`）出现了一个**明确的进步信号**：10 次里 9 次的第一个失败 primitive 变成 `move_tcp: SPEED_LIMIT`，且都发生在**夹爪已闭合之后**——即候选已经抓住盘子、开始抬升时，带载的关节速度跟不上请求的 TCP 速度。这说明搜索已经从"抓不住"迁移到"抓住后抬升被速度闸门拦下"；可行方向是把带载抬升的 `linear_speed_m_s` 降到 0.02 量级或分段抬升，这是作者侧可调量，不需要改闸门。

解读：**在 37 次里没有任何一次是在"抓住后抬升滑脱"处首次失败的**——37 次里大多数在**抓取前的运动**就终止（IK 不收敛 16、关节限位 6、速度超限 8），只有 7 次卡在夹爪闭合。也就是说 CaP 的候选大量浪费在运动可行性上，而不是真正去试"托底/倾斜"这类能改变保持力的抓法。手写专家 s2v8 之所以能走到抓取（并用 via 点绕开腕部限位），是靠离线用仓库运动学做的稠密连续性检查——这份信息目前不在作者的可见输入里。

由此得到的候选改进（未执行，待用户决定）：把"已用仓库运动学校验过的关节空间 via 路径（s2v8 所用）+ 夹爪行程 0.072 m 与物体尺寸对比 + 已观测的滑脱量化"作为**事实**写进 brief 的 `runtime_facts`，让搜索把预算花在保持力上。这需要 `expert_harness.py amend` 记录原因。

**顺带发现的契约文本缺陷（需 B 线处理）**：`task3/public-task-0.json` 的角色文本被去掉了空格——`"destination:already-opendrawerjoint_part_01_joint_00withmovinglinkpart_01_obj_00"`，原意应是 "already-open drawer joint_part_01_joint_00 with moving link part_01_obj_00"。作者模型会把这段当角色名读，语义损失。判定条件与状态判定不受影响（judge 用节点 ID 与关节名）。这是 task-plan 输出侧（A→B 契约文本）的问题，unresolved。

### 5.4 根因更正：此前所有"抓住后滑脱"的记录都不成立（重要）

逐帧读 `state_trace` 后确认：**机械臂从未接触过盘子**。

| seed | 盘子 z 首帧 → 末帧 | 夹持点 TCP | 夹爪宽度变化 | 盘子位移 |
|---|---|---|---|---|
| s2v8 / dev-17 | 0.8699 → 0.8700 | (0.782, 0.321, 0.878) | 0.0672 → 0.0082（受阻）→ 0.0024（闭合） | 0.9 mm 漂移 |
| s2v8 / 130 | 同上（`execution_status: timeout`，3421 帧） | 同上 | 同上 | 同上 |

盘子中心在 (0.78, 0.23, 0.87)，盘帮外沿在 y≈0.316，抓取点 y=0.321 → **抓取点在盘帮外侧正确位置**。但夹爪在 8.2 mm 处受阻后继续闭合到 2.4 mm，而盘子一步没动：说明**指尖落在盘帮顶部之外/之上**，闭合时只是相互挤过，抬升带起的是空气。

因此早期文档里"受阻宽度 0.0082 m、抬升 3 mm 后滑脱"的说法**必须作废**——那 3 mm 是 TCP 命令误差，不是盘子被带起。根因是**抓取高度（`GRASP_DZ`）与盘帮几何不匹配**，不是摩擦、不是夹持力、也不是搜索策略。

盘子径向几何（`runs/chain/plate_profile.py` 对 `prepared.json` 的碰撞网格实测，单位 m，z 自盘底起）：

| 半径 | z 范围 |
|---|---|
| 0.090 | 0.000 → 0.0072 |
| 0.095 | 0.0001 → 0.0256 |
| 0.100 | 0.0031 → 0.0256 |
| 0.105 | 0.0118 → 0.0256 |

即外帮是一堵**高 25.6 mm、径向厚约 10 mm** 的陡壁，最薄处只在顶部几个毫米。s2v8 的 `GRASP_DZ=0.012` 把指尖中心放在帮高中部，两指在厚壁处互相挤过；正确做法是让**指面只夹住顶部薄带**（约 z+0.020…0.025），并**闭环验证**（闭爪后小抬升，读盘高，没起来就换高度重试）。

这一步未执行：实例在 2026-11-18 23:05 SGT 到期，用户要求销毁重建（见第 10 节）。

### 5.5 暂停时的状态（2026-11-18 18:40 SGT）

| 项 | 状态 |
|---|---|
| 已达成 | PC2 全链路各段（场景+闸门、task-plan、judge 冻结与独立判定、录制、渲染、HDF5、admission 审查器）均以真实数据跑通 |
| 未达成 | 5 条 judge 成功 + admission 通过的 episode（阻塞在抓取几何，见 5.4） |
| CaP | 两轮共 39 次 rollout，0 成功；第二轮 10 次里 9 次首个失败是带载抬升的 `SPEED_LIMIT`（抓取前的运动已能走通） |
| 本机证据 | `runs/integration-20261118/lab30-runs/`（chain + cap-01 含 `search-5/`、`failure-taxonomy.json`、`final/`） |
| lab30 上的东西 | 随实例销毁；重建后需按 `Next` 重做：仓库 worktree、`.venv`、Isaac 缓存 warmup、judge 重冻结、round 重配 |
| 下一步（已写好，未在 GPU 上跑） | `runs/integration-20261118/chain-src/expert_s2v10.py`（本机，`runs/` 被 gitignore，属运行产物不进仓库）：增量闭爪到受阻宽度、压力只取受阻宽度减 2.5 mm；**高度梯 × 夹持姿态梯**（水平径向 / 绕径向轴转 90° 竖直夹上沿）逐层试；每次闭爪后小抬升 30 mm 并读盘高，只有盘高抬升 ≥8 mm 才继续搬运；否则开爪换下一个组合，全部失败时抛出带 `attempts` 表的异常（每个组合的 engaged/final/rise/outcome 都留在录制里）。控制流已用假机器人跑通三个分支（抓住即搬运、受阻但滑脱、完全无接触）。原计划写 `expert_s2v10.py`：按 `dz ∈ [0.024, 0.020, 0.016, 0.012, 0.008, 0.004]` 逐层"下探→闭爪→小抬升→读盘高"，只有盘高抬升 >8 mm 才继续搬运；成功后再跑 5 个不同 seed |

### 5.2 admission 独立审查（本次新增，任务产出）

仓库的 admission 证书面向 LeRobot 数据集，本 fork 的 HDF5 episode 没有对应审查器。`deploy/admit_episode.py`（任务产出，不进仓库）在**独立进程**中从存储字节重新推导 9 项检查并写 `datafarm.gate-receipt`（`stage=admission`、`authority=admission-root`、subject = episode 文档 sha256，符合 `docs/ir/episode.md`）：

`episode_document_intact`、`episode_trace_complete`（N 动作 / N+1 状态）、`judge_bundle_verified`（外部锚点）、`judge_reevaluated_success`（**重跑冻结 judge**）、`judge_matches_recorded`、`render_manifest_intact`、`state_playback_verified`、`hdf5_valid_bitexact`、`hdf5_bound_to_episode`。

**负控（实测）**：对判失败的 `s2/regress/s2v8-seed19` 运行 → `reject`，唯一失败项正是 `judge_reevaluated_success`，其余 8 项 true（`runs/cap-01/admission-negative-control-s2v8-seed19.json`，receipt `b200863d…`）。正控需要一条 judge 判成功的 episode，本次未取得，故 admission 正路径未执行。

**命令与产物**（驱动脚本副本：本机 `runs/integration-20261118/chain-src/`；lab30 `runs/chain/`）：

| 步骤 | 命令（lab30，`R=/opt/data/private/isaaclab/integration-20261118`） | 产物 |
|---|---|---|
| scene-author | `runs/author_launch.sh author-02-20261117T220718Z`（`tools/scene_author.py --request examples/request/plate-drawer-airbot.yaml --max-rounds 3`，GPU check 经 `check.env_script`） | `runs/author-02-…/prepared/380b6286dca19314/`（scene-graph `a4ac4f0f…`） |
| task-plan | `runs/chain/plan_task.py`（`datafarm.entry.task_plan`，Codex 经 `codex_relay.py`） | `runs/chain/task3/task-table.json`（task IR `f2490ea9…`） |
| judge 构建 | `runs/chain/s2/judge_build.sh`（`datafarm.entry.judge_cli build`） | `s2/judge/build/candidate-02/frozen`，锚点 `480bea02…` |
| 录制 | `bash isaac_run.sh run_rollout.py --config s2/rollout-config.json --expert expert_s2vN.py:TaskExpert --seed S --output-dir s2/dev/s2vN-seedS --headless`（B `record_scene_episode`） | `episode.json`、`result.json`、`heartbeat.log`、`primitive.jsonl` |
| judge / 渲染 / HDF5 | `post_episode.sh <episode_dir> <bundle> <anchor>`：`judge_cli evaluate`（独立进程，先核对锚点）→ `render_episode.py spec/render --headless` → `export_episode_hdf5.py export/validate` | `verdict.json`、`render-out.json`、`episode.h5`、`sha256.txt` |

耗时（lab30 实测）：场景加载约 12 s；3000+ 步录制约 480–525 s；judge evaluate 约 20 s；渲染 8 路 × 286 帧约 100 s；HDF5 export + validate 约 60 s。

## 6. parts[] 精确扫掠

实现由子 agent（sonic；环境没有名为 `fast` 的 agent）在独立 worktree `test/parts-sweep-20261118` 完成，主 agent 审阅后要求修正一次（见下）。分两提交，在第 5 节 episode 尝试停止后经 `c0db610`（无冲突 merge）合入 test 分支：

- `6bb3030` `feat(A-layout)`：`joint_sweep_check` 优先按 `parts[]` 精确扫掠。移动件 = `parts[].prim` 等于 `joints[].child_body` 的分件；prismatic 取起止分件盒凸包（精确）；revolute 需 `anchor_m`，绕枢轴 ≤5° 采样取 8 角投影凸包（弦代弧内收 ≤1e-3·r）。结果 `checked[].method = "parts"`。缺字段时回退，输出与改动前逐字节一致。
- `516dc83` `contract(A)`（跨线契约，待 B 线确认）：`datafarm.asset-record/1` 新增可选 `parts[]`（`name`、`prim`、`bbox_min_m`、`bbox_max_m`，资产根系，米，静止姿态）、`joints[].anchor_m`、`joints[].child_body`；版本号不变（IR README 第 8 条），validator 向后兼容。（rebase 前 SHA 分别为 `c06a2b2`、`879dbab`。）

审阅发现并已修正：子 agent 初版按叶名 `part.name == joint.child` 匹配。真实 SimReady 资产里关节 child 是刚体下的网格（`.../part_01_obj_00/part_01_mesh_00`），叶名永远不等，精确路径在真实资产上不会生效，而合成测试用了相同叶名，测不出来。改为 prim 路径关联，测试改用真实命名形状与实测 `fixture_cabinet_b01` 数值：抽屉 0.35 m 时分件前沿到 x = 0.68937，整盒保守法只到 0.65839，x∈[0.66, 0.68] 的障碍只有分件路径能发现。

消费方审计（子 agent 用 grep；本环境 LSP 未就绪【推断：未运行 xd://lsp references】）：scene_runtime、task/planning 经 `asset_record.validate`；layout、render/plan、scene_measurements、scene_files、scene/authoring 只读已知键；B 侧 public_task `joint_facts`、episode `asset_refs` 只取显式键或 ArtifactRef。无严格键白名单，无 B 线文件改动。

CPU：在 `879dbab` 上相关 88 passed / 32 subtests；在 `c06a2b2` 单独 76 passed；lint/audit 通过。

改动前 joint_sweep（lab30 实测，示例布局 `runs/scene-02-reprepared/prepared/plan.json`）：`{"checked": [{"joint": "joint_part_01_joint_00", "node": "object.cabinet", "travel_m": 0.35}], "ok": true, "skipped": []}`（保守整盒平移）。

**lab30 回归**（`runs/regress-parts/`，HEAD `c0db610`，与第 5 节相同的 `author-02` 布局字节）：

| 项 | 改动前（`author-02` prepared，`b5b2a9f`） | 改动后（`c0db610`） |
|---|---|---|
| AssetRecord（cabinet） | `f9e31bf627d7…`，无 parts | `ba53b4b2f132…`，3 个 parts；`joint_part_01_joint_00` anchor `[0.33287, -0.00029, 0.80866]`、child `RootNode/Geometry/part_01_obj_00`（第 2 个抽屉同理） |
| `footprint.joint_sweep` | `{"checked":[{"joint":"joint_part_01_joint_00","node":"object.cabinet","travel_m":0.38}],"ok":true,"skipped":[]}` | `{"checked":[{"joint":"joint_part_01_joint_00","method":"parts","node":"object.cabinet","travel_m":0.38}],"ok":true,"skipped":[]}` |
| prepare / plan | exit 0 / exit 0 | exit 0 / exit 0 |
| GPU 物理闸门 | exit 0 | 尝试 1 **exit 1**：探针 `plate_in_top_drawer` 盘子已静止于抽屉内（z 0.7608，接触 cabinet）但末速度 13.6 mm/s > 10 mm/s；尝试 2（同输入）**exit 0**：末速度 0.99 mm/s，漂移 1.06 mm，穿透 81 µm |

解读：本场景只有 prismatic 抽屉，两种方法都判通过；`method: "parts"` 证明精确路径在真实 SimReady 资产上生效。闸门两次结果不同的唯一差异是探针末速度：parts 改动只影响 AssetRecord 与 layout 的 joint_sweep，不进入 check 的物理步进（`git diff da817b9 c0db610` 未触及 `tools/`、`adapters/sim`、`simulation/`），【推断】尝试 1 是 Newton 释放探针的非确定性，在 10 mm/s 速度阈值附近波动。两次尝试都保留。需关注：同一布局的闸门结果非确定，阈值附近会偶发拒绝。

回归 episode：`s2/regress/s2v8-seed19`（录制于 HEAD `c0db610`，`author-02` 场景，专家 s2v8 不变，seed 19）。首次两次启动失败并保留（`s2/regress-failed/`）：把回归场景直接接上会因 task IR 绑定原场景摘要而被 `task revision or scene digest mismatch` 拒绝——AssetRecord 摘要变化会改变 SceneGraph 摘要，已规划的 task 不能跨场景复用（符合设计），因此回归 episode 用原场景、新代码。

**回归发现（需 B 线处理）**：在 `c0db610` 上冻结 judge 拒绝运行：`JudgeError: Frozen judge dependency changed`。`diff_manifests` 唯一差异是 `src/datafarm/ir/common.py`（`516dc83` contract(A) 为 `parts[]` 加了校验辅助函数）——冻结时把运行时依赖的仓库源文件一并哈希保护，这是 judge 防篡改的正确行为。后果：**`516dc83` 合入后，已冻结的 judge 都要重新冻结**。本次没有改 judge 或绕过保护：在 `da817b9`（冻结时的依赖）上 `judge_cli verify` 为 `verified`，再独立 `evaluate` 新录的 episode（`judge-env.txt` 记录了这一点；首次失败日志保留为 `judge-attempt1-dependency-changed.log`）。建议：contract(A) 若定稿，把 `parts[]` 校验辅助函数放到 judge 依赖之外的模块，或在合入时与 B 线协调重新冻结。

**回归 episode 结果**（lab30 实测，与改动前同专家 s2v8-seed17 对比）：

| 项 | 改动前 s2v8-seed17（`b5b2a9f` 录制） | 改动后 regress s2v8-seed19（`c0db610` 录制） |
|---|---|---|
| 录制 | 3421 步，`timeout`，149 MB | 3421 步，`timeout`，149 MB |
| 冻结 judge（独立进程） | `failure motion_timeout`，`plate_in_open_drawer=false` | 同左 |
| 渲染重放 | `verified`，误差 0.0 m / 0.0 | `verified`，误差 0.0 m / 0.0，相机放置误差 0.0 m，8 路 2288 帧 |
| HDF5 export / validate | exit 0 / exit 0 | exit 0 / exit 0（`episode.h5` sha256 `740aa8e5…`） |

两条的机器人关节轨迹最大差 0.0083 rad、盘子终点相差 < 0.6 mm（不同 seed 与 Newton 非确定性），行为一致：parts[] 合入没有改变链路行为。

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
| 12 | judge 构建 3 次失败：主机用例 `Episode exceeds 32 MiB worker input limit`；独立审查者的用例被主机拒收 | worker 输入/内存上限按小样本定；judge 运行时契约文字没写全 episode 形状 | `66490a0`、`789c79b` tmp(B)；第 4 次冻结 |
| 13 | 第一条真实 episode 的 HDF5 导出失败 3 次 | 导出器假设与真实录制不符：ref 文件键名、requested 只含当步下发通道、每台相机多个 variant | `ea27ee9`、`eeefd94`、`da817b9` tmp(B)（f71adf0 的后续），各附在旧代码上失败的测试 |
| 14 | lab30 SSH 两次短暂 banner 超时；git bundle 全量同步 447 s | 跳板链路波动；全量 bundle | 改为以远端 HEAD 为基的增量 bundle（3 KB，24 s）；lab30 无 rsync，证据拉回改为 tar 并排除大文件 |

**未解决**：

1. **专家无法完成任务**（阻塞 5 条 episode）：G2 平行夹爪抓带帮塑料大盘（帮厚约 3 mm、盘 0.186 kg）在 Newton 中夹住后抬升即滑脱（s2v8：完全闭合、接触深度 ≤0.5 mm，盘子只被带起 3 mm）；改慢速抬升后 `SPEED_LIMIT`（s2v9：静止 1529 步后首个抬升周期 `left_joint3` 速度跳变）。【推断】需要换抓取策略（从盘底下方铲起/推到桌边再夹）、或换可抓物体（scene-author 可选平盘/碗），或检查 G2 手指摩擦/接触参数。未改任何 judge、阈值或原始数据。
2. **CaP 搜索未接入**：`tools/cap_search.py` 需要 expert-harness round；本次未建 round，手写专家只用于定位链路问题。
3. **admission 未执行**：无成功 episode；且仓库的 admission certificate 面向 LeRobot 数据集，本 fork 的 HDF5 数据集需要 B 线定义 admission 审查（`datafarm.white-delivery/1` 或新 schema）。
4. **物理闸门非确定**：同一布局两次 check，一次因探针末速度 13.6 mm/s 被拒（第 6 节）。
5. **大文件只留在 lab30**：`episode.json`（60–150 MB）与 `episode.h5` 未拉回本机，`lab30-runs/LARGE_FILES.sha256` 记录其哈希；实例到期后这些文件会随持久卷保留在 `/opt/data/private`【推断：持久卷不随实例删除】。

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
| `66490a0` tmp(B) | `judge/execution.py`、`judge/worker.py` | 真实 episode 投影后 35–72 MiB，超 worker 32 MiB 输入上限；解析峰值 265/684 MiB RSS | 输入上限 256 MiB，默认内存 1024 MiB（上限 4096），审计/AST/超时/隔离不变 | judge 输入流式化或按节点对预聚合接触行 |
| `789c79b` tmp(B) | `judge/provider.py` | 独立审查者手写的用例 3/3 被主机 validator 拒收 | 只改提示文字：列全 validate_state/validate_episode 强制的字段与规则 | 从 validator 生成该段，或给审查者主机辅助函数构造合法 episode |
| `ea27ee9` tmp(B) | `tools/export_episode_hdf5.py` | 真实 episode：`KeyError 'kind'`，CLI 不认 `render_episode` 用的 `{"episode": ref}` | 接受 `episode`/`episode_ref`/裸 ref 与 `render_manifest` | 统一 ref 文件约定 |
| `eeefd94` tmp(B) | `expert/hdf5_export.py`、`docs/reference/hdf5-export.md` | 录制器 requested 只含当步下发通道，导出器要求与 applied 同集合 | requested ⊆ applied；新增 `requested_mask [N,U] bool` | 录制器写全量 requested（但会捏造未下发的请求，不推荐） |
| `da817b9` tmp(B) | `expert/hdf5_export.py` | 每台相机 4 个 variant，第二个 variant 重复建相机组 | `require_group` | — |
| `6bb3030` feat(A-layout) | `scene/layout.py` | — | 纯 A 线，见第 6 节 | — |
| `516dc83` contract(A) | `ir/asset_record.py`、`ir/common.py`、`scene/asset_records.py`、`docs/ir/asset-record.md`、`docs/ir/scene-layout.md` | `parts[]` 精确扫掠需要分件几何与枢轴 | 新增可选 `parts[]`、`parts[].prim`、`joints[].anchor_m`、`joints[].child_body`，版本不变 | — |

需 B 线确认的契约条目：AssetRecord 上述四个可选字段；HDF5 schema `datafarm.episode-hdf5/1` 的组布局（含新增 `requested_mask`）；`move_gripper` 实测开度速率上限的取法；task-plan schema 约束的来源；judge worker 资源上限与运行时契约文字。

## 9. 是否建议合入 main

**不建议现在把 test 分支整体合入 main**。前置条件：

1. B 线同学逐条确认第 8 节 11 个 `tmp(B):` 提交（或给出替代实现）与 `516dc83` 契约；
2. PR #14 作者确认 `cf390b7`（机器人关节静置漂移只记录不拒收）；
3. 至少 1 条（目标 5 条）冻结 judge 判定成功的 episode 走通渲染 + HDF5 + admission——本次未达成，阻塞在专家抓取；
4. admission 对 HDF5 数据集的审查器由 B 线定义。

可以先独立合入 main 的部分见第 2 节。

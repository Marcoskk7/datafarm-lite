# A 线：SimReady 资产检索与场景组装

把 NVIDIA SimReady 资产（Hugging Face `nvidia/simready-assets`）拉下来，结合机器人包和布局说明，产出 `ir.asset-record` 与 `ir.scene-graph`（字段见 [asset-record](../ir/asset-record.md)、[scene-graph](../ir/scene-graph.md)）。仿真固定为 Isaac Lab 3.0 + Newton/MJWarp。

| 代码 | 作用 |
|---|---|
| `src/datafarm/adapters/assets/simready.py`, `tools/simready.py` | 检索索引、探测资产、下载并按 BOM 校验 sha256；固定数据集 commit |
| `src/datafarm/adapters/sim/usd_assets.py` | USD 检查（包围盒、刚体、关节、质量、材质），CoACD 凸包碰撞包装层，铰接资产的世界锚定，机器人基座碰撞包围盒 |
| `src/datafarm/scene/asset_records.py` | 由检查结果生成 AssetRecord；物理量标 `assumed` 并给随机化区间 |
| `src/datafarm/scene/layout.py`, `kinematics.py` | 按 `on` 关系解位姿、占地检查、多起点 IK 可达性、生成 SceneGraph |
| `src/datafarm/adapters/sim/isaac_scene.py` | Isaac Lab 场景配置、写入初始关节状态、静置（主动/被动关节规则）与穿透检查、放置探针、导出测量关节值 |
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
- 关节分**主动**（`active_joints` 里列出的驱动关节）和**被动**（其余铰接关节）。机器人自身关节的静置漂移只记录在 `settle.robot_joint_drift_rad` 与 `measured_joint_state`，不判定：位置驱动在重力下的稳态下垂（lab30 实测 `left_joint3` 0.0127 rad）由机器人包的增益决定，布局改不了，main 上的 `check` 也只记录它。主动关节漂移上限 `--joint-tol-active`（`--joint-tol` 是同一参数的别名）；被动关节没有固定漂移上限，只要求静置后测量值仍在关节限位内、且该节点没有新增接触对（`added_contacts`），`--joint-tol-passive` 只决定什么时候报告"动了"。`check.json` 的 `settle.measured_joint_state` 是按节点、按关节名的静置后测量值，`settle.new_contacts` 是相对初始状态新增的接触对及其深度；顶层 `initial_joint_state` 是声明的意图。下游 episode 的初始状态取测量值。

退出码为 0 当且仅当静置稳定（含主动/被动关节规则）、穿透、探针、可达性、占地全部通过。`WORK/store/` 是内容寻址的证据；主机路径只出现在 `prepared.json`，不会进入 IR。

## 加载已组装的场景

`check` 之后，任何要在 Isaac 里重建这个场景的地方（B 录 episode、A 渲染回放）都走同一个函数 `datafarm.adapters.sim.scene_runtime.load_scene(store, scene_ref, prepared, *, asset_roots)`，不各自拼 `build_scene_cfg` 的参数。需要的输入只有：

| 输入 | 来自 | 作用 |
|---|---|---|
| `WORK/store/` | `prepare` + `check` | SceneGraph、AssetRecord、asset bundle、runtime profile、physics ledger、碰撞报告，全部按 sha256 取 |
| SceneGraph 的 ArtifactRef | `check.json` 的 `scene_graph`，或 Episode/RenderSpec 里引用的那一个 | 节点、位姿、`joint_state`、机器人节点与基座位姿、`runtime_profile` 引用 |
| `prepared.json` | `prepare` | 每个 `asset_id` 的主机路径：`wrapper`（碰撞包装层）、`source_root`（SimReady 取回目录）、机器人 `package`/`package_asset`；以及 `body` 和 `record` 引用 |

不需要 `plan.json`：它的 `nodes` 与 `robot_base_pose` 在 `check` 时原样写进了 SceneGraph（节点位姿、`joint_state`、`semantic_tag: robot` 节点），`plan.json` 只服务于组装过程本身（可达性、占地）。`check` 自己仍从 `plan.json` 加载，因为那时 SceneGraph 还不存在，它是 `check` 的产物。

`load_scene` 是纯 CPU 的解析与校验：SceneGraph 过 IR 校验；`prepared.json` 的资产集合与每个 `record` 引用必须和 SceneGraph 一致；asset bundle 里每个文件（SimReady 成员、碰撞包装层、机器人层）按 sha256 重新哈希；碰撞包装层必须是 AssetRecord 的碰撞报告绑定的那一份；机器人 URDF/USD 必须是 AssetRecord 里的字节。任何不一致都抛 `ValueError`，不回退。它返回 `graph`、`records`、`specs`（`body`）、`wrappers`、`asset_refs`。之后：

- `scene_runtime.scene_config(resolved, robot_cfg, drawer_damping=...)` 生成 `InteractiveSceneCfg`（机器人节点取 SceneGraph 里的位姿）；`robot_cfg` 由机器人包的 `robot_profiles.make_robot_cfg(runtime_profile['robot_profile'], 'newton_mjwarp')` 给出，`drawer_damping` 取 `runtime_profile['articulated_fixture_drive']['damping']`。
- `scene_runtime.initialize(...)` 在调用方的 reset 之后调用 `isaac_scene.apply_initial_state` 写入 `joint_state` 并按给定步数静置（B 录制用）；渲染回放逐帧写状态、不步进物理，因此只 `sim.reset()` + `scene.reset()`，不调用它。
- `scene_runtime.asset_roots(prepared)` 给出 `load_scene` 要的 `asset_roots`（取 `prepared.json` 的 `source_root`）。没有 `source_root` 的旧 `prepared.json` 会被拒绝，重跑 `prepare` 即可。

主机路径不进 IR：IR 要能在另一台机器、另一个缓存目录下按内容校验，路径既不可移植，也证明不了字节（`portable` 校验会拒绝路径字段）。所以路径只在部署本地的 `prepared.json`，正确性由 IR 里的 sha256 保证。

调用方：`tools/render_episode.py render`（渲染回放）、`datafarm.entry.scene_rollout.record_scene_episode`（B 录制，见 [scene-rollout](scene-rollout.md)）。

## 一条命令验证全链路

`tools/verify_a_line.py` 把上面三段和检索、校验串起来，逐关给结论，报告写进 `WORK/verify-a-line.json`：

```bash
python tools/verify_a_line.py --spec examples/scene/plate-drawer-airbot.json \
    --cache /data/simready-cache --robot-package /data/robot_assets_isaaclab3_validated_20261113 \
    --tests-python /path/to/pytest-venv/bin/python --work WORK
```

关卡：`env`（pxr/coacd/isaaclab）→ `cache`（按 manifest 重哈希）→ `index`（联网检索与 probe，`--offline` 跳过）→ `prepare` → `plan` → `check` → `graph`（SceneGraph 过 IR 校验器）→ `source`（跑完后源资产字节未变）→ `tests`。Isaac venv 里通常没有 pytest，`--tests-python` 可指向另一个解释器。本机 lab30 的完整记录见 `verification/a-line/`。

**缓存必须保持原始字节。** 变体选择写在 session layer，碰撞与修补只进 `WORK/collision/`；`source` 关就是为抓写回缓存这类来源绑定破坏。

## 由 agent 生成布局

`tools/scene_author.py` 让 coding agent 从 `request.yaml`（`datafarm.request/1`）写出 `scene-layout.json`，主机每轮跑确定性关卡并决定是否接受。agent 只负责产出，它的自述不参与判定。循环在 `datafarm.scene.authoring.author_layout`（纯逻辑，agent 会话、prepare、机器人描述符都由调用方注入），接线在 `datafarm.entry.scene_author_cli`。

```bash
cp configs/scene-author.template.json my-agent.json   # 选后端、模型、每轮墙钟与预算
python tools/scene_author.py --request request.yaml --work WORK \
    --cache /data/simready-cache --robot-package /data/robot_assets_isaaclab3_validated_20261113 \
    --agent-config my-agent.json --max-rounds 5 --prepare-python /path/to/isaac/python
```

每一轮：

1. 拼 prompt：角色指令（`prompts/roles/scene-author.md` 及目录里登记的文档）+ request 原文 + 上一轮的主机诊断（逐行）。
2. 在 `WORK/workspace/` 里起一个新的 agent 会话（不 resume），记一条 `datafarm.agent-call/1` 到 `WORK/agent-calls.jsonl`，transcript 在 `WORK/sessions/`。agent 只能用 `tools/simready.py search/probe` 并读 `workspace/host/` 下主机写的证据（`asset-records.json`、`plan.json`、`last-attempt.json`）。
3. 读 `workspace/scene-layout.json`。缺文件、不是 JSON、schema 不对都记成诊断，不中断循环。文件原样留存在 `WORK/attempts/<n>/scene-layout.json`。
4. 资产集合（`assets[]` 中 prepare 读的字段 + 机器人）变了才由主机 prepare：缓存缺的资产先 `tools/simready.py fetch`，再跑 `tools/scene_assemble.py prepare`，产物在 `WORK/prepared/<集合摘要>/`。prepare 失败也是诊断。
5. 跑符合性检查（`check_conformance`，有 AssetRecord 所以是完整检查）和 `run_plan`（占地 + 运动学可达）。`plan.json` 写在对应的 prepared 目录里。
6. plan 通过后用 Isaac Lab 解释器跑物理闸门 `tools/scene_assemble.py check --spec WORK/attempts/<n>/scene-layout.json --work WORK/prepared/<集合摘要>/ --headless`。布局是本轮留存的字节（与 `plan.json.spec_sha256` 一致），工作目录与 prepare 相同。超时（默认 1500 s）、解释器起不来、退出码不是 0/1、没有 `check.json` 都是可回给 agent 的诊断。`check.json` 与 `check.log` 留在 prepared 目录。
7. 追加一行到只追加的 `WORK/attempts.jsonl`：轮次、布局 sha256、prepare 结果、符合性报告、plan 是否通过及诊断、`check` 块（`ran` / `ok` / `exit_code` / `summary`（漂移、穿透、探针、新接触对、测量关节值）/ `diagnostics` / `check.json` 路径）、agent 调用 id、时间戳、request sha256。

接受条件：符合性 `ok` 且 `complete`，plan `ok`，并且 check `ok`。接受后布局复制到 `WORK/scene-layout.json`；到 `--max-rounds` 仍未接受就停，报告写明。报告是 `WORK/scene-authoring-report.json`（`datafarm.scene-authoring-report/1`）。同一个 `WORK` 再跑会从下一轮续跑（已接受则不再起会话）；换了 request 会被拒绝。退出码：0 接受，1 未接受，2 配置错误、没有可用沙箱、解释器缺失，或可见性探针发现泄漏（agent 未启动）。

**容差只有一个来源**：agent 配置的 `check` 节（模板见 `configs/scene-author.template.json`）。`check.python` / `check.env_script` / `check.timeout_s` 与 `settle_s`、`probe_s`、`drift_tol`、`speed_tol`、`joint_tol_active`、`joint_tol_passive`、`penetration_tol`、`nconmax`、`njmax` 全部由主机显式转成工具参数；未知键被拒绝。CLI 的 `--check-python`、`--check-env-script`、`--check-timeout-s` 覆盖配置，`--check-python` 缺省依次取 `check.python` 和 `$ISAAC_PYTHON`。解释器路径只取绝对路径、**不解析符号链接**（venv 的 `bin/python` 是指向基础解释器的链接，跟过去就没有 isaaclab）。`--headless` 总是传给工具。`check.env_script` 非空时命令是 `bash -c 'source "$0" || exit $?; exec "$@"' <script> <python> tools/scene_assemble.py check ...`：脚本里 export 的变量（Vulkan 的 `VK_ICD_FILENAMES`、`LD_LIBRARY_PATH`）进入 exec 后的解释器。

### lab30 实测配置（2026-11-18，test/integration-20261118）

| 参数 | 值 |
|---|---|
| `check.python` / `--check-python` | `/home/isaac/IsaacLab30/.venv/bin/python` |
| `check.env_script` / `--check-env-script` | `/home/isaac/isaaclab30_env.sh`（不 source 时 Kit 报 `Failed to create any GPU devices`） |
| `--prepare-python` | 同 `check.python`（pxr + coacd） |
| headless | 主机总是传 `--headless`；在 `DISPLAY`/`XAUTHORITY` 均未设置的 SSH 会话里实跑通过 |
| `--cache` / `--robot-package` | `/opt/data/private/isaaclab/simready-cache`、`/opt/data/private/isaaclab/a-scene/runtime/robot_assets_isaaclab3_validated_20261113` |
| `--endpoint` | `https://hf-mirror.com` |

首次实跑修掉的三处（均附测试）：USD body 路径进 check 证据被可移植校验拒绝（加 `usd:` 前缀）；`bash -c` 包装里多余的 `shift` 把解释器从 argv 丢掉；`resolve_check_interpreter` 解析符号链接后离开 venv。

**没有物理验证就不接受。** 配不出 check 解释器时工具在启动任何 agent 会话之前以 2 退出。唯一例外是显式 `--kinematic-only`：此时只按符合性 + plan 接受，`attempts.jsonl` 与报告都写 `check.mode: "kinematic-only"`，报告的 `not_verified` 保留 check 未跑，`next_step` 给出 GPU 主机上要跑的 check 命令。物理闸门通过时 `verified` 含 check，`next_step` 为空。可达性只是运动学 IK，不做避碰规划，也不判断抓取是否可行。

**隔离。** 每一轮 agent 会话都跑在 OS 沙箱里（`datafarm.entry.scene_author_sandbox`，语义同 expert harness 的 allowlist）：guest 从空根开始，只出现系统目录、`/dev`、`/proc`、后端私有 `/tmp`；`WORK/workspace/` 与本轮私有 HOME（`WORK/sandbox/rounds/<n>/home/`）可写；角色允许的仓库文件快照（`WORK/sandbox/repo/`：两份 IR 文档、`configs/scene-roles.json`、`tools/simready.py`、`src/datafarm/kernel` 及其 import 闭包）、跑 `simready.py` 的解释器前缀、agent CLI 程序、`--cache`（SimReady 缓存）与 `--robot-package` 只读可见——角色指令让 agent 跑 `tools/simready.py search/probe` 并信 `host/asset-records.json`，所以这两处必须看得见。其余都不存在：仓库本体（`examples/`、`runs/`、`verification/`、`.git`、`prompts/`、`src/`、`tools/`、`configs/`）、`WORK` 下的 `sessions/`、`attempts/`、`prepared/`、往轮 HOME、真实 HOME 里的 agent 配置与凭据目录。缓存/机器人包只有在**不**传给构造器（因而留在 `hidden` 里）时才进遮蔽清单：绑定与“断言不存在”互斥。

每轮启动 agent 之前，在同一个沙箱里跑探针（`datafarm.adapters.sandbox.run_probe`）：每个遮蔽路径都必须不可见（先试 `umount` 再读），工作区必须可写。任何泄漏或工作区不可写都失败关闭——本轮不启动 agent，工具以 2 退出，探针 JSON 落在 `WORK/sandbox/rounds/<n>/isolation-probe.json`（spec 在 `sandbox-spec.json`）。调用记录 `extra.isolation` 带同一份摘要（`mode`、`base`、`guest_root`、`ok`、`degraded`、`ro_soft`、`readonly_enforced`、`leaks`、`writable_readonly`、spec/probe 相对路径；`mode: none` 时是显式退出字符串）。

默认 `sandbox.mode = bwrap`，回退 `proot`（`unshare` 会把 allowlist 退化成整个仓库可见的 denylist，所以不接受）；都不可用就失败关闭，除非在 agent 配置里写 `{"sandbox": {"mode": "none"}}` 显式退出（调用记录写 `isolation: none` 与 opt-out 说明）。**proot 没有只读 bind**：未切换用户时（`ro_soft`）只读目录实际可写，摘要如实写 `ro_soft: true` 且 `readonly_enforced: false`，只做遮蔽与可写性判定，绝不声称只读强制；bwrap 下为真实只读 bind。

角色指令禁止 agent 读 `examples/scene/` 下手写的布局：那是另一个任务的产物，照抄会让它绕开从 request 推导的过程，测出来的只是复制能力。格式以 `docs/ir/scene-layout.md` 为准，它自带的片段足以说明文档形状。现在这条有沙箱兜底：仓库本体不在 guest 里，而不只是“叫它别读”。

## 限制

- 物理量（质量、摩擦、恢复系数、抽屉阻尼）是 SimReady 作者值或假设值，标为 `assumed`，不是测量值。
- 可达性只算运动学，不检查手臂和柜体或桌子的碰撞，也不检查抓取姿态是否可行；这些交给 B 线的规划与判定。
- 占地检查只考虑平放物体（只有偏航），也不检查放在地面上的东西之间是否重叠。
- 机器人包许可证未知，记录为 `unknown`。

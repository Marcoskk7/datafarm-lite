# 使用 USDCraft 生成物体资产

DataFarm 需要为真实 episode 中看到的物体提供仿真资产。[USDCraft](https://github.com/NVlabs/USDCraft)（[论文](https://arxiv.org/abs/2610.11322)）让编码 agent 将物体写成 Python 程序，然后将其导出为带关节的 URDF 和 USD 模型。它是可选的，位于自己的仓库中；adapter 调用其命令行，不导入其任何代码。

## 安装

USDCraft 需要 Linux 或 macOS、[`uv`](https://docs.astral.sh/uv/)、Python 3.12（由 `uv` 提供）以及 Node.js ^20.19 或 >=22.12。将其放在自己的环境中，与 simulator 分开。adapter 使用 commit `46fec18` 测试过：

```bash
git clone https://github.com/NVlabs/USDCraft.git && cd USDCraft
git checkout 46fec18
uv sync --frozen
uv run --frozen usdcraft setup
```

adapter 使用 USDCraft 的 Codex workflow，因此一个已认证的 `codex` 必须在 `PATH` 上，或由 `USDCRAFT_CODEX_BIN` 指定。它不使用 USDCraft 的 API-key workflow。将 `USDCRAFT` 绑定设置为该 checkout，然后检查安装：

```bash
datafarm doctor --profile usdcraft
```

`doctor` 检查 `uv`、`codex` 以及 `usdcraft/cli.py` 是否为已测试的版本。`plan.json` 在 `upstream` 中记录 checkout 的 commit；允许使用其他 commit，此时 `tested` 显示它不是这里测试过的那个。

## 描述实际实例

将 `examples/usdcraft.json` 复制到你的 deployment 配置中。它的 `checkout` 使用 `USDCRAFT` 绑定，绝对路径也可以。观测路径应当是绝对路径，以便被监督的进程看到相同的文件。保存一份观测 JSON：

```json
{
  "object_id": "observed-cabinet",
  "image": "/data/episode/evidence/cabinet-front.png",
  "description": "The observed cabinet has one left-hinged door and a recessed handle. Preserve its visible proportions and the handle contact space. Width 0.45 m is inferred from the robot reference, not measured. Interior shelves are unobserved; do not add task-critical hidden structure.",
  "context_files": ["/data/episode/evidence/cabinet-open.png", "/data/episode/evidence/object-observations.json"]
}
```

上下文文档应标明源视频、视角、帧或时间、裁剪坐标、比例来源、观测到的关节结构和未解决的属性。只有 `image` 被传给单图 CLI；额外文件是供后续比较的来源证据。若对应有 mesh，则加上 `mesh`，并可选加 `up_axis`、`front_axis` 和 `meters_per_unit`。这些选择 Reconstruction；没有 mesh 时路线为 Generation。将外部 mesh 纹理和 buffer 放在一起。图像生成是推断出的资产，并不证明已恢复真实隐藏几何或质量。

```bash
python tools/build_usd_asset.py plan --config /data/deployment/usdcraft.json \
  --observation /data/task/objects/cabinet/observation.json \
  --output /data/task/objects/cabinet/build-001
python tools/build_usd_asset.py run --work /data/task/objects/cabinet/build-001 --task-work /data/task
```

`plan` 不调用模型。`run` 不经 shell 执行已记录的 argv，使用 `generation-codex` 或 `reconstruction-codex` 接口并带 `--direct --keep-workspace --json`，并保留失败的输出。对于长时间构建，通过 run ledger 启动 run 命令并监控其 worker 和输出；不要从轮询 agent 反复启动它。可选 timeout 会终止子进程组；默认不施加。

`--task-work` 将构建绑定到一个 task：它在 task 的成本账本中预留一个持久的 external-usage ID，记录未知的后代成本（绝不为零），并附加一个由 `status_file` 支撑的 `external_jobs` 条目。supervisor 在 transport 完成后被唤醒。没有它时，独立运行不声称覆盖 task 预算。

## 保证

1. 当观测指定了 `mesh` 时，构建路由到 Reconstruction，否则路由到 Generation，并对每个输入做哈希。输入改变会拒绝运行。
2. 一旦 `execution.json` 存在，构建绝不会被重新提交：失败、中断或未知的执行按原样返回。并发的调用方会以 `BuildLocked` 失败。
3. 在绑定 task 的形式中，构建锁在 job 注册、预算预留、执行和状态写入期间一直持有。强制执行的 task 预算在启动前被拒绝。
4. 只有当工具报告 `<mode>_ready`（来自 `codex-cli`）、模型文件位于规范记录之内（相对的 `model_path` 针对 checkout 解析）、URDF 含 link、每个 mesh 都是 materialization 内的相对路径、且导出闭包没有外部符号链接时，导出才被绑定。结果状态为 `exported-unqualified`，绝不是 task 完成。`build_usd_asset.py run` 仅在该状态下 exit 0。

代码：`src/datafarm/simulation/usdcraft.py`、`tools/build_usd_asset.py`。

## 数据模型

- **Deployment 配置**（`examples/usdcraft.json`）：`checkout`、`model`、`reasoning`、`launcher`（默认 `["uv", "run", "--frozen", "usdcraft"]`）、`timeout_seconds`（默认 `null`）。`model` 和 `reasoning` 没有默认值。
- **观测：** `object_id`（小写 slug）、`image`、`description`、可选的 `mesh`、`up_axis` 和 `front_axis`（`±X/±Y/±Z`）、`meters_per_unit`（>0）、`context_files`。
- **构建目录：** `plan.json`（`datafarm.usdcraft-plan/1`：mode、argv、输入哈希、`qualification: unverified`、成本 `unknown`）、`execution.json`（状态 `starting` → `running` → `returned`/`failed`/`interrupted` → `exported-unqualified` 或 `invalid-export`）、`stdout.json`、`stderr.log`、`asset.json`（`datafarm.object-asset/1`：record ID、URDF、artifact 哈希、joints、`required_next_step`）、`.lock`。绑定 task 的运行会添加 `job-status.json`，以及 `external_jobs` 条目（`kind: observed-object`），写入 task 的 `state.json`。

## 使用资产

刚性物体导出一个 link 且没有 joint，因此 SAPIEN 的 `load()` 会以 "URDF contains multiple objects" 拒绝它；应使用 `load_multiple()`，它将其作为 actor 返回。在目标 simulator 中加载 URDF，并将名称、坐标系、单位、固定基座选择和相关的关节范围映射到生成的 task scene 中。不要假设 USD 物理设置会在 simulator 之间迁移。验证实际的关节运动、碰撞腔体、抓取和接触面、支撑和沉降。将新的 simulator 图像与真实视角比较，并保留证据和导入设置。

`exported-unqualified` 仅表示该资产已可供检查。task author 收集 simulator 资格探针，然后通过 `state.review_requests` 以源哈希排队一次独立评审。独立 grounding 评审仍然必需。没有任何东西会被自动采纳或在物理上认证。

## 失败行为

改变的输入需要新的版本化 plan。非零 exit 会连同 stdout 和 stderr 保留为 `failed`。无效导出会连同原因保留为 `invalid-export`。中断或过期的 `starting` 或 `running` 记录按原样返回，需要操作员核对。`external_jobs` 中没有执行记录的 job ID 会被拒绝（"reconcile before resubmission"）。

## 限制

- 在已测试的 commit 上，Generation 和 Reconstruction 各自通过 adapter 配合 Codex 端到端运行过一次，SAPIEN 加载了两个导出的 URDF。Isaac Sim 导入和 USDCraft 的 API-key workflow 尚未测试。
- 在 `model.py` 中设置的摩擦没有通过 URDF 到达 SAPIEN：一个以静态摩擦 0.40 编写的刚性鼓加载时用的是 SAPIEN 默认的 0.3。在 task scene 中设置接触材质。
- 导出的资产从不会被 adapter 认证为 simulator 合格，且生成是推断几何。
- 只有 `image` 到达单图 CLI；`context_files` 仅为来源证据而做哈希。
- 在与 task 的预算策略核对上游调用记录之前，成本保持 `unknown`，且无法满足强制预算。adapter 既不强制执行美元上限，也不把原生登录视为零成本使用。
- 加载 `model.py` 会执行 Python，因此 adapter 收集元数据但不导入它。package 或远程 mesh URI 需要显式实现的 resolver。
- `build_lock` 是一个建议性的 `flock`，作用于 `<build>/.lock`，某些网络文件系统不跨主机遵守它。

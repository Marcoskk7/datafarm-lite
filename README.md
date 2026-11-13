<div align="center">

# PyRUA-DataFarm：LLM Agent 作为高性价比遥操作者，产出可直接用于真机的机器人数据

Kewei Zhang<sup>1,*</sup>, Juncheng Ma<sup>1,*</sup>, Guodong Lv<sup>1</sup>, Duomin Wang<sup>2,†</sup>, Feixiang Ruan<sup>1</sup>, Ruiyang Si<sup>1</sup>, Chuanrui Zhang<sup>2</sup>,
Yuxuan Chen<sup>1</sup>, Hao Chen<sup>1</sup>, Yichen Liu<sup>1</sup>, Yintianrun Zhang<sup>1</sup>, Yufan Deng<sup>1</sup>, Enze Xie<sup>2,†</sup>, Daquan Zhou<sup>1,†</sup>

<sup>1</sup>Peking University &nbsp; <sup>2</sup>NVIDIA &nbsp; <sup>*</sup>Equal contribution &nbsp; <sup>†</sup>Project lead

[![arXiv](https://img.shields.io/badge/arXiv-2611.05320-b31b1b.svg)](https://arxiv.org/abs/2611.05320)
[![Project page](https://img.shields.io/badge/project-page-8b1515.svg)](https://dagroup-pku.github.io/PyRUA-Lean/DataFarm/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

</div>

> **datafarm-lite** —— [PyRUA-DataFarm](https://github.com/DAGroup-PKU/PyRUA-Lean) 在提交 `d788388`（MIT）处的 fork。本 fork 接收 YAML 请求而非真实标定 episode，在 Isaac Sim 中构建场景并使用 Isaac RTX 渲染。它产出 HDF5 episodes，**并非可直接用于真机的数据**。下方保留上游文档以供参考；其中描述真实 episode grounding、视频模型渲染或 pi0.5 导出的部分已不再适用。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/paper/teaser-dark.png">
    <img src="assets/paper/teaser.png" width="100%"
         alt="PyRUA-DataFarm at a glance. Top: on five real-robot tasks, policies trained only on farm data reach 93 successes in 100 trials against 84 for teleoperated data, and 87 against 46 under an unseen tablecloth and coloured light. Bottom left: real, simulated and rendered head views of the five tasks. Bottom right: the average cost per episode falls to $1.06 at 100 episodes and towards the marginal $0.80, below teleoperation in North America and China">
  </picture>
</p>

**DataFarm** 让编码 agent 承担遥操作者的工作。根据一个数据请求以及机器人在其场景中的**一个标定 episode**，agent 在仿真中重建任务，再由一个以真实图像为条件的视频模型将每次 rollout 渲染为照片级视频。其结果就是*可直接用于真机的数据*：同步的多相机视角，用精确动作打标，策略可直接在其上训练。


https://github.com/user-attachments/assets/e8ea2b91-7e17-48fe-b9a8-e1ebeccad789



## 目录

- [工作原理](#工作原理)
- [结果](#结果)
- [设计原则](#设计原则)
- [安装](#安装)
- [快速开始](#快速开始)
- [复现论文](#复现论文)
- [仓库结构](#仓库结构)
- [文档](#文档)
- [限制](#限制)
- [引用](#引用)
- [许可证与致谢](#许可证与致谢)

## 工作原理

<p align="center"><img src="assets/paper/overview.png" alt="Overview of DataFarm: survey the field from one calibration episode, build the task in simulation, render real appearance, then inspect and harvest the dataset" width="100%"></p>

<p align="center"><img src="assets/paper/pipeline.png" alt="The DataFarm pipeline on one episode: grounding G, task contract T and expert C, episode E, render O, admission q, on top of the shared capability layer K" width="100%"></p>

agent 构建五个中间表示：grounding *G*，其中每个量都按其已知方式标注；任务契约 *T* 与专家 *C*，它们针对一个隐藏的 judge *J* 编写；仿真 episodes *E*；渲染观测 *O*；以及准入记录 *q*。代码强制冻结 judge、对每次 provider 请求的检查、交付时内容绑定的评审记录以及训练前的证书。编写时的角色分离，以及把拒绝返回到引起它的阶段，是指令要求的。能力层 *K* 由所有任务共享；其上的全部内容都是为当前任务编写的。

| 阶段 | 产出 | 说明 |
|---|---|---|
| 能力层 | 所有任务共享的角色指令、skills 和规则，编译为显式、哈希固定的 agent 上下文 | [capability-layer](docs/reference/capability-layer.md) |
| Agent 运行时 | 持久编排、独立评审会话、崩溃恢复 | [agent-runtime](docs/reference/agent-runtime.md) |
| Grounding | 场景、相机、真实参考，以及一个把每个物理量标注为 measured、fitted、assumed 或 unknown 的台账；缺失的物体资产可以用 [USDCraft](https://github.com/NVlabs/USDCraft) 生成 | [grounding](docs/reference/grounding.md), [usdcraft](docs/reference/usdcraft.md) |
| 仿真器资格认证 | 为任务 skills 认证的仿真器变体 | [simulator-qualification](docs/reference/simulator-qualification.md) |
| 任务契约与专家 | 契约、在专家存在之前冻结的 judge，以及在沙箱中开发并在 held-out 初始状态上评分的专家 | [expert-harness](docs/reference/expert-harness.md) |
| 渲染契约 | Sim-to-real 或 out-of-distribution 意图、真实参考，以及对每个发往视频模型的请求的检查 | [render-contract](docs/reference/render-contract.md) |
| 渲染 | 来自 MiniMax-H3 control hybrid（默认）或 Seedance 的照片级视频。 | [rendering](docs/reference/rendering.md) |
| 验证与准入 | Render verifier、突变校准、独立评审、交付检查、准入证书 | [render-verifier](docs/reference/render-verifier.md), [delivery and admission](docs/reference/delivery-and-admission.md) |
| 策略训练 | 使用 EEF 或 delta-EEF 动作、从已准入数据进行的 pi0.5 训练；absolute-joint 模式存在于 launcher 中，但需要一个带有 joint 契约和 receipt 的数据集，而随附工具都不构建这类数据集 | [policy-training](docs/reference/policy-training.md) |
| 成本控制 | 一个让未知成本保持未知的全任务预算台账 | [cost-control](docs/reference/cost-control.md) |
| 能力演化 | 由冻结 evaluator 评判的、反馈驱动的能力层改进 | [capability-evolution](docs/reference/capability-evolution.md) |
| 运行台账与批量 supervisor | 每次被请求 run 的持久记录，以及有界的逐项尝试 | [run-ledger](docs/reference/run-ledger.md), [batch-supervisor](docs/reference/batch-supervisor.md) |

## 结果

策略用 π<sub>0.5</sub> 在每个来源的 100 个 episodes 上训练，并在每个任务和条件的 20 次试验中测试。只在 farm 数据上训练的策略在原始场景中与遥操作数据相当，并且在他们未见过的外观下依然稳健。

| 在五个真实机器人任务上 | 遥操作数据 | DataFarm 数据 |
|---|---:|---:|
| 原始场景中的成功次数（100 次试验） | 84 | **93** |
| 未见过的桌布和彩色灯光下的成功次数（100 次试验） | 46 | **87** |
| 每个后续 episode 的成本 | 机器人和操作员时间 | 仿真、渲染和评审的 **$0.80** |

<p align="center"><img src="assets/paper/real-robot-results.png" alt="Real-robot successes per task for teleoperated data, Sim + DR and DataFarm, in the original scene and under an unseen tablecloth and coloured light" width="100%"></p>
<p align="center"><img src="assets/paper/real-robot-trials.jpg" alt="Unseen-scene trials: the policy trained on teleoperated data fails, the policy trained only on farm data succeeds" width="100%"></p>
<p align="center"><em>未见场景中的试验：仅在遥操作数据上训练的策略失败（上），而仅在 farm 数据上训练的策略成功（下）。</em></p>

<p align="center"><img src="assets/paper/data-comparison.jpg" alt="Real, simulated, domain-randomized and DataFarm episodes at the same task phase, head view, for the five tasks" width="100%"></p>
<p align="center"><em>同一任务阶段的真实、仿真和 farm episodes（头部视角）。Sim + DR 在仿真器帧上加入域随机化的纹理和光照；我们的方法则渲染仿真器帧。真实帧和仿真帧来自不同的 episodes，因此物体摆放不同。</em></p>

适配一个任务完全自动，平均花费 $21 的 API 用量，按任务一次性支付，另加 $5 的渲染契约费用。一个任务一小时的同步三视角数据平均花费 $107（已含这些成本），略低于中国遥操作 $117 的报价，也低于北美仅人工成本 $290。

<p align="center"><img src="assets/paper/cost.png" alt="Average farm cost per episode against the number of episodes of one task, approaching the marginal cost of $0.80, with flat lines for teleoperation in North America and China" width="60%"></p>
<p align="center"><em>farm 按任务支付一次，之后只为渲染和评审付费：随着数据集增长，每个 episode 的平均成本向边际成本下降。平直线：以 farm 的平均 episode 长度计的遥操作成本。</em></p>

## 设计原则

- **任务代码是输出，不是输入。** 新任务的 adapter、任务 brief、judge、场景、相机拟合和视频 prompts 都由 agent 生成。新任务不需要事先注册，本仓库不随附任何逐任务产物：只有方法、共享能力层和模板。
- **无自我认证。** 由代码强制：judge 在专家被编写之前就按哈希冻结，并在隐藏的 held-out seeds 上给专家评分；交付检查只接受独立评审执行的内容绑定记录。由指令要求：一个单独的角色编写 judge。
- **保留每一次尝试。** 失败、seeds、provider 作业 ID、预算和输入哈希都会被记录。付费作业按其记录的 ID 恢复，而不是重新提交。
- **失败即关闭。** 仿真器、模型、数据和凭据都是运行时绑定。缺少绑定会以指名它的消息停止工具；绝不回退到猜测。

## 安装

DataFarm 需要 Linux 和 Python 3.11 或更新版本。媒体检查使用 `ffmpeg`，隔离专家编写者的沙箱使用 `bubblewrap`。

```bash
git clone --branch DataFarm --single-branch https://github.com/DAGroup-PKU/PyRUA-Lean.git && cd PyRUA-Lean
pip install -e '.[dev,workflow]'
```

核心运行时只需要标准库和 Pillow。`workflow` extra 增加了 verifiers 和数据集工具使用的图像与科学计算包。

默认的 H3 渲染器需要一块 GPU，每个 worker 至少 172 GB 可用显存。

本仓库**不**包含仿真器、视频模型、机器人资产、数据或凭据。安装你需要的组件（参见 [NOTICE.md](NOTICE.md)），并用运行时绑定让 DataFarm 指向它们：

```bash
$EDITOR bindings.json                           # or point DATAFARM_BINDINGS at the file, or set DATAFARM_<KEY>
datafarm doctor --profile core                  # check prerequisites, no API or GPU calls
```

`bindings.json` 把绑定名映射到路径；[docs/deployment.md](docs/deployment.md) 列出了全部：

```json
{
  "TRAINING_PYTHON": "/opt/envs/training/bin/python",
  "OPENPI_CACHE": "/data/openpi-cache",
  "DATASETS": "/data/lerobot",
  "GPU_SCHEDULER": "/opt/bin/gpu-scheduler",
  "H3_MODELS": "/data/h3-models"
}
```

## 快速开始

**1. 检查安装。** 运行 CPU 测试套件、standing-rules lint 和指令层审计：

```bash
make test lint audit
```

**2. 接入一个编码 agent。** DataFarm 不包含模型或 agent。`supervise` 通过一个运行时配置运行你选择的 agent：

| Harness | 运行时配置 | 首次运行前 |
|---|---|---|
| SoL-Pi | [`pi-solpi.json`](examples/runtime/pi-solpi.json) | 安装 Pi 和 SoL-Pi 并如下配置模型 |
| Claude Code | [`claude-code.json`](examples/runtime/claude-code.json) | 安装 Claude Code 并登录 |
| Codex | [`native-codex.json`](examples/runtime/native-codex.json) | 安装 Codex 并运行 `codex login` |

这些是受支持的 harness。要接入其他 CLI，向 [`agent_cli_driver.py`](examples/runtime/agent_cli_driver.py) 添加一个 profile。

对于 SoL-Pi 和 Claude Code，把配置中的 `/path/to/PyRUA-Lean` 替换为此 checkout 的绝对路径。要选择模型，把 `"--model", "<model>"` 添加到 driver 的 `argv` 末尾。[`claude-code.json`](examples/runtime/claude-code.json) 以 `bypassPermissions` 运行 Claude Code，因此请使用专用机器或容器，并在需要审批提示时调整该模式。

设置 SoL-Pi（已用 Pi 1.3.0 和 SoL-Pi 提交 `2195680` 测试）：

```bash
npm install --global @earendil-works/pi-coding-agent@1.3.0
git clone https://github.com/NVlabs/SoL-Pi.git ~/SoL-Pi && git -C ~/SoL-Pi checkout 2195680
pi install ~/SoL-Pi
cp examples/runtime/sol-pi.template.json ~/.pi/agent/sol-pi.json
```

在 Pi 中配置你的模型 provider（`~/.pi/agent/models.json` 和 `settings.json`）。在 `sol-pi.json` 中，把 `<provider>` 和 `<model>` 设置为用于日志归约器的小模型，并把 `cacheWriteReadRatio` 设置为主模型的缓存写入价格除以其缓存读取价格。除非此文件启用它们，否则 SoL-Pi 的机制保持关闭；参见 [SoL-Pi configuration](https://github.com/NVlabs/SoL-Pi/blob/main/docs/configuration.md)。

[examples/runtime](examples/runtime/README.md) 介绍了这些 harness 之间会话和成本的差异。

**3. 启动一个新任务。** 把用户的请求逐字保存在一个文本文件中；所有任务专属内容都由 agent 生成。

```bash
# Bind the episode and the request to a new task workspace
python tools/pipeline_agent.py prepare --episode /data/episode.mp4 \
  --request-file /data/request.txt --target-count 100 --output /data/task

# Compile the orchestrator's instructions into an explicit, hash-recorded context
python tools/pipeline_agent.py context --work /data/task --role orchestrator --output /data/task/context

# Run the coding agent until the task is delivered, recovering from crashes.
# Use the runtime config of the harness you chose in step 2.
python tools/pipeline_agent.py supervise --work /data/task --config examples/runtime/native-codex.json

# Check the delivery: complete, unmodified, reviewed episode by episode
python tools/pipeline_agent.py verify --work /data/task
```

只有当 `verify` 接受其交付时，任务才算完成。[quickstart](docs/quickstart.md) 涵盖停止和恢复一次 run。

为控制成本，可用 [`tools/pipeline_phases.py`](docs/reference/phased-runs.md) 分阶段运行任务：每个阶段是一个带 USD 上限的全新 agent 会话，独立评审者决定契约、专家和每个 episode。流程结束于 white 交付。

若要改为交互式工作，可在仓库根目录启动任意编码 agent，让它阅读 [AGENTS.md](AGENTS.md)，并把 episode 和请求交给它。这样会跳过 `supervise`，因此没有崩溃恢复，且 `verify` 会把评审报告为 declared 而非 verified。

## 复现论文

论文在 PiPER 机械臂上评估五个任务。本次发布提供方法和模板，而非逐任务产物：对每个任务，adapter、judge、场景、专家和 prompts 都是 agent 根据该任务的标定 episode 和请求生成的输出，因此复现结果意味着在某个任务上运行 agent。它需要上述外部组件、用于仿真和渲染的 GPU，以及一个编码 agent。论文运行中的任务代码、渲染数据和 checkpoint 不包含在内。

| 随附内容 | 位置 |
|---|---|
| H3 control-hybrid 和 OOD 渲染 recipe | [`configs/render/`](configs/render/) |
| 冻结的渲染评估 | [`evaluations/render-h3/`](evaluations/render-h3/) |
| Expert-harness adapter 和 brief 模板 | [`configs/expert-harness/`](configs/expert-harness/) |
| Batch、render-verifier 和训练模板 | [`configs/batch/`](configs/batch/), [`configs/render-verifier/`](configs/render-verifier/), [`configs/training/`](configs/training/) |
| 角色指令和阶段 skills | [`prompts/`](prompts/), [`.agents/skills/`](.agents/skills/) |
| 分阶段任务创建 | [`prompts/phases/`](prompts/phases/), [`tools/pipeline_phases.py`](tools/pipeline_phases.py), [phased-runs](docs/reference/phased-runs.md) |

策略用 pi0.5 训练，动作时域 50 步，batch size 128，训练 50k 步；参见 [policy-training](docs/reference/policy-training.md)。

## 仓库结构

```text
.agents/skills/        skills the agents load (orchestration, grounding, expert authoring, rendering, delivery, ...)
prompts/               role instructions, runtime instructions, session messages and render templates
docs/                  quickstart, deployment, architecture and one reference note per component
src/datafarm/
  core/                artifacts, schema, store, source locks, grounding ledger, admission certificates
  agent/               agent context, runtime and sessions, cost ledger, call log
  task/                task contract and delivery check, white delivery
  expert/              sandboxed expert development, hidden judge and held-out seeds
  simulation/          simulator qualification, joint-margin audit, randomization plans, USDCraft adapter
  render/              render contract, H3 control hybrid (render/h3) and Seedance client (render/seedance)
  verify/              spec-driven render verifier, counterfactual suite, reviewer calibration
  batch/               batch supervisor
  ledger/              run ledger
  training/            certificate-gated pi0.5 launch and dataset contracts
  evolution/           capability evolution store and controller
tools/                 CLIs: pipeline_agent.py, pipeline_phases.py, pipeline_evolve.py, build_usd_asset.py (USDCraft), expert_harness.py, datafarm_runs.py,
                       supervise_batch.py, verify_render.py, launch_pi05_training.py, ...
configs/               general policy files, render recipes and one template per config folder; see configs/README.md
vendor/                openpi trainer (Apache-2.0) and the VideoX-Fun patch
examples/              runtime and budget examples, metered driver example, USDCraft example
evaluations/           frozen evaluations
tests/                 CPU test suite
```

## 文档

| 阅读 | 用途 |
|---|---|
| [docs/README.md](docs/README.md) | 所有文档的索引 |
| [docs/quickstart.md](docs/quickstart.md), [docs/troubleshooting.md](docs/troubleshooting.md) | 运行任务，以及从运行时状态中恢复 |
| [docs/deployment.md](docs/deployment.md) | 运行时绑定，以及代码所期望的外部组件 |
| [docs/architecture.md](docs/architecture.md) | 各阶段、记录和运行时如何协同 |
| [docs/reference/](docs/README.md#参考) | 每个组件一篇说明：保证、数据模型、入口点、配置、失败行为、限制 |
| [docs/development.md](docs/development.md) | 修改本代码的规则 |
| [AGENTS.md](AGENTS.md), [prompts/instructions/](prompts/instructions) | 操作 farm 的编码 agent 的指令 |

## 限制

CPU 测试表明软件按规范运行。它们不表明新任务在物理或视觉上会成功；那需要一次真实的 run 和独立评审。每篇功能说明都列出其组件不保证的内容，例如：

- 以同一用户运行的进程不是安全边界；
- 随附的仿真器资格认证 evaluator 只覆盖布料，而任务的 probe runner 属于任务代码；
- H3 路线在预渲染的 seeds 中选择，而不是重新渲染被标记的块；
- MiniMax-H3 的许可证限制将其输出用于训练其他 AI 模型，因此请针对你的用途检查它。

## 引用

```bibtex
@misc{datafarm2026,
  title  = {PyRUA-DataFarm: LLM Agent as a Cost-Efficient Teleoperator for Real-Ready Robot Data},
  author = {Zhang, Kewei and Ma, Juncheng and Lv, Guodong and Wang, Duomin and Ruan, Feixiang and Si, Ruiyang and
            Zhang, Chuanrui and Chen, Yuxuan and Chen, Hao and Liu, Yichen and Zhang, Yintianrun and Deng, Yufan and
            Xie, Enze and Zhou, Daquan},
  year   = {2026},
  eprint = {2611.05320},
  archivePrefix = {arXiv},
  url    = {https://arxiv.org/abs/2611.05320}
}
```

## 许可证与致谢

DataFarm 以 [MIT License](LICENSE) 发布。源自第三方项目的代码保留其上游许可证；参见 [NOTICE.md](NOTICE.md)。

DataFarm 构建于 [openpi](https://github.com/Physical-Intelligence/openpi)、[VideoX-Fun](https://github.com/aigc-apps/VideoX-Fun)、RoboTwin 2.0、SAPIEN、Isaac Sim、MiniMax-H3、Seedance 和 USDCraft 之上。我们感谢它们的作者。

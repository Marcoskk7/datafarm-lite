# 架构

DataFarm 把一个真实标定 episode 和一个数据请求转换为带动作标注、摄影级的
机器人数据。编码 agent 完成具体任务相关的工作；本仓库提供共享的
指令、agent 无法自行签署的检查，以及保留每次尝试的记录。
为某个任务编写的一切（场景、专家、judge、prompt、config）都进入该任务的 workspace，
而不是进入本仓库。

## 流程

```text
episode + request
  │  tools/pipeline_agent.py prepare     pin source, input and cost policy (datafarm.agent)
  │  tools/pipeline_agent.py supervise   run agent sessions until delivery or a stop (datafarm.agent)
  ▼
grounding              scene, cameras, references, labelled physical quantities (datafarm.core.grounding)
  ▼
simulator qualification  a variant certified for the task's skills (datafarm.simulation)
  ▼
task contract, expert  frozen judge, sandboxed author, held-out seeds (datafarm.task.contract, datafarm.expert)
  ▼
rendering              render contract and provider payload checks (datafarm.render.policy),
  │                    H3 control hybrid or Seedance (datafarm.render)
  ▼
verification, review   render verifier and independent review (datafarm.verify, datafarm.task.contract)
  │                    bounded per-item batches around all of the above (datafarm.batch)
  ▼
delivery, admission    tools/pipeline_agent.py verify; admission certificate (datafarm.core.admission)
  ▼
training (on request)  certificate-gated pi0.5 launch (datafarm.training)

the run ledger (datafarm.ledger) records requests, items, workers and verdicts throughout
```

一次拒收会回到造成它的阶段。新的尝试创建新记录，早先的尝试和
失败都会保留。

## 谁可以签署什么

某个阶段的生产者绝不为自己认证。

| 阶段 | 生产者 | 由谁检查 |
|---|---|---|
| Grounding | agent | 独立评审；交付时由 `validate_quantities` 检查 |
| 任务契约 | agent | 独立任务 verifier |
| Episode | 专家与仿真器 | 由专家 harness 在留出 seed 上运行的冻结 judge |
| 渲染 | 视频模型 | render payload 检查、render verifier、独立评审者 |
| 交付 | agent | `verify_delivery` 和 `verify_white_delivery`，它们要求内容绑定的评审 |
| 准入 | 证书编译器 | admission root，从存储的证据字节重新推导 |

回执是记录，不是密码学签名：任何能写存储的人都能写一个。
独立性来自单独的执行、内容绑定，以及在证据可用时重新运行验证。进程边界不是以同一用户运行的 agent 之间的安全边界。

## 产物与准入

`datafarm.core` 定义类型化文档（`ArtifactDocument`、`GateReceipt`），按内容寻址存储在
`ArtifactStore` 中。文档不能包含绝对主机路径、凭据字段或可变的签名
URL。`datafarm validate <file>` 检查单个文档或回执。

训练准入证书绑定已准入的成员 id 和每个成员的媒体摘要、
allowlist 摘要、审计报告、数据集来源和每个数据集文件的摘要 manifest，
以及显式的豁免策略。独立的 admission root 从存储的字节重新推导该声明
并签署一个 gate receipt。`tools/launch_pi05_training.py train-command` 在启动
manifest 存在之前验证证书，没有已验证证书的启动会被拒绝。操作员步骤见
[交付与准入](reference/delivery-and-admission.md)。

## `src/datafarm` 中的包

| 包 | 职责 |
|---|---|
| `core` | Artifact 文档、回执、schema 检查、内容寻址存储、文件绑定、来源锁、grounding ledger、准入证书 |
| `agent` | 编译角色上下文、运行并续接 agent session、计量用量、保留成本 ledger 和调用日志 |
| `task` | 任务契约与交付检查、白模交付 |
| `expert` | 专家 harness：沙箱化作者 session、rollout broker、冻结 judge、留出评估、专家库 |
| `simulation` | 仿真器资格决策逻辑、关节裕度审计、相机与抓取随机化计划、USDCraft adapter |
| `render` | 渲染契约（`render/policy.py`）与渲染器：H3 control hybrid（`render/h3`）和 Seedance 客户端（`render/seedance`） |
| `verify` | spec 驱动的 render verifier、反事实校准、评审者校准 |
| `batch` | Batch supervisor：有界的逐项启动、轮询、验证与重试 |
| `ledger` | 运行 ledger：固定条目分母、推导的运行状态、启动哨兵 |
| `training` | 证书门控的 pi0.5 启动、数据集与动作模式契约、cohort 审计器 |
| `evolution` | 反馈存储与控制器，评估并激活能力层变更 |
| `runtime.py`、`doctor.py`、`cli.py`、`common.py` | 运行时绑定、前置条件检查、`datafarm` 命令、共享辅助函数 |

## 顶层目录

| 目录 | 职责 |
|---|---|
| `src/` | `datafarm` 包 |
| `tools/` | 命令行入口：`pipeline_agent.py`、`pipeline_phases.py`、`expert_harness.py`、`supervise_batch.py`、`verify_render.py`、`launch_pi05_training.py`、... |
| `.agents/skills/` | agent 加载的 stage skill |
| `prompts/` | 角色指令、运行时指令、专家 session 消息、渲染 prompt 模板 |
| `configs/` | 通用策略文件、渲染 recipe、每种任务产物一份模板 |
| `evaluations/` | 冻结的评估器：render EVAL-v1 以及能力演化的软件检查 |
| `examples/` | 运行时与预算示例、USDCraft 示例 |
| `vendor/` | openpi trainer（Apache-2.0）和 VideoX-Fun 补丁 |
| `assets/` | README 用的插图 |
| `tests/` | CPU 测试套件 |
| `docs/` | 本处文档；[索引](README.md) |

外部组件（仿真器、视频模型、数据集、凭据、GPU 调度器）是运行时绑定；
见[部署](deployment.md)。重型运行时 import 不留在核心包中。

## 延伸阅读

- [`AGENTS.md`](../AGENTS.md)、[`prompts/instructions/`](../prompts/instructions)、[`configs/agent-instructions.json`](../configs/agent-instructions.json)：agent 读取和产出什么。
- [参考说明](README.md#参考)：每个组件一篇说明，含保证、记录和限制。

# Deployment 绑定

代码从不硬编码模拟器、模型、数据或凭据所在的位置。每个外部位置都是一个 **runtime binding**。绑定默认指向工作区（checkout，或 `DATAFARM_WORKSPACE`）下的某个路径，并可在 checkout 中的 `bindings.json`（或由 `DATAFARM_BINDINGS` 命名的文件）中覆盖，或按键用环境变量 `DATAFARM_<KEY>` 覆盖。值是路径，绝不是密钥。需要某个绑定的工具会以点名该绑定的消息失败关闭，而不会回退到猜测。

实现：`src/datafarm/runtime.py::defaults`、`::load_bindings`、`::expand`（配置使用 `${KEY}` 占位符）。H3 control hybrid 使用它自己的失败关闭解析器 `src/datafarm/render/h3/h3_paths.py`，它只读取显式环境变量或 `bindings.json` 条目：除 `H3_PYTHON` 外，其键没有工作区默认值。

```bash
pip install -e '.[dev,workflow]'                    # from the source checkout (distribution datafarm-lite)
$EDITOR bindings.json                               # point bindings at your installations
datafarm doctor [--workspace DIR] --profile core    # no API or GPU calls
```

`doctor` profile 有：`core`、`seedance`、`training`、`h3-control`、`h3-ood`、`usdcraft`。

最小的 `bindings.json` 为你要用的键保存路径字符串：

```json
{
  "TRAINING_PYTHON": "/opt/envs/training/bin/python",
  "OPENPI_CACHE": "/data/openpi-cache",
  "DATASETS": "/data/lerobot",
  "GPU_SCHEDULER": "/opt/bin/gpu-scheduler",
  "H3_MODELS": "/data/h3-models"
}
```

## 绑定

| 键 | 默认值（工作区 `<ws>` 下） | 用途 |
|---|---|---|
| `WORKSPACE` | `<ws>` | 所有默认值的根 |
| `TRAINER` | `<ws>/vendor/trainer` | vendored 的 openpi trainer |
| `GPU_SCHEDULER` | `<ws>/external/gpu-scheduler` | 由 deployment 提供的可执行文件；参见 [GPU 调度](#gpu-scheduling) |
| `ROBOTWIN`, `ROBOTWIN_PYTHON` | `<ws>/external/robotwin`, `<ws>/environments/robotwin/bin/python` | 通过 RoboTwin 2.0 的 SAPIEN task |
| `ISAAC_PYTHON`, `ISAAC_KIT`, `ISAAC_ENV` | `<ws>/environments/isaac/...` | Isaac Sim |
| `ANALYSIS_PYTHON`, `ANALYSIS_ENV` | 当前解释器 | CPU 分析工具 |
| `TRAINING_PYTHON`, `OPENPI_CACHE`, `DATASETS` | `<ws>/environments/training/bin/python`, `<ws>/external/openpi-cache`, `<ws>/data/lerobot` | pi0.5 训练 |
| `MODELS`, `EXTERNAL` | `<ws>/external/models`, `<ws>/external` | 模型权重和其他外部 checkout |
| `H3_PYTHON` | `<ws>/environments/h3/bin/python` | H3 runtime（单独获取） |
| `H3_MODELS`, `H3_VIDEOX_FUN`, `H3_TURBO_LORA` | H3 harness 无（需显式设置） | H3 control hybrid；`H3_VIDEOX_FUN` 是已应用 `vendor/videox_fun/videox-fun-datafarm.patch` 的 VideoX-Fun |
| `H3_EVAL_DINO`, `H3_SAM2` | H3 harness 无（需显式设置） | 冻结的渲染评估；OOD 掩码 |
| `H3_HED_ANNOTATOR_REPO` | H3 harness 无（需显式设置） | ControlNet v1.1 HED annotator（soft-HED 工具所需） |
| `EPISODES` | `<ws>/data/episodes` | 真实输入 episode |
| `TRAINING_RUN` | `<ws>/runs/training` | 训练输出 |
| `ARK_CREDENTIALS`, `AGENT_CREDENTIALS` | `<ws>/secrets/*.env` | 凭据**文件**，权限模式 0600；由显式的 keys-file loader 读取，绝不放入 argv |

## 没有默认值的设置

这些设置故意没有默认值。缺失的值会在发起任何调用之前停止工具。

| 设置 | 位置 | 含义 |
|---|---|---|
| `DATAFARM_SEEDANCE_UPLOAD_HOST` 环境变量，或 Seedance 批量配置中的 `upload_host` | `src/datafarm/render/seedance/client.py::upload_host` | 将 carrier 视频提供给 Seedance 的文件主机的 HTTPS 基础 URL。**选择启用会将真实场景媒体发布在一个 URL 上，直到其过期。** |
| `agent.model`，以及 `codex-metered` 的 `agent.pricing` | expert-harness adapter；`src/datafarm/expert/session.py` | 编码模型及其价格表：`{"model", "source", "usd_per_million_tokens": {"input", "cached_input", "cache_write", "output"}, "long_context"?}`。 |
| `--pricing` / `DATAFARM_METERED_PRICING` | `examples/runtime/metered_codex_driver.py` | 相同的价格表格式，用于示例 metered driver。 |
| `H3_EVAL_DINO` / `DATAFARM_H3_EVAL_DINO` | `evaluations/render-h3/h3eval.py` | 用于 `realpool`/`score` 的 DINOv2 目录。 |

## 编码 agent driver

`tools/pipeline_agent.py supervise --config <runtime.json>` 驱动该 agent。
- **原生 Codex CLI**（`{"driver": {"type": "codex"}}`）：deployment 必须提供带已有登录的 `codex exec --json` 和 `codex exec resume`。runtime 从不读取或复制凭据。
- **任何其他 agent**：实现 argv driver 协议 `datafarm.agent-identity/1`，描述见 [agent runtime](reference/agent-runtime.md)。

预算策略位于 `examples/runtime/`，并在 [cost control](reference/cost-control.md) 中描述。

## GPU scheduling

发行版不附带 GPU scheduler。deployment 提供一个带 `submit` 和 `leases` 子命令的可执行文件，工具通过 `GPU_SCHEDULER` 绑定（默认 `<ws>/external/gpu-scheduler`）获得其路径。围绕 Slurm、Kubernetes 或 SSH lease 脚本的薄封装即可。单节点部署可以避开它：expert harness 也有一个 `local` rollout 后端。

有三处会调用 scheduler：`tools/launch_pi05_training.py`（训练作业）、expert harness 的 `command` rollout 后端（通过 adapter 的 `rollout.submit.argv`，`src/datafarm/expert/rollout.py`），以及 H3 workers（`src/datafarm/render/h3/launch_control_worker.sh`、`h3_pool.py`、`h3_requeue_orphans.py`）。`h3_pool.py` 读取 `id` 行（属于 `submit`）并与 `leases` 的第 1 列匹配，将 exit 2 视为 "no GPU free"，且在 `leases` 失败时不启动任何东西。

### `submit`

```text
gpu-scheduler submit [--host HOST]... --gpus N [--min-free-mb MB] [--max-util PCT] \
                     --name NAME [--cwd DIR] [--] COMMAND [ARG...]
```

| 选项 | 要求 |
|---|---|
| `--gpus N` | 分配 N 个 GPU，使其对作业可见，并在后台启动命令。`submit` 在作业启动后即返回；它不等待作业完成。 |
| `--min-free-mb MB`, `--max-util PCT` | 仅使用空闲内存至少为此值且利用率至多为此值的 GPU。 |
| `--name NAME` | 作业名，由 `leases` 回报。 |
| `--cwd DIR` | 命令的工作目录。 |
| `--host HOST` | 可重复。将放置限制到这些主机。H3 workers 总是传入它，来自 `<run>/queue/ALLOWED_HOSTS`。 |

- 命令在选项之后开始，可选地放在 `--` 之后。`tools/launch_pi05_training.py` 省略该分隔符；harness adapter 模板和 H3 workers 使用它。
- Exit 0：作业已启动。Exit 2：没有匹配的空闲 GPU 且未启动任何东西；`tools/launch_pi05_training.py` 随后尝试下一个 GPU 数量。任何其他非零 exit 都是失败。
- 成功时打印 `key=value` 行。expert harness 在尝试 receipt 中记录 `id`、`host`、`gpus`、`pid` 和 `log`。
- `submit` 不得依赖调用方的环境：H3 启动器在命令内部传入它需要的每个变量。
- 缓慢的 `submit` 可能被其调用方终止（harness 中默认 120 秒）。由被终止的 `submit` 启动的作业仍必须出现在 `leases` 中。

### `leases`

`leases` 每行打印一个 lease，以空白分隔的列，并由 `h3_requeue_orphans.py` 读取：第 1 列是 lease ID，形式为 `<name>-YYYYMMDD-HHMMSS-<hex>`；第 3 列是主机；第 5 列是作业的进程 ID。

失败的查询必须以非零退出，而不是打印空表：未知的 lease 表被视为未知，而空表会让调用方把正在运行的作业当作未运行来操作。`h3_requeue_orphans.py` 会把未列出的 worker 的 ticket 退回队列，因此列表必须完整。

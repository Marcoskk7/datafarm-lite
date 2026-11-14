# 快速开始

在完整的源码 checkout 中使用 Python 3.11 或更新版本以及 Git。运行时只需要标准库。图像管线需要 Pillow，完整的 CPU 测试套件需要 `dev` 和 `workflow` extras（`pip install -e '.[dev,workflow]'`）。仿真器和训练环境单独部署；参见 [deployment](deployment.md)。

安装一个编码 agent。要使用 SoL-Pi 或 Claude Code，参见 [examples/runtime](../examples/runtime/README.md)，并在下面的命令中使用其配置。对于内置 driver，安装 Codex 并自行登录，然后确认 `codex exec --help`、`codex exec resume --help` 和 `codex login status`。运行时会继承登录状态，不会读取或复制凭据。若省略模型，则应用部署设置；请在你的部署中测试任何自定义模型或 provider。

## 运行任务

把用户的请求保存到文件，然后准备任务：

```bash
python tools/pipeline_agent.py prepare --episode /data/episode.mp4 \
  --request-file /data/request.txt --target-count 5 --stop-after white --output /data/task
python tools/pipeline_agent.py context --work /data/task --role orchestrator --output /data/task/context
python tools/pipeline_agent.py supervise --work /data/task --config examples/runtime/native-codex.json
```

`prepare` 固定源代码、输入和成本策略。要使用其他模式，添加 `--cost-policy examples/runtime/budget-disabled.json` 或你自己的策略。强制示例不能与原生 Codex 搭配使用。参见 [cost control](reference/cost-control.md)。

后续带 `--work` 的命令会运行固定的源代码，且该固定不会自动改变。

## 分阶段运行

为控制成本，可[分阶段运行任务](reference/phased-runs.md)：每个阶段是一个全新的 agent 会话，带有宿主强制执行的 USD 上限，流程结束于 white 交付。

## 在台账下运行

对于连续运行，提交干净的源代码，并按照 [run ledger](reference/run-ledger.md) 创建一次 run，登记请求、授权、输入和固定的目标分母。然后托管同一个入口点：

```bash
tools/datafarm_runs.py launch PRUN-... --role supervisor -- \
  python tools/pipeline_agent.py supervise --work /data/task --config examples/runtime/native-codex.json
tools/datafarm_runs.py check PRUN-...
python tools/pipeline_agent.py runtime-status --work /data/task
```

启动后，检查实际的 worker、PID、会话和进度。间隔一段时间后再次检查，并观察日志、检查时间或输出是否在推进。sentinel 心跳不能替代 worker。

## 停止、恢复、验证

```bash
python tools/pipeline_agent.py stop --work /data/task --reason 'operator stop'
python tools/pipeline_agent.py runtime-status --work /data/task
python tools/pipeline_agent.py supervise --work /data/task --config examples/runtime/native-codex.json --resume
python tools/pipeline_agent.py verify --work /data/task
```

`stop` 终止当前模型进程组，保留外部任务 ID，并且不会取消远程 GPU 或 provider 作业，因此恢复时应先检查原始作业状态。`--resume` 清除停止请求，并保留预算、会话数、重试截止时间和线程 ID。如果需要更多会话，请在配置中提高上限；不要删除控制文件。

只有当 `verify` 在其交付上通过时，任务才算完成。进程退出码为 0 或处于 `complete` 状态都不能替代这一点。

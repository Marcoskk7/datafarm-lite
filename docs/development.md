# 开发规则

使用 `pip install -e '.[dev,workflow]'` 安装，然后运行 `make test lint audit`。CI 运行同样的检查（`.github/workflows/ci.yml`）。

- 核心测试必须在没有仿真器、网络或凭据的情况下运行。
- 运行时代码不得包含实验 ID 或绝对文件系统路径。
- 输入是 `ArtifactRef` 值；adapter 通过各自的存储解析字节。
- schema 变更会获得新版本。
- 更早的 schema 版本绝不会被静默接受，也绝不会获得兼容别名。
- 每个远程创建操作必须在轮询前持久化稳定的作业身份。
- 每个 verifier 必须包含与其权限相称的 hard negative。
- 生成的观测不能成为几何、动作或任务状态的权威。
- 候选补丁不能修改自己的晋升标签或根 verifier。
- 每个被请求的流水线调用（完整或部分）都注册为账本运行，并通过 `tools/datafarm_runs.py launch` 启动；见 [run ledger](reference/run-ledger.md)。

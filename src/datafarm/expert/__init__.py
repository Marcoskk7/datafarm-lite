"""Task-expert 编写工具链及其评估套件。

一个编写 agent（Claude Code 或 Codex，headless）为某个仿真机器人任务编写控制器程序，
即"task expert"。本包提供：经过 lint 的 brief、操作系统级沙箱、作为 agent 运行 rollout
唯一途径（并维护尝试台账）的 broker 工具、受 provider 上报成本约束的有界会话循环、
针对 judge 与 holdout seed 的篡改清单、仅由工具链执行的 holdout 评估（附分数-成本曲线）、
自动交接包、经过验证的 expert 库，以及多任务评估套件。

入口：``tools/expert_harness.py``（``python -m datafarm.expert.cli``）。
设计与运维指南：``docs/reference/expert-harness.md``。
"""

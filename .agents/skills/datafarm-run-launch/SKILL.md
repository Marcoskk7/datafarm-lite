---
name: datafarm-run-launch
description: 注册、启动、独立校验持续运行、恢复并关闭一次 DataFarm pipeline 运行。
---

# datafarm-run-launch

阅读 `prompts/instructions/operations.md` 和 `docs/reference/run-ledger.md`。完整运行和 cohort 阶段到终点（stage-to-end）请求必须使用 `tools/datafarm_runs.py`；恢复使用父/子 lineage。

1. 保留用户对 GPU 使用和付费 provider 的有效授权。不要在有授权范围内反复请求批准。在提交渲染工作前，分别确定渲染模式和真实参考。
2. 使用干净、可追溯的 commit，不覆盖用户改动；登记请求、授权来源、阶段范围、固定 ID、task/cohort 和产物。
3. 通过 ledger sentinel 启动。在启动器退出后检查实际 worker，包括 PID/PPID/session/log，以及在适用时检查 GPU job/lease。
4. 在适当间隔之后的新的工具调用中，重新检查 worker 以及可观测进度。将等待拆分到 60 秒，以保持通信响应。只有两次检查都报告带 ID 和时间戳的持续运行之后才继续。
5. 查询失败属于未知状态，而不是重新提交的许可。复用已有 task ID/checkpoint，记录干预并限制重试次数。仅有 sentinel heartbeat 是不够的。
6. 在扩展新采集/重新规划的轨迹前应用关节余量（joint-margin）要求；绝不要只单独裁剪动作标签。保留现有 task 的相机/资格（qualification）要求。
7. 依据证据做最终确认，并明确报告缺口；维护尝试、拒绝、替换和成本记录。

Deployment scope：GPU 和付费 provider 的授权来自当前 deployment 和会话。获得仓库本身不授予任何授权，已给予的许可不会再次索取。

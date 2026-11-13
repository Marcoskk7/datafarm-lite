# 仿真器资格认定

仿真器资格认定在编写 expert 之前检查仿真器能否执行
任务所需的技能。脚本化技能探针在一组候选仿真器变体上运行（材质与场景文件的
点号键补丁，外加可选的运行时标志，如夹爪接触
模型）；每个探针按声明的标准判定；在通过每个必需探针的变体中，
选择改动最少的变体，并签发一份绑定确切变体
文件和所有探针证据的证书。

本版本提供决策逻辑和 run 绑定检查。任务的探针运行器（在
GPU 调度器上启动探针并从仿真器状态提取指标）是 agent 为该任务编写的
输出。随附的探针类型 `hold` 和 `fold` 是为布料编写的。

仿真器资格认定和渲染风格是分开的职责。通过仿真器检查
并不意味着允许 OOD 渲染，也不意味着生成的视频是照片级真实的。

## 保证

1. 变体补丁只能替换现有带点路径处的值（`"*k"` 字符串会乘以一个
   数值）；缺失的路径或非数值乘数会报错，且基础文档
   不会被修改。
2. `hold` 探针只有在以下情况下通过：达到悬挂、布料至少上升 `min_lift_m`、每个
   被夹住的补丁滑移至多 `max_slip_m`、p95 应变至多 `max_strain_p95`
   （设置时 p99.9 至多 `max_strain_p999`）；指标缺失则失败；放下后
   的平整度是建议性的。
3. `fold` 探针只有在 `m1_at_release` 为 `True` 时通过；当 spec 设置 `require_settled` 或
   `max_creep_m` 时，还要求 settled 判定和静止漂移（在至少 `min_rest_window_s` 内）
   且未测量时失败；底层捕获和机器人门失败是建议性的。
4. 选择要求至少一个必需探针，并在通过每个必需
   探针的变体中挑选 `change_magnitude` 最小者（材质键数 + 场景键数 + 运行时标志数）；
   并列时取分数更高者，再按名称；若无人通过，则不选择任何变体。
5. 证书只对选定变体签发，并带文件摘要，它记录所选
   补丁、标志、原始和规范化文件摘要、标准、完整排名，以及
   每个变体每个探针的通过标志、失败、建议和指标。
6. 证书检查会从探针结果重新计算选择，比较每个声称的
   逐探针通过标志，并要求绑定文件；伪造的选择或通过标志会被报告。
7. 只有当每个已认证文件都以匹配的摘要被报告
   （`.json` 名称用规范化 JSON 摘要，否则用字节摘要）且没有报告未认证文件时，
   run 才绑定到该证书。

代码：`src/datafarm/simulation/sim_qualification.py`, `tools/check_sim_binding.py`。

## 数据模型

- **Spec**（JSON）：`task_family`；`base_material`、`base_scene`（相对当前目录
  打开的路径）；`variants[]`，含 `name`、`note`、`material` 和 `scene`（点号键补丁）、
  `runtime_flags`（标志 `file:<path>` 会绑定该文件）、`launcher`（相对 spec）；
  `probes[]`，含 `name`、`kind`（`hold` | `fold`）、`expert`（相对 spec）、`seed`（或 null）；
  `required_probes`；`criteria`（`{"hold": {...}, "fold": {...}}`，探针
  求值器的关键字参数）。
- **证书**（`datafarm.sim-qualification.certificate/1.0.0`）：`task_family`、`selected_variant`
  （`name`、`note`、`material_patch`、`scene_patch`、`runtime_flags`、`files_sha256`、
  `files_canonical_sha256`）、`required_probes`、`criteria`、`ranking`（行
  `[name, passes_required, change_magnitude, score]`）、`evidence`（`{variant: {probe: {passed,
  failures, advisories, metrics}}}`), `reason`（可选）。
- **ProbeOutcome** / **VariantResult** / **QualificationDecision**：`sim_qualification.py` 中的
  冻结 dataclass；`VariantResult.score` = 每个通过探针 2 分 − 每个建议性项 0.25 分
  （仅用于并列决胜）。

## 工作原理

1. 任务的探针运行器为每个变体生成 `material.json` 和 `scene.json`（使用 `apply_patch`），
   并在 GPU 调度器上为每个（变体、探针）运行一个作业。
2. 它从每个完成的运行中提取指标。未完成的运行或提取失败会产生空
   指标，从而使探针失败。
3. 每个结果由 `evaluate_hold_probe` 或 `evaluate_fold_probe` 按 spec 的标准判定，
   或由为该任务编写的求值器判定，然后 `select_variant` 对变体排名。
4. 如果选择了某个变体，就对其文件做哈希，`build_certificate` 生成证书，
   `verify_certificate` 对照结果重新检查。
5. 之后，`tools/check_sim_binding.py` 把 run 配置使用的文件与
   证书（`verify_run_binding`）以及每次 run 的 `environment.txt` 比较。

## 入口点

```bash
# check that runs used exactly the certified files (exit 0 only if everything binds)
python tools/check_sim_binding.py /data/task/simqual/certificate.json \
  --file material.json=/data/task/sim/material.json --file scene.json=/data/task/sim/scene.json \
  --runs /data/task/runs/ep-0001 /data/task/runs/ep-0002
```

## 配置

- 标准默认值 —— hold：`max_slip_m 0.03`、`min_lift_m 0.10`、`max_strain_p95 1.15`、
  `max_strain_p999 None`、`min_flat_area 0.85` 和 `max_flat_height_m 0.02`（建议性）；
  fold：`min_bottom_layer_capture 0.5`（建议性）、`require_settled False`、`max_creep_m None`、
  `min_rest_window_s 8.0`。
- `check_sim_binding.py`：`--file NAME=PATH`（可重复）、`--runs DIR...`；用法错误时退出码 64。

## 失败行为

- 没有通过的变体：决策记录排名和原因，且无法构建证书。
- 证书重新检查失败会由 `verify_certificate` 以失败列表报告。
- `check_sim_binding.py` 在遇到任何 `mismatch`、`not_reported`、`not_certified`、
  `canonical_digest_missing`、`files_unbound`、`changed_since_run` 或 `no_recorded_digest` 问题时退出码为 1。

## 局限性

- **探针族是布料专用的。** 只有 `hold` 和 `fold`。共享代码中没有刚体或
  关节物体探针族；新任务需要自己的探针求值器和
  指标提取器。
- **`verify_certificate` 不是独立审查。** 它在给定结果上比较选择和通过
  标志。它不会从原始 run 文件重新提取指标，也不会重新哈希
  变体文件。
- **准入不要求证书。** `task/contract.py::verify_delivery` 和
  `white_delivery.py::verify_white_delivery` 都不检查资格证书或 run 绑定；
  只有在生产者运行 `check_sim_binding.py` 且 reviewer 检查时，
  在已认证仿真器上生产所有 episode 才成立。
- `check_sim_binding.py` 对操作者在命令行指定的文件做摘要；每个 run
  目录需要在 `environment.txt` 中至少记录其中一个，而不是全部。
- 没有 `file:` 前缀的运行时标志只按名称绑定。一个 `file:` 标志，其名称以
  `.json` 结尾，会得到原始摘要但没有规范化摘要，因此 `verify_run_binding` 会为其报告
  `canonical_digest_missing`。
- `change_magnitude` 统计变更的键，而不是变更的大小。通过探针说明仿真器
  支持这些动作；它不是物理参数的测量。

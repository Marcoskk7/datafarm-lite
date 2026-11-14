# Agent 调用：调用账本与 reviewer 校准

pipeline 代码在少数几处运行编码 agent 和视觉语言模型（VLM）reviewer。这些模型都不是权威：模型只能 *propose* 或 *screen*，由 simulator 物理和本地 verifier 决定什么被 admitted。只有当每次模型调用都能被计数和检查时，这条规则才有意义。

系统和 role prompt 如何组装，见 [capability-layer](capability-layer.md)。新的 `prompts/roles` 文件必须被显式加载；仅因文件存在并不使其生效。

| 组成部分 | 代码 | 作用 |
|---|---|---|
| 调用账本 | `src/datafarm/agent/call_log.py` | 每次模型调用尝试一行 JSON（schema `datafarm.agent-call/1`） |
| 账本报告 | `src/datafarm/agent/call_report.py`、`tools/agent_calls_report.py` | 调用、tokens、已知和未知成本、每个组件的延迟、backend、model 和 status |
| Reviewer 校准 | `src/datafarm/verify/reviewer_calibration.py`、`tools/calibrate_vlm_reviewer.py`、`configs/standing-rules.json` | VLM 与人工的一致性；对照 standing rules 的审计 |

## 1. 调用账本（`datafarm.agent-call/1`）

发出调用的 controller 写记录，而不是 agent。每次尝试之后、且在 controller 对其采取行动（retry、fall back、return）*之前*，追加一条记录（在 `flock` 下，已 fsync），因此失败、重试和放弃的尝试都留在分母中。prompt、响应和凭据从不存储，只存 SHA-256 摘要。默认文件是 `<run dir>/agent-calls.jsonl`。

| 字段 | 含义 |
|---|---|
| `schema` | `datafarm.agent-call/1` |
| `call_id` | 每次尝试唯一（`call-<hex>`） |
| `component` | 发出调用的 controller，例如 `expert-session` |
| `backend` | 调用方的 backend，例如 `claude-cli` 或 `codex-exec` |
| `model` | 请求的 model |
| `started_at`, `ended_at` | UTC 时间戳 |
| `status` | 见下文 |
| `request_sha256` | 请求体（不含 headers）的规范 JSON |
| `prompt_sha256` | `messages` 列表的规范 JSON |
| `response_sha256` | 已解析响应体的规范 JSON（若非 JSON 则为原始字节） |
| `response_id` | provider id，例如 `chatcmpl-…` |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | provider 用量；对于 OpenAI 风格 provider，`input_tokens` 包含缓存 tokens |
| `cost_usd`, `cost_source` | 当调用方提供的表为本次调用计价时为 `price_table`，否则 `null` = unknown |
| `turns` | 对于返回 2xx 体的 chat completion 为 1 |
| `latency_s` | 本次尝试的墙钟时间 |
| `error_type` | 例如 `ReadTimeout`、`http_429`、`schema_violation` |
| `run_id`, `attempt` | pipeline run id；logical call 内从 1 开始的实时尝试序号 |
| `extra` | 调用方特定细节 |

状态：`ok`（答案已解析且 schema 有效）、`invalid_response`（收到了答案但不可用）、`error`（传输或 provider 错误）、`timeout`、`aborted` 和 `budget_exhausted`（controller 停止；由 harness 使用）。

### 账本报告

```bash
tools/agent_calls_report.py runs/                      # every agent-calls.jsonl below runs/
tools/agent_calls_report.py a.jsonl b.jsonl --json report.json
tools/agent_calls_report.py runs/ --group-by model,status --json -
```

行按 `component, backend, model, status` 分组（或用 `--group-by` 指定 `component, backend, model, status, run_id, error_type, cost_source` 中的任意项）。每一行和总计报告 `calls`（每条记录）、`logical_calls`、tokens、`cost_usd_known`、`calls_with_unknown_cost` 以及延迟的 sum/mean/p50/p95/max。格式错误的行会使报告失败，除非 `--skip-invalid`（此时它列在 `problems` 下）；一个 `call_id` 出现两次（同一账本传入两次）只计一次并列出。

## 2. 用人工标签校准 VLM reviewer

只有在已知每个准则上与人工的一致程度后，VLM 筛查结论才可用。`tools/calibrate_vlm_reviewer.py` 将每个样本、每个准则的结论与人工标签配对。

**VLM 输入**（`--vlm`，任意混合）：run 目录（每个 `episodes/<id>/visual-review/assessment.json`）、一个摘要文件，或通用的 `{"id", "criteria": {name: {"status", "evidence"}}}` JSON。聚焦的 recheck assessment 会覆盖其点名的准则，但仅当其 `original_assessment_sha256` 等于第一遍文件的哈希时；否则它被忽略并记录。`--no-rechecks` 仅评估第一遍。总体结果被重新推导：任一 `fail` 给出 `flagged`，否则任一 `uncertain` 给出 `uncertain`，否则 `sampled_pass`。

**人工标签**（`--human`）：JSON `{"labels": {sample: {criterion: label}}}`、由 `{sample_id, criterion, label}` 行组成的列表，或长格式（`sample_id,criterion,label`）或宽格式（`sample_id,<criterion>,…`）的 CSV。`--id-map` 重命名人工 id，来源可以是一个对象 `{human_id: review_id}`，也可以是盲密钥（例如 `<run>/blind-key.json`）配合 `--id-template`（例如 `sample-{sample}`）。

**映射**（`--mapping`，可选；默认：每个人工准则映射到同名的 VLM 准则）：

```json
{"schema": "datafarm.vlm-calibration-mapping/1",
 "criteria": {"geometry": {"vlm": ["object_geometry", "camera_layout"]},
              "overall": {"vlm": ["@outcome"]}},
 "human_vocabulary": {"pass": ["acceptable", "none"], "fail": ["unacceptable", "obvious"],
                      "abstain": ["unsure"]}}
```

多个 VLM 准则保守地组合：任一 `fail` 给出 `fail`；否则任一 `uncertain` 或缺失准则给出 `abstain`。`@outcome` 是总体结论。未知标签是错误，而不是猜测。

**指标**（按每个人工准则以及汇总）。`fail` 表示 "defect present"。人工 `abstain` 标签被排除并计数。

- 混淆矩阵：human {pass, fail} × VLM {pass, fail, abstain}；
- 在 *decided* 对（VLM pass/fail）上的 accuracy 和 Cohen's kappa；当未定义时 kappa 为 `null`（两个评分者都使用同一个相同类别）；
- false-accept rate = 人工 fail 上 VLM pass ÷ 所有人工 fail；false-reject rate = 人工 pass 上 VLM fail ÷ 所有人工 pass；两者也仅针对 decided 对；
- abstention rate；"abstain as reject" 变体（操作性 gate，其中 `uncertain` 阻止 admission）：accuracy、kappa 和 false-reject rate；
- accuracy 和两个错误率的 Wilson 95% 区间。

**Standing rules**（`--standing-rules`，可选；`--prompt-file` 添加 prompt 来源）。每条规则有 id、statement、source、`forbidden_criteria`（不得算作缺陷的 VLM 准则）、`forbidden_phrases`/`forbidden_patterns`、`scopes`（`criterion`、`evidence`、`prompt`）和 `exempt_criteria`。对某个 forbidden criterion 给出 `fail` 结论按构造即与该规则矛盾；在 fail 证据或 reviewer prompt 中发现的短语是供人工确认的 *candidate*。报告按规则统计被标记的结论和 prompt，并展示若忽略被标记结论时结果计数会如何变化。`configs/standing-rules.json` 编码了本项目的规则：不要求柔光，硬阴影不是缺陷；曝光只是粗粒度防护。视频内随时间变化的光照仍然是缺陷，因此 `temporal_appearance` 被豁免。

**流程。** 导出标签、映射 id、运行，并在读汇总行之前先读每个准则的行：

```bash
tools/calibrate_vlm_reviewer.py --vlm runs/<run> --human labels.json \
  --id-map runs/<run>/blind-key.json --id-template 'sample-{sample}' \
  --mapping mapping.json --standing-rules configs/standing-rules.json \
  --out calibration.json
```

带 `--human` 但零对时，工具以 exit 3 退出（除非 `--allow-empty`）：没有校准任何东西。不带 `--human` 时，它只报告结论分布和规则审计，并声明未测量一致性。

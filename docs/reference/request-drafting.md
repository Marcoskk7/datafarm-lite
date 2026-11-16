# A 线 1a：一句话 → request.yaml

把用户的一句话变成 [`datafarm.request/1`](../ir/request.md)：一次结构化 LLM 调用抽取句子说了的字段，确定性默认值补齐其余字段，再过 `validate_request`。不重试；输出不合格就报错退出，换句子或模型由人决定。

| 代码 | 作用 |
|---|---|
| `src/datafarm/request/drafting.py` | `draft_request`：拼 messages、派生草稿 schema、补默认、校验 |
| `src/datafarm/request/defaults.py` | 默认值表 `DEFAULTS` 与推导字段（`request_id`、`cameras`、`render.views`），产出逐字段 provenance |
| `src/datafarm/request/ports.py` | `StructuredLLM` 端口；request 包不导入任何厂商 SDK |
| `src/datafarm/adapters/llm/client.py` | `OpenAIStructuredLLM`：官方 `openai` SDK，chat completions |
| `src/datafarm/adapters/llm/fake.py` | `FakeStructuredLLM`：测试替身 |
| `prompts/request/draft.md` | 抽取提示（英文） |
| `src/datafarm/entry/request_cli.py`, `tools/request_draft.py` | 命令入口 |
| `configs/llm.template.json` | 模型配置模板 |

## 用法

```bash
cp configs/llm.template.json configs/llm.json          # 改 model；兼容服务填 base_url
export OPENAI_API_KEY=...                              # 变量名由 api_key_env 指定
python tools/request_draft.py "put the plate with a raised rim into the open drawer" \
    --config configs/llm.json --out WORK/request.yaml [--call-log WORK/agent-calls.jsonl] [--prompt FILE]
```

产出两份文件，已存在则拒绝覆盖（退出码 2）：

- `request.yaml`：`model_dump(by_alias=True, exclude_none=True)` 的 YAML；
- `request.draft.json`（`datafarm.request-draft/1`）：`sentence` 及其 sha256、提示文件 sha256（`prompt_file_sha256`）、实际发送内容 sha256（`prompt_sha256`，messages + schema）、`request.yaml` 的 sha256、`model`、`response_id`、token 数，以及 `provenance`（字段路径 → `sentence` / `default`）。

每次调用尝试都在 `--call-log`（默认 `--out` 同目录的 `agent-calls.jsonl`）追加一条 [`datafarm.agent-call/1`](agent-calls.md)：`component=request-draft`、`backend=openai-chat`，状态 `ok` / `invalid_response` / `error` / `timeout`。记录在返回或抛错之前写入；只存 request / prompt / response 的摘要，不存提示原文、响应正文或 key。

## 配置

| 键 | 说明 |
|---|---|
| `provider` | 固定 `openai`；任何 OpenAI 兼容服务都走这一项 + `base_url` |
| `model` | 必填 |
| `base_url` | 可选，兼容服务的 `/v1` 地址；`null` 用官方端点 |
| `api_key_env` | 存放 key 的**环境变量名**，默认 `OPENAI_API_KEY`；配置里不能写 key（未知键直接拒绝） |
| `timeout_s` | 单次请求超时，默认 120 |
| `response_format` | `json_schema`（默认）或 `json_object`（只支持 JSON mode 的服务；schema 会放进 system 消息） |

SDK 自动重试关闭（`max_retries=0`），每次尝试都进账本。

## 为什么不用 strict 模式

OpenAI 的 strict `json_schema` 要求每个对象把所有属性列进 `required`，可选字段只能写成 `anyOf [..., null]`。1a 的草稿 schema（`draft_json_schema()`，由 `request_json_schema()` 去掉有默认值字段的 `required` 派生）恰恰靠“不写就用默认值”工作，所以发 `strict: false`。schema 只是引导；合格与否一律由 `validate_request` 判定，跨字段约束（seed 不重叠、support 引用、相机能力）也只在那里检查。

## 默认值

默认值是契约的一部分，放在 `defaults.py` 的常量里（不放 `configs/`）：改默认值等于改 1a 的行为，应走代码评审和测试。

| 字段 | 默认 |
|---|---|
| `schema` | `datafarm.request/1` |
| `robot` | `airbot_single_g2` |
| `episodes` | 20 |
| `seeds` | `dev: [0, 99]`，`holdout: [1000, 1019]`（逐个子字段补） |
| `cameras` | `ROBOT_CAMERAS[robot]`，即 `[head, left_wrist]` |
| `render` | `engine: isaac_rtx`，`views: [cameras[0], third_person]`，`materials: 2`，`lights: 2` |
| `dataset.format` | `hdf5` |
| `request_id` | `task` 的 kebab-case slug（≤64 字符） |

句子给出的值（非 `null`）永远不被覆盖。补齐后仍冲突（如句子给的 `holdout` 与默认 `dev` 重叠）就报错，不自动修。`task` 与 `scene` 没有默认值。

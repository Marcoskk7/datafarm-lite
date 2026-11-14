# Capability 层与每任务产物

该发布把每个任务共享的部分（capability 层 K）与 agent 为单个请求生成的部分（任务产物 A_rho）区分开来。K 是仓库源码：角色目录、角色 prompt、阶段 skill、运行时指令、通用常设规则、渲染模板，以及 harness、ledger 与验证器代码。A_rho = {G, T, S, C, J, R, B}（grounding 记录、任务契约、场景/初始状态采样器、expert 控制器、judge 规范、渲染契约、batch/dataset 配置）由 agent 写入任务工作区。本文档涵盖把两者隔开的代码：显式加载 K 的角色目录与上下文编译器、指令层审计、常设规则 lint，以及固定一次 run 所用 K 版本的 source pin。

相关：[capability 演进](capability-evolution.md)（K 如何变化并被激活）、[agent runtime](agent-runtime.md)（谁调用编译器）、[成本控制](cost-control.md)。

## 保证

1. **一个角色的上下文只由目录为它指定的文件构建。** 编译器按该顺序加载 `common` 文档、`prompts/roles/<role>.md`、该角色的 `documents`，以及每个所列 skill 的 `.agents/skills/<skill>/SKILL.md`，并去重。它从不扫描 `runs/` 或任务工作区；`task_products_auto_loaded` 恒为 `false`。
2. **缺失的指令文件是一个错误，而不是静默省略。** 每个路径都以 `strict=True` 解析。
3. **任何指令文件都不得解析到仓库根目录之外**（例如通过符号链接）。
4. **每个被加载的文件与最终消息列表都会被哈希。** receipt 逐文件列出 `{path, sha256}`，并列出覆盖消息规范 JSON 的 `messages_sha256`。
5. **只有通用常设规则进入上下文，且不带其来源字段。** `scope` 不是 `universal` 的规则（例如 `task:<family>`）被排除；`waivers`、`known_violations`、`source` 与 `globs` 会被剥离。
6. **上下文绝不会覆盖已存在的上下文。** 非空目标会被拒绝。
7. **已准备的任务绑定到一个内容寻址的源码快照。** `prepare` 写入 `capability.json`，其中指名一个 capability 版本，其 `source` 是某个文件清单的 SHA-256。
8. **发生变化的快照或版本记录会在使用前被检测到。** 解析 pin 时会重新哈希快照中的每个文件与版本记录。
9. **快照不含符号链接，也不含运行时/凭据目录。** 符号链接的文件或目录会被拒绝；`EXCLUDED` 名称（`.git`、`.datafarm`、`runs`、`secrets`、`keys`、`.env`、`data`、`external` 等）以及 `*.pyc`/`*.log` 会被跳过。
10. **激活新版本不会移动已有的 pin。** 一次 run 保持其 `capability.json` 中写入的版本；激活与回滚只改变 `active.json`。
11. **已 pin 的任务命令执行已 pin 的代码。** 当 pin 与所调用的 checkout 不同时，`context`、`drive`、`verify`、`cost-status`、`wait` 与 `supervise`（带 `--work`）会重新执行 `<pinned>/tools/pipeline_agent.py`。
12. **新的用户需求能到达已 pin 的 run；学到的经验保持 pin。** experience 块按会话用已 pin 版本选取：所有匹配且未被取代的 `user-requirement` 事件，加上该版本至多 8 条经验。
13. **常设规则 lint 在任何未列出的违规上失败。** 已知违规（带 owner 与 reason）会被报告但通过；过期的已知条目会被报告。
14. **当某条通用指令携带机器本地权限时，指令审计失败。**

代码：`src/datafarm/agent/context.py`、`src/datafarm/evolution/integration.py`、`src/datafarm/evolution/versions.py`、`src/datafarm/evolution/events.py`、`tools/pipeline_agent.py`、`tools/lint_standing_rules.py`、`tools/audit_instruction_layers.py`。

## 数据模型

| 文件 | Schema / 必需字段 |
|---|---|
| `configs/agent-instructions.json` | `schema: datafarm.agent-instructions/1`；`common`（仓库路径列表）；`roles.<name>.skills`、`roles.<name>.documents`；`task_products`（信息性字符串，代码从不会读取） |
| `<out>/messages.json` | `[{role: system, content}, {role: user, content}]` |
| `<out>/context.md` | 同样的消息，以文本形式 |
| `<out>/receipt.json` | `schema: datafarm.agent-context/1`、`role`、`files[{path, sha256}]`（最后一项为 `configs/standing-rules.json`，带一条 `selection` 说明）、`messages_sha256`、`task_products_auto_loaded: false`、`experience` |
| `configs/standing-rules.json` | `schema: datafarm.standing-rules/1`；每条规则含 `id`（唯一）、`statement`、`globs`（均必需），可选 `scope`、`forbidden[{regex, why}]`、`numeric_min[{key, min, why}]`、`required_json[{key, allowed}]`、`waivers[{path}]`、`known_violations[{path, ...}]`、`review_audit` |
| `<work>/capability.json` | `schema: datafarm.run-capability/1`、`store`、`version`、`scope`（含 `run_id`）、`managed`、`experience` |
| store `versions/<id>` | `schema: datafarm.capability-version/1`、`source`、`parent`、`lessons`、`evaluation`、`id` = 其余内容的摘要 |
| store `sources/<digest>/` + record | 复制出的文件；record 为 `{id, files: {path: {sha256, executable}}}`，`id` = `files` 的摘要 |

当前目录：5 个 `common` 文档与 7 个角色（`orchestrator`、`task-designer`、`task-expert-author`、`task-verifier`、`render-reviewer`、`verifier-auditor`、`pipeline-improver`）。调用方：`tools/pipeline_agent.py`（orchestrator、task-verifier）、`src/datafarm/expert/round.py`（task-expert-author）、`src/datafarm/evolution/controller.py`（pipeline-improver）。`task-designer`、`render-reviewer` 与 `verifier-auditor` 仅通过 `context --role <role>` 加载。

清单层级（`audit_instruction_layers.py::inventory` 中的路径规则分类）：`universal-instruction`、`universal-runtime-schema`、`deployment-or-device-profile`、`runtime-implementation`、`task-product-or-upstream-reference`、`task-product-or-tool-schema`、`task-product-template-or-example`、`test-evidence`、`implementation-reference`。

## 工作原理

上下文编译（`build_context`）：
1. 读取目录；拒绝未知角色。
2. 构建路径列表（common、角色 prompt、documents、skills）；去重并保持顺序。
3. 对每个路径：严格解析、拒绝逃逸、对字节做哈希、追加一个 `--- PROJECT INSTRUCTION: <path> ---` 小节。
4. 追加通用常设规则（字段已过滤）并对规则文件做哈希。
5. 前置一段固定前言（仓库自有的指令、角色职责边界、任务输入即证据、仓库路径）。
6. 用户消息：调用方的文本（对任务会话为 `pipeline_agent.py::user_context`：request、输入清单、当前状态、成本状态）加上作用域内的 experience 块。
7. 对消息做哈希；`write_context` 写入 `messages.json`、`context.md`、`receipt.json`。

源码 pin：
1. `prepare` 调用 `bind(work, ROOT, store_path=--evolution-store)`。
2. 没有 `--evolution-store` 时，store 为 `<checkout>/.datafarm/evolution`；所调用的 checkout 会被快照并创建一个版本（不激活）。有它时，使用该 store 的活动版本（或该 checkout 成为初始活动版本）。
3. `snapshot` 列出文件（在 Git checkout 中用 `git ls-files --cached --others --exclude-standard`，否则用一次过滤遍历），将它们复制到临时目录、重新哈希，并重命名到 `sources/<manifest digest>`；已存在的摘要会被复用。
4. 之后带 `--work` 的命令调用 `source_for`，它会验证快照并重新执行已 pin 的 `tools/pipeline_agent.py`；在快照内部 `ROOT` 等于该 pin，因此上下文与原生 driver 都来自已 pin 的文件。

Lint：对每条规则，对文件做 glob（规则文件本身除外），应用 `forbidden` 正则（大小写不敏感）、`numeric_min`（JSON 值或 `key: number` 文本）、`required_json`；将每个发现标记为 `waived`、`known` 或 `new`；若有任何 `new` 则以 1 退出。

审计：构建每个角色的上下文以得到已加载集合，列出仓库文件，按路径规则对每个 `.md/.txt/.json/.yaml/.yml/.py` 文件分类，记录名称匹配 `prompt|system_message` 的 Python 赋值，并标记那些包含站点特定文件系统前缀、`ssh -o` 命令或主机别名的通用指令文件。

## Prompt 组装

项目掌控消息体与组装过程。它不能把宿主的隐藏 system prompt 当作仓库资产。

- 每条策略都有一个来源。`configs/standing-rules.json` 注册规则 ID，`prompts/instructions/` 解释边界，渲染模板实现文本要求，任务 overlay 只补充具体细节，skill 承载流程，验证器规范实现可测试的 judge。一处改动要更新每个消费方并通过 lint。
- `prompts/render/` 模板使用 `${UPPER_SNAKE_CASE}` 槽位。缺失或未知的变量、残留的槽位或相互冲突的 overlay 规则会阻塞提交。替换器只组装文本；它不运行 shell 或 eval。组装顺序：common、一个显式模式模板、任务 overlay，然后是冻结的事件与连续性内容。
- expert 轮次的首条、续接与最终消息位于 `prompts/sessions/`，彼此不可互换。角色正文位于 `prompts/roles/`，由 launcher 注入。任务 adapter 声明一个具体的 author 后端与模型。
- `pipeline_agent.py context` 与 `drive` 以及 expert-harness 轮次会显式加载角色。其他工具内部的 prompt 不会被当作通用指令注入。

## 入口

```bash
python tools/pipeline_agent.py context --role orchestrator --output /data/ctx          # bare role context
python tools/pipeline_agent.py context --work /data/task --role task-verifier --output /data/ctx2
python tools/lint_standing_rules.py --json
python tools/audit_instruction_layers.py
```

## 配置

| 项 | 默认值 | 来源 |
|---|---|---|
| `context --role` | `orchestrator` | `tools/pipeline_agent.py::main` |
| `prepare --evolution-store` | 未设置：私有 store `<checkout>/.datafarm/evolution`，`managed: false` | `integration.py::bind` |
| `prepare --scope` | JSON 选择器，与自动的 `run_id` 合并 | `integration.py::bind` |
| 每个上下文的经验条数 | 8（`limit`） | `events.py::select` |
| `lint_standing_rules.py --root`、`--rules`、`--json` | 仓库根、`<root>/configs/standing-rules.json`、文本 | `lint_standing_rules.py::main` |

## 失败行为

- 未知角色 → `ValueError`；缺失文件 → `FileNotFoundError`；逃逸路径或非空目标 → `ValueError`。`pipeline_agent.py` 打印错误并以 2 退出。
- 已 pin 的快照或版本发生变化 → 抛出 `ValueError('pinned source changed' / 'capability
  version changed')` on every command with `--work`；不会自动重新 pin。操作者必须恢复该快照或新建一个工作区。
- 输入文件发生变化 → `context`/`drive` 在任何模型调用之前于 `task/contract.py::verify_input` 中失败。
- 在复制过程中发生变化的快照会被丢弃（`source changed while creating snapshot`）。
- Lint：出现新违规时以 1 退出；规则文件会先被校验（重复 id、缺失字段、错误正则时抛 `ValueError`）。
- 审计：当 `errors` 非空时以 1 退出。

## 局限

- **Pin 范围。** pin 只覆盖仓库文件，不覆盖 Python 解释器、已安装的包、外部 CLI（例如 Codex 可执行文件）、provider 模型或 GPU 镜像。
- **未被重新执行的命令。** `prepare`、`stop`、`runtime-status`、`feedback` 与 `context` 在不带 `--work` 时运行所调用的 checkout。没有 `capability.json` 的工作区回退到所调用的 checkout（`integration.py::source_for`）。
- **其他入口未 pin。** run ledger、batch supervisor 与 expert harness 不绑定 capability 版本；ledger 只记录一个 Git 指纹（`ledger.py::git_fingerprint`）。
- **在测试下改动 K 是一条流程规则。** 代码保护已 pin 的 run 免受后续编辑，演进控制器会评估候选，但直接的源码编辑不受测试把关。
- **消息的可复现性。** `messages_sha256` 覆盖仓库路径、工作区路径、当前 `state.json` 与成本状态，因此它按工作区、按会话变化。每个会话的 `messages.json` 都会被保存；之后重新生成完全相同的字节并不保证。
- **运行时审查只用一个角色。** 由 `drive` 派发的审查者总是使用 `task-verifier`，渲染后 episode 的审查也是如此。`render-reviewer` 与 `verifier-auditor` 仅通过 `context --role` 加载。
- **审计是基于路径的。** 层级来自路径前缀而非内容；机器本地检查是一条固定正则，只施加于 `universal-instruction` 层。
- **Lint 是词法层面的。** 在配置的 glob 上做正则与数值键检查；它无法检测被改写的矛盾，或仅在运行时才被强制的规则。只有规则 SR-005（字面凭据）覆盖 `docs/**/*.md`。
- **渲染模板** 由 `src/datafarm/render/policy.py::load_contract` 保护，它会从当前模板重新编译契约；参见[渲染契约](render-contract.md)。

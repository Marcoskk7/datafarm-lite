---
name: pipeline-improve
description: 将 pipeline 反馈和修复证据转化为有范围的教训与经过评估的能力变更。用于反复出现的任务失败、用户纠正，或有已证明可复用的修复之后；任务专属 adapter 仍属生成的产物。
---

通过 `tools/pipeline_evolve.py` 开展工作；阅读 `prompts/instructions/self-improvement.md`
了解 event、proposal、driver 和 evaluator 契约。宿主控制评估与激活。绝不要把
proposal、通过的软件 fixture 或变化的分数报告为真实机器人/视频质量或递归改进的证据。

先确定实际输入、版本、观测和当前用户需求。把需求与疑似原因区分开。保持范围：run、
task family、硬件和 provider 知识不必成为通用规则。以原始 event 作为来源凭据；
不要虚构对先前对话的访问。

尽可能修复当前任务。对于可复用的变更，提出可证伪的原因和至少一个替代解释，
然后只编辑候选源。把 adapter、任务提示和任务阈值留在任务产物中。当行为需要时，
一项通用变更可以同时触及 skills、prompts、tools 和 workflows。加入有意义的失败回放
以及成功/无关对照组；不要为了获得分数而放宽 judge 或改写证据。evaluator 的修复要遵循
另行评审的策略修订和双重评估，位于优化补丁之外。

把 proposal 写到宿主提供的路径。包含 event_ids、hypothesis、
expected_effect、alternative_explanation 和可选的 lessons（problem、action、
rationale、limits、exact scope）。在显式评估测试其用途之前，lessons 仍是假设。
重试前先阅读先前的拒绝/错误证据。避免在没有新证据或显式重试请求的情况下重复同一 proposal。

在日常仓库工作中，通过 event 接口捕获可见的用户反馈、Git 变更和测试结果。
只对真实的用户需求使用 requirement events；观测和反思不能自行授予权限。
已有的用户授权继续有效。在配置的资源预算处停止；不要为每个可逆修复都增加审批问题。

记录和决策不会自动成为运行时指令。持久化简洁的证据和失败的假设，以便另一个 agent
可以继续。在声称自主性之前，先阅读该 workflow 的限制。

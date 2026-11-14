# H3 渲染评估器（EVAL-v1）

用于 H3 control-hybrid 渲染器的评估器资产（`docs/reference/rendering.md`）。它们衡量候选结果，
并受保护免受自动修改（`evaluations/` 位于 `src/datafarm/evolution/controller.py` 的 PROTECTED 中）。

- `h3eval.py` — 自动评分器（`realpool`、`score`、`pack`、`table`）：相对
  white carrier 的运动/结构相关性，相对真实相机池的 KID/nn-cos/Lab，跳变/翻转/冻结。与
  渲染器无关（H3 和 Seedance 由同一份代码评分）。传 `--dino` 或设置 `H3_EVAL_DINO`（DINOv2-base 目录）；没有
  内置默认值。
- `reviewer-instructions-v1.md` — 独立盲评审者（验收权限）的指令。
- `prompts/` — 生产侧的 seed 筛选 prompt（`seed-screen.txt`；筛选不是验收）以及
  盲评审 agent prompt。它们假设双 Piper T-layout 装置（头部在上，腕部在下）。

更改评分规则或评审者指令属于评估器修订（新版本 + 独立
校准），绝不作为渲染器候选的一部分。像素指标不能替代盲评审：它们会遗漏
内容错误（线缆、额外物体、模拟白色的手臂），而这些是 VLM 筛选和评审者能捕捉的。

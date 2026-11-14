# 渲染模板用法

common-v1.txt 加上一个已确认的模式模板构成核心文本。任务参数来自经过审查的 Task/Episode 契约；每个 `${...}` 都是声明的模板槽位，必须在运行前展开并验证。禁止把原始模板发送到 API。

| 槽位 | 权威输入 |
|---|---|
| MODE | 范围匹配的已确认 run 意图 |
| TASK_DESCRIPTION | 当前任务 brief |
| REAL_REFERENCE_MAP | 实际 payload 中的真实图像槽位和源角色 |
| SOURCE_VIDEO_ROLE | 实际 payload 中的白模视频槽位 |
| CAMERA_TIMING_CONTRACT | 相机、面板、FPS、片段起止、输出时序 |
| TASK_EVENT_CONTRACT | 本片段的物体身份/数量、阶段、事件和最终状态 |
| CONTINUITY_CONTRACT | 本片段与上一片段的关系及连续性参考；不替代真实图像 |
| REAL_BASELINE_DESCRIPTION | 绑定的真实目标和照片描述 |
| APPEARANCE_INVARIANTS | 不得改变的身份、材质/颜色及类似属性 |
| AUTHORIZED_OOD_AXES | 明确批准的 OOD 轴和范围；仅在 OOD 中使用 |

H3 control hybrid 还带有 caption 风格的 lowering 模板 `h3-caption-noevents-v1.txt` 和 `h3-caption-notask-v1.txt`：`render_policy.lower_h3_prompt` 只从编译后的契约（意图、overlay）确定性地展开它们。`-noevents` / `-notask` 分别丢弃事件契约 / 整段任务句，而 `h3-caption-chunkvis-*` lowering 会追加从白模测量的闭合 schema 段内可见性句子。`check_h3_payload` 按清单的 prompt_lowering 重新计算每个作业并逐一比较；不得追加任何自由文本。

Seedance/H3 在槽位编号、图像预处理和上下文能力上不同：adapter 必须从实际 payload 生成映射，核心模板不得假定 IMAGE1/IMAGE2。物体数量、阶段和连续性约束属于任务 overlay，并对照 run 契约验证。`source-only` 配方不可执行，且一次 OOD 运行的授权不得复制到另一次。

这些模板已接入 tools/render_policy.py 以及 Seedance/H3 提交检查。正确的静态规则并不意味着模型会遵守；用 canary 和独立视频审查来验证。

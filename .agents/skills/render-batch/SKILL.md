---
name: render-batch
description: 运行或续接一个以真实参考接地、范围明确的 DataFarm 渲染批次，带 canary 与独立评审。
---

# render-batch

在做任何渲染/续接/扩量决定之前，先阅读 `prompts/instructions/mode-and-style-contract.md` 和 `prompts/instructions/render.md`。

H3 指 control hybrid（MiniMax-H3 Ref2VA + ControlNet-Union soft-HED、独立 chunk、8 个 seed + 每 chunk 的 VLM 筛选、首 chunk 曝光锁定）：阅读 `docs/reference/rendering.md`，从 `configs/render/h3-control-hybrid-v1.json` 开始（OOD 桌面/光照：`configs/render/h3-ood-restyle-v1.json`）并运行 `src/datafarm/render/h3/`；只通过独立的 EVAL-v1 盲审（`evaluations/render-h3/`）接受结果。bundle、参考和 recipe 副本是你为当前任务派生出的任务产物。

1. 从明确的、在范围内的用户指令确定模式，否则询问并等待。OOD 和非 OOD 都要求真实世界图像以及覆盖整个场景的真实世界输出风格。OOD 只允许在记录的基线上做获批的、物理上合理的真实世界改变。绝不要根据 provider、任务名、几何随机化或另一个任务的 recipe 来推断 OOD。
2. 绑定真实图像的来源/哈希并验证实际的 provider payload；拒绝只有来源和缺失/仅结构化的参考。不要通过丢弃所有真实图像来诊断泄漏。
3. 通过 datafarm-run-launch 注册/启动。遵守本部署的 GPU 和 provider 权限；控制成本与重试。
4. 先做 canary：使用有代表性的小 Seedance 样本；H3 先跑两个 prefix，再跑八样本 canary，在首次渲染前固定到 `h3_gate.py` gate 中并用 `h3_enqueue.py --gate` 强制执行。亲自解码并并排查看实际的真实参考/episode 与生成视频。独立检查真实基线、机器人本体、安装的相机/线缆以及任务物体（存在时）、桌面/背景，以及所有任务阶段中每个任务声明视图的摄影一致性。将缺失的组件记录为有证据的不适用，而不是凭空编造。局部纹理/裁剪不是全场景覆盖；prompt 文本和文件哈希不是可见采用的证据。在拿到 canary 证据前不要扩量。
5. 流式检查，遇到系统性早期失败就暂停，使用共享的 batch supervisor 做有界重试/退避。把基础设施故障与质量拒收区分开；保留固定的分母和替换谱系。
6. 根据 `configs/render-verifier/template.json` 把本任务的 render verifier spec 写入任务 workspace，遵循 `docs/reference/render-verifier.md`。用 `tools/calibrate_render_verifier.py` 在一个已接受的 canary 渲染上校准它，并在每个生成 episode 完成时对其运行 `tools/verify_render.py`。未经校准的 spec 不是证据。在 H3 路线上，verifier 与 EVAL-v1 并行运行；两者都不能替代独立评审。
7. 在报告缺陷前，独立复核 verifier 的拒收。允许真实世界的硬阴影和获批的 OOD 变化；60/255 的总曝光默认值不是真实性的判决。
8. 分别报告风格、参考遵循度、结构、覆盖率和未验证项。为每个必需的外观区域产出绑定后的并排对比图像和生成帧索引，以及 ledger 完成记录。真实参考只支配外观：绝不复制它们的动作/布局，也不要添加源 carrier 中不存在的实体。任务特定的外观契约应放在该任务自己的 configs 和 docs 中。

部署范围：GPU 和付费 provider 的授权来自当前部署和 session。获得仓库不授予任何授权，已经给出的权限不要重复询问。

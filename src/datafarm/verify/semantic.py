"""使用自监督 DINOv2 特征检查“不支持的实体”。

颜色无法区分黑色机器人连杆与深色投影或深色墙，
因此幻觉出的手臂在特征空间中判定。每个视频、每个
panel：

1. *生成*帧的 DINOv2 patch 特征汇总到 patch 网格上。
2. carrier 标记为该实体的 patch（经腐蚀）给出实体
   原型（k-means）；远离任何 carrier 实体的 patch 给出背景。
3. 当某个 patch 到实体原型的最佳余弦相似度
   超过 ``tau = max(entity p10, background p99.5)`` 时，它“看起来像该实体”。若这两个
   分布重叠（``background p99.5 >= entity p50``），则特征
   在此无法区分该实体，panel 弃权。
4. 看起来像实体、且一个 patch 内没有 carrier 实体的 patch 构成 blob；
   在 ``persist_samples`` 个采样帧中持续存在、至少有 ``min_patches`` 个 patch 的
   blob 即为幻觉出的实体。

模型路径来自环境变量（``DATAFARM_DINOV2``），绝不出现在代码中。
torch/transformers 惰性导入，以保持核心包轻量。
"""

from __future__ import annotations

import os
from typing import Any

import cv2
import numpy as np

from datafarm.verify.checks import ABSTAIN, FAIL, NA, PASS, CheckResult, Context, Evidence, persistent_runs

_MODEL: dict[str, Any] = {}


def _load(device: str | None = None):
    if "model" in _MODEL:
        return _MODEL["model"], _MODEL["device"]
    path = os.environ.get("DATAFARM_DINOV2")
    if not path:
        raise RuntimeError("DATAFARM_DINOV2 is not set")
    import torch
    from transformers import AutoModel

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model = AutoModel.from_pretrained(path).to(device).eval()
    if device == "cuda":
        model = model.half()
    _MODEL.update(model=model, device=device)
    return model, device


def patch_features(crops: np.ndarray, grid: tuple[int, int], batch: int = 32) -> np.ndarray:
    """[N,H,W,3] BGR 裁剪 -> L2 归一化的 [N,gh,gw,C] patch 特征。"""

    import torch

    model, device = _load()
    gw, gh = grid
    size = (gw * 14, gh * 14)
    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    out = []
    for start in range(0, len(crops), batch):
        chunk = [cv2.cvtColor(cv2.resize(c, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
                 for c in crops[start:start + batch]]
        x = (np.stack(chunk).astype(np.float32) / 255.0 - mean) / std
        t = torch.from_numpy(x).permute(0, 3, 1, 2).to(device)
        if device == "cuda":
            t = t.half()
        with torch.no_grad():
            hidden = model(pixel_values=t).last_hidden_state[:, 1:, :]
        feats = torch.nn.functional.normalize(hidden.float(), dim=-1).cpu().numpy()
        out.append(feats.reshape(len(chunk), gh, gw, -1))
    return np.concatenate(out)


def _pool(mask: np.ndarray, grid: tuple[int, int]) -> np.ndarray:
    gw, gh = grid
    return cv2.resize(mask.astype(np.float32), (gw, gh), interpolation=cv2.INTER_AREA)


def _kmeans(x: np.ndarray, k: int) -> np.ndarray:
    k = max(1, min(k, len(x)))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-4)
    _, _, centers = cv2.kmeans(x.astype(np.float32), k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    return centers / np.linalg.norm(centers, axis=1, keepdims=True).clip(1e-6)


def check_semantic_unsupported(ctx: Context, entity, *, sample_every: int = 4,
                               min_patches: int = 6) -> CheckResult:
    name = f"entity_{entity.role}_semantic"
    spec = ctx.spec
    try:
        _load()
    except Exception as exc:  # 模型不可用：缺少所请求的权威
        return CheckResult(name, "entity", ABSTAIN, reason=f"semantic model unavailable: {exc}")
    persist = int(spec.threshold("persist_samples"))
    idx = np.arange(0, ctx.samples, sample_every)
    carrier = ctx.mask("carrier", entity)
    others = None
    for other in spec.entities:
        if other.role != entity.role:
            m = ctx.mask("carrier", other)
            others = m if others is None else (others | m)
    metrics: dict[str, Any] = {}
    evidence: list[Evidence] = []
    failed, abstained = [], []
    panels = [p for p in spec.entity_panels(entity) if "semantic_unsupported" in entity.active_checks(p.name)]
    if not panels:
        return CheckResult(name, "entity", NA, reason="no panel requests the semantic check")
    for panel in panels:
        rows, cols = ctx.work_slice(panel)
        h, w = rows.stop - rows.start, cols.stop - cols.start
        # 约每 20 个 work 像素（40 个布局像素）一个 patch
        gw, gh = max(8, round(w / 20)), max(6, round(h / 20))
        grid = (gw, gh)
        feats = patch_features(ctx.generated.frames[idx][:, rows, cols], grid)
        own = np.stack([_pool(carrier[j][rows, cols], grid) for j in idx])
        anything = own.copy()
        if others is not None:
            anything = np.maximum(anything, np.stack([_pool(others[j][rows, cols], grid) for j in idx]))
        ent_sel = own >= 0.7
        near = np.stack([cv2.dilate((a > 0.02).astype(np.uint8), np.ones((3, 3), np.uint8)) for a in anything]) > 0
        # 幻觉必须位于 carrier 显示为空工作单元的位置：任何
        # 模拟器内容（例如深色键漏掉的浅灰色连杆）都算作
        # 支撑。
        fg = ctx.carrier_foreground()
        content = np.stack([np.maximum(_pool(fg[j][rows, cols], grid), o) for j, o in zip(idx, own)])
        own_near = np.stack([cv2.dilate((o > 0.05).astype(np.uint8), np.ones((3, 3), np.uint8)) for o in content]) > 0
        bg_sel = ~near
        if ent_sel.sum() < 30 or bg_sel.sum() < 100:
            metrics[panel.name] = {"status": "too_few_patches", "entity_patches": int(ent_sel.sum())}
            abstained.append(panel.name)
            continue
        protos = _kmeans(feats[ent_sel], 4)
        sim = (feats.reshape(-1, feats.shape[-1]) @ protos.T).max(axis=1).reshape(feats.shape[:3])
        ent_sim, bg_sim = sim[ent_sel], sim[bg_sel]
        p10, p50 = float(np.percentile(ent_sim, 10)), float(np.percentile(ent_sim, 50))
        bg995 = float(np.percentile(bg_sim, 99.5))
        tau = max(p10, bg995)
        metrics[panel.name] = {"entity_sim_p10": round(p10, 3), "entity_sim_p50": round(p50, 3),
                               "background_sim_p995": round(bg995, 3), "tau": round(tau, 3), "grid": [gw, gh]}
        if bg995 >= p50:
            metrics[panel.name]["status"] = "features_do_not_separate"
            abstained.append(panel.name)
            continue
        flags = np.zeros(len(idx), bool)
        boxes: list = [None] * len(idx)
        for t in range(len(idx)):
            cand = (sim[t] >= tau) & ~own_near[t]
            count, labels, stats, _ = cv2.connectedComponentsWithStats(cand.astype(np.uint8), connectivity=8)
            if count > 1:
                big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                if stats[big, cv2.CC_STAT_AREA] >= min_patches:
                    flags[t] = True
                    x, y, bw, bh = stats[big, :4]
                    boxes[t] = (x * w / gw, y * h / gh, bw * w / gw, bh * h / gh)
        runs = persistent_runs(flags, persist)
        metrics[panel.name]["flagged_samples"] = int(flags.sum())
        if runs:
            failed.append(panel.name)
            for a, b in runs[:4]:
                mid = next(t for t in range((a + b) // 2, b + 1) if boxes[t] is not None)
                frame = int(idx[mid] * ctx.carrier.stride)
                evidence.append(Evidence(panel.name, f"{entity.role}_semantic_unsupported", frame,
                                         bbox=ctx.to_layout(boxes[mid], panel, ctx.carrier.work_scale),
                                         detail=f"persists frames {int(idx[a] * ctx.carrier.stride)}-"
                                                f"{int(idx[b] * ctx.carrier.stride)}"))
    if failed:
        return CheckResult(name, "entity", FAIL, reason="hallucinated " + entity.role + " in " + ", ".join(failed),
                           metrics=metrics, evidence=evidence)
    if abstained and len(abstained) == len(panels):
        return CheckResult(name, "entity", ABSTAIN, reason="features cannot judge: " + ", ".join(abstained),
                           metrics=metrics)
    return CheckResult(name, "entity", PASS, metrics=metrics,
                       reason=("unjudged panels: " + ", ".join(abstained)) if abstained else "")

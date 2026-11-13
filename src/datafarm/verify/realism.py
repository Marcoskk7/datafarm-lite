"""Realism：渲染出的外观是否接近真实 workcell？

权威是同一 embodiment/workcell 的真实影像（Grounding IR），
绝不是外观参考图片或 prompt。对每个 panel，把帧
划分为若干区域——每个实体一个，加上背景——并将简单、
可解释的统计量与真实帧中的同一区域比较：

* ``color``      CIELAB 平均颜色（依赖光照）
* ``contrast``   CIELAB 亮度扩散（依赖光照）
* ``chroma``     区域颜色相对于帧平均颜色（grey-world
                 归一化，因此全局色偏会抵消）
* ``texture``    梯度幅值直方图
* ``sharpness``  log Laplacian 方差（模糊/过度平滑）
* ``noise``      log 高频残差（传感器颗粒 vs 塑料感）

每个距离都除以固定容差，因此分数 1 大致相当于一个
刚好可察觉的差异。当每个活跃特征都在
``realism_feature_score_max`` 个真实分布容差之内时，区域通过。

sim2real cohort 对每个区域使用每个特征。OOD cohort 保留不变
实体（机器人、task 物体）与现实的绑定，但丢弃所声明轴
所扰动的内容：lighting 丢弃 ``color``/``contrast``（``chroma`` 保留，因此机器人
在新光照下仍须看起来像它自己）；background/table 轴丢弃
背景区域。没有任何规则偏好软光或硬光：如果真实房间有硬
阴影，渲染也应有；只要不变实体能存活，OOD lighting
就是自由的。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from datafarm.verify.checks import ABSTAIN, FAIL, NA, PASS, CheckResult, Context, Evidence, _disk

TOLERANCE = {"color": 8.0, "contrast": 5.0, "chroma": 6.0, "texture": 0.25, "sharpness": 0.35, "noise": 0.30}
LIGHTING_FEATURES = ("color", "contrast")
BACKGROUND_AXES = ("background", "table_material", "distractors")
TEXTURE_BINS = np.array([0, 2, 4, 8, 16, 32, 64, 1e9], np.float32)


def _region_features(panel_bgr: np.ndarray, mask: np.ndarray) -> dict[str, np.ndarray] | None:
    if mask.sum() < 64:
        return None
    lab = cv2.cvtColor(panel_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[..., 0] *= 100.0 / 255.0
    lab[..., 1:] -= 128.0
    pixels = lab[mask]
    frame_mean = lab.reshape(-1, 3).mean(axis=0)
    lightness = lab[..., 0]
    gx = cv2.Sobel(lightness, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(lightness, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.magnitude(gx, gy)[mask]
    hist = np.histogram(grad, bins=TEXTURE_BINS)[0].astype(np.float32)
    hist /= max(float(hist.sum()), 1.0)
    lap = cv2.Laplacian(lightness, cv2.CV_32F)[mask]
    residual = (lightness - cv2.GaussianBlur(lightness, (0, 0), 1.0))[mask]
    return {
        "color": pixels.mean(axis=0),
        "contrast": np.array([pixels[:, 0].std()]),
        "chroma": pixels.mean(axis=0) - frame_mean,
        "texture": hist,
        "sharpness": np.array([np.log10(float(lap.var()) + 1e-3)]),
        "noise": np.array([np.log10(float(np.median(np.abs(residual))) + 1e-3)]),
    }


def _distance(feature: str, a: np.ndarray, b: np.ndarray) -> float:
    if feature == "texture":
        return float(np.abs(a - b).sum())
    return float(np.linalg.norm(a - b))


def _collect(frames: np.ndarray, masks: dict[str, np.ndarray], rows, cols, indices,
             min_frames: int = 3) -> dict[str, dict[str, np.ndarray]]:
    """在所选帧上按区域计算中位特征向量。

    在少于 ``min_frames`` 帧中出现的区域不会被刻画：少量
    特写不足以可靠地反映真实物体看起来的样子。
    """

    per_region: dict[str, dict[str, list[np.ndarray]]] = {}
    for j in indices:
        crop = frames[j][rows, cols]
        for region, mask in masks.items():
            feats = _region_features(crop, mask[j][rows, cols])
            if feats is None:
                continue
            slot = per_region.setdefault(region, {})
            for name, value in feats.items():
                slot.setdefault(name, []).append(value)
    return {region: {name: np.median(np.stack(values), axis=0) for name, values in feats.items()}
            for region, feats in per_region.items() if feats and len(next(iter(feats.values()))) >= min_frames}


def _region_masks(ctx: Context, which: str) -> dict[str, np.ndarray]:
    masks: dict[str, np.ndarray] = {}
    union = None
    for entity in ctx.spec.entities:
        # 渲染区域来自 carrier（几何权威）；真实
        # 区域来自目标外观 key。
        mask = ctx.mask("carrier" if which == "generated" else "real", entity)
        masks[entity.role] = mask
        union = mask if union is None else (union | mask)
    base = ctx.generated.frames if which == "generated" else ctx.real.frames  # type: ignore[union-attr]
    count = len(base) if which == "real" else ctx.samples
    if union is None:
        union = np.zeros((count,) + base.shape[1:3], bool)
    background = np.zeros_like(union)
    disk = _disk(max(2, int(0.02 * base.shape[2])))
    for j in range(len(union)):
        background[j] = ~(cv2.dilate(union[j].astype(np.uint8), disk) > 0)
    masks["background"] = background
    return masks


def active_features(ctx: Context, region: str) -> tuple[str, ...]:
    realism = ctx.spec.realism
    features = tuple(TOLERANCE)
    if realism.mode == "ood":
        axes = set(realism.perturbed_axes)
        if region == "background" and axes & set(BACKGROUND_AXES):
            return ()
        if "lighting" in axes or "camera_exposure" in axes:
            features = tuple(f for f in features if f not in LIGHTING_FEATURES)
        entity = next((e for e in ctx.spec.entities if e.role == region), None)
        if entity is not None and not entity.invariant:
            return ()
    return features


def score_against_real(ctx: Context, frames: np.ndarray, masks: dict[str, np.ndarray],
                       indices, real_feats: dict[str, dict[str, dict[str, np.ndarray]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for panel in ctx.spec.panels:
        rows, cols = ctx.work_slice(panel)
        feats = _collect(frames, masks, rows, cols, indices)
        panel_out = {}
        for region, reference in real_feats.get(panel.name, {}).items():
            active = active_features(ctx, region)
            if not active or region not in feats:
                continue
            scores = {f: round(_distance(f, feats[region][f], reference[f]) / TOLERANCE[f], 3) for f in active}
            panel_out[region] = scores
        out[panel.name] = panel_out
    return out


def check_realism(ctx: Context) -> CheckResult:
    spec = ctx.spec
    if ctx.real is None or not len(ctx.real.frames):
        return CheckResult("realism_vs_real_world", "realism", ABSTAIN,
                           reason="no real-world reference footage bound in Grounding")
    real_masks = _region_masks(ctx, "real")
    count_real = len(ctx.real.frames)
    real_idx = np.linspace(0, count_real - 1, min(spec.realism.real_frames, count_real)).astype(int)
    real_feats = {}
    real_rows = {}
    for panel in spec.panels:
        rows, cols = ctx.work_slice(panel, ctx.real)
        real_rows[panel.name] = (rows, cols)
        real_feats[panel.name] = _collect(ctx.real.frames, real_masks, rows, cols, real_idx,
                                          min_frames=max(3, len(real_idx) // 4))
    gen_masks = _region_masks(ctx, "generated")
    gen_idx = np.linspace(0, ctx.samples - 1, min(24, ctx.samples)).astype(int)
    generated = score_against_real(ctx, ctx.generated.frames, gen_masks, gen_idx, real_feats)
    carrier = score_against_real(ctx, ctx.carrier.frames, gen_masks, gen_idx, real_feats)
    # Real-vs-real 基线：真实影像的前半段对比后半段。
    half = len(real_idx) // 2
    baseline = {}
    if half >= 3:
        first = {}
        for panel in spec.panels:
            rows, cols = real_rows[panel.name]
            first[panel.name] = _collect(ctx.real.frames, real_masks, rows, cols, real_idx[:half])
        baseline = score_against_real(ctx, ctx.real.frames, real_masks, real_idx[half:], first)
    limit = spec.threshold("realism_feature_score_max")
    advisory = spec.threshold("realism_feature_advisory")
    failures, evidence, warnings = [], [], []
    judged = 0
    for panel, regions in generated.items():
        for region, scores in regions.items():
            judged += 1
            # 真实影像本身会变化（移动的腕部相机会随时间看到
            # 不同的东西）；这种自然散布允许叠加在
            # 固定容差之上。
            spread = baseline.get(panel, {}).get(region, {})
            allowed = {f: limit + 1.5 * float(spread.get(f, 0.0)) for f in scores}
            bad = {f: s for f, s in scores.items() if s > allowed[f]}
            soft = {f: s for f, s in scores.items() if advisory + 1.5 * float(spread.get(f, 0.0)) < s <= allowed[f]}
            if soft:
                warnings.append(f"{panel}:{region}:" + "/".join(f"{f}={v}" for f, v in sorted(soft.items())))
            if bad:
                failures.append(f"{panel}:{region}:" + "/".join(sorted(bad)))
                evidence.append(Evidence(panel, "unrealistic_appearance", 0,
                                         detail=f"{region} " + ", ".join(f"{f}={s}" for f, s in bad.items())))
    metrics = {"mode": spec.realism.mode, "perturbed_axes": list(spec.realism.perturbed_axes),
               "generated": generated, "carrier": carrier, "real_holdout": baseline,
               "real_source": ctx.real.source, "limit": limit, "advisory_limit": advisory,
               "advisory_warnings": warnings}
    if judged == 0:
        return CheckResult("realism_vs_real_world", "realism", ABSTAIN,
                           reason="no region could be compared with the real reference", metrics=metrics)
    return CheckResult("realism_vs_real_world", "realism", FAIL if failures else PASS,
                       reason=", ".join(failures), metrics=metrics, evidence=evidence)


__all__ = ["check_realism", "active_features", "NA"]

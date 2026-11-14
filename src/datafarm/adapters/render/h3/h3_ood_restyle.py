#!/usr/bin/env python3
"""对已 ACCEPTED 的 sim2real 渲染做输出侧 OOD 外观编辑。

内容（机器人、物体、运动、接触）就是已接受的 sim2real 视频，不做改动。只有被授权的 OOD 轴会变化：
  * 桌面材质（--material-bgr）：在逐帧桌面掩码内（由 h3_sam2_table_masks.py 在生成视频上跟踪的 SAM2
    掩码，或在对齐后的 WHITE CARRIER 上测得：明亮、低饱和、与 panel 底部 4 连通的平面，并受边缘
    阻挡，使墙面和机器人被排除），像素被重新合成为 material * shading(x, y, t)，其中
    shading = 生成亮度 / 其平滑桌面基线（保留生成的阴影、接触和噪声）；仅支持均匀材质，因为纹理
    无法跟随移动的腕部相机；
  * 灯光颜色（--light）：对整帧施加保均值的逐通道增益。
只做增益与重新着色：不移动、不重定时任何像素；carrier 只用于确定桌面在哪里。

  python h3_ood_restyle.py --src <pkg dir with final-t3-*.mp4 + carrier-t3-*.mp4> --out <dir> \
      [--material-bgr B,G,R] [--masks <prefix of <prefix>-<panel>.npy>] [--light <preset>]
"""

import argparse
import glob
import json
import os
import subprocess

import cv2
import numpy as np

from h3_media import link_or_copy
from h3_spatial import T_LAYOUT_PANELS as PANELS

# BGR 增益，归一化为均值 1（只改色温，曝光不变）
LIGHT = {
    "warm-3000k": (0.86, 0.99, 1.15),
    "warm-amber-2700k": (0.80, 0.98, 1.20),
    "neutral-5000k": (1.0, 1.0, 1.0),
    "cool-6500k": (1.10, 1.00, 0.90),
    "cool-blue-7500k": (1.16, 1.01, 0.85),
}


def table_mask_carrier(p: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(p, cv2.COLOR_BGR2HSV)
    edges = cv2.dilate(cv2.Canny(cv2.cvtColor(p, cv2.COLOR_BGR2GRAY), 18, 45), np.ones((3, 3), np.uint8))
    ok = ((hsv[..., 1] < 30) & (hsv[..., 2] > 95) & (edges == 0)).astype(np.uint8)
    n, lab = cv2.connectedComponents(ok, connectivity=4)
    h, w = ok.shape
    seeds = set(lab[h - 2, :].tolist()) - {0}
    m = np.isin(lab, list(seeds)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    inv = 1 - m  # 填补小空洞（carrier 噪声斑点）
    k, lab2, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    for i in range(1, k):
        x, y, ww, hh, area = st[i]
        if area < 0.003 * h * w and y > 0 and x > 0 and x + ww < w and y + hh < h:
            m[lab2 == i] = 1
    return m


def refine_with_generated(m: np.ndarray, gp: np.ndarray) -> np.ndarray:
    """把掩码向生成像素中最多扩张 7 px，只纳入看起来像生成桌面的像素（消除生成物体轮廓与
    carrier 相差几个像素处产生的白色光晕），随后补上桌面上的投影阴影。"""
    lab = cv2.cvtColor(gp.astype(np.uint8), cv2.COLOR_BGR2LAB).astype(np.float32)
    if m.sum() < 50:
        return m
    ref = np.median(lab[m > 0], 0)
    band = (cv2.dilate(m, np.ones((19, 19), np.uint8)) > 0) & (m == 0)
    close = np.linalg.norm(lab - ref, axis=2) < 16
    m2 = (m.astype(bool) | (band & close)).astype(np.uint8)
    # 投影阴影：低饱和、比桌面暗但不至于机器人全黑的像素，且与桌面 4 连通（SAM2 掩码可能漏掉
    # 较暗的阴影瓣，那些区域会保留原本的桌面颜色）
    hsv = cv2.cvtColor(gp.astype(np.uint8), cv2.COLOR_BGR2HSV)
    tv = float(np.median(hsv[..., 2][m2 > 0]))
    shadow = (hsv[..., 1] < 45) & (hsv[..., 2] > max(70.0, 0.35 * tv)) & (hsv[..., 2] < tv * 1.05)
    # 仅保留每列中桌面自身下缘以下的区域（墙面同样低饱和且比桌面暗）
    h, w = m2.shape
    has = m2.any(0)
    top = np.where(has, m2.argmax(0), h)
    top = cv2.erode(top[None].astype(np.float32), np.ones((1, 31), np.uint8))[0] if has.any() else top
    shadow &= np.arange(h)[:, None] >= (top[None, :] + 3)
    cand = (shadow | (m2 > 0)).astype(np.uint8)
    k, lab3 = cv2.connectedComponents(cand, connectivity=4)
    keep = set(np.unique(lab3[m2 > 0]).tolist()) - {0}
    return np.isin(lab3, list(keep)).astype(np.uint8)


def table_mask(carrier_panel: np.ndarray, generated_panel: np.ndarray, masks, frame: int, history: list) -> np.ndarray:
    carrier_mask = table_mask_carrier(carrier_panel)
    if masks is None:
        return refine_with_generated(carrier_mask, generated_panel)
    # 生成帧上的 SAM2 掩码，取 3 帧时间多数表决，再做闭运算
    stack = masks[max(frame - 1, 0) : frame + 2].astype(np.uint8)
    sam = (stack.sum(0) * 2 > len(stack)).astype(np.uint8)
    sam = cv2.morphologyEx(sam, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    # carrier 判定为“非桌面”的明亮物体，可能被 SAM2 并入桌面：丢弃其内部
    # （先腐蚀，让小的轮廓偏移仍由 SAM 决定）
    h = carrier_mask.shape[0]
    top = np.where(carrier_mask.any(0), carrier_mask.argmax(0), h)
    below = np.arange(h)[:, None] >= top[None, :]
    bright = cv2.cvtColor(carrier_panel, cv2.COLOR_BGR2HSV)[..., 2] > 200
    objects = cv2.erode(((carrier_mask == 0) & below & bright).astype(np.uint8), np.ones((21, 21), np.uint8))
    sam = (sam & (1 - objects)).astype(np.uint8)
    reference_area = float(np.median(history[-48:])) if history else float(sam.mean())
    if sam.mean() < 0.6 * reference_area:  # SAM2 丢失了桌面（遮挡/重提示间隙）：
        sam = np.maximum(sam, carrier_mask)  # 该帧回退到由 carrier 测得的桌面
    history.append(float(sam.mean()))
    return refine_with_generated(sam, generated_panel)


def retable(gp: np.ndarray, m: np.ndarray, material: np.ndarray) -> np.ndarray:
    """桌面像素 = material x 生成 shading x 生成桌面的亮度衰减。"""
    ms = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 1.5)[..., None]
    lum = cv2.cvtColor(gp.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
    tl = np.where(m > 0, lum, np.nan)
    med = np.nanmedian(tl) if np.isfinite(tl).any() else lum.mean()
    base = cv2.GaussianBlur(np.where(m > 0, lum, med).astype(np.float32), (0, 0), 30) + 1e-3
    shade = np.clip(lum / base, 0.15, 1.4)[..., None]
    fall = (base / max(med, 1e-3))[..., None]
    return ms * np.clip(material * shade * fall, 0, 255) + (1 - ms) * gp


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--material-bgr", help="uniform tabletop material colour 'B,G,R', e.g. slate grey 132,120,104")
    ap.add_argument(
        "--masks",
        help="prefix of SAM2-video tabletop masks <prefix>-<panel>.npy tracked on the GENERATED "
        "video (h3_sam2_table_masks.py); default = carrier-measured masks",
    )
    ap.add_argument("--light", choices=sorted(LIGHT), help="light-colour preset (BGR gains, mean-preserving)")
    a = ap.parse_args()
    if not (a.material_bgr or a.light):
        ap.error("nothing to change: give --material-bgr and/or --light")
    material = np.array([float(v) for v in a.material_bgr.split(",")], np.float32) if a.material_bgr else None
    light = np.array(LIGHT[a.light], np.float32) if a.light else None
    src = os.path.abspath(a.src)
    gen = sorted(glob.glob(f"{src}/final-t3-*fps.mp4"))[0]
    car = sorted(glob.glob(f"{src}/carrier-t3-*fps.mp4"))[0]
    fps = int(gen.split("-")[-1].replace("fps.mp4", ""))
    os.makedirs(a.out, exist_ok=True)
    final = os.path.join(a.out, os.path.basename(gen))
    cg, cc = cv2.VideoCapture(gen), cv2.VideoCapture(car)
    W, H = int(cg.get(3)), int(cg.get(4))
    enc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "14", "-pix_fmt", "yuv420p",
         final],
        stdin=subprocess.PIPE,
    )  # fmt: skip
    n, fractions = 0, {k: [] for k in PANELS}
    sam = {k: np.load(f"{a.masks}-{k}.npy", mmap_mode="r") for k in PANELS} if a.masks else None
    history = {k: [] for k in PANELS}
    while True:
        ok1, g = cg.read()
        ok2, c = cc.read()
        if not (ok1 and ok2):
            break
        o = g.astype(np.float32)
        for name, (y0, y1, x0, x1) in PANELS.items():
            gp = g[y0:y1, x0:x1].astype(np.float32)
            mix = gp
            if material is not None:
                m = table_mask(c[y0:y1, x0:x1], gp, sam[name] if sam else None, n, history[name])
                fractions[name].append(float(m.mean()))
                mix = retable(gp, m, material)
            if light is not None:
                mix = np.clip(mix * (light / light.mean()), 0, 255)
            o[y0:y1, x0:x1] = mix
        enc.stdin.write(np.clip(np.rint(o), 0, 255).astype(np.uint8).tobytes())
        n += 1
    enc.stdin.close()
    enc.wait()
    with open(f"{src}/job-manifest.json") as f, open(os.path.join(a.out, "job-manifest.json"), "w") as t:
        t.write(f.read())
    carrier_link = os.path.join(a.out, os.path.basename(car))
    link_or_copy(car, carrier_link)
    with open(f"{src}/assembled.json") as f:
        assembled = json.load(f)
    assembled.update(final=final, carrier_clip=carrier_link, frames=n)
    with open(os.path.join(a.out, "assembled.json"), "w") as f:
        json.dump(assembled, f, indent=2)
    with open(os.path.join(a.out, "restyle.json"), "w") as f:
        json.dump(
            {
                "schema": "h3.ood-restyle/1", "src": src, "frames": n, "light": a.light, "material_bgr": a.material_bgr,
                "masks": os.path.abspath(a.masks) if a.masks else "carrier",
                "table_fraction_mean": {k: round(float(np.mean(v)), 3) for k, v in fractions.items() if v},
                "geometry_changed": False, "retimed": False, "method": __doc__.split("\n\n")[1],
            },
            f, indent=2,
        )  # fmt: skip
    print(json.dumps({"out": final, "frames": n}))


if __name__ == "__main__":
    main()

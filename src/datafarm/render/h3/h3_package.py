#!/usr/bin/env python3
"""构建一个 Ref2VA + H3-Fun ControlNet（soft-HED）+ inpaint 混合渲染包（"backend": "control-hybrid"）。

一个包对应一个 task episode x mode x recipe x seed：contract（render-policy）、带黑边的 1024x768 画布载体、
独立的 17n+5 帧 chunk 及其 keep 范围、job manifest 与 ticket。每个 chunk 都会获得：

  control_video  white chunk 片段的 soft-HED 图，由 VideoX-Fun 工具计算
                 （${H3_VIDEOX_FUN}/tools/prepare_hed_control.py，ControlNetHED annotator，使用
                 ${H3_PYTHON} 运行；--min-edge-intensity 来自 recipe；0 表示 soft 图），画布相同；
  inpaint_video  white chunk 片段本身（80+1280+80 填充画布上的 white RGB inpaint 源）；
  inpaint_mask   活动面板重绘掩码：画布 x=[57,967) 上为 1（重绘），黑色
                 边框上为 0（保留）（重绘占比 910/1024 = 0.888672）。

用 h3_control_worker.py 渲染，并用 h3_assemble.py 组装。render-policy 绑定在构建
时用 datafarm.render.policy.check_h3_payload 检查（prompt 文件 == 编译后的 contract prompt，contract 的真实
引用在前），worker 在渲染前还会再检查一次。ticket.json 最后写入，因此中断或
被拒绝的构建绝不会被入队。

  python h3_package.py build --run-dir <run> --bundle <run>/tasks/<task>/bundle.json \
      --recipe <run>/recipes/<recipe>.json --episode <episode> --mode sim2real \
      --out <run>/pkgs/<recipe>/<task>/<episode>-sim2real [--hed-jobs 4 --hed-threads 16]
  python h3_package.py check <pkg>      # 仅重跑绑定/输入审计

运行目录（--run-dir 或环境变量 H3_RUN）存放共享的 HED 缓存；模型、工具与 checkpoint 路径来自
recipe 或部署绑定（h3_paths.py），绝不使用内置默认值。

Recipe JSON 字段：name、backend、worker_profile、canvas（"t3-guttered-1024x768"）、denoise_forwards、
chunk {frames, advance, keep_head, event_aware?, tail_pad?}、seed_base、control {...}、prompt_lowering?、assembly {...},
bundle_variant?。

chunk.tail_pad 会在分块之前把载体的最后一帧多保留若干帧，从而 episode 的真实最后一
帧永远不会是最后一个 chunk 的结尾，因为生成器往往在结尾处臆造运动或物体；组装与
曝光锁定会把输出裁回 episode 长度（输出的第 i 帧仍然是载体的第 i 帧）。bundle
episode 可以指定 `keep_regions`（h3_keep_regions.py：在 white 显示任务状态处保留真实像素）和
`wide_wrist`（宽视场 wrist white，在曝光锁定后由 h3_wrist_crop.py 裁回）。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import h3_bundle
import h3_keep_regions
import h3_paths
from h3_media import link_or_copy, probe, sha256
from h3_spatial import SPATIAL

h3_paths.datafarm_src()
from datafarm.render.policy import check_h3_payload, lower_h3_prompt  # noqa: E402

CANVAS = "t3-guttered-1024x768"
MANIFEST_SCHEMA = "h3.control-hybrid-manifest/1"
LEGAL = [17 * n + 5 for n in range(7, 22)]  # 124 .. 362
# chunkvis wrist 空置检测。仅凭饱和度会漏掉白色或浅色任务物体，因此只要 wrist 视图具有空房间无法解释的
# 边缘结构（wrist_structure()），也计为显示了物体。
CHUNK_FACTS_DETECTORS = ("wrist-structure-v1",)
WRIST_EDGE_TAU = 12          # 模糊灰色 wrist 视图上的 Canny 低阈值（高阈值为 3 倍）
WRIST_SEAMS = 4              # 每帧剥离的直线房间接缝数量（墙/桌地平线、桌边、墙角）
WRIST_MIN_STRUCTURE = 0.75   # 残余边缘长度（以 wrist 视图宽度为单位），超过则标记该帧中存在任务物体
# recipe 可以指定的 control 路径；${KEY} 占位符（configs/render/h3-control-hybrid-v1.json）由
# 部署绑定展开，缺失的键默认为 h3_paths.default_model_paths()（同样来自绑定）
CONTROL_PATH_KEYS = ("hed_tool", "hed_checkpoint", "base_model", "control_checkpoint", "control_config",
                     "videox_fun_repo")
PROFILES = ("control-turbo",)


def run(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, env=env, capture_output=True, text=True)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def plan_chunks(total: int, frames: int, advance: int, keep_head: int, events: list | tuple = (), before: int = 24,
                min_advance: int = 48) -> list[dict]:
    """由 `frames` 个帧（合法的 17n+5）构成的 chunk，步进为 `advance`；最后一个 chunk 按末端对齐。

    Keep 范围为 [start_k + keep_head, start_{k+1} + keep_head)。给定 `events`（机械臂进入或离开
    头部视图的帧）时，chunk 绝不会在事件后 keep_head 帧内开始：这样的起始点会前移到事件之前
    `before` 帧处，并落在 [min_advance, advance] 内，使该 chunk 自己生成进入或退出过程，而不是
    从其后续影响开始，那样往往画出一个残影夹爪。"""
    if total < LEGAL[0]:
        raise ValueError(f"episode shorter than {LEGAL[0]} frames: {total}")
    if frames not in LEGAL:
        raise ValueError(f"chunk frames {frames} not legal")
    starts, moved = [0], []
    while starts[-1] + frames < total:
        nxt = starts[-1] + advance
        for e in events:
            if e < nxt <= e + keep_head and e - before >= starts[-1] + min_advance:
                moved.append([nxt, e - before, e])
                nxt = e - before
                break
        if nxt + frames >= total:
            nxt = max(total - frames, starts[-1] + 1)
        starts.append(nxt)
    chunks = []
    for k, s in enumerate(starts):
        length = min(frames, total - s) if k == len(starts) - 1 else frames
        if length not in LEGAL:
            length = next(v for v in LEGAL if v >= length)
        chunks.append({"index": k, "start": s, "frames": length})
    for k, c in enumerate(chunks):
        a = 0 if k == 0 else chunks[k - 1]["keep_global"][1]
        b = total if k == len(chunks) - 1 else min(chunks[k + 1]["start"] + keep_head, total)
        if b <= a or b > c["start"] + c["frames"]:
            raise ValueError(f"chunk {k}: invalid keep range {a}..{b} for start {c['start']}")
        c["keep_global"] = [a, b]
        c["keep_local"] = [a - c["start"], b - c["start"]]
    if moved:
        chunks[0]["moved_starts"] = moved
    return chunks


def head_arm_sides(lab, st, hw: int, hy: int) -> set:
    """头部视图中暗色机器人标注显示了哪些机械臂。接触左（右）边界的连通分量即为左
    （右）臂。只接触顶边（掩码中与手臂分离的远端连杆或夹爪）的分量，若恰好只有一侧边界被
    接触，则归属于接触该侧边界的手臂；否则其侧别取顶边接触点的平均列位置。"""
    import numpy as np
    big = [k for k in range(1, len(st)) if st[k][4] >= 0.003 * hw * hy]
    left = any(st[k][0] <= 6 for k in big)
    right = any(st[k][0] + st[k][2] >= hw - 6 for k in big)
    sides = set(["left"] if left else []) | set(["right"] if right else [])
    for k in big:
        bx, by, bw, bh, area = st[k]
        if bx <= 6 or bx + bw >= hw - 6:
            continue
        top = np.nonzero(lab[:6] == k)[1]
        if not top.size:
            continue
        if left != right:
            sides.add("left" if left else "right")
        else:
            sides.add("left" if top.mean() < hw / 2 else "right")
    return sides


def arm_events(canvas: Path, total: int, min_gap: int = 24) -> list:
    """机械臂进入或离开 white 载体头部视图（任一侧）的帧。

    每侧每帧的存在性 = 一个暗色机器人连通分量（V<110、S<90、占头部面积 >= 0.3 %）接触该侧的
    边界（或该半区的顶边），与 chunk_facts 中的判定一致；经 9 帧多数平滑；与上一个保留事件相距小于
    min_gap 帧的切换会被合并。"""
    import cv2
    import numpy as np
    x0, x1, hy = SPATIAL.canvas_active_x0, SPATIAL.canvas_active_x1, SPATIAL.canvas_head_height
    cap = cv2.VideoCapture(str(canvas))
    pres, i = [], 0
    while i < total:
        ok, fr = cap.read()
        if not ok:
            break
        hsv = cv2.cvtColor(fr[:hy, x0:x1], cv2.COLOR_BGR2HSV)
        dark = ((hsv[..., 2] < 110) & (hsv[..., 1] < 90)).astype(np.uint8)
        dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        _, lab, st, _ = cv2.connectedComponentsWithStats(dark)
        ss = head_arm_sides(lab, st, x1 - x0, hy)
        pres.append([int("left" in ss), int("right" in ss)])
        i += 1
    cap.release()
    p = np.array(pres, float)
    if len(p) == 0:
        return []
    k = 9
    pad = np.pad(p, ((k // 2, k // 2), (0, 0)), mode="edge")
    sm = np.stack([np.convolve(pad[:, c], np.ones(k) / k, mode="valid") for c in range(2)], 1) > 0.5
    out = []
    for t in range(1, len(sm)):
        if (sm[t] != sm[t - 1]).any() and (not out or t - out[-1] >= min_gap):
            out.append(int(t))
    return out


def control_settings(recipe: dict) -> dict:
    c = h3_paths.expand(dict(recipe.get("control") or {}))
    c.setdefault("type", "soft-hed")
    c.setdefault("hed_min_intensity", 0)
    c.setdefault("context_scale", 1.0)
    c.setdefault("inpaint_source", "white")
    c.setdefault("mask", "active-repaint")
    c.setdefault("guidance_scale", 1.0)
    c.setdefault("white_video_reference", False)
    c.setdefault("edge_gate", None)
    if c["type"] == "soft-hed":
        for key in ("hed_tool", "hed_checkpoint"):
            if key not in c:
                c[key] = h3_paths.default_model_path(key)
    for key in CONTROL_PATH_KEYS:  # 归一化，使 worker 的已加载模型比对精确一致
        if c.get(key):
            c[key] = str(Path(c[key]))
    if c["type"] != "soft-hed":
        raise SystemExit(f"unsupported control type {c['type']!r} (soft-hed only)")
    if (c["inpaint_source"], c["mask"]) != ("white", "active-repaint"):
        raise SystemExit("inpaint_source/mask must be white/active-repaint")
    if float(c["guidance_scale"]) > 1.0:
        raise SystemExit("Ref2VA is guidance-distilled: guidance_scale must be <= 1")
    if not 0.0 <= float(c["context_scale"]) <= 1.0:
        raise SystemExit("control context scale must be in [0, 1] (model card maximum is 1.0)")
    return c


def make_mask(path: Path, frames: int) -> dict:
    """H3 画布上的活动面板重绘掩码：x=[57,967) 全高为白色（重绘），边框为黑色。"""
    w, h, x0, x1 = SPATIAL.canvas_width, SPATIAL.canvas_height, SPATIAL.canvas_active_x0, SPATIAL.canvas_active_x1
    if not (path.is_file() and probe(path)[:3] == (w, h, frames)):
        path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg("-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r=24", "-vf",
               f"drawbox=x={x0}:y=0:w={x1 - x0}:h={h}:color=white:t=fill,format=yuv420p",
               "-frames:v", str(frames), "-an", "-r", "24", "-c:v", "libx264", "-preset", "medium",
               "-crf", "0", "-pix_fmt", "yuv420p", str(path))
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(path))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"cannot decode mask {path}")
    m = frame[:, :, 2] > 127  # BGR 解码后的 R 通道，按流水线方式阈值化（> 0.5）
    expected = np.zeros_like(m)
    expected[:, x0:x1] = True
    if not np.array_equal(m, expected):
        raise SystemExit(f"mask {path} does not match the active-panel contract")
    return {"path": str(path), "sha256": sha256(path), "frames": frames, "regenerate_fraction": float(m.mean()),
            "active_x": [x0, x1]}


def make_hed(job: dict, out_dir: Path, ctl: dict, threads: int, device: str) -> dict:
    w, h = SPATIAL.canvas_width, SPATIAL.canvas_height
    white = Path(job["white"])
    frames = int(job["generated_frames"])
    mi = int(ctl["hed_min_intensity"])
    tag = "soft" if mi == 0 else f"min{mi}"
    target = out_dir / f"{job['chunk_id']}-hed-{tag}.mp4"
    sidecar = target.with_suffix(".h3.json")
    if target.is_file() and sidecar.is_file():
        rec = json.loads(sidecar.read_text())
        if rec.get("input_sha256") == job["white_sha256"] and rec.get("frames") == frames \
                and rec.get("min_edge_intensity") == mi and probe(target)[:3] == (w, h, frames):
            return rec
    # 全局缓存：相同的 white chunk 片段 + HED 设置 -> 相同的 control 视频（跨 recipe 共享）
    cache = h3_paths.run_dir() / "cache" / "hed" / f"{job['white_sha256']}-{mi}-{frames}"
    if (cache.with_suffix(".mp4")).is_file() and (cache.with_suffix(".h3.json")).is_file():
        rec = json.loads(cache.with_suffix(".h3.json").read_text())
        target.unlink(missing_ok=True)
        link_or_copy(cache.with_suffix(".mp4"), target)
        rec = {**rec, "path": str(target), "input": str(white), "cache": str(cache.with_suffix(".mp4"))}
        sidecar.write_text(json.dumps(rec, indent=2))
        return rec
    env = dict(os.environ, OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads))
    annotator = h3_paths.binding("H3_HED_ANNOTATOR_REPO", required=False, must_exist=False)
    if annotator:  # 由打过补丁的 VideoX-Fun 的 soft-HED 工具读取
        env["H3_HED_ANNOTATOR_REPO"] = annotator
    if device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""
    t0 = time.time()
    r = run([h3_paths.python_exe(), ctl["hed_tool"], "--input", str(white), "--output", str(target), "--checkpoint",
             ctl["hed_checkpoint"], "--width", str(w), "--height", str(h),
             "--max-frames", str(frames), "--min-edge-intensity", str(mi)], env=env)
    meta = json.loads(target.with_suffix(".json").read_text())
    got = probe(target)
    if got[:3] != (w, h, frames) or meta["frames"] != frames:
        raise SystemExit(f"{target}: HED contract {got[:3]} != {w}x{h}x{frames}")
    rec = {"path": str(target), "sha256": sha256(target), "input": str(white),
           "input_sha256": job["white_sha256"], "frames": frames, "min_edge_intensity": mi,
           "tool": ctl["hed_tool"], "tool_sha256": sha256(Path(ctl["hed_tool"])),
           "checkpoint": ctl["hed_checkpoint"], "device": meta.get("device"), "rate": got.rate,
           "mean_active_fraction_gt20": meta["mean_active_fraction_gt20"],
           "mean_normalized_intensity": meta["mean_normalized_intensity"], "seconds": round(time.time() - t0, 1),
           "tool_stdout_tail": r.stdout[-300:]}
    sidecar.write_text(json.dumps(rec, indent=2))
    cache.parent.mkdir(parents=True, exist_ok=True)
    link_or_copy(target, cache.with_suffix(".mp4"))
    cache.with_suffix(".h3.json").write_text(json.dumps(rec, indent=2))
    return rec


def wrist_structure(panel) -> float:
    """单个 BGR wrist 视图中空房间无法解释的边缘结构，以视图宽度为单位的边缘长度计。

    空的白色网格 wrist 视图就是素墙与桌面、二者之间的少量直线接缝（地平线、桌面
    边缘、墙角）以及暗色机器人像素（它自己的手指、另一只手臂、暗色投影）。对模糊灰色视图在膨胀后的暗色
    机器人掩码之外做 Canny 边缘，减去最多 WRIST_SEAMS 条直线接缝（按长度从长到短剥离，每条沿其
    贯穿视图的整条线移除），再减去 < 12 px 的斑点，剩下的就是任务物体无论什么颜色都会带来的内容：
    白色设备的轮廓与按钮、柜面与把手、布料褶皱、白色方块。
    空视图测得约 0-0.5，此类物体约 0.8-7。"""
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(panel, cv2.COLOR_BGR2HSV)
    g = cv2.GaussianBlur(cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY), (0, 0), 1.2)
    h, w = g.shape
    robot = cv2.dilate(((hsv[..., 2] < 110) & (hsv[..., 1] < 90)).astype(np.uint8), np.ones((11, 11), np.uint8))
    edges = cv2.Canny(g, WRIST_EDGE_TAU, 3 * WRIST_EDGE_TAU)
    edges[robot > 0] = 0
    edges[:3] = 0
    edges[-3:] = 0
    edges[:, :3] = 0
    edges[:, -3:] = 0
    reach = 2 * (w + h)
    for _ in range(WRIST_SEAMS):
        lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=20, minLineLength=int(0.1 * w), maxLineGap=4)
        if lines is None:
            break
        a, b, c, d = max(lines[:, 0].tolist(), key=lambda s: math.hypot(s[2] - s[0], s[3] - s[1]))
        n = max(1e-6, math.hypot(c - a, d - b))
        ux, uy = (c - a) / n * reach, (d - b) / n * reach
        band = np.zeros_like(edges)
        cv2.line(band, (int(a - ux), int(b - uy)), (int(c + ux), int(d + uy)), 1, 7)
        edges[band > 0] = 0
    _, lab, st, _ = cv2.connectedComponentsWithStats((edges > 0).astype(np.uint8), connectivity=8)
    keep = st[:, cv2.CC_STAT_AREA] >= 12
    keep[0] = False
    return float(keep[lab].sum()) / w


def chunk_facts(job: dict, cfg: dict | None = None) -> dict:
    """从带边框画布上该 chunk 的 white 载体片段测量 chunk 局部可见性事实
    （T 形布局：head = 上部 2/3，left wrist = 活动区左下，right wrist = 活动区右下）。
    head arm side present  <=> 一个暗色机器人连通分量（V<110、S<90、占头部面积 >=0.3%）接触该侧的
    边界（左边/右边，或该半区的顶边）的采样帧占比 >= 15%；
    wrist empty            <=> 在 >= 90% 的采样帧中该 wrist 视图未显示任务物体：饱和像素
    （S>=70、V>=40）占比 <= 0.4% 且 wrist_structure() < 0.75，因此白色或浅色物体不会被测为不存在；
    head_max_coloured_objects = 各采样帧中头部视图里独立饱和连通分量数（S>=70、V>=40，
    3x3 开运算、9x9 闭运算、占头部视图 >= 0.15%）的最大值；
    wrist_large_objects    <=> 非空 wrist 视图在 >= 25% 的采样帧中饱和像素占比 >= 6%。"""
    import cv2
    import numpy as np
    cfg = cfg or {}
    detector = cfg.get("detector", CHUNK_FACTS_DETECTORS[0])
    if detector not in CHUNK_FACTS_DETECTORS:
        raise SystemExit(f"unknown chunk facts detector {detector!r}; one of {CHUNK_FACTS_DETECTORS}")
    x0, x1, hy = SPATIAL.canvas_active_x0, SPATIAL.canvas_active_x1, SPATIAL.canvas_head_height
    xm = (x0 + x1) // 2
    cap = cv2.VideoCapture(job["white"])
    i, arm, max_obj = 0, {"left": 0, "right": 0}, 0
    wobj = {"left_wrist": 0, "right_wrist": 0}
    wbig = {"left_wrist": 0, "right_wrist": 0}
    n = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i % 4 == 0:
            n += 1
            hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
            head = hsv[:hy, x0:x1]
            dark = ((head[..., 2] < 110) & (head[..., 1] < 90)).astype(np.uint8)
            dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
            nl, lab, st, cen = cv2.connectedComponentsWithStats(dark)
            hw = x1 - x0
            seen = head_arm_sides(lab, st, hw, hy)
            for side in seen:
                arm[side] += 1
            col = ((head[..., 1] >= 70) & (head[..., 2] >= 40)).astype(np.uint8)
            col = cv2.morphologyEx(col, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            col = cv2.morphologyEx(col, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
            nc, _, stc, _ = cv2.connectedComponentsWithStats(col)
            max_obj = max(max_obj, int((stc[1:, cv2.CC_STAT_AREA] >= 0.0015 * hw * hy).sum()))
            for name, (a, b) in (("left_wrist", (x0, xm)), ("right_wrist", (xm, x1))):
                v = hsv[hy:, a:b]
                sat = (v[..., 1] >= 70) & (v[..., 2] >= 40)
                if sat.mean() > 0.004 or wrist_structure(fr[hy:, a:b]) >= WRIST_MIN_STRUCTURE:
                    wobj[name] += 1
                if sat.mean() >= 0.06:  # 手指附近有一个体积大、距离近的任务物体
                    wbig[name] += 1
        i += 1
    cap.release()
    absent = [s for s in ("left", "right") if arm[s] < 0.15 * max(1, n)]
    empty = [w for w in ("left_wrist", "right_wrist") if wobj[w] <= 0.10 * max(1, n)]
    return {"head_absent_arms": absent, "empty_wrists": empty, "head_max_coloured_objects": min(max_obj, 9),
            "wrist_large_objects": [w for w in ("left_wrist", "right_wrist")
                                    if w not in empty and wbig[w] >= 0.25 * max(1, n)]}


def make_gated(job: dict, hed: dict, gate: dict) -> dict:
    """抑制阴影的 control：仅当 white 载体本身具有强局部
    亮度阶跃（模糊灰图在窗口内的 max-min > tau）时才保留 HED 边缘。白色桌面上的仿真器投影边界
    是低对比度阶跃（约 10-30 级），会被移除；物体/机械臂轮廓以及桌面上的白色
    边缘（>= 约 25-40 级）则保留。确定性，按 (white sha, HED sha, gate) 缓存。"""
    import cv2
    import numpy as np
    tau, win, dil = float(gate.get("tau", 25)), int(gate.get("window", 7)), int(gate.get("dilate", 5))
    key = f"{job['white_sha256']}-{hed['sha256'][:16]}-gate{int(tau)}w{win}d{dil}"
    cache = h3_paths.run_dir() / "cache" / "hedgate" / key
    target = Path(hed["path"]).with_name(Path(hed["path"]).stem + f"-gate{int(tau)}.mp4")
    if cache.with_suffix(".mp4").is_file():
        target.unlink(missing_ok=True)
        link_or_copy(cache.with_suffix(".mp4"), target)
    else:
        white, control = cv2.VideoCapture(job["white"]), cv2.VideoCapture(hed["path"])
        tmp = target.with_suffix(".tmp.mp4")
        enc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt",
                                "bgr24", "-s", f"{SPATIAL.canvas_width}x{SPATIAL.canvas_height}", "-r", "24", "-i", "-",
                                "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "10", "-pix_fmt", "yuv420p",
                                str(tmp)], stdin=subprocess.PIPE)
        n = 0
        kern_w, kern_d = np.ones((win, win), np.uint8), np.ones((dil, dil), np.uint8)
        while True:
            okw, fw = white.read()
            okh, fh = control.read()
            if not (okw and okh):
                break
            g = cv2.GaussianBlur(cv2.cvtColor(fw, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 1.2)
            step = cv2.dilate(g, kern_w) - cv2.erode(g, kern_w)
            m = cv2.dilate((step > tau).astype(np.uint8), kern_d)
            enc.stdin.write(np.ascontiguousarray(fh * m[..., None]).tobytes())
            n += 1
        enc.stdin.close()
        if enc.wait() or n != int(job["generated_frames"]):
            raise SystemExit(f"gated control failed for {job['chunk_id']}: {n} frames")
        tmp.replace(target)
        cache.parent.mkdir(parents=True, exist_ok=True)
        link_or_copy(target, cache.with_suffix(".mp4"))
    return {**hed, "path": str(target), "sha256": sha256(target), "ungated_path": hed["path"],
            "ungated_sha256": hed["sha256"], "edge_gate": {"type": "luma-step", "tau": tau, "window": win, "dilate": dil}}


def audit(pkg_dir: Path) -> dict:
    """render-policy 绑定 + control 输入契约；构建时与 worker 都会使用。"""
    manifest = json.loads((pkg_dir / "job-manifest.json").read_text())
    policy = check_h3_payload(manifest, pkg_dir / "contract" / "contract.json")
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("controlnet") is not True:
        raise ValueError("not a control-hybrid manifest")
    if manifest.get("reference_order") != "images-first":
        raise ValueError("reference_order must be images-first")
    problems = []
    canvas = (SPATIAL.canvas_width, SPATIAL.canvas_height)
    for job in manifest["jobs"]:
        n = int(job["generated_frames"])
        if n < 124 or n > 362 or (n - 5) % 17:
            problems.append(f"{job['chunk_id']}: {n} frames is not a legal 17n+5 H3 shape")
        for key in ("white", "control_video", "inpaint_video", "inpaint_mask"):
            p = Path(job[key])
            if not p.is_file():
                problems.append(f"{job['chunk_id']}: missing {key} {p}")
                continue
            sha = job.get(f"{key}_sha256")
            if sha and sha256(p) != sha:
                problems.append(f"{job['chunk_id']}: {key} sha256 mismatch")
            info = probe(p)
            if info[:2] != canvas or info.frames < n:
                problems.append(f"{job['chunk_id']}: {key} is {info.width}x{info.height}x{info.frames}, "
                                f"need canvas x {n}")
    if problems:
        raise ValueError("control package audit failed:\n" + "\n".join(problems))
    return policy


def build_chunk_jobs(out: Path, task: str, ep_id: str, contract: dict, canvas: Path, chunks: list, total: int,
                     seed: int, steps: int, prompt: str) -> list[dict]:
    """white chunk 片段（当合法帧数超出 episode 末端时，在末尾克隆补齐）及其 job 记录。"""
    refs = [r["path"] for r in contract["intent"]["real_references"]]
    jobs = []
    for c in chunks:
        cid = f"{task}-{ep_id}-c{c['index']:02d}"
        clip = out / "inputs" / f"{cid}-white.mp4"
        pad = max(0, c["start"] + c["frames"] - total)
        vf = f"trim=start_frame={c['start']}:end_frame={min(total, c['start'] + c['frames'])},setpts=PTS-STARTPTS"
        if pad:
            vf += f",tpad=stop_mode=clone:stop={pad}"
        ffmpeg("-i", str(canvas), "-vf", vf, "-fps_mode", "cfr", "-an", "-r", "24", "-c:v", "libx264", "-preset", "fast",
               "-crf", "10", "-pix_fmt", "yuv420p", str(clip))
        if probe(clip).frames != c["frames"]:
            raise SystemExit(f"{cid}: clip frames {probe(clip).frames} != {c['frames']}")
        jobs.append({"chunk_id": cid, "chunk_index": c["index"], "sample_id": ep_id,
                     "source_frame_range": [c["start"], c["start"] + c["frames"]],
                     "generated_frames": c["frames"], "keep_local_range": c["keep_local"],
                     "keep_global_range": c["keep_global"], "white": str(clip), "white_sha256": sha256(clip),
                     "reference_images": refs, "prompt": prompt, "seed": seed, "steps": steps})
    return jobs


def add_control_inputs(jobs: list[dict], ctl: dict, inputs: Path, args) -> tuple[dict, list]:
    """为 chunk job 添加 control 输入（soft-HED，可选 gated，以及重绘掩码）。"""
    masks = {n: make_mask(inputs / f"repaint-mask-active-{n}f.mp4", n)
             for n in sorted({int(j["generated_frames"]) for j in jobs})}
    with cf.ThreadPoolExecutor(max(1, args.hed_jobs)) as ex:
        heds = list(ex.map(lambda j: make_hed(j, inputs, ctl, args.hed_threads, args.hed_device), jobs))
    if ctl.get("edge_gate"):
        with cf.ThreadPoolExecutor(4) as ex:
            heds = list(ex.map(lambda jh: make_gated(jh[0], jh[1], ctl["edge_gate"]), zip(jobs, heds)))
    for job, hed in zip(jobs, heds):
        mask = masks[int(job["generated_frames"])]
        job.update({"guidance_scale": float(ctl["guidance_scale"]),
                    "control_context_scale": float(ctl["context_scale"]),
                    "control_video": hed["path"], "control_video_sha256": hed["sha256"],
                    "control_meta": {k: hed[k] for k in ("min_edge_intensity", "mean_active_fraction_gt20",
                                                         "mean_normalized_intensity", "device", "tool_sha256")},
                    "inpaint_video": job["white"], "inpaint_video_sha256": job["white_sha256"],
                    "inpaint_mask": mask["path"], "inpaint_mask_sha256": mask["sha256"],
                    "inpaint_regenerate_fraction": mask["regenerate_fraction"]})
    return masks, heds


def add_chunk_prompts(jobs: list[dict], contract: dict, lowering: str, contract_dir: Path, detector: str) -> None:
    """根据每个 white 片段上测得的事实，生成 chunk 局部降级 prompt。"""
    all_facts = [chunk_facts(job, {"detector": detector}) for job in jobs]
    if all(f.get("head_max_coloured_objects", 0) == 0 for f in all_facts):
        # 载体从未显示可测量的彩色物体（例如浅色小方块）：完全不陈述数量，
        # 这样测量遗漏绝不会告诉生成器丢弃一个真实物体
        for f in all_facts:
            f.pop("head_max_coloured_objects", None)
    for job, facts in zip(jobs, all_facts):
        pf = contract_dir / f"prompt-chunkvis-{job['chunk_id']}.txt"
        pf.write_text(lower_h3_prompt(contract, lowering, assets=h3_paths.REPO, chunk_facts=facts))
        job.update(chunk_facts=facts, chunk_facts_detector=detector, prompt=str(pf))


def cmd_build(args) -> int:
    h3_paths.run_dir()  # 先失败关闭再开始任何工作：HED 缓存位于运行目录中
    recipe_path = Path(args.recipe).resolve()
    recipe = json.loads(recipe_path.read_text())
    if recipe.get("backend") != "control-hybrid":
        raise SystemExit("recipe backend must be 'control-hybrid'")
    if recipe.get("worker_profile") not in PROFILES:
        raise SystemExit(f"control-hybrid recipes must use a worker_profile in {PROFILES}")
    if recipe.get("canvas", CANVAS) != CANVAS:
        raise SystemExit(f"unknown canvas {recipe['canvas']}")
    ctl = control_settings(recipe)
    bundle = h3_bundle.load(args.bundle)
    ep = next(e for e in bundle["episodes"] if e["id"] == args.episode)
    out = Path(args.out).resolve()
    if (out / "package.json").exists() and not args.force:
        raise SystemExit(f"package exists: {out}")
    t0 = time.time()
    inputs = out / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    carrier = Path(ep["carrier"])
    fr = ep.get("frame_range") or [0, probe(carrier).frames]
    total = fr[1] - fr[0]
    ch = recipe["chunk"]
    tail_pad = int(ch.get("tail_pad", 0))
    if tail_pad < 0:
        raise SystemExit("chunk.tail_pad must be >= 0")
    render_total = total + tail_pad
    # contract + 画布载体（最后一帧多保留 tail_pad 帧）
    contract = h3_bundle.compile_contract(bundle, args.mode, ep["id"], args.variant, out / "contract")
    canvas = inputs / "carrier-canvas-24fps.mp4"
    # 在重定时之前持有最后一帧：tpad 按流的标称帧率计算帧数，因此经过 setpts 之后，30 fps
    # 载体在输出重采样到 24 fps 时只会保留 48 x 24/30 个被持有的帧
    ffmpeg("-i", str(carrier), "-vf", f"trim=start_frame={fr[0]}:end_frame={fr[1]},"
           + (f"tpad=stop_mode=clone:stop={tail_pad}," if tail_pad else "")
           + f"setpts=N/(24*TB),{SPATIAL.ffmpeg_to_canvas()}",
           "-fps_mode", "cfr", "-an", "-r", "24", "-c:v", "libx264",
           "-preset", "fast", "-crf", "10", "-pix_fmt", "yuv420p", str(canvas))
    if probe(canvas).frames != render_total:
        raise SystemExit(f"canvas carrier frame count {probe(canvas).frames} != {render_total}")
    # chunk 计划（基于填充后的载体）
    events = ()
    if ch.get("event_aware"):
        events = arm_events(canvas, render_total)
    chunks = plan_chunks(render_total, ch["frames"], ch["advance"], ch["keep_head"], events,
                         before=int((ch.get("event_aware") or {}).get("before", 24)),
                         min_advance=int((ch.get("event_aware") or {}).get("min_advance", 48)))
    if ch.get("event_aware"):
        (out / "chunk-plan-events.json").write_text(json.dumps(
            {"arm_events": events, "moved_starts": chunks[0].get("moved_starts", []),
             "starts": [c["start"] for c in chunks]}, indent=1))
    # jobs：基础 prompt、chunk 片段、control 输入、chunk 局部 prompt
    prompt = out / "contract" / "prompt.txt"
    lowering = recipe.get("prompt_lowering")
    if lowering:
        prompt = out / "contract" / f"prompt-{lowering}.txt"
        prompt.write_text(lower_h3_prompt(contract, lowering, assets=h3_paths.REPO))
    seed = int(recipe.get("seed_base", 1000)) + int(args.seed_offset)
    steps = int(recipe["denoise_forwards"])
    jobs = build_chunk_jobs(out, bundle["task"], ep["id"], contract, canvas, chunks, render_total, seed, steps,
                            str(prompt))
    masks, heds = add_control_inputs(jobs, ctl, inputs, args)
    keep = None
    if ep.get("keep_regions"):   # 在 white 显示任务状态处保留真实像素（h3_keep_regions.py）
        keep = h3_keep_regions.apply(jobs, ep["keep_regions"], h3_paths.run_dir() / "cache" / "keep")
    if lowering:
        add_chunk_prompts(jobs, contract, lowering, out / "contract",
                          recipe.get("chunk_facts_wrist", CHUNK_FACTS_DETECTORS[0]))
    manifest = {"schema": MANIFEST_SCHEMA, "backend": "control-hybrid", "controlnet": True, "prompt": str(prompt),
                "reference_images": [r["path"] for r in contract["intent"]["real_references"]],
                "reference_order": "images-first", "control": ctl, "denoise_forwards": steps, "canvas": CANVAS,
                "jobs": jobs}
    if lowering:
        manifest["prompt_lowering"] = lowering
    (out / "job-manifest.json").write_text(json.dumps(manifest, indent=2))
    policy = audit(out)
    render_root = out / "render"
    package = {"schema": "h3.package/1", "backend": "control-hybrid", "task": bundle["task"], "episode": ep["id"],
               "mode": args.mode, "variant": args.variant, "recipe": recipe, "recipe_path": str(recipe_path),
               "bundle": str(Path(args.bundle).resolve()), "carrier": str(carrier), "carrier_sha256": sha256(carrier),
               "carrier_fps": bundle["carrier_fps"], "frame_range": fr, "frames": total, "canvas": CANVAS,
               "seed_offset": int(args.seed_offset), "chunks": chunks, "canvas_carrier": str(canvas),
               "tail_pad": tail_pad, "render_frames": render_total,
               "contract": str(out / "contract" / "contract.json"), "prompt_sha256": contract["prompt_sha256"],
               "render_root": str(render_root), "spatial": SPATIAL.to_dict(), "control": ctl,
               "control_inputs": {"masks": list(masks.values()), "hed": heds, "jobs": len(jobs)},
               "build_seconds": round(time.time() - t0, 1)}
    if keep:
        package["keep_regions"] = keep
    if ep.get("wide_wrist"):
        package["wide_wrist"] = ep["wide_wrist"]
    (out / "package.json").write_text(json.dumps(package, indent=2, ensure_ascii=False))
    ticket = {"package": str(out), "manifest": str(out / "job-manifest.json"), "contract": package["contract"],
              "output_root": str(render_root), "profile": recipe["worker_profile"]}
    (out / "ticket.json").write_text(json.dumps(ticket, indent=2))
    print(json.dumps({"package": str(out), "chunks": len(jobs), "frames": total, "profile": recipe["worker_profile"],
                      "prompt_sha256": policy["prompt_sha256"],
                      "hed_active_fraction": [round(h["mean_active_fraction_gt20"] or 0, 4) for h in heds],
                      "build_seconds": package["build_seconds"]}))
    return 0


def cmd_check(args) -> int:
    print(json.dumps(audit(Path(args.package).resolve()), indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--bundle", required=True)
    b.add_argument("--recipe", required=True)
    b.add_argument("--episode", required=True)
    b.add_argument("--mode", choices=["sim2real", "ood"], required=True)
    b.add_argument("--variant")
    b.add_argument("--seed-offset", type=int, default=0)
    b.add_argument("--out", required=True)
    b.add_argument("--force", action="store_true")
    b.add_argument("--hed-jobs", type=int, default=4, help="chunks whose HED runs in parallel")
    b.add_argument("--hed-threads", type=int, default=16, help="CPU threads per HED process")
    b.add_argument("--hed-device", choices=["cpu", "cuda"], default="cpu",
                   help="cpu outside a GPU lease (default); cuda only inside a scheduler GPU lease")
    h3_paths.add_run_dir_arg(b)
    c = sub.add_parser("check")
    c.add_argument("package")
    args = ap.parse_args()
    if getattr(args, "run_dir", None) is not None:
        h3_paths.set_run_dir(args.run_dir)
    return {"build": cmd_build, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

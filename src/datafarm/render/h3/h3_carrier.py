#!/usr/bin/env python3
"""H3 control hybrid 的 white carrier 准备与检查。

  compose  把三个同步的 white 视图（head 1280x720，wrists 640x360）合成为一张 1280x1080 的 T 形布局
           carrier，速率与各视图自身一致（24 或 30 fps）。视图可以是视频，也可以是 replay 的 ffmpeg
           concat PNG 帧列表（`<camera>.concat`，从 attempt 目录改写到 replay 目录）。低于 MIN_WHITE_FPS
           的 white 会被拒绝：应密集地重新渲染（datafarm.simulation.white_replay 配 image_stride），
           绝不对其插值。运动插值会扭曲并拖拽快速移动的物体，H3 会把这些漂移复制进输出。
  check    给出可预测 H3 失败的 carrier 测量值，由 `h3_bundle.py check` 以 warning 形式报告：
           frame rate，以及 head 视图是否存在 control 可见的 room seam（wall/table 边缘）。soft-HED
           control 只保留具有强局部亮度阶跃的边缘（edge_gate tau 25）：在平坦的 white 场景中，若 table
           与 wall 只差几个灰阶，control 就没有 table 边缘，H3 会在 wrist 视图中凭空造出 wall 或 table
           边缘。带明暗的 white（例如 table 237，wall 186）能保住该 seam。

  python h3_carrier.py compose --head cam_high.mp4 --left cam_left_wrist.mp4 --right cam_right_wrist.mp4 --out t3.mp4
  python h3_carrier.py compose --replay <replay dir> --attempt <attempt dir> --out t3.mp4
  python h3_carrier.py check <carrier.mp4>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from h3_media import probe

MIN_WHITE_FPS = 20.0
CARRIER_FPS = (24, 30)
CAMERAS = {"head": "cam_high", "left": "cam_left_wrist", "right": "cam_right_wrist"}
# room-seam 检查：与默认 control 相同的 luma-step gate（h3-control-hybrid-v1.json control.edge_gate）
GATE_TAU, GATE_WINDOW = 25.0, 7
SEAM_MIN_LENGTH = 0.25        # 占 head 视图宽度的比例
SEAM_MAX_TILT_DEG = 35.0      # wall/table seam 或 table 边缘，而非竖直的 robot 或 object 边缘


def fps_of(path) -> float:
    return float(Fraction(probe(path).rate))


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _image_rate(replay: Path, attempt: Path | None, cam: str) -> str | None:
    """replay 的 PNG 帧的 frame rate：取自其 image-manifest.json（fps，或 physics_hz / stride），否则取该
    attempt 中该相机自己的视频（未加密的 replay 会沿用 attempt 的图像速率）。"""
    for d in (replay, attempt):
        man = Path(d) / "image-manifest.json" if d else None
        if man and man.is_file():
            m = json.loads(man.read_text())
            if m.get("fps"):
                return str(m["fps"])
            if m.get("physics_hz") and m.get("stride"):
                return str(Fraction(str(m["physics_hz"])) / int(m["stride"]))
        if d is attempt:
            break
    if attempt is not None and (Path(attempt) / f"{cam}.mp4").is_file():
        return probe(Path(attempt) / f"{cam}.mp4").rate
    return None


def replay_views(replay: Path, attempt: Path | None = None) -> dict:
    """{head, left, right} 的 white replay 视频：优先用 replay 目录中的 <camera>.mp4，否则用其
    <camera>.concat 中列出的 PNG 帧（或 attempt 中的列表，并把路径改到 replay 目录），按列表顺序每个 PNG
    一帧，以 replay 的图像速率编码。"""
    replay = Path(replay)
    views = {}
    for side, cam in CAMERAS.items():
        mp4 = replay / f"{cam}.mp4"
        if mp4.is_file():
            views[side] = mp4
            continue
        names = (f"{cam}.concat", f"{cam}-frames.txt")   # 所采集 PNG 帧的 ffmpeg concat 列表
        conc = next((replay / n for n in names if (replay / n).is_file()), None)
        if conc is None and attempt is not None:
            src = next((Path(attempt) / n for n in names if (Path(attempt) / n).is_file()), None)
            if src is not None:
                conc = replay / src.name
                conc.write_text(src.read_text().replace(str(Path(attempt)) + "/", str(replay) + "/"))
        if conc is None:
            raise SystemExit(f"replay {replay} has neither {cam}.mp4 nor a frame list ({' / '.join(names)})")
        pngs = [line.split("'")[1] for line in conc.read_text().splitlines() if line.startswith("file ")]
        missing = [p for p in pngs if not Path(p).is_file()]
        if not pngs or missing:
            raise SystemExit(f"{conc}: {len(missing)} of {len(pngs)} listed frames missing")
        rate = _image_rate(replay, attempt, cam)
        if rate is None:
            raise SystemExit(f"{conc}: frame rate unknown (no image-manifest.json and no original {cam}.mp4)")
        seq = replay / f"{cam}-seq"
        seq.mkdir(exist_ok=True)
        for old in seq.glob("*.png"):
            old.unlink()
        for i, p in enumerate(pngs):
            os.symlink(p, seq / f"{i:06d}.png")
        tmp = mp4.with_suffix(".tmp.mp4")
        ffmpeg("-framerate", str(rate), "-i", str(seq / "%06d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-crf", "12", str(tmp))
        os.replace(tmp, mp4)
        if probe(mp4).frames != len(pngs):
            raise SystemExit(f"{mp4}: {probe(mp4).frames} frames, {len(pngs)} listed")
        views[side] = mp4
    return views


def compose(views: dict, out: Path) -> dict:
    """三个同步视图的 T 形布局 carrier；每个视图的第 i 帧就是 carrier 的第 i 帧（不重新计时）。"""
    rates = {side: fps_of(v) for side, v in views.items()}
    frames = {side: probe(v).frames for side, v in views.items()}
    if len(set(frames.values())) != 1:
        raise SystemExit(f"views have different frame counts {frames}")
    fps = rates["head"]
    if any(abs(r - fps) > 0.01 for r in rates.values()):
        raise SystemExit(f"views have different frame rates {rates}")
    if fps < MIN_WHITE_FPS:
        raise SystemExit(f"white at {fps:g} fps is below {MIN_WHITE_FPS:g}: re-render it densely (white_replay "
                         "image_stride); a motion-interpolated carrier makes H3 copy warped, drifting objects")
    rate = min(CARRIER_FPS, key=lambda r: abs(r - fps))
    if abs(rate - fps) > 0.01:
        raise SystemExit(f"white at {fps:g} fps: the carrier rate must be one of {CARRIER_FPS} without resampling")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    fc = ("[0:v]scale=1280:720:flags=lanczos,setsar=1[h];[1:v]scale=640:360:flags=lanczos,setsar=1[l];"
          "[2:v]scale=640:360:flags=lanczos,setsar=1[r];[l][r]hstack=inputs=2[b];[h][b]vstack=inputs=2,format=yuv420p[o]")
    ffmpeg("-i", str(views["head"]), "-i", str(views["left"]), "-i", str(views["right"]), "-filter_complex", fc,
           "-map", "[o]", "-r", str(rate), "-fps_mode", "cfr", "-an", "-c:v", "libx264", "-preset", "fast",
           "-crf", "10", str(tmp))
    os.replace(tmp, out)
    n = probe(out).frames
    if n != frames["head"]:
        raise SystemExit(f"composed carrier has {n} frames, views have {frames['head']}")
    meta = {"carrier": str(out), "fps": rate, "frames": n, "views": {k: str(v) for k, v in views.items()}}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    return meta


def room_seams(head_bgr) -> list[dict]:
    """head 视图一帧中、能通过 control 的 luma-step gate 且位于深色 robot 之外的长而大致水平的边缘。每项：
    {length（占视图宽度比例）、tilt_deg、step（跨越该边缘的灰阶）}。"""
    import cv2
    import numpy as np
    h, w = head_bgr.shape[:2]
    g = cv2.GaussianBlur(cv2.cvtColor(head_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 1.2)
    kern = np.ones((GATE_WINDOW, GATE_WINDOW), np.uint8)
    step = cv2.dilate(g, kern) - cv2.erode(g, kern)
    hsv = cv2.cvtColor(head_bgr, cv2.COLOR_BGR2HSV)
    robot = cv2.dilate(((hsv[..., 2] < 110) & (hsv[..., 1] < 90)).astype(np.uint8), np.ones((15, 15), np.uint8))
    edges = cv2.Canny(g.astype(np.uint8), 8, 24)
    edges[(step <= GATE_TAU) | (robot > 0)] = 0
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=40, minLineLength=int(SEAM_MIN_LENGTH * w), maxLineGap=12)
    out = []
    for a, b, c, d in ([] if lines is None else lines[:, 0].tolist()):
        tilt = abs(math.degrees(math.atan2(d - b, c - a)))
        tilt = min(tilt, 180 - tilt)
        if tilt <= SEAM_MAX_TILT_DEG:
            out.append({"length": round(math.hypot(c - a, d - b) / w, 3), "tilt_deg": round(tilt, 1),
                        "step": round(float(step[(b + d) // 2, (a + c) // 2]), 1)})
    return sorted(out, key=lambda s: -s["length"])


def check(carrier: Path, samples: int = 5) -> dict:
    """carrier 的测量值与 warning（不是 gate：相机始终看不到 wall 或 table 边缘是合法的）。"""
    import cv2
    info = probe(carrier)
    fps = float(Fraction(info.rate))
    warnings = []
    if fps < MIN_WHITE_FPS:
        warnings.append(f"carrier at {fps:g} fps: re-render the white densely (white_replay image_stride)")
    cap = cv2.VideoCapture(str(carrier))
    seams = []
    for k in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(k * max(1, info.frames - 1) / max(1, samples - 1)))
        ok, f = cap.read()
        if not ok:
            continue
        head = cv2.resize(f, (1280, 1080))[:720]
        seams.append(room_seams(head))
    cap.release()
    with_seam = sum(1 for s in seams if s)
    if seams and with_seam == 0:
        warnings.append("head view has no wall/table seam with a luminance step above the control gate (tau 25): "
                        "H3 tends to invent a wall or table edge; shade the white scene (distinct table and wall "
                        "brightness) or dress it with white_replay extras")
    return {"carrier": str(carrier), "fps": fps, "frames": info.frames, "sampled_frames": len(seams),
            "frames_with_room_seam": with_seam, "longest_seam": max((s[0] for s in seams if s),
                                                                    key=lambda x: x["length"], default=None),
            "warnings": warnings}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compose")
    c.add_argument("--head")
    c.add_argument("--left")
    c.add_argument("--right")
    c.add_argument("--replay", type=Path, help="white replay directory (datafarm.simulation.white_replay OUT_DIR)")
    c.add_argument("--attempt", type=Path, help="the replayed attempt (for its concat lists and frame rate)")
    c.add_argument("--out", required=True, type=Path)
    k = sub.add_parser("check")
    k.add_argument("carrier", type=Path)
    args = ap.parse_args()
    if args.cmd == "check":
        rep = check(args.carrier)
        print(json.dumps(rep, indent=1))
        return 0
    if args.replay:
        views = replay_views(args.replay, args.attempt)
    elif args.head and args.left and args.right:
        views = {"head": Path(args.head), "left": Path(args.left), "right": Path(args.right)}
    else:
        ap.error("give --head/--left/--right or --replay")
    print(json.dumps(compose(views, args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""单个任务的 real references，取自该任务自己的 real episode，并受 H3 的 reference 内存预算约束。

在 2026-11 生产渲染中通过独立盲审的 reference 集合（双臂 Piper 上的 drum、weigh-apple 和
stack-cups；RealBot-01 上的 scoop-cereal）：

  f0           全视角 T 形布局的 real frame 0（carrier 也从该起始状态开始）；
  arm frame    全视角 real frame，其中真实机器人占据头部视野最多的部分（真实连杆外观）；
  robot sheet  取自头部视野的未修改真实机器人像素（左半取左臂最可见的那一帧，右半同理），
               置于中性灰背景上。reference 中没有真实连杆时，H3 会照抄 carrier 中的白色
               仿真机械臂；
  object sheet 起始状态下完整的真实任务物体，放大后置于中性灰背景上，使腕部视角即使比
               头部相机更近地看到物体，也仍能得到真实材质和颜色。若物体外观在 episode 中发生
               变化（例如显示屏亮起），则从更晚的帧再取第二块 tile；
  wrist sheet  对每个真实腕部相机，取任务物体内容最多的那一帧，纵向堆叠（640x720）。

H3 会把每个图像 reference 编码到 2048 像素短边，因此一个 reference 的开销是缩放后的面积
（1280x1080 的 T 形帧为 5.0 MP，1280x720 的 sheet 为 7.5 MP，1280x360 的条带为 14.9 MP）。在
178 GiB B200 上实测的峰值：24.9 MP 的 references 为 171.0 GiB，29.8 MP 为 175.4 GiB，31.9 MP 即
超出内存。超过 `budget_mpix`（默认 25）时，先丢弃 f0 之后的全视角帧，再从末尾开始丢弃 sheet。

机器人像素来自以下两种来源之一：
  * 默认启发式：机器人是视野中最暗的物体（grey < 60、低饱和、厚重连通域），这对 Piper 这类
    深色连杆的机械臂成立；
  * 投影标签掩码（`robot_masks`，见 h3_robot_masks.py）：用于浅色机器人或深色家具。

当任务物体无法自动与背景分离时（白底白物、深色把手把碗分成两半），spec 提供
`object_boxes` [{name, box: [x0, y0, x1, y1] in the 1280x720 head view, frame?}]，每个物体在其 box
内用 GrabCut 抠出。

object sheet 面向在 episode 中持续可见的物体。对于会把物体收起来的任务（放进抽屉、移出视野），
应关闭 `object_sheet`：否则物体图像会让生成器把该物体重新插入。

  python h3_episode_references.py build --spec <run>/tasks/<task>/episode-references.json
  python h3_episode_references.py robot-colors --spec <spec>     # {"@robot": rgba, "@robot_links": {...}}

Spec（任务产物，schema h3.episode-references/1）：
  {"schema": "h3.episode-references/1", "out_dir": "<run>/tasks/<task>/refs-episode",
   "real": {"head": cam_high.mp4, "left": cam_left_wrist.mp4, "right": cam_right_wrist.mp4},
   "robot_sheet": true, "object_sheet": true, "wrist_sheet": true, "arm_frame": true,
   "object_boxes": null, "robot_masks": null, "budget_mpix": 25}
`build` 写入 <out_dir>/references.json：{"references": [REFERENCE, ...], "dropped": [...], "mpix": ...}；把
该列表复制到 bundle 的 real_references（或 per_episode_references）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SPEC_SCHEMA = "h3.episode-references/1"
GREY = 128
BUDGET_MPIX = 25.0
MAX_REFERENCES = 9
DARK = 60               # 低于该灰度级的像素在默认启发式中计为机器人
MASK_SLACK_PX = 9       # 投影掩码：标定/运动学残差以及连杆边缘未建模的线缆
HEAD = {"head": [0, 0, 1280, 720]}
T_REGIONS = {"head": [0, 0, 1280, 720], "left_wrist": [0, 720, 640, 360], "right_wrist": [640, 720, 640, 360]}


# ------------------------------------------------------------------ 帧访问
def frame_count(video) -> int:
    import cv2
    cap = cv2.VideoCapture(str(video))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return n


def read_frame(video, i: int, size=(1280, 720)):
    import cv2
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
    ok, f = cap.read()
    cap.release()
    return cv2.resize(f, size, interpolation=cv2.INTER_AREA) if ok else None


def sample(video, indices, size=(1280, 720)):
    """(i, frame)，按顺序返回每个可读索引对应的帧。"""
    import cv2
    cap = cv2.VideoCapture(str(video))
    out = []
    for i in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, f = cap.read()
        if ok:
            out.append((int(i), cv2.resize(f, size, interpolation=cv2.INTER_AREA)))
    cap.release()
    return out


# ------------------------------------------------------------------ 机器人像素
def dark_robot_mask(f):
    """默认启发式：厚重、接近中性的深色连通域（深色机器人连杆、夹爪、腕部相机）。"""
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    colour = (hsv[..., 1] > 90) & (hsv[..., 2] > 60)
    m = ((g < DARK) & ~colour).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros_like(m)
    sat = hsv[..., 1].astype(np.float32)
    for i in range(1, n):
        comp = lab == i
        if stats[i, cv2.CC_STAT_AREA] >= 0.003 * m.size and float(sat[comp].mean()) < 110:
            keep[comp] = 1
    keep = cv2.morphologyEx(keep, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    hn, hlab, hstats, _ = cv2.connectedComponentsWithStats((1 - keep).astype(np.uint8), 4)
    for i in range(1, hn):   # 填补小的内部空洞（高光）；大的空洞（线缆环内部）保持敞开
        x, y, w, h, area = hstats[i]
        if area < 0.002 * keep.size and x > 0 and y > 0 and x + w < keep.shape[1] and y + h < keep.shape[0]:
            keep[hlab == i] = 1
    return cv2.dilate(keep, np.ones((5, 5), np.uint8)).astype(bool) & ~colour & (g < 160)


class RobotMasks:
    """真实 episode 的逐连杆投影标签掩码：<dir>/<camera>/<frame:06d>.png（uint8，0 = 非机器人，
    k = <dir>/links.json 中的第 k 根连杆），当 <dir>/done.json 存在时即完整（由 h3_robot_masks.py 写入）。"""

    def __init__(self, root):
        self.root = Path(root)
        if not (self.root / "done.json").is_file():
            raise SystemExit(f"robot masks {root} are incomplete (no done.json)")
        self.links = json.loads((self.root / "links.json").read_text()).get("links") or []

    def frames(self, cam: str = "cam_high") -> list[int]:
        return sorted(int(p.stem) for p in (self.root / cam).glob("*.png"))

    def label(self, i: int, cam: str = "cam_high", size=(1280, 720)):
        import cv2
        fr = self.frames(cam)
        if not fr:
            return None
        k = min(fr, key=lambda x: abs(x - int(i)))
        lab = cv2.imread(str(self.root / cam / f"{k:06d}.png"), cv2.IMREAD_UNCHANGED)
        return None if lab is None else cv2.resize(lab, size, interpolation=cv2.INTER_NEAREST)

    def mask(self, i: int, cam: str = "cam_high", slack: int = MASK_SLACK_PX):
        import cv2
        import numpy as np
        lab = self.label(i, cam)
        if lab is None:
            return None
        return cv2.dilate((lab > 0).astype(np.uint8), np.ones((slack, slack), np.uint8)).astype(bool)

    def pixels(self, frame, i: int, slack: int = MASK_SLACK_PX, cam: str = "cam_high"):
        """投影掩码加上与之相接的小块深色连通域（线缆、投影遗漏的连杆边缘）；
        机器人旁边的大块深色连通域（家电、深色桌沿）不计为机器人。"""
        import cv2
        import numpy as np
        rm = self.mask(i, cam, slack)
        dark = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) < DARK
        _, lab, st, _ = cv2.connectedComponentsWithStats(dark.astype(np.uint8), connectivity=8)
        near = cv2.dilate(rm.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool) & dark
        touch = [t for t in np.unique(lab[near]) if t > 0 and st[t, cv2.CC_STAT_AREA] <= 0.02 * dark.size]
        return rm | np.isin(lab, touch) if touch else rm


def robot_pixels(frame, i: int, masks: RobotMasks | None, slack: int = MASK_SLACK_PX):
    import cv2
    if masks is not None:
        return masks.pixels(frame, i, slack)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) < DARK


# ------------------------------------------------------------------ 帧选择
def arm_frame(head, n: int, masks: RobotMasks | None = None) -> int:
    """真实机器人占据视野最多的头部帧（给定投影掩码时用掩码，否则依据暗度）。"""
    import cv2
    if masks is not None and masks.frames():
        return max(masks.frames()[::2], key=lambda i: float((masks.label(i) > 0).mean()))
    best = (-1.0, int(n * 0.4))
    for i, f in sample(head, range(0, n, max(1, n // 120))):
        best = max(best, (float((cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) < DARK).mean()), i))
    return best[1]


def half_arm_frames(head, n: int, masks: RobotMasks | None = None) -> tuple[int, int]:
    """左/右机器人手臂各自占据其所在半幅头部视野最多的帧。"""
    import cv2
    best = [(-1.0, 0), (-1.0, 0)]
    if masks is not None:
        items = [(i, masks.label(i) > 0) for i in masks.frames()[::2]]
    else:
        items = [(i, cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) < DARK) for i, f in sample(head, range(0, n, max(1, n // 120)))]
    for i, d in items:
        w = d.shape[1] // 2
        best = [max(best[0], (float(d[:, :w].mean()), i)), max(best[1], (float(d[:, w:].mean()), i))]
    return best[0][1], best[1][1]


# ------------------------------------------------------------------ references
def _reference(path, source, frame, description, scope, views, regions, coverage) -> dict:
    return {"path": str(path), "provenance": {"kind": "real-episode-frame", "source": str(source), "frame": int(frame),
                                              "description": description},
            "scope": scope, "views": views, "view_regions": regions, "coverage": coverage}


def full_view(real: dict, frame: int, out: Path, tag: str) -> dict:
    """未修改的 real T 形布局帧：上方 head 1280x720，下方左右腕 640x360，全部取自第 `frame` 帧。"""
    import cv2
    import numpy as np
    head = read_frame(real["head"], frame)
    left = read_frame(real["left"], frame, (640, 360))
    right = read_frame(real["right"], frame, (640, 360))
    if head is None or left is None or right is None:
        raise SystemExit(f"cannot read real frame {frame} of every view")
    p = out / f"real-3view-f{frame}.png"
    cv2.imwrite(str(p), np.vstack([head, np.hstack([left, right])]))
    return _reference(p, real["head"], frame, f"{tag}: unmodified real T-layout frame {frame} of this task's real "
                      "episode (head, left wrist and right wrist cameras at the same frame)", "full-view",
                      ["head", "left_wrist", "right_wrist"], T_REGIONS,
                      ["full_scene", "robot_body", "table_and_background", "photographic_character", "task_objects"])


def robot_sheet(real: dict, n: int, out: Path, masks: RobotMasks | None = None) -> dict | None:
    import cv2
    import numpy as np
    fl, fr = half_arm_frames(real["head"], n, masks)
    sheet = np.full((720, 1280, 3), GREY, np.uint8)
    frac = 0.0
    for fi, (x0, x1) in ((fl, (0, 640)), (fr, (640, 1280))):
        f = read_frame(real["head"], fi)
        if f is None:
            continue
        k = masks.pixels(f, fi, slack=3) if masks is not None else dark_robot_mask(f)
        k[:, :x0] = False
        k[:, x1:] = False
        sheet[k] = f[k]
        frac += float(k.mean())
    if frac < 0.02:
        return None
    p = out / "robot-sheet.png"
    cv2.imwrite(str(p), sheet)
    how = "projected robot masks" if masks is not None else "the dark-robot heuristic"
    return _reference(p, real["head"], fl, f"real robot appearance sheet: unmodified real head-view pixels of the robot "
                      f"(arm links, grippers, wrist cameras; selected by {how}), left half from real frame {fl}, right "
                      f"half from real frame {fr}; everything else neutral grey", "local-detail", ["head"], HEAD,
                      ["robot_body", "attached_cameras_and_cables"]) | {"robot_fraction": round(frac, 4)}


def _tile_sheet(crops: list):
    """将裁剪图放大（<= 4x，最高 340 px）后并排铺在 1280x720 的灰色 sheet 上。返回 (sheet, scale)。"""
    import cv2
    import numpy as np
    hmax = max(c.shape[0] for c in crops)
    k = min(4.0, 340.0 / hmax)
    wsum = sum(c.shape[1] for c in crops) * k
    room = 1280 - 20 * (len(crops) + 1) - 4
    if wsum > room:
        k *= room / wsum
    sheet = np.full((720, 1280, 3), GREY, np.uint8)
    x = 20
    for c in crops:
        cc = cv2.resize(c, (max(1, int(c.shape[1] * k)), max(1, int(c.shape[0] * k))), interpolation=cv2.INTER_CUBIC)
        y = (720 - cc.shape[0]) // 2
        sheet[y:y + cc.shape[0], x:x + cc.shape[1]] = cc
        x += cc.shape[1] + 20
    return sheet, round(float(k), 3)


def object_components(f, robot):
    """起始状态头部帧的物体裁剪：偏离房间尺度背景（LAB > 22）或高饱和、且非机器人的像素；
    占视野 0.15-15% 且不属于房间边缘的连通域，以凸包抠图形式返回。"""
    import cv2
    import numpy as np
    lab = cv2.cvtColor(f, cv2.COLOR_BGR2LAB).astype(np.float32)
    bg = cv2.resize(cv2.medianBlur(cv2.resize(f, (320, 180), interpolation=cv2.INTER_AREA), 61), (1280, 720))
    dev = np.linalg.norm(lab - cv2.cvtColor(bg, cv2.COLOR_BGR2LAB).astype(np.float32), axis=2)
    hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
    m = (((dev > 22) | ((hsv[..., 1] > 70) & (hsv[..., 2] > 50))) & ~robot).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8))
    nc, lb, st, _ = cv2.connectedComponentsWithStats(m, 8)
    crops = []
    for i in range(1, nc):
        x, y, w, h, area = st[i]
        if (not (0.0015 * m.size <= area <= 0.15 * m.size) or y <= 2 or x <= 2 or x + w >= 1278
                or w > 520 or h > 400 or area < 0.12 * w * h):
            continue
        hull = cv2.convexHull(np.argwhere(lb == i)[:, ::-1].astype(np.int32))
        mk = np.zeros_like(m)
        cv2.fillConvexPoly(mk, hull, 1)
        pad = 8
        x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(1280, x + w + pad), min(720, y + h + pad)
        mm = cv2.dilate(mk, np.ones((pad, pad), np.uint8))[y0:y1, x0:x1].astype(bool)
        crops.append(((x0, y0, x1, y1), mm))
    return crops, bg


def object_sheet(real: dict, n: int, out: Path, masks: RobotMasks | None = None) -> dict | None:
    """起始状态下的完整真实任务物体（取前 20% 帧中机器人最少的一帧；有投影掩码时用 frame 0），
    最多 4 个，从左到右排列，外加状态变化 tile（目标像素的 3-25% 在最新的未被遮挡的后续帧中 LAB 差异
    > 25：属于状态变化；被移动或受阴影影响的物体变化远大于此）。"""
    import cv2
    import numpy as np
    if masks is not None:
        f0 = 0
    else:
        cands = sample(real["head"], range(0, max(1, n // 5), max(1, n // 100)))
        f0 = min(((float(robot_pixels(f, i, None).mean()), i) for i, f in cands), default=(0, 0))[1]
    f = read_frame(real["head"], f0)
    if f is None:
        return None
    rob0 = robot_pixels(f, f0, masks)
    comps, bg = object_components(f, rob0)
    if masks is not None:
        comps = sorted(comps, key=lambda t: -int(t[1].sum()))[:4]
    comps = sorted(comps, key=lambda t: t[0][0])[:4]
    if not comps:
        return None
    lab0 = cv2.cvtColor(f, cv2.COLOR_BGR2LAB).astype(np.float32)
    bgl = cv2.cvtColor(bg, cv2.COLOR_BGR2LAB).astype(np.float32)
    later = sample(real["head"], range(n // 2, max(n // 2 + 1, n - 2), max(1, n // 80)))
    tiles, states = [], {}
    for (x0, y0, x1, y1), mm in comps:
        c = np.full((y1 - y0, x1 - x0, 3), GREY, np.uint8)
        c[mm] = f[y0:y1, x0:x1][mm]
        tiles.append(c)
        occ0 = float(rob0[y0:y1, x0:x1][mm].mean())
        pick = None
        for i, fi in later:
            if float(robot_pixels(fi, i, masks)[y0:y1, x0:x1][mm].mean()) <= occ0 + 0.02:
                pick = (i, fi)
        if pick is None:
            continue
        i, fi = pick
        li = cv2.cvtColor(fi, cv2.COLOR_BGR2LAB).astype(np.float32)
        present0 = float((np.linalg.norm(lab0[y0:y1, x0:x1] - bgl[y0:y1, x0:x1], axis=2)[mm] > 22).mean())
        present1 = float((np.linalg.norm(li[y0:y1, x0:x1] - bgl[y0:y1, x0:x1], axis=2)[mm] > 22).mean())
        if present1 < 0.5 * present0:   # 物体已移开：较晚的裁剪只会显示裸露的桌面
            continue
        frac = float((np.linalg.norm(li[y0:y1, x0:x1] - lab0[y0:y1, x0:x1], axis=2)[mm] > 25).mean())
        if 0.03 < frac <= 0.25:
            c2 = np.full_like(c, GREY)
            c2[mm] = fi[y0:y1, x0:x1][mm]
            tiles.append(c2)
            states[f"{x0},{y0},{x1},{y1}"] = {"frame": i, "changed_fraction": round(frac, 3)}
    sheet, k = _tile_sheet(tiles[:8])
    p = out / "object-sheet.png"
    cv2.imwrite(str(p), sheet)
    boxes = [list(map(int, b)) for b, _ in comps]
    desc = (f"whole real task objects cut from real head frame {f0} (start state) with a background-deviation "
            f"convex-hull matte on neutral grey, enlarged x{k}; boxes {boxes}")
    if states:
        desc += f"; objects whose appearance changes during the episode have a second tile from a later frame: {states}"
    return _reference(p, real["head"], f0, desc, "local-detail", ["head"], HEAD, ["task_objects"]) | {
        "boxes": boxes, "state_tiles": states, "scale": k}


def object_sheet_boxes(real: dict, boxes: list, out: Path, masks: RobotMasks | None = None) -> dict | None:
    """从任务给定的 boxes 生成 object sheet：每个物体在其 box 内用 GrabCut 抠出，并移除机器人像素。"""
    import cv2
    import numpy as np
    tiles, used = [], []
    for ob in boxes:
        x0, y0, x1, y1 = [int(v) for v in ob["box"]]
        fr = ob.get("frame")
        if fr is None:
            cands = masks.frames()[::3] if masks is not None else [0]
            fr = min(cands or [0], key=lambda i: float(masks.mask(i)[y0:y1, x0:x1].mean()) if masks is not None else 0.0)
        f = read_frame(real["head"], fr)
        if f is None:
            continue
        gc = np.zeros(f.shape[:2], np.uint8)
        bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.grabCut(f, gc, (x0, y0, x1 - x0, y1 - y0), bgd, fgd, 6, cv2.GC_INIT_WITH_RECT)
        mm = (gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD)
        if masks is not None:
            mm &= ~masks.pixels(f, fr, slack=5)
        mm = cv2.morphologyEx(mm.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)).astype(bool)
        c = np.full((y1 - y0, x1 - x0, 3), GREY, np.uint8)
        c[mm[y0:y1, x0:x1]] = f[y0:y1, x0:x1][mm[y0:y1, x0:x1]]
        tiles.append(c)
        used.append({"name": ob.get("name"), "box": [x0, y0, x1, y1], "frame": int(fr),
                     "matte_fraction": round(float(mm[y0:y1, x0:x1].mean()), 3)})
    if not tiles:
        return None
    sheet, k = _tile_sheet(tiles)
    p = out / "object-sheet.png"
    cv2.imwrite(str(p), sheet)
    desc = ("whole real task objects cropped from the head view of this task's real episode ("
            + "; ".join(f"{o['name']}: real frame {o['frame']}, box {o['box']}" for o in used)
            + f") with a GrabCut matte on neutral grey (robot pixels removed), enlarged x{k}")
    return _reference(p, real["head"], used[0]["frame"], desc, "local-detail", ["head"], HEAD, ["task_objects"]) | {
        "objects": used, "scale": k}


def wrist_sheet(real: dict, out: Path) -> dict | None:
    """单张 640x720 的 sheet：左腕帧在上、右腕帧在下，各自取该腕部视角任务物体内容最多的帧
    （偏离平滑背景或高饱和、且非深色夹爪，位于最低四分之一——夹爪本身所在区域——之上）。纵向堆叠
    而非并排：单条 1280x360 条带本身就要约 15 MP。"""
    import cv2
    import numpy as np
    tiles, meta = [], {}
    for side in ("left", "right"):
        video = real[side]
        n = frame_count(video)
        best = (-1.0, 0, None)
        for i, f in sample(video, range(0, n, max(1, n // 100)), (640, 360)):
            bg = cv2.resize(cv2.medianBlur(cv2.resize(f, (160, 90)), 31), (640, 360))
            dev = np.linalg.norm(cv2.cvtColor(f, cv2.COLOR_BGR2LAB).astype(np.float32)
                                 - cv2.cvtColor(bg, cv2.COLOR_BGR2LAB).astype(np.float32), axis=2)
            hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
            obj = ((dev > 22) | ((hsv[..., 1] > 70) & (hsv[..., 2] > 50))) & (cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) >= DARK)
            score = float(obj[:270].mean())
            if score > best[0]:
                best = (score, i, f)
        if best[2] is None:
            return None
        tiles.append(best[2])
        meta[side] = {"frame": best[1], "object_fraction": round(best[0], 4), "source": str(video)}
    p = out / "wrist-sheet.png"
    cv2.imwrite(str(p), np.vstack(tiles))
    return _reference(p, real["left"], meta["left"]["frame"],
                      f"real wrist sheet: unmodified left-wrist camera frame {meta['left']['frame']} (upper half) and "
                      f"right-wrist camera frame {meta['right']['frame']} (lower half) of this task's real episode, each "
                      "the frame where that wrist view shows the most task-object content", "local-detail",
                      ["left_wrist", "right_wrist"], {"left_wrist": [0, 0, 640, 360], "right_wrist": [0, 360, 640, 360]},
                      ["task_objects", "robot_body", "table_and_background"]) | {"frames": meta}


def ref_mpix(path) -> float:
    """H3 为一个图像 reference 编码的百万像素数（缩放到 2048 像素短边后）。"""
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
    s = 2048 / min(w, h)
    return w * h * s * s / 1e6


def budget(full_views: list, sheets: list, limit: float = BUDGET_MPIX) -> tuple[list, list]:
    """在 `limit` MP 和 MAX_REFERENCES 之内按序保留 references：先丢弃首个之后的全视角帧（从末尾开始），
    再丢弃末尾的 sheet。返回 (kept, dropped)。"""
    full, sh, dropped = list(full_views), list(sheets), []

    def cost(items):
        return sum(ref_mpix(r["path"]) for r in items)
    while len(full) > 1 and cost(full + sh) > limit:
        dropped.append(full.pop())
    refs = full + sh
    while len(refs) > 1 and (cost(refs) > limit or len(refs) > MAX_REFERENCES):
        dropped.append(refs.pop())
    return refs, dropped


def load_spec(path) -> dict:
    spec = json.loads(Path(path).read_text())
    if spec.get("schema") != SPEC_SCHEMA:
        raise SystemExit(f"spec schema must be {SPEC_SCHEMA}")
    real = spec.get("real") or {}
    for side in ("head", "left", "right"):
        if not real.get(side) or not Path(real[side]).is_file():
            raise SystemExit(f"real.{side} video missing: the references must come from this task's own real episode")
    if not spec.get("out_dir"):
        raise SystemExit("out_dir is required")
    return spec


def build(spec: dict) -> dict:
    real, out = spec["real"], Path(spec["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    masks = RobotMasks(spec["robot_masks"]) if spec.get("robot_masks") else None
    n = frame_count(real["head"])
    frames = [0]
    if spec.get("arm_frame", True):
        a = arm_frame(real["head"], n, masks)
        if a != 0:
            frames.append(a)
    full = [full_view(real, f, out, "start state" if f == 0 else "real robot most visible") for f in frames]
    sheets = []
    if spec.get("robot_sheet", True):
        r = robot_sheet(real, n, out, masks)
        if r:
            sheets.append(r)
    if spec.get("object_sheet", True):
        o = (object_sheet_boxes(real, spec["object_boxes"], out, masks) if spec.get("object_boxes")
             else object_sheet(real, n, out, masks))
        if o:
            sheets.append(o)
    if spec.get("wrist_sheet", True):
        w = wrist_sheet(real, out)
        if w:
            sheets.append(w)
    refs, dropped = budget(full, sheets, float(spec.get("budget_mpix", BUDGET_MPIX)))
    extra = {}
    for r in refs + dropped:   # 测量数据保留在记录中，不进入 bundle 的 REFERENCE
        extra[r["path"]] = {k: r.pop(k) for k in list(r) if k not in
                            ("path", "provenance", "scope", "views", "view_regions", "coverage")}
    rec = {"schema": "h3.episode-references-result/1", "references": refs, "dropped": [r["path"] for r in dropped],
           "mpix": round(sum(ref_mpix(r["path"]) for r in refs), 2), "budget_mpix": float(spec.get("budget_mpix",
                                                                                                   BUDGET_MPIX)),
           "measurements": extra, "robot_pixels": "projected masks" if masks is not None else "dark heuristic"}
    (out / "references.json").write_text(json.dumps(rec, indent=2))
    return rec


# ------------------------------------------------------------------ white replay 的真实机器人颜色
def robot_color(sheet_path) -> list | None:
    """所有机器人连杆共用一个 sim 基色：robot sheet 非灰色像素的中位数，sRGB/255，下限为
    0.06，使连杆保有可见的明暗和 HED 边缘。适用于单色机器人（深色 Piper 连杆）。"""
    import cv2
    import numpy as np
    im = cv2.imread(str(sheet_path))
    if im is None:
        return None
    px = im.reshape(-1, 3).astype(np.int16)
    keep = np.abs(px - GREY).max(axis=1) > 6
    if keep.mean() < 0.01:
        return None
    b, g, r = np.median(px[keep], axis=0) / 255.0
    return [round(max(0.06, float(x)), 3) for x in (r, g, b)] + [1.0]


def robot_link_colors(head, masks: RobotMasks, min_pixels: int = 300) -> dict:
    """逐连杆的真实颜色（连杆并非单色的机器人）：对每根连杆，取其投影掩码下真实头部视野像素的
    中位数，掩码先腐蚀 11 px，只统计深居连杆内部的像素。仅用头部视野：腕部相机从数厘米处看自己的
    夹爪，标定残差可达数十像素。"""
    import cv2
    import numpy as np
    acc: dict = {}
    fr = masks.frames()
    for k, f in sample(head, fr[::max(1, len(fr) // 60)]):
        lab = masks.label(k)
        for li in np.unique(lab):
            if li == 0:
                continue
            m = cv2.erode((lab == li).astype(np.uint8), np.ones((11, 11), np.uint8)).astype(bool)
            if m.sum() >= 30:
                acc.setdefault(int(li), []).append(f[m][::max(1, int(m.sum()) // 2000)])
    res = {}
    for li, parts in acc.items():
        px = np.concatenate(parts)
        if len(px) >= min_pixels and 0 < li <= len(masks.links):
            b, g, r = np.median(px, axis=0) / 255.0
            res[masks.links[li - 1]] = [round(max(0.06, float(x)), 3) for x in (r, g, b)] + [1.0]
    return res


def robot_colors(spec: dict) -> dict:
    """由 robot sheet 得到 {"@robot": rgba}，若有投影掩码再加 "@robot_links"：white-replay 颜色。"""
    out = Path(spec["out_dir"])
    sheet = out / "robot-sheet.png"
    if not sheet.is_file():
        raise SystemExit(f"no robot sheet at {sheet}: run build first (robot_sheet: true)")
    res = {}
    col = robot_color(sheet)
    if col:
        res["@robot"] = col
    if spec.get("robot_masks"):
        links = robot_link_colors(spec["real"]["head"], RobotMasks(spec["robot_masks"]))
        if links:
            res["@robot_links"] = links
    (out / "robot-colors.json").write_text(json.dumps(res, indent=2))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "robot-colors"):
        p = sub.add_parser(name)
        p.add_argument("--spec", required=True)
    args = ap.parse_args()
    spec = load_spec(args.spec)
    res = build(spec) if args.cmd == "build" else robot_colors(spec)
    print(json.dumps(res if args.cmd == "robot-colors" else
                     {k: res[k] for k in ("mpix", "budget_mpix", "dropped")} | {"references": len(res["references"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

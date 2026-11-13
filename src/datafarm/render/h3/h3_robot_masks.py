#!/usr/bin/env python3
"""基于真实关节状态摆姿的 URDF，生成 REAL episode 的逐 link robot label mask。

h3_episode_references.py 中的参考启发式假设 robot 是视野中最暗的物体。对于带浅色 link 的 robot（白色或
银色机械臂）或带深色家具的场景，它们会选错像素。本工具用每个采样帧的真实 robot 状态对 URDF 视觉 mesh
摆姿，并用真实相机模型投影；每个采样帧和相机都会得到一张 uint8 label 图像（0 = 非 robot，k = links.json
中的第 k 个 link），link 按由远及近绘制，使较近的 link 赢得重叠。

robot 运动学与相机标定都是任务产物（在 grounding 中校验）。任务提供一个 Python 模块，包含：

  URDF                          robot URDF 的路径（mesh 文件名：package://<pkg>/...、相对路径，或 meshes/<name>）
  real_states() -> array        每个真实帧的 robot 状态（N x D），frame i = 真实视频第 i 帧
  link_poses(state) -> dict     针对一个状态返回 {link name: 4x4 world-from-link}
  camera(view, poses) -> tuple  为该状态下的视图 cam_high / cam_left_wrist / cam_right_wrist 返回
                                 （OpenCV 光学坐标系下的 4x4 world-from-camera：x 向右、y 向下、z 向前，
                                 3x3 K、畸变系数、width、height）（wrist 相机会随其 link 移动）

  python h3_robot_masks.py --kinematics <task>/kinematics.py --out <run>/tasks/<task>/robot-masks [--step 15]

写出 <out>/links.json、<out>/<view>/<frame:06d>.png，最后写 <out>/done.json。需要 numpy、opencv 和 trimesh。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

VIEWS = ("cam_high", "cam_left_wrist", "cam_right_wrist")


def origin_matrix(el):
    """由 URDF 的 <origin xyz rpy> 元素得到 4x4 矩阵（缺失时为单位阵）。"""
    import numpy as np
    T = np.eye(4)
    if el is None:
        return T
    x, y, z = (float(v) for v in (el.get("xyz") or "0 0 0").split())
    r, p, yw = (float(v) for v in (el.get("rpy") or "0 0 0").split())
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(yw), np.sin(yw)
    T[:3, :3] = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                 [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                 [-sp, cp * sr, cp * cr]]
    T[:3, 3] = [x, y, z]
    return T


def mesh_path(urdf: Path, filename: str) -> Path:
    if filename.startswith("package://"):
        rel = filename[len("package://"):].split("/", 1)[-1]
        for root in (urdf.parent, *urdf.parents):
            if (root / rel).is_file():
                return root / rel
    p = urdf.parent / filename
    if p.is_file():
        return p
    for root in (urdf.parent, *urdf.parents[:2]):
        if (root / "meshes" / Path(filename).name).is_file():
            return root / "meshes" / Path(filename).name
    raise SystemExit(f"mesh {filename} of {urdf} not found")


def link_meshes(urdf: Path) -> tuple[list, dict]:
    """返回每个 mesh visual 的（按 URDF 顺序排列的 link 名，{link: [(link 坐标系中的顶点, faces), ...]}）。"""
    import numpy as np
    import trimesh
    root = ET.parse(urdf).getroot()
    links, meshes = [], {}
    for ln in root.findall("link"):
        for vis in ln.findall("visual"):
            g = vis.find("geometry/mesh")
            if g is None:
                continue
            m = trimesh.load(str(mesh_path(urdf, g.get("filename"))), force="mesh")
            sc = np.array([float(x) for x in (g.get("scale") or "1 1 1").split()])
            T = origin_matrix(vis.find("origin"))
            v = (m.vertices * sc) @ T[:3, :3].T + T[:3, 3]
            name = ln.get("name")
            if name not in meshes:
                links.append(name)
                meshes[name] = []
            meshes[name].append((v.astype(np.float64), m.faces.astype(np.int32)))
    return links, meshes


def project_labels(links: list, meshes: dict, poses: dict, cam_T, K, dist, width: int, height: int):
    """某个相机的 label 图像：投影每个 link 的三角形（仅限约在镜头视场之内、畸变多项式有效的区域；没有
    边长超过图像四分之一的三角形），先画较远的 link。"""
    import cv2
    import numpy as np
    lab = np.zeros((height, width), np.uint8)
    K, dist = np.asarray(K, float), np.asarray(dist, float)
    R, t = np.asarray(cam_T, float)[:3, :3], np.asarray(cam_T, float)[:3, 3]
    order = []
    for li, n in enumerate(links):
        if n not in poses:
            continue
        P = np.asarray(poses[n], float)
        polys, depth = [], []
        for verts, faces in meshes[n]:
            Pc = (verts @ P[:3, :3].T + P[:3, 3] - t) @ R
            z = np.maximum(Pc[:, 2], 1e-6)
            ok = (Pc[:, 2] > 0.05) & (np.abs(Pc[:, 0] / z) < 1.3) & (np.abs(Pc[:, 1] / z) < 1.0)
            if not ok.any():
                continue
            uv = cv2.projectPoints(Pc.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), K, dist)[0].reshape(-1, 2)
            tri = uv[faces[ok[faces].all(1)]]
            tri = tri[(np.abs(tri) < 4000).all((1, 2))]
            if len(tri):
                tri = tri[np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2).max(1) < width / 4]
            if len(tri):
                polys.append(np.round(tri).astype(np.int32))
                depth.append(float(Pc[ok, 2].mean()))
        if polys:
            order.append((float(np.mean(depth)), li, np.concatenate(polys)))
    for _, li, tri in sorted(order, key=lambda o: -o[0]):
        cv2.fillPoly(lab, list(tri), int(li + 1))
    return lab


def load_kinematics(path: Path):
    spec = importlib.util.spec_from_file_location("task_kinematics", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    for name in ("URDF", "real_states", "link_poses", "camera"):
        if not hasattr(mod, name):
            raise SystemExit(f"{path} must define {name} (see h3_robot_masks.py)")
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kinematics", required=True, type=Path, help="task kinematics module (interface above)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--step", type=int, default=15, help="real frames between masks")
    ap.add_argument("--views", default=",".join(VIEWS))
    a = ap.parse_args()
    import cv2
    kin = load_kinematics(a.kinematics.resolve())
    urdf = Path(kin.URDF)
    links, meshes = link_meshes(urdf)
    if len(links) > 255:
        raise SystemExit("more than 255 mesh links do not fit a uint8 label image")
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "done.json").unlink(missing_ok=True)
    (a.out / "links.json").write_text(json.dumps({"urdf": str(urdf), "links": links,
                                                  "label_of": {n: i + 1 for i, n in enumerate(links)}}, indent=1))
    views = a.views.split(",")
    for v in views:
        (a.out / v).mkdir(exist_ok=True)
    states = kin.real_states()
    for i in range(0, len(states), a.step):
        poses = kin.link_poses(states[i])
        for v in views:
            cam_T, K, dist, w, h = kin.camera(v, poses)
            cv2.imwrite(str(a.out / v / f"{i:06d}.png"), project_labels(links, meshes, poses, cam_T, K, dist, w, h))
    (a.out / "done.json").write_text(json.dumps({"frames": len(states), "step": a.step, "views": views}))
    print(json.dumps({"out": str(a.out), "frames": len(states), "masks_per_view": len(range(0, len(states), a.step))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

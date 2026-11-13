"""针对显式 presence key 的观测边界与局部对比回退。

只有实际渲染出的像素才能补充颜色 presence。New-instance 和
extent 检查保留其原始 mask；这不是身份分类器。
"""
import cv2
import numpy as np


def source_boundary_presence(source, generated_mask, generated_bgr, min_area=1000):
    result = generated_mask.copy()
    closed = cv2.morphologyEx(source, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(closed)
    gray = cv2.cvtColor(generated_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.dilate(cv2.Canny(gray, 30, 60), np.ones((11, 11), np.uint8)) > 0
    lab = cv2.cvtColor(generated_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    all_near = cv2.dilate(source, np.ones((11, 11), np.uint8)) > 0
    evidence = []
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        mask = labels == i
        boundary = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_GRADIENT,
                                    np.ones((3, 3), np.uint8)) > 0
        # 面板边框不是观测到的物体边界。
        boundary[:3] = boundary[-3:] = False
        boundary[:, :3] = boundary[:, -3:] = False
        ring = (cv2.dilate(mask.astype(np.uint8), np.ones((41, 41), np.uint8)) > 0) & ~all_near
        if boundary.sum() < 32 or ring.sum() < 100:
            continue
        background = np.median(lab[ring], axis=0)
        foreground = np.linalg.norm(lab - background, axis=2) > 20
        edge_support = float(edges[boundary].mean())
        contrast_fraction = float(foreground[mask].mean())
        median_separation = float(np.linalg.norm(np.median(lab[mask], axis=0) - background))
        # 连贯的颜色分离可以拒绝有纹理的空桌面，即使
        # 它的单个像素和密集边缘能通过那两个比例。
        # 至少一半的源物体主体必须具备实际对比度，与
        # 密集的缺失物体召回阈值一致；未观测到的像素
        # 绝不单独从源填充。
        accepted = edge_support >= .70 and contrast_fraction >= .50 and median_separation > 20
        if accepted:
            result[mask & foreground] = 255
        evidence.append(dict(bbox=stats[i, :4].tolist(), edge_support=edge_support,
                             contrast_fraction=contrast_fraction, median_separation=median_separation,
                             supplemented=accepted))
    return result, evidence

"""解码为按布局归一化的工作表示。

生成的视频可能以 provider 特定的分辨率到达（例如
统一的 1.225x 放大）。因此每段视频在任何比较之前都会缩放到 carrier 的
布局坐标，同时保留原始尺寸用于
transport 检查。这里保存两份副本：

* ``frames``：布局 ``work_scale`` 下的彩色帧，每隔 ``stride`` 帧取一帧
  （样本 ``j`` 即源帧 ``j * stride``）；
* ``gray``：所有帧的灰度图，位于布局的 ``gray_scale``，用于
  帧率精确的时间、接缝与事件测量。
"""

from __future__ import annotations

from datafarm.common import sha256_file
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Clip:
    frames: np.ndarray
    gray: np.ndarray
    stride: int
    fps: float
    frame_count: int
    native_size: tuple[int, int]
    layout_size: tuple[int, int]
    work_scale: float
    gray_scale: float
    sha256: str | None = None
    source: str | None = None

    @property
    def sample_frames(self) -> np.ndarray:
        return np.arange(len(self.frames)) * self.stride

    def sample_of(self, frame: int) -> int:
        return int(min(len(self.frames) - 1, max(0, round(frame / self.stride))))


def _sizes(layout_size: tuple[int, int], work_scale: float, gray_scale: float):
    lw, lh = layout_size
    work = (max(8, int(round(lw * work_scale))), max(8, int(round(lh * work_scale))))
    gray = (max(8, int(round(lw * gray_scale))), max(8, int(round(lh * gray_scale))))
    return work, gray


def load_clip(
    path: str | Path,
    *,
    layout_size: tuple[int, int],
    work_scale: float = 0.5,
    gray_scale: float = 0.25,
    stride: int = 2,
    hash_file: bool = True,
    max_frames: int | None = None,
) -> Clip:
    path = Path(path)
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise OSError(f"cannot open video {path}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    native = (int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    work_size, gray_size = _sizes(layout_size, work_scale, gray_scale)
    frames: list[np.ndarray] = []
    grays: list[np.ndarray] = []
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if max_frames is not None and index >= max_frames:
            break
        if (frame.shape[1], frame.shape[0]) != tuple(layout_size):
            frame = cv2.resize(frame, tuple(layout_size), interpolation=cv2.INTER_AREA)
        if index % stride == 0:
            frames.append(cv2.resize(frame, work_size, interpolation=cv2.INTER_AREA))
        small = cv2.resize(frame, gray_size, interpolation=cv2.INTER_AREA)
        grays.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        index += 1
    capture.release()
    empty_work = np.zeros((0, work_size[1], work_size[0], 3), np.uint8)
    empty_gray = np.zeros((0, gray_size[1], gray_size[0]), np.uint8)
    return Clip(
        frames=np.stack(frames) if frames else empty_work,
        gray=np.stack(grays) if grays else empty_gray,
        stride=stride,
        fps=fps,
        frame_count=index,
        native_size=native,
        layout_size=tuple(layout_size),
        work_scale=work_scale,
        gray_scale=gray_scale,
        sha256=sha256_file(path) if hash_file else None,
        source=str(path),
    )


def load_reference_clip(path: str | Path, *, layout_size: tuple[int, int], work_scale: float = 0.5,
                        target_samples: int = 120) -> Clip:
    """真实世界素材：无论长度如何，都采样约 ``target_samples`` 张彩色帧。"""

    capture = cv2.VideoCapture(str(path))
    count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    capture.release()
    stride = max(1, count // max(1, target_samples))
    return load_clip(path, layout_size=layout_size, work_scale=work_scale, stride=stride)


def derive_clip(base: Clip, *, frames: np.ndarray | None = None, gray: np.ndarray | None = None,
                **changes) -> Clip:
    """复制一个 clip 并替换数组（供反事实生成器使用）。"""

    values = dict(base.__dict__)
    if frames is not None:
        values["frames"] = frames
    if gray is not None:
        values["gray"] = gray
        values["frame_count"] = len(gray)
    values.update(changes)
    values["sha256"] = None
    return Clip(**values)


def panel_slice(box, scale: float, shape: tuple[int, int]) -> tuple[slice, slice]:
    """给定缩放下布局坐标系盒子的行/列范围，裁剪到 shape。"""

    height, width = shape
    x0 = int(round(box.x * scale))
    y0 = int(round(box.y * scale))
    x1 = int(round((box.x + box.w) * scale))
    y1 = int(round((box.y + box.h) * scale))
    return slice(max(0, y0), min(height, y1)), slice(max(0, x0), min(width, x1))

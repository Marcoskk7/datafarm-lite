"""把 RGB 帧编码成 H.264 mp4（系统 ffmpeg，raw rgb24 走 stdin）。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np


class Encoder:
    def __init__(self, path: Path, *, width: int, height: int, fps: int, crf: int):
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is not on PATH; the Isaac renderer needs it to write mp4")
        self.shape = (height, width, 3)
        self.path = path
        self.process = subprocess.Popen(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
             "-r", str(fps), "-i", "-", "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p",
             "-r", str(fps), str(path)],
            stdin=subprocess.PIPE)
        self.count = 0

    def write(self, frame: np.ndarray) -> None:
        if frame.shape != self.shape or frame.dtype != np.uint8:
            raise ValueError(f"frame {frame.shape} {frame.dtype} does not match encoder {self.shape} uint8")
        self.process.stdin.write(frame.tobytes())
        self.count += 1

    def close(self) -> bytes:
        self.process.stdin.close()
        if self.process.wait() != 0:
            raise RuntimeError(f"ffmpeg failed writing {self.path}")
        return self.path.read_bytes()

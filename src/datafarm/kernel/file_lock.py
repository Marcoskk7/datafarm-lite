"""标准库跨平台独占文件锁：POSIX 使用 flock，Windows 使用 msvcrt 首字节区间锁。

本模块属于 kernel，只依赖标准库；JSONL 追加与 run ledger 共用同一实现。
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from typing import IO, Any, Iterator


@contextmanager
def exclusive_lock(handle: IO[Any]) -> Iterator[None]:
    """在 with 块内持有 handle 对应文件的独占锁；退出时释放。"""
    if os.name != "nt":
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt
    # Windows 锁定的是字节区间：统一锁文件首字节，并恢复调用方的读写位置。
    position = handle.tell()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    handle.seek(position)
    try:
        yield
    finally:
        position = handle.tell()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.seek(position)

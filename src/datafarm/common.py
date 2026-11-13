"""共享的仅依赖标准库的辅助函数：digests、canonical JSON、JSON 读取、原子写入、UTC 时间戳、JSONL 追加。

保持本模块不依赖第三方库与 datafarm 导入，以便包内任何部分都能使用它。
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping


_SHA256 = re.compile(r"[0-9a-f]{64}")


def is_sha256(value: Any) -> bool:
    """当值为小写十六进制 SHA-256 digest 时返回 True。"""
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def is_finite_number(value: Any) -> bool:
    """当值为有限 int 或 float 时返回 True；此处布尔值不算数字。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_bytes(value: Any, *, allow_nan: bool = False) -> bytes:
    """排序键、紧凑分隔符、UTF-8；除非 allow_nan，否则拒绝 NaN 与无穷大。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=allow_nan).encode("utf-8")


def sha256_json(value: Any, *, allow_nan: bool = False) -> str:
    return sha256_bytes(canonical_json_bytes(value, allow_nan=allow_nan))


def utc_now(timestamp: float | None = None, *, timespec: str = "seconds") -> str:
    """带 Z 后缀的 ISO 8601 UTC 时间，可用于当前时刻或 POSIX 时间戳。"""
    moment = dt.datetime.fromtimestamp(time.time() if timestamp is None else float(timestamp), tz=dt.timezone.utc)
    return moment.isoformat(timespec=timespec).replace("+00:00", "Z")


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_json_or_none(path: str | Path) -> Any:
    """用于可能不存在或只写入一部分的文件的 read_json；两种情况都返回 None。"""
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def _fsync_directory(directory: Path) -> None:
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def atomic_write_bytes(path: str | Path, data: bytes, *, mode: int | None = None, fsync: bool = True,
                       fsync_dir: bool = False) -> None:
    """先写入同目录下的临时文件，再重命名。

    mode 为 None 时保留临时文件仅属主可用的权限。  fsync_dir 还会同步目录项。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    if fsync_dir:
        _fsync_directory(target.parent)


def atomic_write_json(path: str | Path, value: Any, *, indent: int = 2, sort_keys: bool = True,
                      allow_nan: bool = True, mode: int | None = None, fsync: bool = True,
                      fsync_dir: bool = False) -> None:
    text = json.dumps(value, indent=indent, sort_keys=sort_keys, ensure_ascii=False, allow_nan=allow_nan) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode, fsync=fsync, fsync_dir=fsync_dir)


def append_jsonl(path: str | Path, value: Mapping[str, Any], *, lock: bool = True, fsync: bool = True,
                 allow_nan: bool = False, default: Callable[[Any], Any] | None = None) -> None:
    """追加一条按键排序的记录；lock 为真时在写入期间持有独占 flock。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=allow_nan, default=default) + "\n"
    with open(target, "a", encoding="utf-8") as handle:
        if lock:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        finally:
            if lock:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

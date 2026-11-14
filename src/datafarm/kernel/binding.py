"""绑定文件引用：``{path, sha256}`` 记录必须解析到它所指向的确切字节；并验证导出的源码树。"""
from __future__ import annotations

import json
from pathlib import Path

from datafarm.kernel.io import sha256_file


class ContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def bound_file(root: Path, ref: dict, *, external: bool = False) -> Path:
    require(isinstance(ref, dict) and isinstance(ref.get('path'), str), 'invalid file reference')
    raw = Path(ref['path'])
    p = (root / raw).resolve(strict=True)
    require(external or (not raw.is_absolute() and p.is_relative_to(root.resolve())),
            'artifact escapes task workspace')
    require(p.is_file() and p.stat().st_size > 0, 'artifact must be a nonempty file')
    require(sha256_file(p) == ref.get('sha256'), f'artifact changed: {ref["path"]}')
    return p


def verify_source_lock(root: Path, manifest: Path, expected_sha256: str) -> dict:
    """在不依赖开发者 Git checkout 的情况下验证导出的源码树。"""
    if sha256_file(manifest) != expected_sha256:
        raise ValueError("source lock manifest digest mismatch")
    value = json.loads(manifest.read_text())
    if value.get("schema") != "datafarm.source-lock/1" or not value.get("files"):
        raise ValueError("source lock must contain a nonempty file manifest")
    root = root.resolve()
    seen = set()
    for record in value["files"]:
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts or str(relative) in seen:
            raise ValueError("unsafe or duplicate source lock path")
        seen.add(str(relative))
        path = root / relative
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError("source lock cannot resolve outside its tree or through a symlink")
        if not path.is_file() or sha256_file(path) != record["sha256"]:
            raise ValueError(f"source lock mismatch: {relative}")
    actual = {str(p.relative_to(root)) for folder in ("src", "scripts")
              for p in (root / folder).rglob("*.py")}
    if not actual.issubset(seen):
        raise ValueError("unlocked Python source present in exported backend")
    return value

"""A 线 IR 文档共用的校验原语：位姿、标识、ArtifactRef 与可移植性。"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from datafarm.kernel.io import is_finite_number
from datafarm.kernel.schema import (IDENTIFIER_PATTERN, SchemaError, canonical_json_bytes,
                                    is_absolute_filesystem_path, validate_ref)

PRIM_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PRIM_PATH_EXAMPLE = "RootNode/Geometry/body_01_obj_00"
PRIM_PATH_RULE = (f"must be a prim path relative to the asset root, like {PRIM_PATH_EXAMPLE!r}: "
                  "no leading '/', no '.', '..' or empty segments")


class IRError(SchemaError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise IRError(message)


def require(document: Mapping[str, Any], keys: tuple[str, ...], label: str) -> None:
    missing = [key for key in keys if key not in document]
    check(not missing, f"{label} is missing required fields {missing}")


def identifier(value: Any, label: str) -> str:
    check(isinstance(value, str) and bool(IDENTIFIER_PATTERN.fullmatch(value)),
          f"{label} must match {IDENTIFIER_PATTERN.pattern}")
    return value


def is_prim_path(value: Any) -> bool:
    """`prim_ref` 形式的 prim 路径（USD prim 路径去掉前导 `/`，见 `adapters.sim.usd_assets.prim_ref`）。"""
    return (isinstance(value, str) and bool(value) and not is_absolute_filesystem_path(value)
            and all(PRIM_NAME_PATTERN.fullmatch(segment) for segment in value.split("/")))


def prim_path(value: Any, label: str) -> str:
    check(is_prim_path(value), f"{label} {PRIM_PATH_RULE}")
    return value


def ref(value: Any, label: str) -> None:
    check(isinstance(value, Mapping), f"{label} must be an ArtifactRef")
    try:
        validate_ref(value, label)
    except SchemaError as exc:
        raise IRError(str(exc)) from exc


def vector(value: Any, size: int, label: str) -> list[float]:
    check(isinstance(value, list) and len(value) == size and all(is_finite_number(x) for x in value),
          f"{label} must be {size} finite numbers")
    return value


def pose(value: Any, label: str) -> None:
    check(isinstance(value, Mapping), f"{label} must be a Pose")
    require(value, ("frame_id", "position_m", "quaternion_xyzw"), label)
    check(isinstance(value["frame_id"], str) and bool(value["frame_id"]), f"{label}.frame_id must be non-empty")
    vector(value["position_m"], 3, f"{label}.position_m")
    quaternion = vector(value["quaternion_xyzw"], 4, f"{label}.quaternion_xyzw")
    check(abs(math.sqrt(sum(x * x for x in quaternion)) - 1.0) <= 1e-3,
          f"{label}.quaternion_xyzw must be a unit quaternion (xyzw)")


def portable(document: Mapping[str, Any], label: str) -> None:
    """复用 kernel 的可移植性检查：无路径字段、无绝对路径、无凭据、无可变 URL。"""
    try:
        canonical_json_bytes(document)
    except SchemaError as exc:
        raise IRError(f"{label}: {exc}") from exc

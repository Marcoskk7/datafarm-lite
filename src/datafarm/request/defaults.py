"""请求草稿的确定性默认值：句子没说的字段在校验前补齐，并记录每个字段的来源。

默认值是 `datafarm.request/1` 契约的一部分，和 `ROBOT_CAMERAS` 一起随 schema 版本走，所以放在代码常量里，
不放 configs/：换默认值等于换 1a 的行为，应当走代码评审和测试。
`cameras` 与 `render.views` 依赖已定的 `robot` / `cameras`，由 `fill_defaults` 推导，不在常量表里。
"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any, Literal

from datafarm.request.schema import ROBOT_CAMERAS, SCHEMA

Source = Literal["sentence", "default"]

# 叶子路径 → 默认值；嵌套对象按路径逐层补齐，不整体替换。
DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA,
    "robot": "airbot_single_g2",
    "episodes": 20,
    "seeds.dev": [0, 99],
    "seeds.holdout": [1000, 1019],
    "render.engine": "isaac_rtx",
    "render.materials": 2,
    "render.lights": 2,
    "dataset.format": "hdf5",
}
VIRTUAL_VIEW = "third_person"
# 由其他字段推导的默认字段（推导顺序即列表顺序）。
DERIVED = ("request_id", "cameras", "render.views")
# 句子必须给出的字段：1a 的意义就在这两项，不设默认。
SENTENCE_ONLY = ("task", "scene")
OPTIONAL_PATHS = (*DEFAULTS, *DERIVED)


def task_slug(task: str) -> str:
    """`put the plate into the open drawer` → `put-the-plate-into-the-open-drawer`（≤64 字符，满足标识规则）。"""
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:64].rstrip("-")
    return slug or "request"


def _get(document: Mapping[str, Any], path: str) -> Any:
    node: Any = document
    for key in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _set_default(document: dict[str, Any], path: str, value: Any, provenance: dict[str, Source]) -> None:
    *parents, leaf = path.split(".")
    node = document
    for key in parents:
        if node.get(key) is None:
            node[key] = {}
        node = node[key]
        if not isinstance(node, dict):
            return  # 句子给了非对象值：保持原样，交给校验器报错
    if node.get(leaf) is None:
        node[leaf] = copy.deepcopy(value)
        provenance[path] = "default"
    else:
        provenance[path] = "sentence"


def _derived(document: Mapping[str, Any], path: str) -> Any:
    if path == "request_id":
        task = document.get("task")
        return task_slug(task) if isinstance(task, str) else None
    if path == "cameras":
        return list(ROBOT_CAMERAS.get(document.get("robot"), ())) or None
    cameras = document.get("cameras")
    return [cameras[0], VIRTUAL_VIEW] if isinstance(cameras, list) and cameras else [VIRTUAL_VIEW]


def fill_defaults(draft: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Source]]:
    """返回 `(补齐后的文档, provenance)`；不改 `draft`，不覆盖句子给出的值（`null` 视为未给出）。

    provenance 覆盖 `SENTENCE_ONLY` 与 `OPTIONAL_PATHS` 中的每个路径，值为 `sentence` 或 `default`。
    """
    document = copy.deepcopy(dict(draft))
    provenance: dict[str, Source] = {path: "sentence" for path in SENTENCE_ONLY}
    for path, value in DEFAULTS.items():
        _set_default(document, path, value, provenance)
    for path in DERIVED:
        value = _derived(document, path)
        if value is None and _get(document, path) is None:
            continue  # 推不出来（如缺 task）：留空交给校验器报错
        _set_default(document, path, value, provenance)
    return document, provenance

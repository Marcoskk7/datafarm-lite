"""场景组装的主机文件加载：机器人包 URDF 描述符与 `prepare` 产物（`tools/scene_assemble.py`、`tools/scene_author.py` 共用）。"""

from __future__ import annotations

import json
from pathlib import Path

from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.store import ArtifactStore
from datafarm.simulation.robot_binding import discover_urdf
from datafarm.simulation.robot_semantics import file_mesh_resolver


def robot_delivery(package: Path, asset: str) -> dict:
    """机器人包 `delivery_manifest.json` 中被选中资产的完整交付记录（含 `layer_sha256`）。"""
    return json.loads((Path(package) / "delivery_manifest.json").read_text(encoding="utf-8"))["assets"][asset]


def robot_layer_files(package: Path, asset: str) -> dict[str, str]:
    """该资产自己的 USD 层 `{member: sha256}`；顶层的 `layer_sha256` 不属于任何资产。"""
    return dict(robot_delivery(package, asset).get("layer_sha256") or {})


def robot_descriptor(package: Path, asset: str) -> tuple[dict, Path]:
    """机器人包 `delivery_manifest.json` 中 `asset` 的 URDF 描述符（robot-descriptor/2）与 URDF 路径。"""
    delivery = robot_delivery(package, asset)
    urdf = Path(package) / delivery["urdf"]
    return discover_urdf(urdf.read_bytes(), mesh_resolver=file_mesh_resolver(urdf)), urdf


def load_prepared(work: Path) -> tuple[ArtifactStore, dict, dict]:
    """`prepare` 的产物：`work/store`、`work/prepared.json` 索引与按 asset_id 的 AssetRecord。"""
    store = ArtifactStore(Path(work) / "store")
    prepared = json.loads((Path(work) / "prepared.json").read_text(encoding="utf-8"))
    records = {k: store.load(ArtifactRef.from_dict(v["record"])) for k, v in prepared["assets"].items()}
    return store, prepared, records

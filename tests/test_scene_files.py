"""Robot package `delivery_manifest.json`: the selected asset's own record, never the top-level document."""
import json
from pathlib import Path

from datafarm.entry.scene_files import robot_delivery, robot_layer_files

LAYERS = {
    "validated_usd/toy/robot.usda": "a" * 64,
    "validated_usd/toy/payloads/Physics/mujoco.usda": "b" * 64,
    "validated_usd/toy/payloads/geometries.usd": "c" * 64,
}


def package(root: Path, **top_level: object) -> Path:
    """AIRBOT 式交付清单：`layer_sha256` 在每个资产自己的记录里。"""
    path = root / "robot-package"
    (path / "toy").mkdir(parents=True)
    (path / "toy" / "toy.urdf").write_text("<robot/>", encoding="utf-8")
    manifest = {"assets": {"toy": {"urdf": "toy/toy.urdf", "usd": "toy/toy.usda", "layer_sha256": LAYERS}},
                **top_level}
    (path / "delivery_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_delivery_record_is_the_selected_asset_not_the_top_level_document(tmp_path):
    record = robot_delivery(package(tmp_path), "toy")
    assert record["usd"] == "toy/toy.usda"
    assert record["layer_sha256"] == LAYERS


def test_layer_files_are_that_assets_own_layers(tmp_path):
    assert robot_layer_files(package(tmp_path), "toy") == LAYERS


def test_top_level_layers_are_ignored(tmp_path):
    other = {"validated_usd/other/x.usda": "d" * 64}
    assert robot_layer_files(package(tmp_path, layer_sha256=other), "toy") == LAYERS

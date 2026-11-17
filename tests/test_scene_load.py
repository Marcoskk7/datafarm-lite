"""Scene loading without a simulator: SceneGraph + `prepared.json` -> the resolved scene `scene_runtime.load_scene`
returns, with `asset_roots` taken from `prepared.json` exactly as `tools/render_episode.py render` does."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from datafarm.adapters.sim import scene_runtime
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.io import sha256_file
from datafarm.kernel.store import ArtifactStore
from datafarm.scene import asset_records, layout
from datafarm.simulation.robot_binding import discover_urdf
from tests.test_scene_layout import DRAWER, SPEC, URDF, fetch, inspection

USD = "model/vnd.usd"
USDA = "model/vnd.usda"
BODIES = {"object.table_a": "kinematic", "object.cabinet_a": "articulation", "object.plate_a": "rigid"}


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def build_work(root: Path) -> tuple[Path, ArtifactStore, dict, dict]:
    """A WORK dir shaped like `scene_assemble.py prepare` + `check` output, with real bytes behind every hash."""
    work = root / "work"
    store = ArtifactStore(work / "store")
    prepared: dict = {"spec_sha256": "0" * 64, "assets": {}, "bundle_files": []}
    shapes = {"object.table_a": ([-0.5, -0.4, -0.7], [0.5, 0.4, 0.05], ()),
              "object.cabinet_a": ([-0.3, -0.3, 0.0], [0.3, 0.3, 0.6], (DRAWER,)),
              "object.plate_a": ([-0.1, -0.1, 0.0], [0.1, 0.1, 0.02], ())}
    records = {}
    for asset_id, body in BODIES.items():
        source = root / "simready-cache" / asset_id
        usd = _write(source / "root.usd", f"#usda 1.0 {asset_id}".encode())
        texture = _write(source / "textures/albedo.png", asset_id.encode())
        wrapper = _write(work / "collision" / f"{asset_id}.usda", f"#usda 1.0 wrapper of {usd}".encode())
        wrapper_ref = store.put_bytes("asset.collision-wrapper", wrapper.read_bytes(), media_type=USDA)
        collision_ref = store.put_json("asset.collision-prep", {"wrapper_sha256": wrapper_ref.sha256})
        lo, hi, joints = shapes[asset_id]
        record = asset_records.build_record(
            asset_id=asset_id, role={"object.table_a": "background", "object.cabinet_a": "receptacle"}.get(
                asset_id, "task_object"), fetch=fetch(asset_id), inspection=inspection(lo, hi, joints=joints),
            usd_ref=store.put_bytes("asset.usd", usd.read_bytes(), media_type=USD),
            inspection_ref=store.put_json("asset.usd-inspection", {"asset": asset_id}),
            collision_ref=collision_ref, collision_bbox=(lo, hi))
        records[asset_id] = record
        prepared["assets"][asset_id] = {
            "record": store.put_json("ir.asset-record", record).to_dict(), "body": body,
            "wrapper": str(wrapper.resolve()), "source_root": str(source.resolve()),
            "fetch_bom_sha256": "1" * 64, "revision": "c" * 40}
        prepared["bundle_files"] += [
            {"asset_id": asset_id, "role": "wrapper", "sha256": wrapper_ref.sha256},
            *({"asset_id": asset_id, "role": "simready", "member": p.relative_to(source).as_posix(),
               "sha256": sha256_file(p)} for p in (usd, texture))]
    package = root / "robot-package"
    urdf = _write(package / "toy/toy.urdf", URDF)
    robot_usd = _write(package / "toy/toy.usda", b"#usda 1.0 toy")
    layer = _write(package / "validated_usd/toy/payload.usda", b"#usda 1.0 toy payload")
    _write(package / "delivery_manifest.json", json.dumps(
        {"assets": {"toy": {"urdf": "toy/toy.urdf", "usd": "toy/toy.usda"}}}).encode())
    descriptor = discover_urdf(URDF)
    robot = asset_records.robot_record(
        asset_id="robot.toy", descriptor=descriptor,
        urdf_ref=store.put_bytes("robot.urdf", urdf.read_bytes(), media_type="application/urdf+xml"),
        usd_ref=store.put_bytes("robot.usd", robot_usd.read_bytes(), media_type=USDA), license="unknown",
        retrieved_at="2026-11-15T00:00:00Z", uri="local:toy",
        base_collision={"bbox_min_m": [-0.1, -0.1, -0.02], "bbox_max_m": [0.1, 0.1, 0.1]})
    records["robot.toy"] = robot
    prepared["assets"]["robot.toy"] = {"record": store.put_json("ir.asset-record", robot).to_dict(), "body": "robot",
                                       "package": str(package.resolve()), "package_asset": "toy"}
    prepared["bundle_files"].append({"asset_id": "robot.toy", "role": "robot_layer",
                                     "member": "validated_usd/toy/payload.usda", "sha256": sha256_file(layer)})

    nodes = layout.resolve_nodes(SPEC, records)
    graph = layout.build_scene_graph(
        spec=SPEC, nodes=nodes, records=records,
        record_refs={k: v["record"] for k, v in prepared["assets"].items()}, descriptor=descriptor,
        asset_bundle=store.put_json("scene.asset-bundle", {"schema": "datafarm.asset-bundle/1",
                                                           "files": prepared["bundle_files"]}).to_dict(),
        ledger=store.put_json("ir.physics-ledger", layout.physics_ledger(records, nodes)).to_dict(),
        runtime_profile=store.put_json("sim.runtime-profile", {"schema": "datafarm.sim-runtime-profile/1",
                                                               "robot_profile": "toy"}).to_dict(),
        reach=layout.reachability(SPEC, nodes, records, descriptor),
        settle={"ran": True, "steps": 240, "max_drift_m": 0.0004, "stable": True},
        penetration={"ran": True, "max_depth_m": 0.0, "ok": True})
    graph_ref = store.put_json("ir.scene-graph", graph).to_dict()
    (work / "prepared.json").write_text(json.dumps(prepared))
    return work, store, prepared, graph_ref


class SceneLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work, self.store, self.prepared, self.graph_ref = build_work(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, prepared=None):
        prepared = self.prepared if prepared is None else prepared
        return scene_runtime.load_scene(self.store, self.graph_ref, prepared,
                                        asset_roots=scene_runtime.asset_roots(prepared))

    def test_asset_roots_come_from_prepared_and_skip_the_robot(self):
        self.assertEqual(scene_runtime.asset_roots(self.prepared),
                         {k: self.prepared["assets"][k]["source_root"] for k in BODIES})

    def test_scene_resolves_from_graph_and_prepared_without_layout_plan(self):
        resolved = self.load()
        graph = resolved["graph"]
        nodes = {n["node_id"]: n for n in graph["nodes"]}
        records = {k: self.store.load(ArtifactRef.from_dict(v["record"])) for k, v in self.prepared["assets"].items()}
        self.assertEqual(resolved["records"], records)
        for node in layout.resolve_nodes(SPEC, records):  # what `plan.json` held is exactly what the graph carries
            self.assertEqual(nodes[node["node_id"]]["pose"], node["pose"])
            self.assertEqual(nodes[node["node_id"]].get("joint_state"), node.get("joint_state"))
        self.assertEqual(nodes["robot.arm"]["pose"], graph["robot"]["base_pose"])
        self.assertEqual(nodes["object.cabinet"]["joint_state"], {"drawer_joint": 0.2})
        self.assertEqual(resolved["specs"], {**{k: {"body": v} for k, v in BODIES.items()},
                                             "robot.toy": {"body": "robot"}})
        self.assertEqual(resolved["wrappers"], {k: self.prepared["assets"][k]["wrapper"] for k in BODIES})
        self.assertEqual(resolved["asset_refs"], [e["record"] for e in graph["assets"]])
        self.assertEqual(resolved["scene_ref"], self.graph_ref)

    def test_changed_simready_bytes_are_rejected(self):
        texture = Path(self.prepared["assets"]["object.plate_a"]["source_root"]) / "textures/albedo.png"
        texture.write_bytes(b"edited in the cache")
        with self.assertRaisesRegex(ValueError, "asset bundle bytes changed"):
            self.load()

    def test_changed_collision_wrapper_is_rejected(self):
        Path(self.prepared["assets"]["object.cabinet_a"]["wrapper"]).write_bytes(b"#usda 1.0 rebuilt")
        with self.assertRaisesRegex(ValueError, "asset bundle bytes changed"):
            self.load()

    def test_changed_robot_urdf_is_rejected(self):
        (Path(self.prepared["assets"]["robot.toy"]["package"]) / "toy/toy.urdf").write_bytes(URDF + b"\n")
        with self.assertRaisesRegex(ValueError, "robot source bytes changed"):
            self.load()

    def test_prepared_record_from_another_prepare_is_rejected(self):
        prepared = deepcopy(self.prepared)
        prepared["assets"]["object.plate_a"]["record"] = prepared["assets"]["object.table_a"]["record"]
        with self.assertRaisesRegex(ValueError, "asset source mismatch"):
            self.load(prepared)

    def test_missing_asset_in_prepared_is_rejected(self):
        prepared = deepcopy(self.prepared)
        del prepared["assets"]["object.plate_a"]
        with self.assertRaisesRegex(ValueError, "prepared assets differ from scene"):
            self.load(prepared)

    def test_prepared_without_source_root_names_the_fix(self):
        prepared = deepcopy(self.prepared)
        del prepared["assets"]["object.table_a"]["source_root"]
        with self.assertRaisesRegex(ValueError, "rerun `scene_assemble.py prepare`"):
            scene_runtime.asset_roots(prepared)


if __name__ == "__main__":
    unittest.main()


class RobotBundleFilesTests(unittest.TestCase):
    """`prepare` 必须把机器人包的每一层写进 asset bundle，否则 `load_scene` 拒绝整个场景（lab30 实跑踩到）。"""

    def test_layers_come_from_the_per_asset_delivery_entry(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("scene_assemble", Path(__file__).resolve().parents[1]
                                                      / "tools" / "scene_assemble.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        # 实际机器人包 delivery_manifest.json 的形状：摘要挂在 assets.<name>.layer_sha256 下，顶层没有。
        delivery = {"release": "r", "assets": {"airbot_single": {
            "usd": "validated_usd/airbot_single/robot.usda", "urdf": "assets/a.urdf",
            "layer_sha256": {"validated_usd/airbot_single/robot.usda": "a" * 64,
                             "validated_usd/airbot_single/payloads/base.usda": "b" * 64}}}}
        files = tool.robot_bundle_files(delivery, "robot.airbot", "airbot_single")
        self.assertEqual(files, [
            {"asset_id": "robot.airbot", "role": "robot_layer",
             "member": "validated_usd/airbot_single/payloads/base.usda", "sha256": "b" * 64},
            {"asset_id": "robot.airbot", "role": "robot_layer",
             "member": "validated_usd/airbot_single/robot.usda", "sha256": "a" * 64}])
        del delivery["assets"]["airbot_single"]["layer_sha256"]
        with self.assertRaises(SystemExit):
            tool.robot_bundle_files(delivery, "robot.airbot", "airbot_single")

"""Resolve A artifacts and local bundles; reuse A construction and initialization."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path

from datafarm.adapters.sim import isaac_scene
from datafarm.ir import asset_record, scene_graph
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.io import sha256_file, read_json
from datafarm.kernel.schema import canonical_json_bytes


def _local(root, member):
    root = Path(root).resolve(strict=True)
    if not isinstance(member, str) or Path(member).is_absolute() or ".." in Path(member).parts:
        raise ValueError("bundle member must be relative")
    path = (root/member).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("bundle member outside root")
    return path


def load_scene(store, scene_ref, prepared, *, asset_roots):
    if scene_ref.get("kind") != "ir.scene-graph":
        raise ValueError("expected A SceneGraph reference")
    for reference in (scene_ref,):
        from datafarm.kernel.schema import validate_ref
        validate_ref(reference, "scene_ref")
    graph = store.load(ArtifactRef.from_dict(scene_ref))
    scene_graph.validate(graph)
    ids = [n["node_id"] for n in graph["nodes"]]
    prims = [isaac_scene.prim_name(n) for n in ids]
    keys = [n.replace(".", "__") for n in ids]
    if len(set(prims)) != len(ids) or len(set(keys)) != len(ids) or any(k in ("ground",) for k in keys):
        raise ValueError("A runtime node/prim mapping collision")
    bundle = store.load(ArtifactRef.from_dict(graph["asset_bundle"]))
    if bundle.get("schema") != "datafarm.asset-bundle/1" or canonical_json_bytes(bundle["files"]) != canonical_json_bytes(prepared["bundle_files"]):
        raise ValueError("prepared bundle differs from immutable A bundle")
    entries = graph["assets"]
    bundle_ids = {i["asset_id"] for i in bundle["files"]}
    if not bundle["files"] or bundle_ids != {e["asset_id"] for e in entries}:
        raise ValueError("bundle coverage incomplete")
    for key in ("runtime_profile", "physics_parameter_ledger"):
        store.load(ArtifactRef.from_dict(graph[key]))
    if set(prepared["assets"]) != {e["asset_id"] for e in entries}:
        raise ValueError("prepared assets differ from scene")
    records, specs, wrappers = {}, {}, {}
    for entry in entries:
        asset = entry["asset_id"]
        record = store.load(ArtifactRef.from_dict(entry["record"]))
        asset_record.validate(record)
        spec = prepared["assets"][asset]
        if record["asset_id"] != asset or spec["record"] != entry["record"]:
            raise ValueError("asset source mismatch")
        if spec["body"] not in ("robot", "kinematic", "rigid", "articulation"):
            raise ValueError("unsupported asset body")
        if (record["role"] == "robot") != (spec["body"] == "robot"):
            raise ValueError("robot body/record mismatch")
        records[asset], specs[asset] = record, {"body": spec["body"]}
        if spec["body"] != "robot":
            wrappers[asset] = str(Path(spec["wrapper"]).resolve(strict=True))
        for item in [i for i in bundle["files"] if i["asset_id"] == asset]:
            if item["role"] == "wrapper":
                path = Path(wrappers[asset])
            elif item["role"] in ("robot_layer", "simready"):
                root = spec["package"] if item["role"] == "robot_layer" else asset_roots[asset]
                path = _local(root, item["member"])
            else:
                raise ValueError("unsupported bundle member role")
            if sha256_file(path) != item["sha256"]:
                raise ValueError("asset bundle bytes changed")
        if spec["body"] == "robot":
            delivery = read_json(Path(spec["package"])/"delivery_manifest.json")["assets"][spec["package_asset"]]
            for file in record["files"]:
                if file["format"] not in ("urdf", "usd"):
                    raise ValueError("unsupported robot source file")
                local = _local(spec["package"], delivery[file["format"]])
                if sha256_file(local) != file["artifact"]["sha256"]:
                    raise ValueError("robot source bytes changed")
        if spec["body"] != "robot":
            wrapper = Path(wrappers[asset])
            matches = [i for i in bundle["files"] if i["asset_id"] == asset and i["role"] == "wrapper"]
            if len(matches) != 1 or sha256_file(wrapper) != matches[0]["sha256"]:
                raise ValueError("unbound collision wrapper")
            collision = record.get("collision", {}).get("artifact")
            if collision is not None:
                report = store.load(ArtifactRef.from_dict(collision))
                if report.get("wrapper_sha256") != matches[0]["sha256"]:
                    raise ValueError("collision wrapper not bound to AssetRecord")
    return {"graph": graph, "records": records, "specs": specs, "wrappers": wrappers,
            "scene_ref": deepcopy(scene_ref), "asset_refs": [deepcopy(e["record"]) for e in entries]}


def scene_config(resolved, robot_cfg, *, drawer_damping):
    graph = resolved["graph"]
    robots = [n for n in graph["nodes"] if n["semantic_tag"] == "robot"]
    if len(robots) != 1:
        raise ValueError("exactly one source-bound robot required")
    return isaac_scene.build_scene_cfg(nodes=[n for n in graph["nodes"] if n not in robots],
        assets=resolved["specs"], wrappers=resolved["wrappers"], robot_cfg=robot_cfg,
        robot_node=robots[0], drawer_damping=drawer_damping)


def initialize(simulation, scene, resolved, *, reset, settle_steps):
    if not callable(reset) or type(settle_steps) is not int or settle_steps < 0:
        raise ValueError("explicit owned reset and settle count required")
    reset()
    nodes = [n for n in resolved["graph"]["nodes"] if n["semantic_tag"] != "robot"]
    isaac_scene.apply_initial_state(simulation, scene, nodes=nodes, assets=resolved["specs"])
    for _ in range(settle_steps):
        scene.write_data_to_sim()
        simulation.step(render=False)
        scene.update(simulation.get_physics_dt())
    return {"reset": "host_owned", "settle_steps": settle_steps, "state_restore": "unverified"}

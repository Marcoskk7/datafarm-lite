"""RenderSpec `datafarm.render-spec/1` 与 RenderManifest `datafarm.render-manifest/1`（docs/ir/render-spec.md）。

RenderSpec 只能改外观：视角、材质槽、灯光。几何、位姿、运动、接触不是它的字段，所以变体在结构上就改不了它们。
RenderManifest 是一次渲染的产物清单；其 `frame_map` 条目与 Episode `payload.frame_map` 同构，由 B 合入 episode。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from datafarm.ir.common import check, identifier, portable, pose, ref, require, vector
from datafarm.ir.scene_graph import CONTROL_HZ
from datafarm.kernel.io import is_finite_number

SCHEMA = "datafarm.render-spec/1"
MANIFEST_SCHEMA = "datafarm.render-manifest/1"
ENGINE = "isaac_rtx"
LIGHT_TYPES = ("dome", "distant", "sphere")
FRAME_STATUS = ("ok", "missing", "error")
PLAYBACK_STATUS = ("verified", "failed", "unverified")
INTRINSIC_KEYS = ("fx", "fy", "cx", "cy", "width", "height")


def _unit(value: Any, label: str) -> None:
    check(is_finite_number(value) and 0.0 <= value <= 1.0, f"{label} must be in [0, 1]")


def intrinsics(value: Any, label: str) -> None:
    check(isinstance(value, Mapping) and all(is_finite_number(value.get(k)) and value[k] > 0 for k in INTRINSIC_KEYS),
          f"{label} must have positive {', '.join(INTRINSIC_KEYS)}")
    check(isinstance(value["width"], int) and isinstance(value["height"], int), f"{label}.width/height must be integers")


def _fps(value: Any, label: str) -> None:
    check(isinstance(value, int) and not isinstance(value, bool) and value > 0 and CONTROL_HZ % value == 0,
          f"{label} must be a positive integer dividing {CONTROL_HZ}")


def _unique(items: Any, key: str, label: str) -> dict[str, Mapping[str, Any]]:
    check(isinstance(items, list), f"{label} must be a list")
    out: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        check(isinstance(item, Mapping), f"{label}[{index}] must be an object")
        name = identifier(item.get(key), f"{label}[{index}].{key}")
        check(name not in out, f"duplicate {key} {name!r}")
        out[name] = item
    return out


def validate(document: Any, scene_graph: Mapping[str, Any] | None = None) -> None:
    """校验 RenderSpec；给出 SceneGraph 时同时核对物理相机与材质槽是否存在于该场景。"""
    check(isinstance(document, Mapping), "render spec must be an object")
    require(document, ("schema", "episode", "scene_graph", "engine", "views", "materials", "lights", "variants",
                       "output"), "render spec")
    check(document["schema"] == SCHEMA, f"render spec schema must be {SCHEMA}")
    check(document["engine"] == ENGINE, f"render spec engine must be {ENGINE!r}")
    portable(document, "render spec")
    ref(document["episode"], "episode")
    ref(document["scene_graph"], "scene_graph")

    cameras = {c["camera_id"]: c for c in scene_graph["cameras"]} if scene_graph is not None else None
    tags = {n["semantic_tag"] for n in scene_graph["nodes"]} if scene_graph is not None else None
    views = _unique(document["views"], "view_id", "views")
    check(bool(views), "views must be non-empty")
    for view_id, view in views.items():
        where = f"views.{view_id}"
        check(view.get("kind") in ("physical", "virtual"), f"{where}.kind must be physical or virtual")
        _fps(view.get("fps"), f"{where}.fps")
        if view["kind"] == "physical":
            camera_id = identifier(view.get("camera_id"), f"{where}.camera_id")
            check("pose" not in view and "intrinsics" not in view,
                  f"{where}: physical views take pose and intrinsics from the SceneGraph camera")
            if cameras is not None:
                check(camera_id in cameras and cameras[camera_id]["kind"] == "physical",
                      f"{where}.camera_id {camera_id!r} is not a physical camera of the scene graph")
                check(cameras[camera_id]["fps"] == view["fps"], f"{where}.fps must equal the camera's fps")
        else:
            check("camera_id" not in view, f"{where}: virtual views have no camera_id")
            pose(view.get("pose"), f"{where}.pose")
            check(view["pose"]["frame_id"] == "world", f"{where}.pose.frame_id must be 'world'")
            intrinsics(view.get("intrinsics"), f"{where}.intrinsics")

    materials = _unique(document["materials"], "material_id", "materials")
    for material_id, material in materials.items():
        where = f"materials.{material_id}"
        identifier(material.get("slot"), f"{where}.slot")
        if tags is not None:
            check(material["slot"] in tags, f"{where}.slot {material['slot']!r} has no node in the scene graph")
        params = material.get("params")
        check(isinstance(params, Mapping), f"{where}.params must be an object")
        require(params, ("base_color", "metallic", "roughness"), f"{where}.params")
        for channel in vector(params["base_color"], 3, f"{where}.params.base_color"):
            _unit(channel, f"{where}.params.base_color")
        _unit(params["metallic"], f"{where}.params.metallic")
        _unit(params["roughness"], f"{where}.params.roughness")

    lights = _unique(document["lights"], "light_id", "lights")
    for light_id, light in lights.items():
        where = f"lights.{light_id}"
        check(light.get("type") in LIGHT_TYPES, f"{where}.type must be one of {list(LIGHT_TYPES)}")
        check(is_finite_number(light.get("intensity")) and light["intensity"] > 0, f"{where}.intensity must be > 0")
        check(is_finite_number(light.get("color_temperature")) and 1000 <= light["color_temperature"] <= 15000,
              f"{where}.color_temperature must be in [1000, 15000] K")
        if light["type"] == "dome":
            check("pose" not in light, f"{where}: a dome light has no pose")
        else:
            pose(light.get("pose"), f"{where}.pose")
            check(light["pose"]["frame_id"] == "world", f"{where}.pose.frame_id must be 'world'")
        if light["type"] == "sphere":
            check(is_finite_number(light.get("radius_m")) and light["radius_m"] > 0, f"{where}.radius_m must be > 0")

    variants = _unique(document["variants"], "variant_id", "variants")
    check(bool(variants), "variants must be non-empty")
    for variant_id, variant in variants.items():
        where = f"variants.{variant_id}"
        check(variant.get("view") in views, f"{where}.view must name a view")
        for key, known in (("material_set", materials), ("light_set", lights)):
            members = variant.get(key)
            check(isinstance(members, list) and all(m in known for m in members) and len(set(members)) == len(members),
                  f"{where}.{key} must list distinct known ids")
        slots = [materials[m]["slot"] for m in variant["material_set"]]
        check(len(set(slots)) == len(slots), f"{where}: two materials override the same slot")
        check(bool(variant["light_set"]), f"{where}.light_set must be non-empty: the scene graph has no lights")

    output = document["output"]
    check(isinstance(output, Mapping) and output.get("format") == "mp4" and output.get("codec") == "h264",
          "output must be {format: 'mp4', codec: 'h264', crf}")
    check(isinstance(output.get("crf"), int) and 0 <= output["crf"] <= 51, "output.crf must be an integer in [0, 51]")


def frame_steps(state_steps: Sequence[int], fps: int) -> list[int]:
    """按 fps 选出要渲染的控制步：从 0 开始每 CONTROL_HZ/fps 步一帧，止于 episode 末步；不插值。"""
    _fps(fps, "fps")
    check(bool(state_steps) and list(state_steps) == list(range(len(state_steps))),
          "state_trace control_step must run 0..N without gaps")
    stride = CONTROL_HZ // fps
    return list(range(0, len(state_steps), stride))


def validate_manifest(document: Any, spec: Mapping[str, Any] | None = None) -> None:
    """校验 RenderManifest；给出 RenderSpec 时核对变体全覆盖、每变体帧序连续且控制步按 fps 递增。"""
    check(isinstance(document, Mapping), "render manifest must be an object")
    require(document, ("schema", "render_spec", "episode", "scene_graph", "engine", "streams", "frame_map",
                       "state_playback"), "render manifest")
    check(document["schema"] == MANIFEST_SCHEMA, f"render manifest schema must be {MANIFEST_SCHEMA}")
    portable(document, "render manifest")
    for key in ("render_spec", "episode", "scene_graph"):
        ref(document[key], key)
    engine = document["engine"]
    check(isinstance(engine, Mapping) and engine.get("name") == ENGINE
          and all(isinstance(engine.get(k), str) and engine[k] for k in ("isaac_sim", "isaac_lab")),
          "engine must be {name: 'isaac_rtx', isaac_sim, isaac_lab}")

    streams = _unique(document["streams"], "variant_id", "streams")
    for variant_id, stream in streams.items():
        where = f"streams.{variant_id}"
        require(stream, ("view_id", "camera_id", "video", "intrinsics", "extrinsics", "frame_count"), where)
        identifier(stream["camera_id"], f"{where}.camera_id")
        for key in ("video", "intrinsics", "extrinsics"):
            ref(stream[key], f"{where}.{key}")
        check(stream["video"]["media_type"] == "video/mp4", f"{where}.video must be video/mp4")
        check(isinstance(stream["frame_count"], int) and stream["frame_count"] > 0, f"{where}.frame_count must be > 0")

    by_variant: dict[str, list[Mapping[str, Any]]] = {v: [] for v in streams}
    check(isinstance(document["frame_map"], list), "frame_map must be a list")
    for index, entry in enumerate(document["frame_map"]):
        where = f"frame_map[{index}]"
        check(isinstance(entry, Mapping), f"{where} must be an object")
        require(entry, ("camera_id", "variant_id", "frame_index", "control_step", "status", "intrinsics",
                        "extrinsics", "image"), where)
        stream = streams.get(entry["variant_id"])
        check(stream is not None, f"{where}.variant_id has no stream")
        check(entry["camera_id"] == stream["camera_id"], f"{where}.camera_id differs from its stream")
        check(entry["status"] in FRAME_STATUS, f"{where}.status must be one of {list(FRAME_STATUS)}")
        check(entry["status"] == "ok" or isinstance(entry.get("reason"), str) and entry["reason"],
              f"{where}: a {entry['status']} frame needs a reason")
        for key in ("intrinsics", "extrinsics", "image"):
            check(dict(entry[key]) == dict(stream[key if key != "image" else "video"]),
                  f"{where}.{key} must reference its stream's artifact")
        by_variant[entry["variant_id"]].append(entry)
    for variant_id, entries in by_variant.items():
        indices = [e["frame_index"] for e in entries]
        check(indices == list(range(streams[variant_id]["frame_count"])),
              f"variant {variant_id}: frame_index must run 0..frame_count-1 once each")
        steps = [e["control_step"] for e in entries]
        check(all(isinstance(s, int) for s in steps) and steps == sorted(set(steps)),
              f"variant {variant_id}: control_step must strictly increase")

    playback = document["state_playback"]
    check(isinstance(playback, Mapping) and playback.get("status") in PLAYBACK_STATUS,
          f"state_playback.status must be one of {list(PLAYBACK_STATUS)}")
    require(playback, ("max_position_error_m", "max_joint_error", "tolerance_m", "notes"), "state_playback")

    if spec is not None:
        variants = {v["variant_id"]: v for v in spec["variants"]}
        views = {v["view_id"]: v for v in spec["views"]}
        check(set(streams) == set(variants), "manifest streams must cover exactly the spec's variants")
        for variant_id, stream in streams.items():
            view = views[variants[variant_id]["view"]]
            check(stream["view_id"] == view["view_id"], f"stream {variant_id}: view differs from the spec")
            stride = CONTROL_HZ // view["fps"]
            steps = [e["control_step"] for e in by_variant[variant_id]]
            check(steps == [k * stride for k in range(len(steps))],
                  f"stream {variant_id}: control_step must be 0, {stride}, {2 * stride}, ... for {view['fps']} fps")

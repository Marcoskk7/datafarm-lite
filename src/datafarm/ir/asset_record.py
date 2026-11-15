"""AssetRecord — `datafarm.asset-record/1`（docs/ir/asset-record.md）。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from datafarm.grounding.quantities import GroundingError, validate_quantities
from datafarm.ir.common import IRError, check, identifier, portable, ref, require, vector
from datafarm.kernel.io import is_finite_number
from datafarm.kernel.schema import validate_ref

SCHEMA = "datafarm.asset-record/1"
ROLES = ("robot", "task_object", "receptacle", "background", "camera_rig")
SOURCE_KINDS = ("simready", "partnet_mobility", "usdcraft", "local")
FILE_FORMATS = ("usd", "urdf", "mjcf", "glb", "obj")
COLLISION_KINDS = ("mesh", "convex_hull", "convex_decomposition", "primitive")
JOINT_TYPES = ("revolute", "prismatic", "fixed")
REQUIRED = ("schema", "asset_id", "role", "source", "files", "bbox_m", "collision", "joints", "frames",
            "physical_quantities")


def validate(document: Any) -> dict[str, int]:
    """校验一份 AssetRecord；返回物理量账本各 label 的数量。"""
    check(isinstance(document, Mapping), "asset record must be an object")
    require(document, REQUIRED, "asset record")
    check(document["schema"] == SCHEMA, f"asset record schema must be {SCHEMA}")
    label = identifier(document["asset_id"], "asset_id")
    check(document["role"] in ROLES, f"{label}: role must be one of {list(ROLES)}")
    portable(document, label)

    source = document["source"]
    check(isinstance(source, Mapping), f"{label}: source must be an object")
    require(source, ("kind", "uri", "license", "retrieved_at"), f"{label}.source")
    check(source["kind"] in SOURCE_KINDS, f"{label}.source.kind must be one of {list(SOURCE_KINDS)}")
    check(isinstance(source["uri"], str) and bool(source["uri"]), f"{label}.source.uri must be non-empty")
    check(isinstance(source["license"], str) and bool(source["license"]), f"{label}.source.license must be non-empty")
    check(source["license"] != "unknown" or bool(document.get("notes")),
          f"{label}: license 'unknown' must be explained in notes")
    check(isinstance(source["retrieved_at"], str) and source["retrieved_at"].endswith("Z"),
          f"{label}.source.retrieved_at must be UTC ISO8601 ending in Z")

    files = document["files"]
    check(isinstance(files, list) and bool(files), f"{label}: files must be a non-empty list")
    for index, item in enumerate(files):
        check(isinstance(item, Mapping), f"{label}.files[{index}] must be an object")
        ref(item.get("artifact"), f"{label}.files[{index}].artifact")
        check(item.get("format") in FILE_FORMATS, f"{label}.files[{index}].format must be one of {list(FILE_FORMATS)}")

    bbox = vector(document["bbox_m"], 3, f"{label}.bbox_m")
    check(all(x > 0 for x in bbox), f"{label}.bbox_m must be positive")

    collision = document["collision"]
    check(isinstance(collision, Mapping) and collision.get("kind") in COLLISION_KINDS,
          f"{label}.collision.kind must be one of {list(COLLISION_KINDS)}")
    if "artifact" in collision:
        ref(collision["artifact"], f"{label}.collision.artifact")

    joints = document["joints"]
    check(isinstance(joints, list), f"{label}.joints must be a list")
    names = set()
    for index, joint in enumerate(joints):
        where = f"{label}.joints[{index}]"
        check(isinstance(joint, Mapping), f"{where} must be an object")
        require(joint, ("name", "type", "axis", "limits", "parent", "child"), where)
        check(isinstance(joint["name"], str) and bool(joint["name"]), f"{where}.name must be non-empty")
        check(joint["name"] not in names, f"{label}: duplicate joint {joint['name']!r}")
        names.add(joint["name"])
        check(joint["type"] in JOINT_TYPES, f"{where}.type must be one of {list(JOINT_TYPES)}")
        axis = vector(joint["axis"], 3, f"{where}.axis")
        check(abs(sum(x * x for x in axis) - 1.0) <= 1e-3, f"{where}.axis must be a unit vector")
        limits = joint["limits"]
        check(isinstance(limits, Mapping) and is_finite_number(limits.get("lower"))
              and is_finite_number(limits.get("upper")) and limits["lower"] <= limits["upper"],
              f"{where}.limits must be {{lower <= upper}}")
        for end in ("parent", "child"):
            check(isinstance(joint[end], str) and bool(joint[end]), f"{where}.{end} must be a link name")
        if joint.get("mimic") is not None:
            mimic = joint["mimic"]
            check(isinstance(mimic, Mapping) and isinstance(mimic.get("joint"), str)
                  and is_finite_number(mimic.get("multiplier")) and is_finite_number(mimic.get("offset", 0.0)),
                  f"{where}.mimic must be {{joint, multiplier, offset}}")
    if document["role"] == "receptacle":
        check(any(j["type"] in ("prismatic", "revolute") for j in joints),
              f"{label}: a receptacle needs a prismatic or revolute joint; jointless cabinets are background")

    frames = document["frames"]
    check(isinstance(frames, Mapping) and frames.get("up_axis") == "Z", f"{label}.frames.up_axis must be Z")
    identifier(frames.get("root_frame_id"), f"{label}.frames.root_frame_id")
    check(isinstance(frames.get("link_frame_ids", {}), Mapping), f"{label}.frames.link_frame_ids must be an object")

    try:
        return validate_quantities(document["physical_quantities"],
                                   bind=lambda value: validate_ref(value, f"{label} quantity evidence"))
    except GroundingError as exc:
        raise IRError(f"{label}.physical_quantities: {exc}") from exc

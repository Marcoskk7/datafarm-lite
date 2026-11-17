"""Versioned nested public-task allowlists, not a dump of a private host or Judge."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from datafarm.kernel.identifiers import logical_id
from datafarm.kernel.io import is_sha256
from datafarm.kernel.schema import canonical_json_bytes
from datafarm.task.planning import CAPABILITY_SCHEMA, validate_capabilities
from datafarm.task.request_profile import object_fields, parameter, requirements, string_list, text

PROFILE = "datafarm.cap.public-task/1"


def validate_public_task(value: Any) -> dict[str, Any]:
    object_fields(value, {"profile", "task_id", "task_revision", "request_id", "request_sha256", "scene_id",
                          "name", "instruction", "roles", "goals", "constraints", "forbidden_shortcuts",
                          "parameters", "api", "program_contract", "observations", "control_budgets",
                          "frame_ids", "joint_facts"}, label="public task")
    if value["profile"] != PROFILE:
        raise ValueError("unsupported public task profile")
    for key in ("task_id", "task_revision", "request_id", "scene_id"):
        logical_id(value[key], label=key)
    if not is_sha256(value["request_sha256"]):
        raise ValueError("public request digest must be a complete SHA-256")
    text(value["name"], "display name", 256)
    text(value["instruction"], "public instruction")
    frames = string_list(value["frame_ids"], "public frames", identifiers=True)
    if not isinstance(value["roles"], list) or not 1 <= len(value["roles"]) <= 128:
        raise ValueError("public roles must be a bounded list")
    names = set()
    for role in value["roles"]:
        object_fields(role, {"role", "node_id", "frame_id"}, label="public role")
        for key in role:
            logical_id(role[key], label=key)
        if role["role"] in names or role["frame_id"] not in frames:
            raise ValueError("duplicate role or unknown public frame")
        names.add(role["role"])
    requirement_ids = set()
    for key in ("goals", "constraints", "forbidden_shortcuts"):
        requirements(value[key], key)
        ids = {item["id"] for item in value[key]}
        if ids & requirement_ids:
            raise ValueError("duplicate public requirement ID across sections")
        requirement_ids.update(ids)
    if not isinstance(value["parameters"], list) or len(value["parameters"]) > 256:
        raise ValueError("public parameters must be a bounded list")
    for item in value["parameters"]:
        # The public projection contains source requirement IDs, never private evidence refs.
        if "evidence_ref" in item:
            raise ValueError("public parameters cannot expose private evidence references")
        parameter(item, requirement_ids)
    if len({item["id"] for item in value["parameters"]}) != len(value["parameters"]):
        raise ValueError("duplicate public parameter")
    scene_ref = {"kind": "ir.scene-graph", "sha256": "0" * 64, "media_type": "application/json"}
    validate_capabilities({"schema": CAPABILITY_SCHEMA, "scene_ref": scene_ref,
                           **{key: value[key] for key in ("api", "program_contract", "observations", "control_budgets")}},
                          scene_ref)
    if not value["api"]:
        raise ValueError("public executable task needs an actual API")
    if any(o["frame_id"] not in frames for o in value["observations"]):
        raise ValueError("public observation has an unknown frame")
    joints = value["joint_facts"]
    if not isinstance(joints, list) or len(joints) > 1024:
        raise ValueError("public joints must be a bounded list")
    seen = set()
    from datafarm.kernel.io import is_finite_number

    for joint in joints:
        object_fields(joint, {"node_id", "name", "type", "unit", "limits"}, label="public joint")
        logical_id(joint["node_id"], label="joint node")
        text(joint["name"], "native joint name", 256)
        key = (joint["node_id"], joint["name"])
        limits = object_fields(joint["limits"], {"lower", "upper"}, label="joint limits")
        if (key in seen or joint["type"] not in ("prismatic", "revolute")
                or joint["unit"] != ("m" if joint["type"] == "prismatic" else "rad")
                or not all(is_finite_number(v) for v in limits.values()) or limits["lower"] > limits["upper"]):
            raise ValueError("invalid public joint facts")
        seen.add(key)
    forbidden = {"judge", "judge_source", "judge_code", "hidden_seeds", "heldout", "evaluation_seeds",
                 "image_inputs", "images", "image_url", "video", "frame_map", "provider_sessions"}
    def check(item: Any) -> None:
        if isinstance(item, Mapping):
            if any(key.lower().replace("-", "_") in forbidden for key in item):
                raise ValueError("private evaluation or visual fields in public task")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(value)
    canonical_json_bytes(value)
    return deepcopy(dict(value))


def project_public_task(task_payload: Mapping[str, Any], entry: Mapping[str, Any],
                        request: Mapping[str, Any], request_ref: Mapping[str, Any],
                        member: Mapping[str, Any]) -> dict[str, Any]:
    from datafarm.task.request_profile import validate_request, validate_task_payload

    request = validate_request(request)
    from datafarm.task.request_profile import reference
    from datafarm.kernel.io import sha256_json

    reference(request_ref, "normalized request", kind="task-request")
    if request_ref["sha256"] != sha256_json(request):
        raise ValueError("public normalized request digest differs from its data")
    payload = validate_task_payload(task_payload)
    if (entry["status"] != "planned" or entry["task_id"] != payload["task_id"]
            or entry["task_revision"] != payload["task_revision"]
            or payload["request_id"] != request["request_id"]
            or payload["scene_id"] != member["scene_id"] or entry["scene_ref"] != member["scene_ref"]):
        raise ValueError("public task requires matching planned task/request/scene bindings")
    capability = validate_capabilities(member["capabilities"], member["scene_ref"])
    known_nodes = {n["node_id"] for n in member["nodes"]}
    if any(r["node_id"] not in known_nodes or r["frame_id"] not in member["frame_ids"] for r in payload["roles"]):
        raise ValueError("public role is not bound to an A scene entity")
    ids = set(payload["requirement_ids"])
    expected_parameters = [p for p in request.get("parameters", []) if set(p["requirement_ids"]) <= ids]
    expected_predicates = [{"id": "request.demand", "text": request["demand"]},
        *[i for i in request.get("goals", []) if i["id"] in ids],
        *request.get("constraints", []), *request.get("variation", [])]
    if (payload["demand"] != request["demand"] or payload["parameters"] != expected_parameters
            or payload["predicates"] != expected_predicates
            or payload["forbidden_shortcuts"] != request.get("forbidden_shortcuts", [])):
        raise ValueError("task semantics or thresholds differ from the accepted request")
    available_obs = {o["id"] for o in capability["observations"] if o["availability"] == "available"}
    if (not set(entry["needed_apis"]) <= set(capability["api"])
            or not set(entry["needed_observations"]) <= available_obs):
        raise ValueError("planned entry requires an unavailable capability")
    return validate_public_task({"profile": PROFILE, "task_id": payload["task_id"],
        "task_revision": payload["task_revision"], "request_id": request["request_id"],
        "request_sha256": request_ref["sha256"], "scene_id": member["scene_id"], "name": entry["name"],
        "instruction": request["demand"], "roles": payload["roles"],
        "goals": [{"id": "request.demand", "text": request["demand"]},
                  *[i for i in request.get("goals", []) if i["id"] in ids]],
        "constraints": [*request.get("constraints", []), *request.get("variation", [])],
        "forbidden_shortcuts": request.get("forbidden_shortcuts", []),
        "parameters": [{k: deepcopy(v) for k, v in p.items() if k != "evidence_ref"} for p in payload["parameters"]],
        **{key: capability[key] for key in ("api", "program_contract", "observations", "control_budgets")},
        "frame_ids": member["frame_ids"], "joint_facts": member["joint_facts"]})

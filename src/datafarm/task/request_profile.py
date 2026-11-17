"""Strict B request/task profiles; no stage-graph execution or inferred authorization."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from datafarm.kernel.identifiers import logical_id
from datafarm.kernel.io import is_finite_number
from datafarm.kernel.schema import canonical_json_bytes, validate_ref

REQUEST_SCHEMA = "datafarm.task-request/1"
TASK_PROFILE = "request-task/1"
PARAMETER_PURPOSES = {"target", "control_convergence", "safety", "task_success", "replay", "cohort"}


def object_fields(value: Any, required: set[str], optional: set[str] | None = None,
                  *, label: str = "object") -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not required <= set(value) or set(value) - required - (optional or set()):
        raise ValueError(f"{label} has missing or unknown fields")
    return value


def text(value: Any, label: str, limit: int = 12000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be bounded nonempty text")
    return value


def string_list(value: Any, label: str, *, identifiers: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError(f"{label} must be a bounded list")
    for item in value:
        logical_id(item, label=label) if identifiers else text(item, label)
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicate values")
    return value


def reference(value: Any, label: str, *, kind: str | None = None) -> None:
    object_fields(value, {"kind", "sha256", "media_type"}, label=label)
    validate_ref(value, label)
    if kind is not None and value["kind"] != kind:
        raise ValueError(f"{label} must reference {kind}")


def requirements(value: Any, label: str) -> None:
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError(f"{label} must be a bounded list")
    ids = set()
    for item in value:
        object_fields(item, {"id", "text"}, label=label)
        logical_id(item["id"], label=label)
        text(item["text"], label)
        if item["id"] in ids:
            raise ValueError(f"duplicate requirement in {label}")
        ids.add(item["id"])


def parameter(value: Any, requirement_ids: set[str]) -> None:
    object_fields(value, {"id", "value", "unit", "purpose", "origin", "basis", "requirement_ids"},
                  {"evidence_ref"}, label="parameter")
    logical_id(value["id"], label="parameter.id")
    if not is_finite_number(value["value"]):
        raise ValueError("parameter value must be finite numeric data")
    text(value["unit"], "parameter.unit", 80)
    text(value["basis"], "parameter.basis")
    if value["purpose"] not in PARAMETER_PURPOSES or value["origin"] != "provided":
        raise ValueError("request parameters need an explicit purpose and provided origin; derivation needs review")
    ids = string_list(value["requirement_ids"], "parameter requirements", identifiers=True)
    if not ids or not set(ids) <= requirement_ids:
        raise ValueError("parameter has missing or unknown source requirements")
    if "evidence_ref" in value:
        reference(value["evidence_ref"], "parameter evidence")


def validate_request(value: Any) -> dict[str, Any]:
    object_fields(value, {"schema", "request_id", "demand", "original_request_ref"},
                  {"goals", "constraints", "forbidden_shortcuts", "variation", "parameters",
                   "task_count", "episode_count"}, label="request")
    if value["schema"] != REQUEST_SCHEMA:
        raise ValueError("unsupported request schema; use an explicit normalization adapter")
    logical_id(value["request_id"], label="request_id")
    text(value["demand"], "demand")
    reference(value["original_request_ref"], "original request", kind="task-request-source")
    all_ids = {"request.demand"}
    for name in ("goals", "constraints", "forbidden_shortcuts", "variation"):
        items = value.get(name, [])
        requirements(items, name)
        for item in items:
            if item["id"] in all_ids:
                raise ValueError("requirement IDs must be unique across the request")
            all_ids.add(item["id"])
    parameters = value.get("parameters", [])
    if not isinstance(parameters, list) or len(parameters) > 256:
        raise ValueError("parameters must be a bounded list")
    for item in parameters:
        parameter(item, all_ids)
    if len({p["id"] for p in parameters}) != len(parameters):
        raise ValueError("duplicate parameter ID")
    for name in ("task_count", "episode_count"):
        if name in value and (type(value[name]) is not int or not 1 <= value[name] <= 100000):
            raise ValueError(f"{name} must be a positive bounded integer")
    canonical_json_bytes(value)
    return deepcopy(dict(value))


def validate_task_payload(value: Any) -> dict[str, Any]:
    object_fields(value, {"profile", "task_id", "task_revision", "request_id", "demand", "roles",
                          "stage_graph", "predicates", "forbidden_shortcuts", "skill_evidence",
                          "requirement_ids", "parameters", "scene_id"}, label="request task payload")
    if value["profile"] != TASK_PROFILE or value["stage_graph"] != {"stages": [], "sync": []}:
        raise ValueError("request-task/1 has no prescribed stage graph; never convert stage-graph/1 implicitly")
    for key in ("task_id", "task_revision", "request_id", "scene_id"):
        logical_id(value[key], label=key)
    text(value["demand"], "task demand")
    string_list(value["requirement_ids"], "task requirements", identifiers=True)
    if not isinstance(value["roles"], list) or not 1 <= len(value["roles"]) <= 128:
        raise ValueError("task needs explicit roles")
    role_ids = set()
    for role in value["roles"]:
        object_fields(role, {"role", "node_id", "frame_id"}, label="role")
        for field in role:
            logical_id(role[field], label=field)
        if role["role"] in role_ids:
            raise ValueError("duplicate task role")
        role_ids.add(role["role"])
    if not isinstance(value["parameters"], list) or len(value["parameters"]) > 256:
        raise ValueError("task parameters must be a bounded list")
    requirements(value["predicates"], "predicates")
    requirements(value["forbidden_shortcuts"], "forbidden shortcuts")
    if value["skill_evidence"] != []:
        raise ValueError("planner cannot fabricate skill evidence")
    for item in value["parameters"]:
        parameter(item, set(value["requirement_ids"]))
    if len({p["id"] for p in value["parameters"]}) != len(value["parameters"]):
        raise ValueError("duplicate task parameter ID")
    canonical_json_bytes(value)
    return deepcopy(dict(value))

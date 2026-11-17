"""Explicit measured Episode projection; structured paths keep dotted node IDs intact."""
from copy import deepcopy
import json
from datafarm.expert.episode import validate_episode as validate_measured
from datafarm.kernel.schema import canonical_json_bytes

PROFILE = "judge-episode/2"


def evidence_path(parts):
    if not isinstance(parts, list) or not parts or any(not isinstance(v, str) or not v for v in parts):
        raise ValueError("nonempty structured path segments required")
    return json.dumps(parts, separators=(",", ":"))


def path_exists(value, encoded):
    parts = json.loads(encoded)
    if not isinstance(parts, list) or not parts or not all(isinstance(v, str) and v for v in parts):
        return False
    def visit(obj, segments):
        if not segments:
            return obj is not None
        if isinstance(obj, list):
            return bool(obj) and all(visit(v, segments) for v in obj)
        return isinstance(obj, dict) and segments[0] in obj and visit(obj[segments[0]], segments[1:])
    return visit(value, parts)


def project_episode(payload):
    payload = validate_measured(payload)
    setup = payload["setup"]
    value = {"schema_version": PROFILE, "task_id": setup["task_id"], "task_revision": setup["task_revision"],
             "scene_ref": deepcopy(setup["scene_ref"]), "units": {"length":"m", "angle":"rad", "time":"s"},
             "frame_id":"world", "control_dt_s": setup["control_dt_s"], "states": deepcopy(payload["state_trace"]),
             "actions": deepcopy(payload["actions"]), "contacts": deepcopy(payload["contacts"]),
             "events": deepcopy(payload["events"]), "termination": {**deepcopy(payload["termination"]),
                 "reason": payload["termination"]["status"]}, "node_ids": deepcopy(setup["node_ids"])}
    canonical_json_bytes(value)
    return value


def validate_input(value, contract):
    if value.get("schema_version") != PROFILE or contract.get("schema_version") != "task-contract/2":
        raise ValueError("explicit Judge Episode/contract v2 required")
    original = contract["source_binding"]
    if any(value.get(k) != original[k] for k in ("task_id", "task_revision", "scene_ref", "node_ids")):
        raise ValueError("Judge Episode source binding differs")
    if value["units"] != {"length":"m", "angle":"rad", "time":"s"} or value["frame_id"] != "world":
        raise ValueError("Judge Episode units/frame differ")
    from datafarm.expert.episode import validate_state
    states, actions = value["states"], value["actions"]
    if not states or len(states) != len(actions)+1 or not value["termination"]["trace_complete"]:
        raise ValueError("complete measured trajectory required")
    for i, state in enumerate(states):
        validate_state(state, value["node_ids"], i, value["control_dt_s"])
    if [a["control_step"] for a in actions] != list(range(len(actions))):
        raise ValueError("action clock differs")
    if value["contacts"] != [s["contacts"] for s in states]:
        raise ValueError("contact source differs")
    for condition in contract["conditions"]:
        for field in condition["evidence_fields"]:
            if not path_exists(value, field):
                raise ValueError("required full-trajectory evidence unavailable")
    canonical_json_bytes(value)
    return deepcopy(value)

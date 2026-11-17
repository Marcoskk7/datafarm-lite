"""Explicit measured Episode projection; structured paths keep dotted node IDs intact."""
from copy import deepcopy
import json
from datafarm.expert.episode import validate_episode as validate_measured
from datafarm.kernel.schema import canonical_json_bytes

PROFILE = "judge-episode/1"


def evidence_path(parts):
    if not isinstance(parts, list) or not parts or any(not isinstance(v, str) or not v for v in parts):
        raise ValueError("nonempty structured path segments required")
    return json.dumps(parts, separators=(",", ":"))


def path_exists(value, encoded):
    try:
        parts = json.loads(encoded)
    except (TypeError, ValueError):
        return False
    if not isinstance(parts, list) or not parts or not all(isinstance(v, str) and v for v in parts):
        return False
    def visit(obj, segments):
        if not segments:
            return obj is not None
        if isinstance(obj, list):
            return bool(obj) and all(visit(v, segments) for v in obj)
        if isinstance(obj, dict) and (obj.get("availability") == "unavailable" or
                segments[0] == "rows" and obj.get("coverage") != "complete"):
            return False
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
    if value.get("schema_version") != PROFILE or contract.get("schema_version") != "task-contract/1":
        raise ValueError("current Judge Episode/contract /1 required")
    validate_observation_spec(contract["observations"])
    if contract["source_binding"] != contract["observations"]["source_binding"]:
        raise ValueError("contract observation source differs")
    original = contract["source_binding"]
    if any(value.get(k) != original[k] for k in ("task_id", "task_revision", "scene_ref", "node_ids")):
        raise ValueError("Judge Episode source binding differs")
    if value["units"] != {"length":"m", "angle":"rad", "time":"s"} or value["frame_id"] != "world":
        raise ValueError("Judge Episode units/frame differ")
    from datafarm.expert.episode import validate_state
    dt = value["control_dt_s"]
    import math
    if type(dt) not in (int, float) or not math.isfinite(dt) or dt <= 0:
        raise ValueError("positive finite physics dt required")
    nodes = value["node_ids"]
    from datafarm.kernel.identifiers import logical_id
    if not isinstance(nodes, list) or not nodes or len(nodes) != len(set(nodes)):
        raise ValueError("complete unique scene nodes required")
    for node in nodes:
        logical_id(node)
    states, actions = value["states"], value["actions"]
    if not isinstance(states, list) or not isinstance(actions, list):
        raise ValueError("states/actions must be arrays")
    terminal = value["termination"]
    status = terminal.get("status")
    if status not in ("completed", "failed", "aborted", "error", "timeout") or terminal.get("reason") != status:
        raise ValueError("execution status/reason differs")
    complete = terminal.get("trace_complete")
    if type(complete) is not bool or not states:
        raise ValueError("explicit trace completeness and initial state required")
    if complete:
        if len(states) != len(actions)+1:
            raise ValueError("expected N inputs and N+1 states")
    elif status == "completed" or len(states) not in (len(actions), len(actions)+1):
        raise ValueError("invalid partial execution trace")
    for i, state in enumerate(states):
        validate_state(state, value["node_ids"], i, value["control_dt_s"])
    if any(type(a.get("control_step")) is not int or a["control_step"] != i for i, a in enumerate(actions)):
        raise ValueError("action clock differs")
    coverage = {n: [(j["name"], j["type"]) for j in states[0]["entities"][n]["joints"]] for n in nodes}
    links = {n: set(states[0]["entities"][n]["links"]) for n in nodes}
    for state in states:
        if coverage != {n:[(j["name"], j["type"]) for j in state["entities"][n]["joints"]] for n in nodes} or links != {n:set(state["entities"][n]["links"]) for n in nodes}:
            raise ValueError("joint/link coverage differs across trajectory")
    for action in actions:
        if action.get("application") != "actuator_readback":
            raise ValueError("actual applied actuator readback required")
        for key in ("requested_joint_targets", "applied_joint_targets"):
            targets = action.get(key)
            if not isinstance(targets, dict) or any(not isinstance(n,str) or not n or type(v) not in (int,float) or not math.isfinite(v) for n,v in targets.items()):
                raise ValueError("finite named actuator targets required")
    if value["contacts"] != [s["contacts"] for s in states]:
        raise ValueError("contact source differs")
    for i, sample in enumerate(value["contacts"]):
        if not isinstance(sample, dict) or type(sample.get("control_step")) is not int or sample["control_step"] != i or sample.get("sampling") != "cycle_end_snapshot":
            raise ValueError("contact clock/sampling differs")
        if sample.get("availability") not in ("available", "unavailable") or sample.get("coverage") not in ("unknown", "complete", "truncated"):
            raise ValueError("contact availability/coverage missing")
        if sample["availability"] == "unavailable" and sample.get("rows") is not None:
            raise ValueError("unavailable contact is not empty contact")
    # An interrupted execution is classified by the host, never scored as failure.
    # Its existing measurements and source must still be valid; absent tail data is
    # not synthesized to make a completed Episode.
    for condition in contract["conditions"] if status == "completed" else []:
        for field in condition["evidence_fields"]:
            if field not in contract["observations"]["available_fields"]:
                raise ValueError("undeclared measurement evidence")
            if not path_exists(value, field):
                raise ValueError("required full-trajectory evidence unavailable")
    canonical_json_bytes(value)
    return deepcopy(value)


def validate_observation_spec(value):
    """A single current /1 with required full-scene source and public semantics."""
    from datafarm.task.request_profile import object_fields
    from datafarm.kernel.identifiers import logical_id
    from datafarm.kernel.schema import validate_ref
    from datafarm.cap.public_task import validate_public_task
    object_fields(value, {"profile","source_binding","public_task","available_fields","capabilities"}, label="Judge observations")
    if value["profile"] != PROFILE:
        raise ValueError("current Judge observations /1 required")
    binding = value["source_binding"]
    object_fields(binding, {"node_ids","task_id","task_revision","task_ref","scene_ref","request_ref","original_request_ref"}, label="Judge source binding")
    for k in ("task_id","task_revision"):
        logical_id(binding[k])
    for k, kind in (("task_ref","task-ir"),("scene_ref","ir.scene-graph"),("request_ref","task-request"),("original_request_ref","task-request-source")):
        validate_ref(binding[k], k)
        if binding[k]["kind"] != kind:
            raise ValueError("wrong source reference kind")
    if not isinstance(binding["node_ids"],list) or not binding["node_ids"] or len(binding["node_ids"])!=len(set(binding["node_ids"])):
        raise ValueError("unique source node IDs required")
    for n in binding["node_ids"]:
        logical_id(n)
    public = validate_public_task(value["public_task"])
    if public["task_id"] != binding["task_id"] or public["task_revision"] != binding["task_revision"] or public["request_sha256"] != binding["request_ref"]["sha256"]:
        raise ValueError("public task source differs")
    for key in ("available_fields","capabilities"):
        if not isinstance(value[key],list) or any(not isinstance(v,str) or not v for v in value[key]) or len(set(value[key]))!=len(value[key]):
            raise ValueError("unique observation fields/capabilities required")
    actual_caps = {o["id"] for o in public["observations"] if o["availability"] == "available"}
    if set(value["capabilities"]) != actual_caps:
        raise ValueError("capability claims differ from public observations")
    for field in value["available_fields"]:
        try:
            parts = json.loads(field)
        except (TypeError, ValueError):
            raise ValueError("structured evidence path required") from None
        evidence_path(parts)
        if parts[0] not in ("states","contacts"):
            raise ValueError("measurement evidence only; targets/termination are not score authority")
    canonical_json_bytes(value)
    return deepcopy(value)

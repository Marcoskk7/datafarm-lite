"""Request-bound task planning from validated A scene facts and host capabilities."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from datafarm.ir import asset_record, scene_graph
from datafarm.kernel.artifacts import ArtifactDocument, ArtifactRef, IRKind
from datafarm.kernel.identifiers import storage_key
from datafarm.kernel.io import sha256_json
from datafarm.kernel.schema import IDENTIFIER_PATTERN, canonical_json_bytes
from datafarm.kernel.store import ArtifactStore
from datafarm.task.request_profile import (
    logical_id, object_fields, reference, string_list, text, validate_request, validate_task_payload,
)

CONTEXT_SCHEMA = "datafarm.task-plan-context/1"
TABLE_SCHEMA = "datafarm.scene-task-table/1"
CAPABILITY_SCHEMA = "datafarm.task-capabilities/1"
PROPOSAL_SCHEMA = "datafarm.task-plan-proposal/1"


def validate_capabilities(value: Any, scene_ref: Mapping[str, Any]) -> dict[str, Any]:
    object_fields(value, {"schema", "scene_ref", "api", "program_contract", "observations", "control_budgets"},
                  label="capabilities")
    if value["schema"] != CAPABILITY_SCHEMA or value["scene_ref"] != scene_ref:
        raise ValueError("capabilities must bind the exact executing SceneGraph reference")
    reference(value["scene_ref"], "capability scene", kind="ir.scene-graph")
    api = value["api"]
    if not isinstance(api, Mapping) or len(api) > 128:
        raise ValueError("api must be a bounded mapping")
    for key, detail in api.items():
        text(key, "API key", 160)
        text(detail, "API description", 4000)
    contract = value["program_contract"]
    object_fields(contract, {"entry_point", "signature", "return", "allowed_imports", "action_semantics",
                             "observation_mode"}, label="program contract")
    for key in ("entry_point", "signature", "return", "action_semantics"):
        text(contract[key], key, 4000)
    string_list(contract["allowed_imports"], "allowed imports")
    if contract["observation_mode"] != "privileged_state":
        raise ValueError("this profile requires explicit privileged_state observation")
    observations = value["observations"]
    if not isinstance(observations, list) or len(observations) > 256:
        raise ValueError("observations must be a bounded list")
    ids = set()
    for observation in observations:
        object_fields(observation, {"id", "availability", "unit", "frame_id"}, {"reason"}, label="observation")
        logical_id(observation["id"], label="observation ID")
        logical_id(observation["frame_id"], label="observation frame")
        text(observation["unit"], "observation unit", 80)
        if observation["id"] in ids or observation["availability"] not in ("available", "unavailable"):
            raise ValueError("duplicate or invalid observation")
        ids.add(observation["id"])
        if observation["availability"] == "unavailable":
            text(observation.get("reason"), "unavailable observation reason")
    budgets = value["control_budgets"]
    object_fields(budgets, {"max_actions", "max_steps", "max_wall_s"}, label="control budgets")
    from datafarm.kernel.io import is_finite_number

    if (any(type(budgets[k]) is not int or budgets[k] <= 0 for k in ("max_actions", "max_steps"))
            or not is_finite_number(budgets["max_wall_s"]) or budgets["max_wall_s"] <= 0):
        raise ValueError("control budgets must be positive")
    forbidden = {"judge", "judge_code", "judge_source", "hidden_seeds", "heldout", "evaluation_seeds",
                 "image_inputs", "images", "image_url", "video", "frame_map", "provider_sessions"}
    if any(key.lower().replace("-", "_") in forbidden for key in api):
        raise ValueError("runtime API mapping contains a private/unsupported capability")
    canonical_json_bytes(value)
    return deepcopy(dict(value))


def load_scene_group(store: ArtifactStore, scene_refs: list[Mapping[str, Any]],
                     capabilities: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(scene_refs, list) or not 1 <= len(scene_refs) <= 64 or len(scene_refs) != len(capabilities):
        raise ValueError("scene group needs explicit SceneGraph refs and one capability record per member")
    members, ids = [], set()
    for scene_ref, capability in zip(scene_refs, capabilities):
        reference(scene_ref, "scene", kind="ir.scene-graph")
        graph = store.load(ArtifactRef.from_dict(scene_ref))
        scene_graph.validate(graph)
        if graph["scene_id"] in ids:
            raise ValueError("duplicate scene_id in scene group")
        ids.add(graph["scene_id"])
        assets = {}
        for item in graph["assets"]:
            reference(item["record"], "asset record", kind="ir.asset-record")
            record = store.load(ArtifactRef.from_dict(item["record"]))
            asset_record.validate(record)
            if record["asset_id"] != item["asset_id"]:
                raise ValueError("SceneGraph and AssetRecord identities differ")
            assets[item["asset_id"]] = record
        checked = validate_capabilities(capability, scene_ref)
        frames = {item["frame_id"] for item in graph["frames"]} | {n["node_id"] for n in graph["nodes"]}
        # TCP frame is explicitly declared by A, not manufactured from a runtime name.
        frames.add(graph["robot"]["tcp"]["frame_id"])
        if any(o["frame_id"] not in frames for o in checked["observations"]):
            raise ValueError("observation refers to an unknown A frame")
        joints = [{"node_id": node["node_id"], "name": joint["name"], "type": joint["type"],
                   "unit": "m" if joint["type"] == "prismatic" else "rad",
                   "limits": deepcopy(joint["limits"])}
                  for node in graph["nodes"] for joint in assets[node["asset_id"]]["joints"]
                  if joint["type"] != "fixed"]
        members.append({"scene_id": graph["scene_id"], "scene_ref": deepcopy(dict(scene_ref)),
                        "asset_refs": [deepcopy(a["record"]) for a in graph["assets"]],
                        "nodes": [{k: deepcopy(node[k]) for k in ("node_id", "asset_id", "pose", "semantic_tag")}
                                  for node in graph["nodes"]],
                        "frame_ids": sorted(frames), "joint_facts": joints, "capabilities": checked})
    return members


def build_context(request: Mapping[str, Any], request_ref: Mapping[str, Any], scene_group_id: str,
                  members: list[Mapping[str, Any]]) -> dict[str, Any]:
    request = validate_request(request)
    reference(request_ref, "normalized request", kind="task-request")
    logical_id(scene_group_id, label="scene_group_id")
    if not members or len({m["scene_id"] for m in members}) != len(members):
        raise ValueError("scene group must contain unique validated members")
    from datafarm.kernel.io import canonical_json_bytes as encode_json, sha256_bytes

    if request_ref["sha256"] != sha256_bytes(encode_json(request)):
        raise ValueError("planning request digest differs from its data")
    context = {"schema": CONTEXT_SCHEMA, "request": request, "request_ref": deepcopy(dict(request_ref)),
               "scene_group_id": scene_group_id, "members": deepcopy(members)}
    canonical_json_bytes(context)
    return context


def proposal_schema(context: Mapping[str, Any]) -> dict[str, Any]:
    # The model selects source IDs and exact bindings, not replacement requirement text or numbers.
    requirement_ids = ["request.demand"] + [i["id"] for name in
        ("goals", "constraints", "forbidden_shortcuts", "variation") for i in context["request"].get(name, [])]
    text_schema = {"type": "string"}
    ids = {"type": "array", "items": {"type": "string", "enum": requirement_ids}}
    # validate_proposal 要求 role/node_id/frame_id 都是 logical ID；schema 不说，模型就会写自然语言角色名。
    identifier = {"type": "string", "pattern": IDENTIFIER_PATTERN.pattern}
    node_ids = sorted({n["node_id"] for m in context["members"] for n in m["nodes"]})
    frame_ids = sorted({f for m in context["members"] for f in m["frame_ids"]})
    role = {"type": "object", "additionalProperties": False,
            "properties": {"role": identifier, "node_id": {"type": "string", "enum": node_ids},
                           "frame_id": {"type": "string", "enum": frame_ids}},
            "required": ["role", "node_id", "frame_id"]}
    props = {"name": text_schema, "scene_id": {"type": "string", "enum": [m["scene_id"] for m in context["members"]]},
             "requirement_ids": ids, "roles": {"type": "array", "items": role},
             "needed_apis": {"type": "array", "items": text_schema},
             "needed_observations": {"type": "array", "items": text_schema},
             "episode_count": {"type": "integer", "minimum": 1}}
    task = {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}
    unresolved = {"type": "object", "additionalProperties": False,
                  "properties": {"requirement_id": {"type": "string", "enum": requirement_ids}, "reason": text_schema},
                  "required": ["requirement_id", "reason"]}
    return {"type": "object", "additionalProperties": False,
            "properties": {"schema": {"type": "string", "enum": [PROPOSAL_SCHEMA]},
                           "tasks": {"type": "array", "items": task},
                           "unresolved": {"type": "array", "items": unresolved}},
            "required": ["schema", "tasks", "unresolved"]}


def validate_proposal(value: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    object_fields(value, {"schema", "tasks", "unresolved"}, label="task plan proposal")
    if value["schema"] != PROPOSAL_SCHEMA or not isinstance(value["tasks"], list) or not 1 <= len(value["tasks"]) <= 128:
        raise ValueError("proposal needs one or more bounded tasks")
    request = validate_request(context["request"])
    if not isinstance(context["members"], list) or not context["members"]:
        raise ValueError("task planning context has no scene members")
    required = {"request.demand"} | {i["id"] for name in
        ("goals", "constraints", "forbidden_shortcuts", "variation") for i in request.get(name, [])}
    members = {m["scene_id"]: m for m in context["members"]}
    covered = set()
    for task in value["tasks"]:
        object_fields(task, {"name", "scene_id", "requirement_ids", "roles", "needed_apis",
                             "needed_observations", "episode_count"}, label="proposed task")
        text(task["name"], "task display name", 256)
        logical_id(task["scene_id"], label="task scene_id")
        if task["scene_id"] not in members:
            raise ValueError("task must bind one exact scene group member")
        ids = string_list(task["requirement_ids"], "source requirements", identifiers=True)
        if not ids or not set(ids) <= required:
            raise ValueError("task adds an unrequested goal or lacks source requirements")
        covered.update(ids)
        member = members[task["scene_id"]]
        nodes = {n["node_id"] for n in member["nodes"]}
        if not isinstance(task["roles"], list) or not 1 <= len(task["roles"]) <= 128:
            raise ValueError("task needs bounded explicit role bindings")
        roles = set()
        for role in task["roles"]:
            object_fields(role, {"role", "node_id", "frame_id"}, label="task role")
            for key in role:
                logical_id(role[key], label=key)
            if role["role"] in roles or role["node_id"] not in nodes or role["frame_id"] not in member["frame_ids"]:
                raise ValueError("duplicate role or unknown A node/frame binding")
            roles.add(role["role"])
        string_list(task["needed_apis"], "needed APIs")
        string_list(task["needed_observations"], "needed observations", identifiers=True)
        if type(task["episode_count"]) is not int or not 1 <= task["episode_count"] <= 100000:
            raise ValueError("task episode_count must be a positive bounded integer")
    if not isinstance(value["unresolved"], list) or len(value["unresolved"]) > 256:
        raise ValueError("unresolved requirements must be a bounded list")
    unresolved_ids = set()
    for item in value["unresolved"]:
        object_fields(item, {"requirement_id", "reason"}, label="unresolved requirement")
        text(item["reason"], "unresolved reason")
        key = logical_id(item["requirement_id"], label="unresolved requirement ID")
        if key not in required or key in covered or key in unresolved_ids:
            raise ValueError("unknown, duplicate or already covered unresolved requirement")
        unresolved_ids.add(key)
    if covered | unresolved_ids != required:
        raise ValueError("proposal silently drops request requirements")
    inherited = {"request.demand"} | {i["id"] for name in
        ("constraints", "forbidden_shortcuts", "variation") for i in request.get(name, [])}
    for parameter in request.get("parameters", []):
        sources = set(parameter["requirement_ids"])
        if (not any(sources <= set(task["requirement_ids"]) | inherited for task in value["tasks"])
                and not sources & unresolved_ids):
            raise ValueError("proposal separates a parameter from its conjunctive requirements")
    if "task_count" in request and len(value["tasks"]) != request["task_count"]:
        raise ValueError("proposal changes the requested task count")
    if "episode_count" in request and sum(t["episode_count"] for t in value["tasks"]) != request["episode_count"]:
        raise ValueError("proposal changes the requested episode count")
    canonical_json_bytes(value)
    return deepcopy(dict(value))


def compile_table(store: ArtifactStore, context: Mapping[str, Any], proposal: Mapping[str, Any], *,
                  created_at: str) -> tuple[dict[str, Any], ArtifactRef]:
    object_fields(context, {"schema", "request", "request_ref", "scene_group_id", "members"}, label="planning context")
    if context["schema"] != CONTEXT_SCHEMA:
        raise ValueError("unsupported task planning context")
    request = validate_request(context["request"])
    reference(context["request_ref"], "normalized request", kind="task-request")
    if store.load(ArtifactRef.from_dict(context["request_ref"])) != request:
        raise ValueError("planning request differs from its immutable source reference")
    original = ArtifactRef.from_dict(request["original_request_ref"])
    store.load_bytes(original)
    canonical_members = load_scene_group(store, [m["scene_ref"] for m in context["members"]],
                                         [m["capabilities"] for m in context["members"]])
    if canonical_members != context["members"]:
        raise ValueError("scene facts differ from their bound A records")
    logical_id(context["scene_group_id"], label="scene_group_id")
    proposal = validate_proposal(proposal, context)
    members = {m["scene_id"]: m for m in context["members"]}
    identity = {"context_sha256": sha256_json(context), "proposal_sha256": sha256_json(proposal)}
    table_id = storage_key("task-table", identity)
    entries = []
    unresolved = bool(proposal["unresolved"])
    for index, task in enumerate(proposal["tasks"]):
        member = members[task["scene_id"]]
        capability = member["capabilities"]
        available_obs = {o["id"] for o in capability["observations"] if o["availability"] == "available"}
        missing = ["api:" + key for key in task["needed_apis"] if key not in capability["api"]]
        missing += ["observation:" + key for key in task["needed_observations"] if key not in available_obs]
        if not capability["api"]:
            missing.append("api:executable-program")
        # Stable logical task identity and distinct accepted-input revision identity.
        task_id = storage_key("task", {"request_id": request["request_id"],
                                       "scene_group_id": context["scene_group_id"], "slot": index})
        requirement_ids = sorted(set(task["requirement_ids"]) | {"request.demand"} |
            {i["id"] for name in ("constraints", "forbidden_shortcuts", "variation") for i in request.get(name, [])})
        goals = [i for i in request.get("goals", []) if i["id"] in requirement_ids]
        task_revision = storage_key("task-revision", {**identity, "task_id": task_id})
        payload = {"profile": "request-task/1", "task_id": task_id, "task_revision": task_revision,
                   "request_id": request["request_id"], "scene_id": task["scene_id"], "demand": request["demand"],
                   "roles": task["roles"], "stage_graph": {"stages": [], "sync": []},
                   "predicates": [{"id": "request.demand", "text": request["demand"]}, *goals,
                                  *request.get("constraints", []), *request.get("variation", [])],
                   "forbidden_shortcuts": request.get("forbidden_shortcuts", []), "skill_evidence": [],
                   "requirement_ids": requirement_ids,
                   "parameters": [p for p in request.get("parameters", []) if set(p["requirement_ids"]) <= set(requirement_ids)]}
        validate_task_payload(payload)
        document = ArtifactDocument(kind=IRKind.TASK, artifact_id=task_revision, created_at=created_at,
                                    producer="b-task-planner", predecessors=(ArtifactRef.from_dict(context["request_ref"]),),
                                    evidence=(ArtifactRef.from_dict(member["scene_ref"]),), payload=payload)
        task_ref = store.put_document(document)
        entries.append({"task_id": task_id, "task_revision": task_revision, "name": task["name"],
                        "scene_id": task["scene_id"], "scene_ref": member["scene_ref"],
                        "task_ref": task_ref.to_dict(), "requirement_ids": requirement_ids,
                        "needed_apis": task["needed_apis"], "needed_observations": task["needed_observations"],
                        "episode_count": task["episode_count"], "missing_capabilities": missing,
                        "status": "needs_input" if unresolved else "needs_capability" if missing else "planned",
                        "semantic_review": "unverified"})
    table = {"schema": TABLE_SCHEMA, "table_id": table_id, "identity": identity,
             "request_id": request["request_id"], "request_ref": context["request_ref"],
             "scene_group_id": context["scene_group_id"], "scene_refs": [m["scene_ref"] for m in context["members"]],
             "asset_refs": [ref for m in context["members"] for ref in m["asset_refs"]],
             "capability_refs": [store.put_json("task-capabilities", m["capabilities"]).to_dict() for m in context["members"]],
             "tasks": entries, "unresolved": proposal["unresolved"],
             "request_parameters": deepcopy(request.get("parameters", [])), "semantic_review": "unverified"}
    used_parameters = {p["id"] for entry in entries
                       for p in store.document(ArtifactRef.from_dict(entry["task_ref"])).payload["parameters"]}
    unassigned = [p for p in request.get("parameters", []) if p["id"] not in used_parameters]
    unresolved_ids = {item["requirement_id"] for item in proposal["unresolved"]}
    if any(not set(p["requirement_ids"]) & unresolved_ids for p in unassigned):
        raise ValueError("task split does not preserve every request parameter")
    table["unassigned_parameter_ids"] = [p["id"] for p in unassigned]
    return table, store.put_json("scene-task-table", table)

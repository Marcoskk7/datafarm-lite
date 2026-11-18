"""Measured-scene Episode -> HDF5 (docs/reference/b-control-interfaces.md section 8).

The JSON Episode IR remains the authority; this export is a derived, lossless view of
its numeric streams plus verbatim JSON for contacts/events. Nothing is resampled,
interpolated or rounded. ``episode_arrays`` is pure Python (testable without h5py);
``write_episode_hdf5`` lazily imports numpy/h5py (``pip install .[workflow]``).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from datafarm.expert.episode import validate_episode

EXPORT_SCHEMA = "datafarm.episode-hdf5/1"
NAN = float("nan")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _robot_entity(state: Mapping[str, Any]) -> str:
    """Scene node that owns the observed robot joints; the Episode stores no separate id."""
    names = set(state["robot"]["joint_names"])
    owners = [node for node, entity in state["entities"].items()
              if names and names <= {j["name"] for j in entity["joints"]}]
    return owners[0] if len(owners) == 1 else "robot"


def episode_arrays(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``{"attrs": {...}, "datasets": {path: (rows, dtype)}}`` from a sealed Episode payload."""
    episode = validate_episode(payload)
    states, actions, setup = episode["state_trace"], episode["actions"], episode["setup"]
    robot_id = _robot_entity(states[0])
    joint_names = list(states[0]["robot"]["joint_names"])
    data: dict[str, tuple[list, str]] = {}
    data["state/control_step"] = ([s["control_step"] for s in states], "int64")
    data["state/time_s"] = ([s["time_s"] for s in states], "float64")
    prefix = f"state/robots/{robot_id}"
    data[prefix + "/q"] = ([list(s["robot"]["q"]) for s in states], "float64")
    data[prefix + "/dq"] = ([list(s["robot"]["dq"]) for s in states], "float64")
    data[prefix + "/tcp_pose_xyzw"] = ([(s["robot"]["tcp"]["position_m"] + s["robot"]["tcp"]["quaternion_xyzw"])
                                        if "tcp" in s["robot"] else [NAN]*7 for s in states], "float64")
    data[prefix + "/gripper_width_m"] = ([s["robot"]["gripper"]["width_m"] if "gripper" in s["robot"] else NAN
                                          for s in states], "float64")
    entity_joints: dict[str, list[str]] = {}
    for node in setup["node_ids"]:
        base = f"state/entities/{node}"
        # Lab 3.0 is natively XYZW; the recorder already stores x,y,z,qx,qy,qz,qw without swapping.
        data[base + "/pose_xyzw"] = ([s["entities"][node]["position_m"] + s["entities"][node]["quaternion_xyzw"]
                                      for s in states], "float64")
        data[base + "/linear_velocity_m_s"] = ([s["entities"][node]["linear_velocity_m_s"] for s in states], "float64")
        data[base + "/angular_velocity_rad_s"] = ([s["entities"][node]["angular_velocity_rad_s"] for s in states], "float64")
        names = [j["name"] for j in states[0]["entities"][node]["joints"]]
        entity_joints[node] = names
        if names:
            data[base + "/joint_q"] = ([[j["q"] for j in s["entities"][node]["joints"]] for s in states], "float64")
            data[base + "/joint_dq"] = ([[j["dq"] for j in s["entities"][node]["joints"]] for s in states], "float64")
    # Primitives target different joint subsets per step; columns are the union, NaN = not commanded.
    requested_names = sorted({n for a in actions for n in a["requested_joint_targets"]})
    applied_names = sorted({n for a in actions for n in a["applied_joint_targets"]})
    data["action/interval_steps"] = ([[a["control_step"], a["control_step"]+1] for a in actions], "int64")
    data[f"action/robots/{robot_id}/requested"] = ([[a["requested_joint_targets"].get(n, NAN) for n in requested_names]
                                                    for a in actions], "float64")
    data[f"action/robots/{robot_id}/applied"] = ([[a["applied_joint_targets"].get(n, NAN) for n in applied_names]
                                                  for a in actions], "float64")
    data["action/call_id"] = ([str((a.get("command") or {}).get("call_id", "")) for a in actions], "utf8")
    data["action/command"] = ([_json(a.get("command")) for a in actions], "utf8")
    data["contacts/records"] = ([_json(c) for c in episode["contacts"]], "utf8")
    data["events/records"] = ([_json(e) for e in episode["events"]], "utf8")
    attrs = {"export_schema": EXPORT_SCHEMA, "episode_profile": episode["profile"],
             "episode_id": setup["episode_id"], "task_id": setup["task_id"], "task_revision": setup["task_revision"],
             "attempt_id": setup["attempt_id"], "control_dt_s": setup["control_dt_s"], "control_mode": "joint_position_target",
             "frame_id": "world", "quaternion_order": "xyzw", "units": _json(states[0]["units"]),
             "robot_id": robot_id, "joint_names": _json(joint_names),
             "control_names_requested": _json(requested_names), "control_names_applied": _json(applied_names),
             "entity_joint_names": _json(entity_joints), "node_ids": _json(setup["node_ids"]),
             "termination": _json(episode["termination"]), "replay": _json(episode["replay"]),
             "judgement": episode["judgement"], "provenance": _json({k: setup.get(k) for k in (
                 "request_ref", "original_request_ref", "task_ref", "scene_ref", "runtime_ref", "expert_ref",
                 "task_contract_ref", "source_sha256")})}
    return {"attrs": attrs, "datasets": data}


def write_episode_hdf5(payload: Mapping[str, Any], output: str | Path, *, episode_ref: Mapping[str, Any] | None = None,
                       judge_result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write one fresh HDF5 file atomically; return a small manifest (path, sha256, dataset shapes)."""
    try:
        import h5py
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on optional extras
        raise RuntimeError("HDF5 export needs numpy and h5py (pip install '.[workflow]')") from exc
    from datafarm.kernel.io import sha256_file
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"{output} exists; exports are never overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    arrays = episode_arrays(payload)
    attrs = dict(arrays["attrs"])
    attrs["episode_ref"] = _json(episode_ref) if episode_ref is not None else ""
    # Judge result is reported, never inferred; absent means unjudged.
    attrs["judge_result"] = _json(judge_result) if judge_result is not None else ""
    shapes = {}
    temporary = output.with_name(output.name + ".partial")
    try:
        with h5py.File(temporary, "w") as handle:
            meta = handle.create_group("meta")
            for key, value in attrs.items():
                meta.attrs[key] = value
            for path, (rows, dtype) in arrays["datasets"].items():
                if dtype == "utf8":
                    value = np.array(rows, dtype=h5py.string_dtype(encoding="utf-8"))
                else:
                    value = np.asarray(rows, dtype=dtype)
                    if dtype == "float64" and value.size and not all(math.isfinite(v) or math.isnan(v) for v in value.ravel().tolist()):
                        raise ValueError(f"{path} contains infinite values")
                handle.create_dataset(path, data=value)
                shapes[path] = list(value.shape)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"schema": EXPORT_SCHEMA, "path": str(output.resolve()), "sha256": sha256_file(output),
            "episode_id": attrs["episode_id"], "datasets": shapes}

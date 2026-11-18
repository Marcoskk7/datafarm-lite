"""Shared measured Episode IR state; consumers preserve field names.

Adapted from Marcos PR13 (74bf9ec), with stricter contact and gripper evidence.
The current /1 layout is source/freeze-bound; earlier layouts are not converted.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from datafarm.ir.common import check, portable, pose, require
from datafarm.kernel.io import is_finite_number

UNITS = {"length": "m", "angle": "rad", "time": "s"}
STATE_KEYS = ("control_step", "time_s", "units", "frame_id", "entities", "robot", "contacts")
ENTITY_KEYS = ("availability", "asset_id", "position_m", "quaternion_xyzw", "linear_velocity_m_s",
               "angular_velocity_rad_s", "joints", "links")
ROBOT_REQUIRED = ("joint_names", "q", "dq")
ROBOT_OPTIONAL = ("tcp", "gripper")
GRIPPER_KEYS = ("width_m", "width_range_m", "state_source", "verification", "within_defined_range")
JOINT_KEYS = ("name", "type", "unit", "q", "dq")
JOINT_UNITS = {"revolute": "rad", "prismatic": "m"}


def _vector(value: Any, size: int, label: str) -> list[float]:
    check(isinstance(value, list) and len(value) == size and all(is_finite_number(x) for x in value),
          f"{label} must be {size} finite numbers")
    return value


def _unit_quaternion(value: Any, label: str) -> None:
    _vector(value, 4, label)
    check(math.isclose(math.hypot(*value), 1, abs_tol=1e-6), f"{label} must be a unit quaternion (xyzw)")


def _exact_keys(value: Any, keys: Sequence[str], label: str) -> None:
    check(isinstance(value, Mapping), f"{label} must be an object")
    check(set(value) == set(keys), f"{label} fields must be exactly {sorted(keys)}, got {sorted(value)}")


def validate_robot(value: Any, label: str = "robot") -> None:
    """Robot observation and the robot segment of a scene snapshot."""
    check(isinstance(value, Mapping), f"{label} must be an object")
    require(value, ROBOT_REQUIRED, label)
    extra = set(value) - set(ROBOT_REQUIRED) - set(ROBOT_OPTIONAL)
    check(not extra, f"{label} has unknown fields {sorted(extra)}")
    names = value["joint_names"]
    check(isinstance(names, list) and all(isinstance(n, str) and n for n in names) and bool(names) and len(set(names)) == len(names),
          f"{label}.joint_names must be distinct non-empty strings")
    _vector(value["q"], len(names), f"{label}.q")
    _vector(value["dq"], len(names), f"{label}.dq")
    if "tcp" in value:
        _exact_keys(value["tcp"], ("frame_id", "position_m", "quaternion_xyzw"), f"{label}.tcp")
        pose(value["tcp"], f"{label}.tcp")
        _unit_quaternion(value["tcp"]["quaternion_xyzw"], f"{label}.tcp.quaternion_xyzw")
        check(value["tcp"]["frame_id"] == "world", f"{label}.tcp must be in the world frame")
    if "gripper" in value:
        gripper = value["gripper"]
        _exact_keys(gripper, GRIPPER_KEYS, f"{label}.gripper")
        check(is_finite_number(gripper["width_m"]), f"{label}.gripper.width_m must be finite")
        _vector(gripper["width_range_m"], 2, f"{label}.gripper.width_range_m")
        check(isinstance(gripper["within_defined_range"], bool), f"{label}.gripper.within_defined_range must be bool")
        lower, upper = gripper["width_range_m"]
        check(0 <= lower <= upper, f"{label}.gripper width range must be ordered and nonnegative")
        expected = lower - 1e-8 <= gripper["width_m"] <= upper + 1e-8
        check(gripper["within_defined_range"] is expected, f"{label}.gripper range flag contradicts width")
        check(all(isinstance(gripper[k], str) and gripper[k] for k in ("state_source", "verification")),
              f"{label}.gripper.state_source/verification must be non-empty strings")


def _entity(value: Any, label: str) -> None:
    _exact_keys(value, ENTITY_KEYS, label)
    check(value["availability"] == "available", f"{label} is unavailable")
    check(isinstance(value["asset_id"], str) and bool(value["asset_id"]), f"{label}.asset_id must be non-empty")
    _vector(value["position_m"], 3, f"{label}.position_m")
    _unit_quaternion(value["quaternion_xyzw"], f"{label}.quaternion_xyzw")
    _vector(value["linear_velocity_m_s"], 3, f"{label}.linear_velocity_m_s")
    _vector(value["angular_velocity_rad_s"], 3, f"{label}.angular_velocity_rad_s")
    links = value["links"]
    check(isinstance(links, Mapping), f"{label}.links must be an object")
    for name, link in links.items():
        _exact_keys(link, ("position_m", "quaternion_xyzw"), f"{label}.links.{name}")
        _vector(link["position_m"], 3, f"{label}.links.{name}.position_m")
        _unit_quaternion(link["quaternion_xyzw"], f"{label}.links.{name}.quaternion_xyzw")
    joints = value["joints"]
    check(isinstance(joints, list), f"{label}.joints must be a list")
    for i, joint in enumerate(joints):
        where = f"{label}.joints[{i}]"
        _exact_keys(joint, JOINT_KEYS, where)
        check(isinstance(joint["name"], str) and bool(joint["name"]), f"{where}.name must be non-empty")
        check(JOINT_UNITS.get(joint["type"]) == joint["unit"] and joint["unit"] is not None,
              f"{where}: type/unit must be revolute/rad or prismatic/m")
        check(is_finite_number(joint["q"]) and is_finite_number(joint["dq"]), f"{where}.q/dq must be finite")
    check(len({j["name"] for j in joints}) == len(joints), f"{label}.joints names must be distinct")


def validate_contacts(value: Any, step: int, node_ids: Sequence[str] | None = None) -> None:
    check(isinstance(value, Mapping), "contacts must be an object")
    required = {"availability", "rows", "coverage", "sampling", "control_step"}
    optional = {"force_availability", "normal_availability", "reason_code"}
    require(value, tuple(required), "contacts")
    check(not set(value) - required - optional, "unknown contact fields")
    if "reason_code" in value:
        check(isinstance(value["reason_code"],str) and bool(value["reason_code"]), "contact reason code required")
    check(type(value["control_step"]) is int and value["control_step"] == step, "contacts.control_step must equal the state control_step")
    check(value["sampling"] == "cycle_end_snapshot", "contacts.sampling must be cycle_end_snapshot")
    check(value["availability"] in ("available", "unavailable"), "contacts.availability must be available/unavailable")
    check(value["coverage"] in ("unknown", "complete", "truncated"), "contacts.coverage must be unknown/complete/truncated")
    if value["availability"] == "unavailable":
        check(value["rows"] is None, "unavailable contacts must have rows=null, not empty contact")
    else:
        check(isinstance(value["rows"], list), "available contacts.rows must be a list")
        for row in value["rows"]:
            _exact_keys(row, ("a", "b", "body_a", "body_b", "dist_m"), "contact row")
            check(is_finite_number(row["dist_m"]), "contact distance must be finite")
            for end in ("a", "b"):
                check(row[end] is None or isinstance(row[end], str) and bool(row[end]), "invalid contact endpoint")
                check(node_ids is None or row[end] is None or row[end] in node_ids, "unknown contact endpoint")
            check(all(isinstance(row[k], str) and bool(row[k]) for k in ("body_a", "body_b")), "contact body labels required")
    for key in ("force_availability", "normal_availability"):
        if key in value:
            check(value[key] == "unavailable", "unmeasured force/normal cannot be declared available")


def validate_state(state: Any, *, node_ids: Sequence[str] | None = None, step: int | None = None,
                   dt: float | None = None) -> dict[str, Any]:
    """Validate exact measured fields, optional scene coverage and clock; copy data."""
    _exact_keys(state, STATE_KEYS, "state")
    control_step = state["control_step"]
    check(type(control_step) is int and control_step >= 0, "state.control_step must be a non-negative integer")
    check(step is None or control_step == step, f"state clock gap: expected control_step {step}, got {control_step}")
    check(state["units"] == UNITS and state["frame_id"] == "world", "state units/frame must be SI and world")
    check(is_finite_number(state["time_s"]) and state["time_s"] >= 0, "state.time_s must be finite and non-negative")
    if dt is not None:
        check(is_finite_number(dt) and dt > 0, "positive finite control_dt_s required")
        check(math.isclose(state["time_s"], control_step * dt, rel_tol=0, abs_tol=1e-9),
              "state.time_s must equal control_step * control_dt_s")
    entities = state["entities"]
    check(isinstance(entities, Mapping) and bool(entities), "state.entities must be a non-empty object")
    if node_ids is not None:
        check(set(entities) == set(node_ids), "state.entities must cover exactly the scene node_ids")
    for node_id, entry in entities.items():
        _entity(entry, f"entities.{node_id}")
    validate_robot(state["robot"])
    validate_contacts(state["contacts"], control_step, list(entities))
    portable(state, "state")
    return deepcopy(dict(state))

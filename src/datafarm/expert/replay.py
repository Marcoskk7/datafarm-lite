"""Whole-trajectory replay comparison; evidence is separate from task judgement."""
from copy import deepcopy
import math

from datafarm.expert.episode import validate_episode, validate_state
from datafarm.kernel.schema import canonical_json_bytes

PROFILE = "datafarm.action-replay-report/1"
TOLERANCES = {"position_m", "orientation_rad", "linear_velocity_m_s", "angular_velocity_rad_s", "joint_position_m", "joint_position_rad", "joint_velocity_m_s", "joint_velocity_rad_s"}


def validate_tolerances(value):
    if not isinstance(value, dict) or set(value) != TOLERANCES:
        raise ValueError("explicit unit-specific replay tolerances required")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in value.values()):
        raise ValueError("finite nonnegative replay tolerances required")
    return deepcopy(value)


def quaternion_error(a, b):
    # q and -q represent the same rotation; chord form avoids acos near one.
    chord = min(math.dist(a, b), math.dist(a, [-v for v in b]))
    return 4 * math.asin(min(1., chord / 2))


def compare_states(expected, actual, tolerances):
    errors = {key: 0. for key in TOLERANCES}
    if set(expected["entities"]) != set(actual["entities"]):
        raise ValueError("replay entity coverage differs")
    def update(key, value):
        errors[key] = max(errors[key], value)
    def poses(a, b):
        update("position_m", math.dist(a["position_m"], b["position_m"]))
        update("orientation_rad", quaternion_error(a["quaternion_xyzw"], b["quaternion_xyzw"]))
    for node, a in expected["entities"].items():
        b = actual["entities"][node]
        poses(a, b)
        update("linear_velocity_m_s", math.dist(a["linear_velocity_m_s"], b["linear_velocity_m_s"]))
        update("angular_velocity_rad_s", math.dist(a["angular_velocity_rad_s"], b["angular_velocity_rad_s"]))
        if set(a["links"]) != set(b["links"]):
            raise ValueError("replay link coverage differs")
        for name, link in a["links"].items():
            poses(link, b["links"][name])
        if [(j["name"], j["type"]) for j in a["joints"]] != [(j["name"], j["type"]) for j in b["joints"]]:
            raise ValueError("replay joint identity differs")
        for left, right in zip(a["joints"], b["joints"]):
            unit = "m" if left["type"] == "prismatic" else "rad"
            update("joint_position_" + unit, abs(left["q"] - right["q"]))
            update("joint_velocity_" + unit + "_s", abs(left["dq"] - right["dq"]))
    # TCP/gripper are independently measured and must not silently disappear.
    for key in ("tcp_pose",):
        if (key in expected["robot"]) != (key in actual["robot"]):
            raise ValueError("replay robot semantic coverage differs")
        if key in expected["robot"]:
            poses(expected["robot"][key], actual["robot"][key])
    if "gripper" in expected["robot"]:
        update("position_m", abs(expected["robot"]["gripper"]["width_m"] - actual["robot"]["gripper"]["width_m"]))
    return errors, all(errors[k] <= tolerances[k] for k in errors)


def verify_action_replay(payload, host, tolerances, *, verifier_id):
    """Host restores measured initial state and applies every recorded actuator input.

    Host is trusted deployment code. This proves tolerance-bounded sampled state
    reproduction, not hidden solver restoration, bitwise determinism or contacts.
    """
    from datafarm.kernel.identifiers import logical_id
    logical_id(verifier_id, label="verifier_id")
    payload = validate_episode(payload)
    tolerances = validate_tolerances(tolerances)
    if not payload["termination"]["trace_complete"]:
        raise ValueError("incomplete trace cannot be replay-qualified")
    if host.source_binding() != payload["setup"]:
        raise ValueError("replay source/runtime binding differs")
    states = payload["state_trace"]
    maxima = {k: 0. for k in TOLERANCES}
    checked, mismatch = 0, []
    try:
        host.restore(deepcopy(states[0]))
        for step, expected in enumerate(states):
            if step:
                applied = deepcopy(payload["actions"][step-1]["applied_joint_targets"])
                if host.apply(applied) != applied:
                    raise ValueError("replay actuator readback differs")
                host.step()
            actual = validate_state(host.snapshot(step), payload["setup"]["node_ids"], step, payload["setup"]["control_dt_s"])
            errors, ok = compare_states(expected, actual, tolerances)
            checked += 1
            for k, v in errors.items():
                maxima[k] = max(maxima[k], v)
            if not ok:
                mismatch.append(step)
        status, reason = ("verified", "WITHIN_EXPLICIT_TOLERANCES") if not mismatch else ("rejected", "STATE_DIVERGENCE")
    except Exception:
        status, reason = "error", "REPLAY_RUNTIME_OR_COVERAGE_ERROR"
    report = {"schema": PROFILE, "verifier_id": verifier_id, "episode_id": payload["setup"]["episode_id"],
              "source_binding": deepcopy(payload["setup"]), "status": status, "reason_code": reason,
              "checked_states": checked, "expected_states": len(states), "max_errors": maxima,
              "mismatched_steps": mismatch, "tolerances": tolerances,
              "contact_replay": "unverified", "hidden_solver_state_restore": "unverified", "bitwise_replay": "unverified"}
    canonical_json_bytes(report)
    return report

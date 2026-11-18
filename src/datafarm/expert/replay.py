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
    tolerances = validate_tolerances(tolerances)
    errors = {key: 0. for key in TOLERANCES}
    if set(expected["entities"]) != set(actual["entities"]):
        raise ValueError("replay entity coverage differs")
    def update(key, value):
        if not math.isfinite(value):
            raise ValueError("nonfinite replay difference")
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
    for value in (expected, actual):
        robot = value.get("robot")
        if not isinstance(robot, dict) or not isinstance(robot.get("joint_names"), list):
            raise ValueError("measured robot joint names required")
        names = robot["joint_names"]
        if not names or len(set(names)) != len(names) or any(not isinstance(n, str) or not n for n in names):
            raise ValueError("unique robot joint names required")
        for key in ("q", "dq"):
            if not isinstance(robot.get(key), list) or len(robot[key]) != len(names) or any(type(v) not in (int,float) or not math.isfinite(v) for v in robot[key]):
                raise ValueError("finite measured robot q/dq required")
        if "tcp_pose" in robot:
            pose = robot["tcp_pose"]
            if pose.get("frame_id") != "world":
                raise ValueError("TCP world frame required")
            for key, size in (("position_m",3),("quaternion_xyzw",4)):
                if not isinstance(pose.get(key),list) or len(pose[key]) != size or any(type(v) not in (int,float) or not math.isfinite(v) for v in pose[key]):
                    raise ValueError("finite TCP pose required")
            if not math.isclose(math.hypot(*pose["quaternion_xyzw"]),1,abs_tol=1e-6):
                raise ValueError("normalized TCP quaternion required")
        if "gripper" in robot and (type(robot["gripper"].get("width_m")) not in (int,float) or not math.isfinite(robot["gripper"]["width_m"])):
            raise ValueError("finite measured gripper width required")
    if expected["robot"]["joint_names"] != actual["robot"]["joint_names"] or ("gripper" in expected["robot"]) != ("gripper" in actual["robot"]):
        raise ValueError("robot semantic coverage differs")
    # Bind duplicated robot q/dq to the measured, source-typed scene articulation.
    for value in (expected, actual):
        robot = value["robot"]
        candidates = [e for e in value["entities"].values()
                      if [j["name"] for j in e["joints"]] == robot["joint_names"]]
        if len(candidates) != 1:
            raise ValueError("robot joint measurements need one scene articulation")
        for index, joint in enumerate(candidates[0]["joints"]):
            if any(not math.isclose(robot[key][index], joint[key], rel_tol=0, abs_tol=1e-9) for key in ("q","dq")):
                raise ValueError("robot q/dq differs from full-scene measurements")
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
    qualification = host.qualification()
    if qualification not in ("software_fixture", "source_bound_runtime"):
        raise ValueError("explicit replay host qualification required")
    states = payload["state_trace"]
    maxima = {k: 0. for k in TOLERANCES}
    checked, mismatch = 0, []
    acquired = False
    try:
        if not host.acquire():
            raise ValueError("replay physics owner busy")
        acquired = True
        if host.source_binding() != payload["setup"]:
            raise ValueError("source changed before owned replay")
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
        if host.source_binding() != payload["setup"]:
            raise ValueError("source changed during replay")
        status, reason = ("verified", "WITHIN_EXPLICIT_TOLERANCES") if not mismatch else ("rejected", "STATE_DIVERGENCE")
    except Exception:
        status, reason = "error", "REPLAY_RUNTIME_OR_COVERAGE_ERROR"
    finally:
        if acquired:
            try:
                host.release()
            except Exception:
                status, reason = "error", "REPLAY_OWNER_RELEASE_ERROR"
    report = {"schema": PROFILE, "verifier_id": verifier_id, "episode_id": payload["setup"]["episode_id"],
              "source_binding": deepcopy(payload["setup"]), "qualification": qualification, "status": status, "reason_code": reason,
              "checked_states": checked, "expected_states": len(states), "max_errors": maxima,
              "mismatched_steps": mismatch, "tolerances": tolerances,
              "contact_replay": "unverified", "hidden_solver_state_restore": "unverified", "bitwise_replay": "unverified"}
    canonical_json_bytes(report)
    return report

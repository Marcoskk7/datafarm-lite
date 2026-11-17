"""move_gripper 的实测开度速率上限取源模型手指速度，而不是请求的参考速率（lab30 G2 实跑踩到）。"""
from datafarm.simulation.tcp_control import finger_width_rate_limit


def _binding(fingers=("l", "r"), velocity=0.2):
    joints = [{"name": "master", "limits": {"velocity": 0.2}}] + [
        {"name": f, "limits": {"velocity": velocity}, "mimic": {"joint": "master", "multiplier": 0.5}} for f in fingers]
    return {"descriptor": {"joints": joints},
            "semantics": {"gripper": {"master_joint": "master", "finger_joints": list(fingers),
                                      "width_observation": {"joint_coefficients": {f: 1.0 for f in fingers}}}}}


def test_g2_shape_bounds_width_rate_by_both_finger_limits():
    # AIRBOT G2: each finger 0.2 m/s, width = q_left + q_right -> 0.4 m/s, above the 0.18 m/s overshoot measured
    # for a 0.03 m/s reference ramp, below the 0.64 m/s measured for a 0.1 m/s ramp (that one must still fail).
    assert finger_width_rate_limit(_binding()) == 0.4


def test_missing_finger_limits_disable_the_model_bound():
    binding = _binding()
    binding["descriptor"]["joints"][1]["limits"] = None
    assert finger_width_rate_limit(binding) is None
    assert finger_width_rate_limit({"descriptor": {"joints": []}, "semantics": {}}) is None

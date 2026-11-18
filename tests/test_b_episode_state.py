"""Shared /1 state: reject legacy, invalid and contradictory measurements."""

from copy import deepcopy
import pytest
from datafarm.ir.episode_state import validate_state, validate_robot
from datafarm.expert.replay import compare_states, TOLERANCES
from b_contract_fixtures import state as base_state


def state(step):
    s = base_state(step)
    s["entities"]["robot"]["joints"] = [
        {"name": "joint", "type": "revolute", "unit": "rad", "q": 0.0, "dq": 0.0}
    ]
    s["robot"]["tcp"] = {
        "frame_id": "world",
        "position_m": [0.0, 0.0, 0.0],
        "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
    }
    s["robot"]["gripper"] = {
        "width_m": 0.01,
        "width_range_m": [0.0, 0.1],
        "state_source": "fixture",
        "verification": "fixture",
        "within_defined_range": True,
    }
    return s


def test_shared_and_legacy_rejection():
    s = state(0)
    assert validate_state(s, node_ids=list(s["entities"]), step=0, dt=0.1) == s
    s["robot"]["tcp_pose"] = s["robot"].pop("tcp")
    with pytest.raises(ValueError):
        validate_state(s)


def test_tcp_divergence_is_measured():
    a = state(0)
    b = deepcopy(a)
    b["robot"]["tcp"]["position_m"][0] = 0.2
    # Joint/scene evidence remains unchanged.
    errors, ok = compare_states(a, b, {k: 0.001 for k in TOLERANCES})
    assert not ok and errors["position_m"] == 0.2


def test_contact_row_and_endpoint():
    s = state(0)
    s["contacts"].update(
        availability="available",
        rows=[
            {
                "a": "robot",
                "b": None,
                "body_a": "usd:robot",
                "body_b": "usd:ground",
                "dist_m": -0.001,
            }
        ],
    )
    validate_state(s)
    s["contacts"]["rows"][0]["a"] = "missing"
    with pytest.raises(ValueError):
        validate_state(s)
    s["contacts"]["rows"] = [{"dist_m": 0}]
    with pytest.raises(ValueError):
        validate_state(s)


def test_gripper_order_and_flag():
    robot = state(0)["robot"]
    robot["gripper"]["width_range_m"] = [0.1, 0]
    with pytest.raises(ValueError):
        validate_robot(robot)
    robot = state(0)["robot"]
    robot["gripper"]["within_defined_range"] = False
    with pytest.raises(ValueError):
        validate_robot(robot)


@pytest.mark.parametrize(
    "mutation",
    [
        "nan",
        "clock",
        "units",
        "legacy",
        "contact_null",
        "contact_force",
        "joint_unit",
        "duplicates",
    ],
)
def test_state_measurement_contract_rejects_incomplete_or_false_evidence(mutation):
    value = state(0)
    if mutation == "nan":
        value["entities"]["object.a"]["linear_velocity_m_s"][0] = float("nan")
    elif mutation == "clock":
        value["contacts"]["control_step"] = 1
    elif mutation == "units":
        value["units"]["length"] = "cm"
    elif mutation == "legacy":
        value["objects"] = value.pop("entities")
    elif mutation == "contact_null":
        value["contacts"]["rows"] = []
    elif mutation == "contact_force":
        value["contacts"]["force_availability"] = "available"
    elif mutation == "joint_unit":
        value["entities"]["robot"]["joints"][0]["unit"] = "m"
    else:
        value["entities"]["robot"]["joints"] *= 2
    with pytest.raises(ValueError):
        validate_state(value)


def test_state_fixtures_do_not_share_mutable_units_or_measurements():
    first, second = state(0), state(0)
    first["units"]["length"] = "cm"
    first["entities"]["object.a"]["position_m"][0] = 10
    assert second["units"]["length"] == "m"
    assert second["entities"]["object.a"]["position_m"] == [0, 0, 0]

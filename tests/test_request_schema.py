"""`datafarm.request/1`: the 1a → 1b contract rejects requests that 1b could not honour."""
import copy
from pathlib import Path

import pytest
import yaml

from datafarm.request.schema import RequestError, load_request, request_json_schema, validate_request

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/request/plate-drawer-airbot.yaml"


@pytest.fixture
def request_doc():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def objects(doc):
    return {obj["object_id"]: obj for obj in doc["scene"]["objects"]}


def rejected(doc, fragment):
    with pytest.raises(RequestError) as info:
        validate_request(doc)
    assert fragment in str(info.value)


def test_example_validates_with_structured_constraints():
    request = load_request(EXAMPLE)
    cabinet, plate = request.scene.objects[1], request.scene.objects[2]
    assert (cabinet.role, cabinet.constraints.joint, cabinet.constraints.joint_state) == (
        "receptacle", "prismatic", "open")
    assert (plate.role, plate.constraints.support, plate.count) == ("task_object", "object.table", 1)
    assert request.model_dump(by_alias=True, exclude_none=True)["schema"] == "datafarm.request/1"


@pytest.mark.parametrize("holdout", [[99, 120], [50, 60], [0, 0], [-5, 0]])
def test_seed_partitions_must_not_overlap(request_doc, holdout):
    request_doc["seeds"]["holdout"] = holdout
    with pytest.raises(RequestError):
        validate_request(request_doc)


def test_adjacent_seed_partitions_are_disjoint(request_doc):
    request_doc["seeds"]["holdout"] = [100, 119]
    assert validate_request(request_doc).seeds.holdout == [100, 119]


def test_reversed_seed_range_rejected(request_doc):
    request_doc["seeds"]["dev"] = [99, 0]
    rejected(request_doc, "first <= last")


def test_unknown_support_reference_rejected(request_doc):
    objects(request_doc)["object.plate"]["constraints"]["support"] = "object.counter"
    rejected(request_doc, "object.counter")


def test_support_cycle_rejected(request_doc):
    objects(request_doc)["object.table"]["constraints"]["support"] = "object.plate"
    rejected(request_doc, "support cycle")


def test_duplicate_object_ids_rejected(request_doc):
    request_doc["scene"]["objects"].append(copy.deepcopy(objects(request_doc)["object.plate"]))
    rejected(request_doc, "duplicate object_id ['object.plate']")


def test_right_wrist_rejected_for_single_arm_robot(request_doc):
    request_doc["cameras"].append("right_wrist")
    rejected(request_doc, "['right_wrist'] are not available on airbot_single_g2")


def test_render_view_of_unrecorded_camera_rejected(request_doc):
    request_doc["cameras"] = ["head"]
    request_doc["render"]["views"] = ["left_wrist", "third_person"]
    rejected(request_doc, "left_wrist")


def test_free_text_constraints_rejected(request_doc):
    """Objects are structured, not `drawer_cabinet(open, prismatic)` strings."""
    request_doc["scene"]["objects"][1] = "drawer_cabinet(open, prismatic)"
    rejected(request_doc, "request.scene.objects[1]")
    objects_ = request_doc["scene"]["objects"]
    objects_[1] = {"object_id": "object.cabinet", "category": "drawer_cabinet", "role": "receptacle",
                   "constraints": {"state": "open, prismatic"}}
    rejected(request_doc, "request.scene.objects[1].constraints.state")


def test_joint_state_requires_joint(request_doc):
    del objects(request_doc)["object.cabinet"]["constraints"]["joint"]
    rejected(request_doc, "joint_state requires joint")


def test_request_needs_a_task_object(request_doc):
    objects(request_doc)["object.plate"]["role"] = "background"
    rejected(request_doc, "task_object")


def test_strict_types_and_fixed_engine(request_doc):
    request_doc["episodes"] = "20"
    request_doc["render"]["engine"] = "blender"
    with pytest.raises(RequestError) as info:
        validate_request(request_doc)
    assert "request.episodes" in str(info.value) and "request.render.engine" in str(info.value)


def test_host_paths_rejected(request_doc):
    request_doc["task"] = "/home/user/task.txt"
    rejected(request_doc, "absolute filesystem path")


def test_json_schema_is_llm_ready():
    schema = request_json_schema()
    assert schema["properties"]["schema"]["const"] == "datafarm.request/1"
    assert schema["additionalProperties"] is False
    assert "right_wrist" in schema["properties"]["cameras"]["items"]["enum"]
    assert schema["$defs"]["SceneObject"]["properties"]["role"]["enum"] == ["task_object", "receptacle", "background"]

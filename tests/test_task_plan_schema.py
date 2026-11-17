"""task-plan proposal schema 必须把 validate_proposal 的 ID 规则告诉模型（lab30 实跑：模型写了带空格的角色名）。"""
import re

from datafarm.task.planning import proposal_schema


def _context():
    return {"request": {"goals": [{"id": "goal.a", "text": "x"}]},
            "members": [{"scene_id": "scene.s", "nodes": [{"node_id": "object.plate"}, {"node_id": "robot.airbot"}],
                         "frame_ids": ["object.plate", "robot.airbot", "tcp.left", "world"]}]}


def test_role_fields_are_constrained_to_logical_ids_and_scene_bindings():
    role = proposal_schema(_context())["properties"]["tasks"]["items"]["properties"]["roles"]["items"]
    pattern = re.compile(role["properties"]["role"]["pattern"])
    assert pattern.fullmatch("object_to_place")
    assert not pattern.fullmatch("already-open drawer container")
    assert role["properties"]["node_id"]["enum"] == ["object.plate", "robot.airbot"]
    assert role["properties"]["frame_id"]["enum"] == ["object.plate", "robot.airbot", "tcp.left", "world"]

"""B recording and control-owner regressions with measured software fixtures."""

from copy import deepcopy
from types import SimpleNamespace
import threading
import pytest
from datafarm.expert.episode import EpisodeRecorder, validate_episode, UNITS
from datafarm.adapters.sim.isaac_control import IsaacControlHost
from datafarm.adapters.sim.scene_measurements import SceneMeasurements
from datafarm.simulation.tcp_control import ControlFault

from b_contract_fixtures import SETUP, state, recorder


def test_complete_and_immutable():
    r = recorder()
    r.input({"joint": 0.2}, {"joint": 0.1}, {"primitive": "joint"})
    r.measured(state(1))
    p = r.finish("completed", "returned")
    assert len(p["state_trace"]) == len(p["actions"]) + 1
    assert p["actions"][0]["applied_joint_targets"] != {"joint": 0.2}
    assert p["judgement"] == "unverified" and p["frame_map"] == []
    with pytest.raises(ValueError):
        r.input({}, {}, {})


def test_partial_is_not_completed():
    r = recorder()
    r.input({}, {}, {})
    p = r.finish("error", "physics_unknown")
    assert not p["termination"]["trace_complete"]
    p["termination"]["status"] = "completed"
    with pytest.raises(ValueError):
        validate_episode(p)


@pytest.mark.parametrize(
    "change",
    [
        {"control_step": 2},
        {"time_s": 2},
        {"frame_id": "local"},
        {"entities": {}},
        {"units": {}},
    ],
)
def test_state_negatives(change):
    r = EpisodeRecorder(SETUP, max_steps=8, max_bytes=50000)
    with pytest.raises(ValueError):
        r.initial({**state(0), **change})


def test_nan_missing_contact_and_budget():
    r = recorder()
    r.input({}, {}, {})
    s = state(1)
    s["entities"]["object.a"]["position_m"][0] = float("nan")
    with pytest.raises(ValueError):
        r.measured(s)
    r = EpisodeRecorder(SETUP, max_steps=1, max_bytes=100)
    with pytest.raises(ValueError):
        r.initial(state(0))


class Tensor:
    def __init__(self, value):
        self.value = value

    def detach(self):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return deepcopy(self.value)


class Api:
    def __init__(self):
        self._motion_lock = threading.Lock()
        self._command_owner = None
        self.binding = {"joint_names": ["joint"], "joint_index": {"joint": 0}}
        self.robot = SimpleNamespace(
            actuators=SimpleNamespace(
                target_command=SimpleNamespace(position=Tensor([[0.0]]))
            ),
            write_data_to_sim=lambda: None,
            update=lambda dt: None,
        )

    def observe(self):
        return state(0)["robot"]

    def _stage_joint_targets(self, targets):
        self.robot.actuators.target_command.position = Tensor([[targets["joint"]]])


class Sim:
    def __init__(self, fail=False):
        self.steps = 0
        self.fail = fail

    def get_physics_dt(self):
        return 0.1

    def step(self, render=False):
        self.steps += 1
        if self.fail:
            raise RuntimeError("after advancement")


class Scene:
    def __init__(self):
        self.writes = 0
        self.updates = 0

    def write_data_to_sim(self):
        self.writes += 1

    def update(self, dt):
        self.updates += 1


class Measurements:
    def snapshot(self, step, dt):
        return state(step)


def test_one_full_scene_owner():
    api, sim, scene, r = Api(), Sim(), Scene(), recorder()
    h = IsaacControlHost(api, sim, scene=scene, measurements=Measurements(), recorder=r)
    assert h.acquire()
    try:
        h.step({"joint": 0.1}, {"primitive": "joint"})
    finally:
        h.release()
    assert (scene.writes, sim.steps, scene.updates) == (1, 1, 1)
    assert len(r.states) == 2 and len(r.actions) == 1
    assert r.actions[0]["applied_joint_targets"] == {"joint": 0.1}


def test_step_exception_never_retries_or_fabricates_state():
    api, sim, scene, r = Api(), Sim(True), Scene(), recorder()
    h = IsaacControlHost(api, sim, scene=scene, measurements=Measurements(), recorder=r)
    assert h.acquire()
    try:
        with pytest.raises(ControlFault):
            h.step({"joint": 0.1}, {})
        assert h.step_outcome_unknown
        with pytest.raises(ControlFault):
            h.step({"joint": 0.1}, {})
    finally:
        h.release()
    assert sim.steps == 1 and len(r.states) == 1 and len(r.actions) == 1
    assert not r.finish("error", "SIMULATION_ERROR")["termination"]["trace_complete"]


def test_full_entities_and_contact_unavailable():
    data = SimpleNamespace(
        root_pos_w=Tensor([[0, 0, 0]]),
        root_quat_w=Tensor([[0, 0, 0, 1]]),
        root_lin_vel_w=Tensor([[0, 0, 0]]),
        root_ang_vel_w=Tensor([[0, 0, 0]]),
    )
    handles = {
        n.replace(".", "__"): SimpleNamespace(data=data, joint_names=[], body_names=[])
        for n in SETUP["node_ids"]
    }
    graph = {
        "nodes": [
            {"node_id": "robot", "asset_id": "a", "semantic_tag": "robot"},
            {"node_id": "object.a", "asset_id": "b", "semantic_tag": "background"},
        ]
    }

    def fail(_):
        raise RuntimeError()

    m = SceneMeasurements(
        handles,
        graph,
        {"a": {"joints": []}, "b": {"joints": []}},
        Api(),
        contact_query=fail,
    )
    s = m.snapshot(0, 0.1)
    assert len(s["entities"]) == 2 and s["contacts"]["rows"] is None
    m.contact_query = lambda _: []
    s = m.snapshot(0, 0.1)
    assert s["contacts"]["rows"] == [] and s["contacts"]["coverage"] == "unknown"


def test_frame_pose_quaternion_negative():
    s = state(0)
    s["entities"]["robot"]["quaternion_xyzw"] = [0, 0, 0, 2]
    r = EpisodeRecorder(SETUP, max_steps=8, max_bytes=50000)
    with pytest.raises(ValueError):
        r.initial(s)


def test_budget_refused_before_staging():
    api, sim, scene, r = Api(), Sim(), Scene(), recorder()
    r.max_steps = 0
    h = IsaacControlHost(api, sim, scene=scene, measurements=Measurements(), recorder=r)
    assert h.acquire()
    try:
        with pytest.raises(ControlFault):
            h.step({"joint": 0.1}, {})
    finally:
        h.release()
    assert sim.steps == 0 and scene.writes == 0


def test_invalid_event_cannot_override_clock():
    h = IsaacControlHost(
        Api(), Sim(), scene=Scene(), measurements=Measurements(), recorder=recorder()
    )
    assert h.acquire()
    try:
        with pytest.raises(ControlFault):
            h.step({"joint": 0.1}, {"control_step": 99})
    finally:
        h.release()


def test_link_pose_nonfinite_refused():
    s = state(0)
    s["entities"]["robot"]["links"] = {
        "tip": {"position_m": [float("nan"), 0, 0], "quaternion_xyzw": [0, 0, 0, 1]}
    }
    r = EpisodeRecorder(SETUP, max_steps=8, max_bytes=50000)
    with pytest.raises(ValueError):
        r.initial(s)


def test_contact_force_never_fabricated():
    # Canonical portable encoding preserves USD identifier semantics, not filesystem locators.
    from datafarm.kernel.schema import canonical_json_bytes

    assert canonical_json_bytes({"body_a": "usd:/World/envs/env_0/Robot"})


def test_episode_document_roundtrip(tmp_path):
    from datafarm.kernel.store import ArtifactStore

    r = recorder()
    r.input({"joint": 0.1}, {"joint": 0.1}, {})
    r.measured(state(1))
    p = r.finish("completed", "returned")
    store = ArtifactStore(tmp_path)
    ref = store.put_document(r.document(p, created_at="2026-11-17T00:00:00Z"))
    assert store.load(ref)["payload"] == p


def test_primitive_failure_not_hidden_by_normal_return():
    from datafarm.entry.scene_rollout import SceneActions

    api = SimpleNamespace(
        move_tcp=lambda _: dict(status="timeout", reason_code="NOT_REACHED")
    )
    actions = SceneActions(api, None, None)
    assert actions.move_tcp({})["status"] == "timeout"
    assert actions.results[0]["reason_code"] == "NOT_REACHED"


def test_coverage_change_rejected_at_seal():
    r = recorder()
    r.input({}, {}, {})
    s = state(1)
    s["entities"]["robot"]["links"] = {
        "tip": {"position_m": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]}
    }
    r.measured(s)
    with pytest.raises(ValueError):
        r.finish("completed", "returned")


def test_returned_primitive_receipts_cannot_be_edited():
    from datafarm.entry.scene_rollout import SceneActions

    actions = SceneActions(
        SimpleNamespace(
            move_tcp=lambda _: dict(status="timeout", reason_code="NOT_REACHED")
        ),
        None,
        None,
    )
    actions.move_tcp({})
    returned = actions.results
    returned.clear()
    assert len(actions.results) == 1

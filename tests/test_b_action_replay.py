"""All-state replay, source binding and owner release; no physical claims."""

from copy import deepcopy
import pytest
from datafarm.expert.replay import verify_action_replay, TOLERANCES
import b_contract_fixtures as f


def payload():
    from datafarm.expert.episode import EpisodeRecorder

    r = EpisodeRecorder(f.SETUP, max_steps=3, max_bytes=50000)
    for i in range(2):
        value = f.state(i)
        value["entities"]["robot"]["joints"] = [
            {"name": "joint", "type": "revolute", "unit": "rad", "q": 0.0, "dq": 0.0}
        ]
        if i:
            r.input({"joint": 0.1}, {"joint": 0.1}, {})
        r.initial(value) if not i else r.measured(value)
    return r.finish("completed", "done")


class Host:
    def __init__(self, p, alter=False):
        self.p = p
        self.alter = alter
        self.steps = 0

    def acquire(self):
        return True

    def release(self):
        self.released = True

    def qualification(self):
        return "software_fixture"

    def source_binding(self):
        return deepcopy(self.p["setup"])

    def restore(self, s):
        self.initial = s

    def apply(self, a):
        return a

    def step(self):
        self.steps += 1

    def snapshot(self, i):
        s = deepcopy(self.p["state_trace"][i])
        if self.alter:
            s["entities"]["object.a"]["position_m"][0] = 0.1
        return s


def test_all_states():
    p = payload()
    h = Host(p)
    v = verify_action_replay(
        p, h, {k: 0.0001 for k in TOLERANCES}, verifier_id="verifier.a"
    )
    assert v["status"] == "verified" and v["checked_states"] == 2 and h.steps == 1
    assert v["contact_replay"] == "unverified"


def test_divergence():
    p = payload()
    assert (
        verify_action_replay(
            p, Host(p, True), {k: 0 for k in TOLERANCES}, verifier_id="v"
        )["status"]
        == "rejected"
    )


def test_binding():
    p = payload()
    h = Host(p)
    h.source_binding = lambda: {}
    with pytest.raises(ValueError):
        verify_action_replay(p, h, {k: 0 for k in TOLERANCES}, verifier_id="v")


def test_missing():
    p = payload()
    h = Host(p)
    h.snapshot = lambda i: {}
    assert (
        verify_action_replay(p, h, {k: 0 for k in TOLERANCES}, verifier_id="v")[
            "status"
        ]
        == "error"
    )


def test_sign():
    p = payload()
    h = Host(p)
    old = h.snapshot

    def snapshot(i):
        s = old(i)
        s["entities"]["object.a"]["quaternion_xyzw"] = [0, 0, 0, -1]
        return s

    h.snapshot = snapshot
    assert (
        verify_action_replay(p, h, {k: 0 for k in TOLERANCES}, verifier_id="v")[
            "status"
        ]
        == "verified"
    )


@pytest.mark.parametrize(
    "kind", ["nan_tcp", "missing_gripper", "wrong_frame", "busy", "step_error"]
)
def test_fail_closed(kind):
    p = payload()
    h = Host(p)
    old = h.snapshot
    if kind == "busy":
        h.acquire = lambda: False
    elif kind == "step_error":
        h.step = lambda: (_ for _ in ()).throw(RuntimeError())
    else:
        for state in p["state_trace"]:
            state["robot"]["tcp"] = {
                "frame_id": "world",
                "position_m": [0, 0, 0],
                "quaternion_xyzw": [0, 0, 0, 1],
            }
            state["robot"]["gripper"] = {
                "width_m": 0.1,
                "width_range_m": [0.0, 0.2],
                "state_source": "fixture",
                "verification": "fixture",
                "within_defined_range": True,
            }

        def snapshot(i):
            value = old(i)
            if kind == "nan_tcp":
                value["robot"]["tcp"]["position_m"][0] = float("nan")
            elif kind == "wrong_frame":
                value["robot"]["tcp"]["frame_id"] = "base"
            else:
                del value["robot"]["gripper"]
            return value

        h.snapshot = snapshot
    report = verify_action_replay(p, h, {k: 0 for k in TOLERANCES}, verifier_id="v")
    assert report["status"] == "error"
    if kind != "busy":
        assert h.released


def test_isaac_owner_and_unknown_step():
    from types import SimpleNamespace
    from datafarm.adapters.sim.isaac_replay import IsaacReplayHost
    import threading

    api = SimpleNamespace(_motion_lock=threading.Lock(), _command_owner=None)

    class Sim:
        steps = 0

        def get_physics_dt(self):
            return 0.1

        def step(self, render=False):
            self.steps += 1
            raise RuntimeError("unknown")

    sim = Sim()
    host = IsaacReplayHost(
        setup={"control_dt_s": 0.1},
        scene=SimpleNamespace(update=lambda dt: None),
        simulation=sim,
        api=api,
        measurements=None,
        reset=lambda: None,
        store=None,
        prepared=None,
        asset_roots=None,
    )
    with pytest.raises(ValueError):
        host.step()
    assert host.acquire()
    with pytest.raises(ValueError):
        host.step()
    host.pending = True
    with pytest.raises(RuntimeError):
        host.step()
    assert host.unhealthy and sim.steps == 1
    with pytest.raises(ValueError):
        host.step()
    host.release()
    assert not api._motion_lock.locked()


def test_isaac_source_is_loaded_not_echoed(monkeypatch):
    from types import SimpleNamespace
    from datafarm.adapters.sim.isaac_replay import IsaacReplayHost
    from datafarm.adapters.sim import scene_runtime

    sim = SimpleNamespace(get_physics_dt=lambda: 0.1)
    host = IsaacReplayHost(
        setup={"control_dt_s": 0.1, "scene_ref": {}},
        scene=None,
        simulation=sim,
        api=None,
        measurements=None,
        reset=lambda: None,
        store=None,
        prepared=None,
        asset_roots=None,
    )
    seen = []

    def reject(*args, **kw):
        seen.append(True)
        raise ValueError("source differs")

    monkeypatch.setattr(scene_runtime, "load_scene", reject)
    with pytest.raises(ValueError, match="source differs"):
        host.source_binding()
    assert seen


def test_robot_duplicate_measurement_mismatch():
    p = payload()
    h = Host(p)
    old = h.snapshot

    def snapshot(i):
        value = old(i)
        value["robot"]["q"][0] = 0.8
        return value

    h.snapshot = snapshot
    assert (
        verify_action_replay(p, h, {k: 1 for k in TOLERANCES}, verifier_id="v")[
            "status"
        ]
        == "error"
    )


def test_release_failure_never_passes():
    p = payload()
    h = Host(p)
    h.release = lambda: (_ for _ in ()).throw(RuntimeError("release"))
    assert (
        verify_action_replay(p, h, {k: 0 for k in TOLERANCES}, verifier_id="v")[
            "reason_code"
        ]
        == "REPLAY_OWNER_RELEASE_ERROR"
    )


def test_source_change_after_acquisition():
    p = payload()
    h = Host(p)
    calls = []

    def binding():
        calls.append(1)
        return deepcopy(p["setup"]) if len(calls) == 1 else {}

    h.source_binding = binding
    assert (
        verify_action_replay(p, h, {k: 0 for k in TOLERANCES}, verifier_id="v")[
            "status"
        ]
        == "error"
    )
    assert h.released

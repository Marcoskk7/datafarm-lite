"""B-line review fixes: expert-failure classification, shared worker policy, HDF5 arrays."""
import pytest

from datafarm.adapters.agent_harness.scene_worker import ExpertPolicyError, check_source
from datafarm.adapters.export.hdf5_episode import episode_arrays
from datafarm.expert.episode import EpisodeRecorder
from datafarm.ir.episode_state import UNITS
from datafarm.judge.integration import classify_result


def _error(reason):
    return {"schema_version": "judge-result/1", "outcome": "error", "success": None, "reason_code": reason,
            "predicate_results": {}, "metrics": {}, "terminated": False, "truncated": False}


def test_expert_caused_execution_failure_is_counted():
    assert classify_result(_error("EXECUTION_FAILED:JOINT_LIMIT")) == ("expert_error", False, "EXECUTION_FAILED:JOINT_LIMIT")
    assert classify_result(_error("EXECUTION_ERROR:EXPERT_EXCEPTION"))[0] == "expert_error"
    assert classify_result(_error("EXECUTION_ERROR:HOST_REQUALIFICATION_REQUIRED"))[0] == "judge_failure"
    assert classify_result(_error("EXECUTION_ERROR"))[0] == "judge_failure"


EXPERT = """import math
LIMIT = 0.5
class Expert:
    def __init__(self, gain=0.5):
        self.gain = gain
        self.scale = lambda x: x * 2
    def run(self, actions):
        obs = actions.observe()
        tcp = obs.get("robot", {}).get("tcp")
        angle = math.tan(self.gain) + math.radians(3) + self.scale(LIMIT)
        try:
            actions.move_gripper({"width_m": 0.0})
        except ValueError:
            pass
"""


def test_worker_policy_accepts_ordinary_expert_code():
    check_source(EXPERT, "Expert")


@pytest.mark.parametrize("source", [
    "def run(a):\n    return a.__class__\n",
    "def run(a):\n    open('x')\n",
    "def run(a):\n    return '{0.x}'.format(a)\n",
    "from os import path\ndef run(a):\n    pass\n",
    "import math\ndef run(a):\n    return math.nosuch\n",
    "def run(a):\n    with a:\n        pass\n",
])
def test_worker_policy_rejects_escapes_with_reason(source):
    with pytest.raises(ExpertPolicyError, match="line"):
        check_source(source)


def test_hdf5_arrays_keep_xyzw_and_n_plus_one_states():
    ref = lambda k: {"kind": k, "sha256": "a" * 64, "media_type": "application/json"}
    setup = {"episode_id": "ep1", "task_id": "t1", "task_revision": "r1", "attempt_id": "a1",
             **{k: ref(k) for k in ("request_ref", "original_request_ref", "task_ref", "scene_ref", "runtime_ref", "expert_ref")},
             "source_sha256": {"m": "b" * 64}, "asset_refs": [], "control_dt_s": 0.01, "node_ids": ["robot", "plate"]}

    def state(i, q):
        entity = lambda joints: {"availability": "available", "asset_id": "x", "position_m": [0, 0, i * 0.1],
                                 "quaternion_xyzw": [0, 0, 0, 1], "linear_velocity_m_s": [0, 0, 0],
                                 "angular_velocity_rad_s": [0, 0, 0], "joints": joints, "links": {}}
        return {"control_step": i, "time_s": i * 0.01, "units": UNITS, "frame_id": "world",
                "entities": {"robot": entity([{"name": "j1", "type": "revolute", "unit": "rad", "q": q, "dq": 0.0}]),
                             "plate": entity([])},
                "robot": {"joint_names": ["j1"], "q": [q], "dq": [0.0]},
                "contacts": {"availability": "available", "rows": [], "coverage": "complete",
                             "sampling": "cycle_end_snapshot", "control_step": i,
                             "force_availability": "unavailable", "normal_availability": "unavailable"}}
    recorder = EpisodeRecorder(setup, max_steps=10, max_bytes=10 ** 6)
    recorder.initial(state(0, 0.0))
    recorder.input({"j1": 0.1}, {"j1": 0.1}, {"call_id": "c1"})
    recorder.measured(state(1, 0.1))
    arrays = episode_arrays(recorder.finish("completed", "EXPERT_RETURNED"))
    data = arrays["datasets"]
    assert len(data["state/control_step"][0]) == 2 and len(data["action/interval_steps"][0]) == 1
    assert data["state/entities/plate/pose_xyzw"][0][1] == [0, 0, 0.1, 0, 0, 0, 1]
    assert arrays["attrs"]["quaternion_order"] == "xyzw" and arrays["attrs"]["robot_id"] == "robot"

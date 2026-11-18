"""B regressions against #17/#18: self-check, worker, outcomes and HDF5.

Scripted execution facts are not evidence of model or physical task success.
"""

from copy import deepcopy
import json

import pytest

from b_contract_fixtures import recorder, state
from datafarm.cap.library import SkillLibrary
from datafarm.cap.search import SearchConfig, SearchRunner
from datafarm.cap.self_check import run_check
from datafarm.cap.state_feedback import feedback
from datafarm.adapters.agent_harness import scene_execution
from datafarm.adapters.export.hdf5_episode import episode_arrays, write_episode_hdf5
from datafarm.ir.execution_outcome import classify_execution, classify_result

CHECK = """def check(episode):
    x = episode['privileged_state']['final']['entities']['object.a']['position_m'][0]
    return {'success': x == 0, 'score': 1.0 if x == 0 else 0.0, 'reason': 'public measured x'}
"""


def test_search_scores_shared_state_not_engine_verdict_or_hidden_identity(
    tmp_path, monkeypatch
):
    from datafarm.judge import execution, contract_binding

    def forbidden_judge(*args, **kwargs):
        pytest.fail("CaP search must not call the frozen Judge")

    monkeypatch.setattr(execution, "run_source", forbidden_judge)
    monkeypatch.setattr(contract_binding, "evaluate_episode", forbidden_judge)

    class Engine:
        def evaluate(self, code, seeds, phase):
            episodes = []
            for seed in seeds:
                final = state(1)
                final["entities"]["object.a"]["position_m"][0] = seed
                episodes.append(
                    {
                        "episode_id": "episode." + str(seed),
                        "seed": seed,
                        "phase": phase,
                        "execution": {
                            "expert_error": False,
                            "execution_status": "completed",
                        },
                        "trace": [],
                        "privileged_state": feedback(
                            [{"before": state(0), "after": final}]
                        ),
                        "success": seed != 0,
                        "score": 0 if seed == 0 else 1,
                        "evaluation": {"private_judge": "must not be forwarded"},
                    }
                )
            return {"episodes": episodes}

    seen = []

    def checker(source, episodes):
        seen.extend(deepcopy(episodes))
        for episode in episodes:
            assert set(episode) == {"phase", "execution", "trace", "privileged_state"}
        return run_check(source, episodes)

    runner = SearchRunner(
        None,
        Engine(),
        SkillLibrary(tmp_path / "skills"),
        SearchConfig(observation_mode="privileged_state"),
        checker=checker,
    )
    runner.adopt_self_check(CHECK, "actor")
    result = runner.execute("scripted", [0, 1], "debug")
    assert [e["success"] for e in result["episodes"]] == [True, False]
    assert (
        result["success_basis"] == "author_self_check" and result["success_rate"] == 0.5
    )
    assert seen and all("evaluation" not in e and "seed" not in e for e in seen)


@pytest.mark.parametrize("replacement", [CHECK, "def check(e):\n    return True\n"])
def test_actor_or_verifier_cannot_replace_frozen_self_check(tmp_path, replacement):
    runner = SearchRunner(None, None, SkillLibrary(tmp_path / "skills"))
    runner.adopt_self_check(CHECK, "actor")
    for role in ("actor", "verifier"):
        with pytest.raises(ValueError, match="frozen"):
            runner.adopt_self_check(replacement, role)
    assert runner.self_check == CHECK


def test_expert_fault_overrides_positive_self_check_without_becoming_judge_success(
    tmp_path,
):
    class Engine:
        def evaluate(self, code, seeds, phase):
            return {
                "episodes": [
                    {
                        "episode_id": "fault.one",
                        "seed": 0,
                        "phase": phase,
                        "execution": {
                            "expert_error": True,
                            "reason_code": "EXPERT_EXCEPTION",
                        },
                        "trace": [],
                    }
                ]
            }

    runner = SearchRunner(None, Engine(), SkillLibrary(tmp_path / "skills"))
    runner.adopt_self_check("def check(e):\n    return True\n", "actor")
    result = runner.execute("bad", [0], "debug")
    assert result["episodes"][0]["success"] is False and result["score"] == 0.0
    assert "EXPERT_EXCEPTION" in result["episodes"][0]["self_check"]["reason"]


def test_current_worker_executes_defaults_lambdas_math_and_print_without_ipc_corruption(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        scene_execution, "probe_available", lambda _: (True, "protocol-only fixture")
    )
    monkeypatch.setattr(scene_execution, "wrap", lambda spec, argv: argv)
    source = """import math
GAIN = 0.5
class Expert:
    def __init__(self, gain=0.5):
        self.gain = gain
        self.scale = lambda x: x * 2
    def run(self, actions):
        observation = actions.observe()
        print('expert diagnostic, not an IPC message')
        result = actions.move_gripper({'width_m': self.scale(GAIN) / 10})
        if result.get('status') != 'blocked':
            raise ValueError('unexpected status')
        actions.move_tcp({'x': math.radians(0) + observation['robot']['q'][0]})
"""

    class Actions:
        def __init__(self):
            self.calls = []

        def observe(self):
            self.calls.append("observe")
            return {"robot": {"q": [0.25]}}

        def move_gripper(self, request):
            self.calls.append(("gripper", request))
            return {"status": "blocked"}

        def move_tcp(self, request):
            self.calls.append(("tcp", request))
            return {"status": "completed"}

    path = tmp_path / "expert.py"
    path.write_text(source, encoding="utf-8")
    actions = Actions()
    scene_execution.run_expert(
        path, actions, class_name="Expert", timeout_s=5, max_calls=3
    )
    assert actions.calls == [
        "observe",
        ("gripper", {"width_m": 0.1}),
        ("tcp", {"x": 0.25}),
    ]


def test_current_worker_expert_exception_is_short_and_does_not_leak_host_details(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        scene_execution, "probe_available", lambda _: (True, "protocol-only fixture")
    )
    monkeypatch.setattr(scene_execution, "wrap", lambda spec, argv: argv)
    path = tmp_path / "expert.py"
    path.write_text(
        "def run(actions):\n    raise ValueError('private diagnostic must not leak')\n"
    )
    with pytest.raises(
        scene_execution.ExpertRaised, match="exception: ValueError"
    ) as error:
        scene_execution.run_expert(path, None, timeout_s=5, max_calls=1)
    assert "private diagnostic" not in str(error.value)


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("EXPERT_EXCEPTION", "expert_error"),
        ("CALL_BUDGET", "expert_error"),
        ("HOST_REQUALIFICATION_REQUIRED", "sim_failure"),
        ("INITIALIZATION_OR_STATE_ERROR", "sim_failure"),
    ],
)
def test_current_execution_classification_preserves_responsibility(reason, expected):
    summary = {
        "episode_ref": {"kind": "episode", "sha256": "a" * 64},
        "execution_status": "error",
        "trace_complete": False,
        "reason_code": reason,
    }
    outcome, success, detail = classify_execution(summary)
    assert outcome == expected and success is (
        False if expected == "expert_error" else None
    )
    assert reason in detail


@pytest.mark.parametrize("success", [None, 0, 1, "true", "false"])
def test_judge_success_is_never_coerced_from_null_or_nonbool(success):
    verdict = {
        "schema_version": "judge-result/1",
        "outcome": "success",
        "success": success,
    }
    assert classify_result(verdict)[0:2] == ("judge_failure", None)


def export_episode():
    initial = state(0)
    initial["entities"]["robot"]["joints"] = [
        {"name": "joint", "type": "revolute", "unit": "rad", "q": 0.0, "dq": 0.0}
    ]
    # Replace only the software fixture before recording: never edit a sealed source Episode.
    from b_contract_fixtures import SETUP
    from datafarm.expert.episode import EpisodeRecorder

    r = EpisodeRecorder(deepcopy(SETUP), max_steps=4, max_bytes=100000)
    r.initial(initial)
    for step in (1, 2):
        r.input(
            {} if step == 1 else {"joint": 0.3},
            {} if step == 1 else {"joint": 0.2},
            {"call_id": "call." + str(step)},
        )
        measured = state(step)
        measured["robot"]["q"] = [0.2 * step]
        measured["entities"]["robot"]["joints"] = [
            {
                "name": "joint",
                "type": "revolute",
                "unit": "rad",
                "q": 0.2 * step,
                "dq": 0.0,
            }
        ]
        measured["entities"]["object.a"]["position_m"][0] = step * 0.1
        r.measured(measured)
    return r.finish("completed", "EXPERT_RETURNED")


def test_hdf5_current_export_is_derived_preserves_action_readback_and_unavailable_sensors(
    tmp_path,
):
    import h5py
    import numpy as np
    from datafarm.kernel.io import sha256_file

    episode = export_episode()
    original = deepcopy(episode)
    arrays = episode_arrays(episode)
    assert arrays["attrs"]["quaternion_order"] == "xyzw"
    output = tmp_path / "episode.h5"
    manifest = write_episode_hdf5(
        episode, output, episode_ref={"kind": "episode", "sha256": "a" * 64}
    )
    assert manifest["sha256"] == sha256_file(output)
    with h5py.File(output, "r") as handle:
        assert handle["state/control_step"][...].tolist() == [0, 1, 2]
        assert handle["action/interval_steps"][...].tolist() == [[0, 1], [1, 2]]
        assert handle["state/entities/object.a/pose_xyzw"].shape == (3, 7)
        assert handle["state/entities/object.a/pose_xyzw"][2].tolist() == [
            0.2,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        ]
        requested = handle["action/robots/robot/requested"][...]
        applied = handle["action/robots/robot/applied"][...]
        assert np.isnan(requested[0, 0]) and np.isnan(applied[0, 0])
        assert requested[1, 0] == 0.3 and applied[1, 0] == 0.2
        assert np.isnan(handle["state/robots/robot/tcp_pose_xyzw"][...]).all()
        assert handle["meta"].attrs["judge_result"] == ""
        assert handle["meta"].attrs["judgement"] == "unverified"
        assert json.loads(handle["contacts/records"][0])["rows"] is None
        assert (
            json.loads(handle["meta"].attrs["provenance"])["original_request_ref"]
            == episode["setup"]["original_request_ref"]
        )
    assert episode == original
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_episode_hdf5(episode, output)
    assert (
        output.read_bytes() == before
        and not output.with_name("episode.h5.partial").exists()
    )


def test_hdf5_partial_episode_keeps_incomplete_trace_and_error_status(tmp_path):
    r = recorder()
    r.input({"joint": 0.2}, {"joint": 0.1}, {"call_id": "failed.step"})
    episode = r.finish("error", "HOST_REQUALIFICATION_REQUIRED")
    arrays = episode_arrays(episode)
    termination = json.loads(arrays["attrs"]["termination"])
    assert (
        termination == episode["termination"] and termination["trace_complete"] is False
    )
    assert len(arrays["datasets"]["state/control_step"][0]) == 1
    assert arrays["attrs"]["judgement"] == "unverified"


@pytest.mark.parametrize("coverage", ["complete", "truncated", "invalid"])
def test_contact_coverage_proof_is_preserved_or_marked_unavailable(coverage):
    from types import SimpleNamespace
    from test_b_episode_recording import Tensor, Api
    from b_contract_fixtures import SETUP
    from datafarm.adapters.sim.scene_measurements import SceneMeasurements

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
            {"node_id": "robot", "asset_id": "robot.asset", "semantic_tag": "robot"},
            {
                "node_id": "object.a",
                "asset_id": "object.asset",
                "semantic_tag": "background",
            },
        ]
    }
    query = lambda nodes: {"rows": [], "coverage": coverage}
    measurements = SceneMeasurements(
        handles,
        graph,
        {"robot.asset": {"joints": []}, "object.asset": {"joints": []}},
        Api(),
        contact_query=query,
    )
    contacts = measurements.snapshot(0, 0.1)["contacts"]
    if coverage == "invalid":
        assert contacts["availability"] == "unavailable" and contacts["rows"] is None
        assert (
            contacts["coverage"] == "unknown"
            and contacts["reason_code"] == "CONTACT_QUERY_UNAVAILABLE"
        )
    else:
        assert (
            contacts["availability"] == "available" and contacts["coverage"] == coverage
        )
        assert (
            contacts["rows"] == [] and contacts["force_availability"] == "unavailable"
        )


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("JOINT_LIMIT", "expert_error"),
        ("HOST_REQUALIFICATION_REQUIRED", "judge_failure"),
    ],
)
def test_judge_interruption_keeps_exact_execution_reason_and_null_success(
    reason, expected
):
    from test_b_judge_episode import data, contract
    from datafarm.judge.episode_input import project_episode
    from datafarm.judge.execution import run_source

    episode = data()
    episode["termination"].update(status="error", reason_code=reason)
    result = run_source(
        "def evaluate(episode, task_contract, scene_metadata):\n    return result({'goal': True}, {}, 'not a task score')",
        project_episode(episode),
        contract(episode),
        {},
    )
    assert result["success"] is None and result["outcome"] == "error"
    assert result["reason_code"] == "EXECUTION_ERROR:" + reason
    assert classify_result(result)[0] == expected


def test_export_cli_reads_source_bound_episode_without_certifying_it(tmp_path):
    import h5py
    from datafarm.entry.episode_export import main
    from datafarm.kernel.store import ArtifactStore
    from datafarm.expert.episode import EpisodeRecorder

    episode = export_episode()
    store = ArtifactStore(tmp_path / "store")
    reference = store.put_document(
        EpisodeRecorder(episode["setup"], max_steps=4, max_bytes=100000).document(
            episode, created_at="2026-11-18T00:00:00Z"
        )
    )
    ref_file = tmp_path / "episode-ref.json"
    ref_file.write_text(
        json.dumps({"episode_ref": reference.to_dict(), "task_success": None})
    )
    output, manifest = tmp_path / "episode.h5", tmp_path / "manifest.json"
    assert (
        main(
            [
                "--store",
                str(tmp_path / "store"),
                "--episode-ref",
                str(ref_file),
                "--output",
                str(output),
                "--manifest",
                str(manifest),
            ]
        )
        == 0
    )
    assert (
        json.loads(manifest.read_text())["episode_id"] == episode["setup"]["episode_id"]
    )
    with h5py.File(output, "r") as handle:
        assert json.loads(handle["meta"].attrs["episode_ref"]) == reference.to_dict()
        assert handle["meta"].attrs["judge_result"] == ""
        assert handle["meta"].attrs["judgement"] == "unverified"


@pytest.mark.parametrize(
    "follows_target,expected", [(False, "blocked"), (True, "completed")]
)
def test_gripper_loop_distinguishes_blocked_from_reached_without_early_stall(
    follows_target, expected
):
    from types import SimpleNamespace
    from test_robot_binding import SOURCE, REF
    from datafarm.simulation.robot_binding import discover_urdf, bind_robot
    from datafarm.simulation.tcp_control import TCPMotionService

    profile = {
        "schema_version": "robot-profile/2",
        "groups": {"arm": ["arm_joint"]},
        "gripper": {
            "master_joint": "master_joint",
            "width_range_m": [0, 0.1],
            "joint_per_m": 1,
            "joint_offset": 0,
            "calibration_ref": REF,
        },
    }
    binding = bind_robot(
        discover_urdf(SOURCE),
        profile,
        ["finger_joint", "master_joint", "arm_joint"],
        ["base", "arm", "master", "finger"],
    )

    # Software host declares one active position drive, as IsaacRobotAPI binds
    # at runtime; this is not evidence of a real actuator or simulator.
    from datafarm.simulation.robot_binding import digest

    binding["position_driven_joints"] = ["master_joint"]
    binding.pop("binding_sha256")
    binding["binding_sha256"] = digest(binding)

    class Host:
        def __init__(self):
            self.control_step = self.step_attempts = 0
            self.step_outcome_unknown = False
            self.width = 0.0
            self.targets = []
            self.held = self.released = False

        def joint_speed_limit(self, name):
            return 0.2

        def acquire(self):
            return True

        def release(self):
            self.released = True

        def observe(self):
            return {
                "joint_names": ["master_joint"],
                "q": [self.width],
                "dq": [0.0],
                "gripper": {"width_m": self.width},
            }

        def begin(self, names):
            return {"master_joint": self.width}

        def step(self, targets, event):
            self.step_attempts += 1
            self.control_step += 1
            self.targets.append(targets["master_joint"])
            if follows_target:
                self.width = targets["master_joint"]
            return self.observe()

        def cancelled(self):
            return False

        def hold(self, names):
            self.held = True

    host = Host()
    # Test the real bounded gripper loop in isolation, not IK/backend qualification.
    service = TCPMotionService.__new__(TCPMotionService)
    service.binding = binding
    service.host = host
    service.profile = SimpleNamespace(
        dt_s=0.1,
        max_linear_speed_m_s=0.2,
        position_tolerance_m=0.001,
        settle_steps=2,
        max_control_steps=30,
        max_total_control_steps=50,
        max_wall_time_s=5,
        max_calls=3,
        joint_speed_limits={"master_joint": 0.2},
    )
    service.calls = service.total_steps = 0
    service.capabilities = lambda: {"set_gripper": True}
    request = {
        "schema_version": "gripper-motion-request/1",
        "width_m": 0.02,
        "speed_m_s": 0.02,
        "tolerance_m": 0.001,
        "settle_steps": 2,
        "max_control_steps": 30,
    }
    result = service.move_gripper(request)
    assert result["status"] == expected and host.released and not host.held
    assert result["step_outcome_unknown"] is False
    assert host.targets[-1] == pytest.approx(0.02)
    if follows_target:
        assert (
            result["width_m"] == pytest.approx(0.02)
            and result["reason_code"] == "SETTLED"
        )
    else:
        assert result["width_m"] == 0.0 and result["reason_code"] == "GRIPPER_BLOCKED"
        assert result["stalled_steps"] == 2 and host.control_step >= 11

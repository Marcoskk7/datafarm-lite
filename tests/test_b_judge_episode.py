"""Judge /1 projection, source-bound freeze and review fail-closed tests.

Author/reviewer are scripted fixtures, not model independence evidence.
"""

from pathlib import Path
from copy import deepcopy
import pytest
from datafarm.judge.protocol import normalize_task, validate_candidate
from datafarm.judge.episode_input import project_episode, evidence_path, validate_input
from datafarm.judge.execution import run_source
import b_contract_fixtures as f
from b_contract_fixtures import planning_fixture


def data():
    r = f.recorder()
    r.input({"joint": 0.1}, {"joint": 0.1}, {})
    r.measured(f.state(1))
    return r.finish("completed", "done")


def contract(p):
    from datafarm.judge.contract_binding import observation_spec

    setup = p["setup"]
    binding = {
        k: setup[k]
        for k in (
            "node_ids",
            "task_id",
            "task_revision",
            "task_ref",
            "scene_ref",
            "request_ref",
            "original_request_ref",
        )
    }
    public = {
        "profile": "datafarm.cap.public-task/1",
        "task_id": setup["task_id"],
        "task_revision": setup["task_revision"],
        "request_id": "request.a",
        "request_sha256": setup["request_ref"]["sha256"],
        "scene_id": "scene.a",
        "name": "Task",
        "instruction": "Reach then return",
        "roles": [{"role": "object", "node_id": "object.a", "frame_id": "world"}],
        "goals": [{"id": "goal", "text": "Reach then return"}],
        "constraints": [],
        "forbidden_shortcuts": [],
        "parameters": [],
        "api": {"move": "measured movement"},
        "program_contract": {
            "entry_point": "run",
            "signature": "run(actions)",
            "return": "None",
            "allowed_imports": ["math"],
            "action_semantics": "bounded measured motion",
            "observation_mode": "privileged_state",
        },
        "observations": [
            {
                "id": "pose",
                "availability": "available",
                "unit": "m",
                "frame_id": "world",
            }
        ],
        "control_budgets": {"max_actions": 20, "max_steps": 200, "max_wall_s": 30},
        "frame_ids": ["world"],
        "joint_facts": [],
    }
    obs = observation_spec(p, public, binding)
    return {
        "schema_version": "task-contract/1",
        "source_binding": binding,
        "observations": obs,
        "conditions": [
            {
                "id": "goal",
                "evidence_fields": [
                    evidence_path(["state_trace", "entities", "object.a", "position_m"])
                ],
            }
        ],
        "thresholds": {},
    }


def test_worker_current_one():
    p = data()
    c = contract(p)
    v = run_source(
        "def evaluate(episode, task_contract, scene_metadata):\n    return result({'goal': all(s['entities']['object.a']['position_m'][0] == 0 for s in episode['state_trace'])}, {}, 'measured')",
        project_episode(p),
        c,
        {},
    )
    assert v["outcome"] == "success", v


def test_missing_sensor_error():
    p = data()
    c = contract(p)
    c["conditions"][0]["evidence_fields"] = [evidence_path(["contacts", "rows"])]
    v = run_source(
        "def evaluate(episode, task_contract, scene_metadata):\n    return result({'goal': True}, {}, 'measured')",
        project_episode(p),
        c,
        {},
    )
    assert v["outcome"] == "error" and v["success"] is None


def test_cross_scene():
    p = data()
    c = contract(p)
    c["source_binding"]["scene_ref"] = {}
    with pytest.raises(ValueError):
        validate_input(project_episode(p), c)


def test_aborted():
    p = data()
    p["termination"]["status"] = "aborted"
    v = run_source(
        "def evaluate(episode, task_contract, scene_metadata):\n    return result({'goal': True}, {}, 'measured')",
        project_episode(p),
        contract(p),
        {},
    )
    assert v["outcome"] == "aborted" and v["success"] is None


def test_request_task_normalization():
    import tempfile
    from datafarm.task.planning import compile_table
    from datafarm.kernel.artifacts import ArtifactRef

    with tempfile.TemporaryDirectory() as d:
        store, _, scene, _, context, proposal = planning_fixture(Path(d))
        table, _ = compile_table(
            store, context, proposal, created_at="2026-11-17T00:00:00Z"
        )
        entry = table["tasks"][0]
        task = store.load(ArtifactRef.from_dict(entry["task_ref"]))
        normal = normalize_task(task)
        assert (
            normal["task_id"] == task["payload"]["task_id"]
            and normal["task_version"] == task["payload"]["task_revision"]
        )
        assert set(r["id"] for r in normal["requirements"]) == set(
            task["payload"]["requirement_ids"]
        )


def test_one_freeze_binding_and_evaluation(tmp_path):
    from datafarm.kernel.artifacts import ArtifactRef
    from datafarm.task.planning import compile_table
    from datafarm.cap.public_task import project_public_task
    from datafarm.judge.contract_binding import (
        source_binding,
        observation_spec,
        bind_contract,
        evaluate_episode,
    )
    from datafarm.judge.protocol import build_input, sha
    from datafarm.judge.compiler import JudgeCompiler
    from datafarm.expert.episode import EpisodeRecorder
    from datafarm.kernel.io import sha256_json

    store, _, scene, cap, context, proposal = planning_fixture(tmp_path)
    graph = store.load(ArtifactRef.from_dict(scene))
    graph["runtime_profile"] = store.put_json(
        "sim.runtime-profile", {"physics_dt_s": 0.1}
    ).to_dict()
    scene = store.put_json("ir.scene-graph", graph).to_dict()
    cap["scene_ref"] = scene
    from datafarm.task.planning import load_scene_group, build_context

    context = build_context(
        context["request"],
        context["request_ref"],
        context["scene_group_id"],
        load_scene_group(store, [scene], [cap]),
    )
    table, _ = compile_table(
        store, context, proposal, created_at="2026-11-17T00:00:00Z"
    )
    entry = table["tasks"][0]
    task = store.load(ArtifactRef.from_dict(entry["task_ref"]))
    binding = source_binding(store, entry["task_ref"], scene)
    public = project_public_task(
        task["payload"],
        entry,
        context["request"],
        context["request_ref"],
        context["members"][0],
    )
    p = data()
    p["setup"].update(binding)
    p["setup"]["runtime_ref"] = store.load(ArtifactRef.from_dict(scene))[
        "runtime_profile"
    ]
    runtime = store.load(ArtifactRef.from_dict(p["setup"]["runtime_ref"]))
    p["setup"]["control_dt_s"] = runtime["physics_dt_s"]
    p["setup"]["expert_ref"] = store.put_bytes(
        "expert", b"def run(actions): pass", media_type="text/x-python"
    ).to_dict()
    # Exemplar covers all actual A scene nodes; node identities are never renamed.
    nodes = binding["node_ids"]
    p["setup"]["node_ids"] = nodes
    for i, state in enumerate(p["state_trace"]):
        state["time_s"] = i * p["setup"]["control_dt_s"]
        state["entities"] = {n: deepcopy(f.reading()) for n in nodes}
    goal_node = nodes[-1]
    obs = observation_spec(p, public, binding)
    field = evidence_path(["state_trace", "entities", goal_node, "position_m"])
    assert field in obs["available_fields"]
    inputs = build_input(task, store.load(ArtifactRef.from_dict(scene)), obs, [])
    ids = [r["id"] for r in inputs["task"]["requirements"]]
    candidate = {
        "source": "def evaluate(episode, task_contract, scene_metadata):\n    return result({r['id']: episode['state_trace'][-1]['entities'][episode['node_ids'][-1]]['position_m'][0] == 0 for r in task_contract['conditions']}, {}, 'measured')",
        "conditions": [
            {
                "id": r,
                "requirement_ids": [r],
                "description": r,
                "measurement": "measured pose",
                "unit": "m",
                "threshold_ids": [],
                "evidence_fields": [field],
            }
            for r in ids
        ],
        "thresholds": [
            {
                "id": k,
                "value": v["value"],
                "unit": v["unit"],
                "origin": "provided",
                "basis": v["basis"],
                "requirement_ids": v["requirement_ids"],
            }
            for k, v in inputs["task"]["provided_thresholds"].items()
        ],
        "required_capabilities": [],
        "missing_inputs": [],
    }
    positive = project_episode(p)
    negative = deepcopy(positive)
    negative["state_trace"][-1]["entities"][goal_node]["position_m"][0] = 1

    class Provider:
        fingerprint = "local-current-one-fixture"
        same_session = False
        blocking_issue = False

        def complete(self, role, payload):
            if role == "judge_author":
                value = deepcopy(candidate)
            else:
                value = {
                    "verdict": "pass",
                    "condition_checks": [
                        {
                            "condition_id": i,
                            "faithful": True,
                            "thresholds_supported": True,
                            "observables_sufficient": True,
                            "reason": "local scripted check",
                        }
                        for i in ids
                    ],
                    "independent_cases": [
                        {
                            "id": "independent.good",
                            "episode_json": __import__("json").dumps(positive),
                            "expected_outcome": "success",
                            "violates": [],
                        },
                        {
                            "id": "independent.bad",
                            "episode_json": __import__("json").dumps(negative),
                            "expected_outcome": "failure",
                            "violates": ids,
                        },
                    ],
                    "issues": [],
                }
            # Explicit scripted fixture; never relabeled production model independence.
            if role == "judge_reviewer" and self.blocking_issue:
                value["issues"] = ["blocking defect"]
            receipt = {
                "sessions": [
                    (
                        "local.author"
                        if role == "judge_author" or self.same_session
                        else "local.reviewer"
                    )
                ],
                "status": "complete",
                "decision_sha256": sha256_json(value),
            }
            return value, {
                "kind": "scripted_fixture",
                "role": role,
                "receipt": receipt,
                "response_sha256": sha256_json(value),
                "events_sha256": "a" * 64,
            }

    result = JudgeCompiler(Provider(), max_candidates=1).build(
        inputs, tmp_path / "judge-work"
    )
    assert result["status"] == "frozen", result
    bundle = Path(result["bundle"])
    anchor = result["freeze_ref"]["sha256"]
    ref = bind_contract(
        store, entry["task_ref"], scene, bundle, anchor, allow_fixture=True
    )
    p["setup"]["task_contract_ref"] = ref.to_dict()
    rec = EpisodeRecorder(p["setup"], max_steps=3, max_bytes=2000000)
    rec.initial(p["state_trace"][0])
    rec.input({"joint": 0.1}, {"joint": 0.1}, {})
    rec.measured(p["state_trace"][1])
    payload = rec.finish("completed", "done")
    ep = store.put_document(rec.document(payload, created_at="2026-11-17T00:00:00Z"))
    with pytest.raises(ValueError):
        bind_contract(store, entry["task_ref"], scene, bundle, anchor)
    receipt, _ = evaluate_episode(
        store, ep.to_dict(), ref.to_dict(), bundle, anchor, allow_fixture=True
    )
    assert receipt["result"]["outcome"] == "success", receipt
    # A changed runtime implementation must never reuse this frozen source version.
    from datafarm.judge.compiler import verify_bundle

    with pytest.raises((ValueError, RuntimeError)):
        evaluate_episode(
            store, ep.to_dict(), {**ref.to_dict(), "sha256": "0" * 64}, bundle, anchor
        )
    from datafarm.judge.contract_binding import verify_contract

    original_authority = store.load(ref)
    changed = deepcopy(original_authority)
    changed["conditions"][0]["description"] = "altered"
    altered = store.put_json("judge-episode-contract", changed)
    with pytest.raises(ValueError, match="portable authority"):
        verify_contract(store, altered.to_dict(), allow_fixture=True)
    changed = deepcopy(original_authority)
    key = next(iter(changed["thresholds"]))
    changed["thresholds"][key]["value"] += 1
    altered = store.put_json("judge-episode-contract", changed)
    with pytest.raises(ValueError, match="portable authority"):
        verify_contract(store, altered.to_dict(), allow_fixture=True)
    for key in ("value", "unit", "basis", "requirement_ids"):
        changed = deepcopy(candidate)
        changed["thresholds"][0][key] = {
            "value": 999,
            "unit": "different",
            "basis": "different",
            "requirement_ids": [ids[-1]],
        }[key]
        assert changed["thresholds"][0][key] != candidate["thresholds"][0][key]
        with pytest.raises(ValueError):
            validate_candidate(changed, inputs)
    changed = deepcopy(task)
    changed["payload"]["demand"] = "changed request semantics"
    with pytest.raises(ValueError):
        build_input(changed, store.load(ArtifactRef.from_dict(scene)), obs, [])
    for flag in ("same_session", "blocking_issue"):
        provider = Provider()
        setattr(provider, flag, True)
        rejected = JudgeCompiler(provider, max_candidates=1).build(
            inputs, tmp_path / flag
        )
        assert rejected["status"] != "frozen", rejected
        assert not (
            tmp_path / flag / "candidate-01" / "frozen" / "freeze.json"
        ).exists()
    # Runtime/dependency updates produce a new identity, never overwrite an old work.
    dependency = tmp_path / "dependency.txt"
    dependency.write_text("first")
    first = JudgeCompiler(Provider(), max_candidates=1).build(
        inputs, tmp_path / "with-dep", {"sensor": dependency}
    )
    dependency.write_text("second")
    with pytest.raises(ValueError, match="changed"):
        JudgeCompiler(Provider(), max_candidates=1).build(
            inputs, tmp_path / "with-dep", {"sensor": dependency}
        )
    second = JudgeCompiler(Provider(), max_candidates=1).build(
        inputs, tmp_path / "with-new-dep", {"sensor": dependency}
    )
    assert (
        first["identity"] != second["identity"]
        and first["freeze_ref"] != second["freeze_ref"]
    )
    with pytest.raises(ValueError, match="dependency changed"):
        verify_bundle(
            first["bundle"], first["freeze_ref"]["sha256"], allow_fixture=True
        )


def test_obsolete_judge_two_and_old_one_rejected():
    from datafarm.judge.protocol import build_input, JudgeError

    with pytest.raises(ValueError):
        normalize_task({"task_id": "legacy", "instruction": "move"})
    p = data()
    c = contract(p)
    ep = project_episode(p)
    for version in ("judge-episode/2", "old"):
        with pytest.raises(ValueError):
            validate_input({**ep, "schema_version": version}, c)
    with pytest.raises(ValueError):
        validate_input(ep, {**c, "schema_version": "task-contract/2"})


def test_nonfinite_and_missing_full_measurement():
    p = data()
    c = contract(p)
    ep = project_episode(p)
    ep["state_trace"][1]["entities"]["object.a"]["position_m"][0] = float("nan")
    with pytest.raises(ValueError):
        validate_input(ep, c)


def test_reasoning_and_source_contract_prompt_kept():
    from datafarm.judge.provider import REVIEWER, RUNTIME_CONTRACT

    assert "For verdict=pass, issues MUST be []" in REVIEWER
    assert "Your sole output is the structured REVIEW object" in REVIEWER
    assert "judge-episode/2" not in RUNTIME_CONTRACT


def test_error_not_task_failure():
    p = data()
    p["termination"]["status"] = "error"
    v = run_source(
        "def evaluate(episode, task_contract, scene_metadata):\n    return result({'goal': True}, {}, 'measured')",
        project_episode(p),
        contract(p),
        {},
    )
    assert v["outcome"] == "error" and v["success"] is None


@pytest.mark.parametrize("status", ["aborted", "failed", "error", "timeout"])
def test_partial_execution_is_unscored(status):
    p = data()
    c = contract(p)
    p["termination"].update(status=status, trace_complete=False)
    p["state_trace"] = p["state_trace"][:1]
    p["contacts"] = p["contacts"][:1]
    ep = project_episode(p)
    source = "def evaluate(episode, task_contract, scene_metadata):\n    return result({'goal': True}, {}, 'not executed')"
    result = run_source(source, ep, c, {})
    assert (
        result["outcome"] == ("aborted" if status == "aborted" else "error")
        and result["success"] is None
    )


@pytest.mark.parametrize("status", ["aborted", "failed", "error", "timeout"])
def test_invalid_source_not_masked_by_interruption(status):
    p = data()
    c = contract(p)
    ep = project_episode(p)
    ep["termination"].update(status=status, reason_code=status)
    ep["scene_ref"]["sha256"] = "0" * 64
    source = "def evaluate(episode, task_contract, scene_metadata):\n    return result({'goal': True}, {}, 'not executed')"
    result = run_source(source, ep, c, {})
    assert result["outcome"] == "error" and result["success"] is None


@pytest.mark.parametrize("field", ["trace_complete", "reason"])
def test_execution_metadata_cannot_fake_completion(field):
    p = data()
    c = contract(p)
    ep = project_episode(p)
    ep["termination"][field] = "true" if field == "trace_complete" else "aborted"
    with pytest.raises(ValueError):
        validate_input(ep, c)

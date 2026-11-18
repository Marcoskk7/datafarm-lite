"""Real worker subprocess IPC with mocked OS wrapper, not containment."""

import json
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
import pytest
from datafarm.adapters.agent_harness import scene_execution, scene_worker
from datafarm.entry import scene_job


class Actions:
    def __init__(self):
        self.calls = []

    def observe(self):
        self.calls.append("observe")
        return {"robot": {"q": [0.25]}}

    def move_tcp(self, arg):
        self.calls.append(("tcp", arg))
        return {"status": "completed"}

    def move_gripper(self, arg):
        self.calls.append(("gripper", arg))
        return {"status": "completed"}


@pytest.fixture
def fixture_wrapper(monkeypatch):
    # Exercise real worker IPC with an explicitly mocked OS wrapper. Not containment.
    monkeypatch.setattr(
        scene_execution, "probe_available", lambda name: (True, "software fixture")
    )
    monkeypatch.setattr(scene_execution, "wrap", lambda spec, cmd: cmd)


@pytest.mark.parametrize("class_name", [None, "Expert"])
def test_actual_worker_ipc(tmp_path, fixture_wrapper, class_name):
    file = tmp_path / "expert.py"
    source = "def run(actions):\n    observation=actions.observe()\n    actions.move_tcp({'x':observation['robot']['q'][0]})\n    actions.move_gripper({'width_m':0.1})\n"
    if class_name:
        source = "class Expert:\n    " + source.replace("\n", "\n    ").replace(
            "def run(actions)", "def run(self,actions)"
        )
    file.write_text(source)
    actions = Actions()
    scene_execution.run_expert(
        file, actions, timeout_s=5, max_calls=3, class_name=class_name
    )
    assert actions.calls == [
        "observe",
        ("tcp", {"x": 0.25}),
        ("gripper", {"width_m": 0.1}),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "def run(actions):\n    open('private')",
        "def run(actions):\n    actions._host",
        "def run(actions):\n    import os",
        "def run(actions):\n    return getattr(actions, '__dict__')",
    ],
)
def test_worker_forbidden(source, tmp_path, fixture_wrapper):
    p = tmp_path / "expert.py"
    p.write_text(source)
    with pytest.raises((ValueError, RuntimeError)):
        scene_execution.run_expert(p, Actions(), timeout_s=2, max_calls=2)


def test_no_automatic_proot_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scene_execution, "probe_available", lambda name: (False, "unavailable")
    )
    p = tmp_path / "expert.py"
    p.write_text("def run(actions):\n    pass")
    with pytest.raises(RuntimeError, match="ISOLATION_UNAVAILABLE"):
        scene_execution.run_expert(p, Actions(), timeout_s=2, max_calls=2)


@pytest.mark.parametrize("mode", ["timeout", "calls"])
def test_worker_budgets(mode, tmp_path, fixture_wrapper):
    p = tmp_path / "expert.py"
    p.write_text(
        "def run(actions):\n    while True:\n        pass"
        if mode == "timeout"
        else "def run(actions):\n    actions.observe()\n    actions.observe()"
    )
    with pytest.raises((TimeoutError, RuntimeError)):
        scene_execution.run_expert(
            p, Actions(), timeout_s=0.1 if mode == "timeout" else 2, max_calls=1
        )


@pytest.mark.parametrize("timeout", [True, 0, float("nan"), float("inf")])
def test_budget_validation(timeout, tmp_path, fixture_wrapper):
    p = tmp_path / "expert.py"
    p.write_text("def run(actions):\n    pass")
    with pytest.raises(ValueError):
        scene_execution.run_expert(p, Actions(), timeout_s=timeout, max_calls=2)


def test_job_binds_source_and_output(tmp_path, monkeypatch):
    p = tmp_path / "expert.py"
    p.write_text("def run(actions):\n    actions.observe()")
    ref = {"kind": "expert", "sha256": "a" * 64, "media_type": "text/x-python"}
    seen = []

    def factory(config, seed):
        seen.append(seed)
        return {
            "store": SimpleNamespace(load_bytes=lambda r: p.read_bytes()),
            "expert_ref": ref,
            "expert_budget": {"timeout_s": 2, "max_calls": 2},
        }

    def record(**kw):
        kw["expert_executor"](Actions(), p.read_bytes())
        return {"episode_ref": {"sha256": "b" * 64}, "execution_status": "completed"}

    monkeypatch.setattr(scene_job, "record_scene_episode", record)
    monkeypatch.setattr(scene_job, "run_expert", lambda *a, **kw: seen.append("expert"))
    value = scene_job.execute_job(
        {"family": "family.a"}, factory, p, 3, "attempt.a", tmp_path / "result"
    )
    assert seen == [3, "expert"] and value["episode_ref"]
    assert json.loads((tmp_path / "result" / "initial-state.json").read_text()) == {
        "valid": True,
        "family": "family.a",
    }
    with pytest.raises(ValueError):
        scene_job.execute_job(
            {"family": "family.a"}, factory, p, 3, "attempt.a", tmp_path / "result"
        )


def test_job_source_mismatch_no_execution(tmp_path, monkeypatch):
    p = tmp_path / "expert.py"
    p.write_text("def run(actions):\n    pass")
    factory = lambda config, seed: {
        "store": SimpleNamespace(load_bytes=lambda ref: b"different"),
        "expert_ref": {
            "kind": "expert",
            "sha256": "a" * 64,
            "media_type": "text/x-python",
        },
    }
    with pytest.raises(ValueError, match="snapshot"):
        scene_job.execute_job(
            {"family": "family.a"}, factory, p, 3, "attempt.a", tmp_path / "result"
        )
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize(
    "budget",
    [
        {"timeout_s": "2", "max_calls": 2},
        {"timeout_s": float("nan"), "max_calls": 2},
        {"timeout_s": 2, "max_calls": True},
        {"timeout_s": 2, "max_calls": 0},
    ],
)
def test_job_bad_budget_before_record(tmp_path, monkeypatch, budget):
    p = tmp_path / "expert.py"
    p.write_text("def run(actions): pass")
    ref = {"kind": "expert", "sha256": "a" * 64, "media_type": "text/x-python"}
    factory = lambda config, seed: {
        "store": SimpleNamespace(load_bytes=lambda _: p.read_bytes()),
        "expert_ref": ref,
        "expert_budget": budget,
    }
    monkeypatch.setattr(
        scene_job,
        "record_scene_episode",
        lambda **kw: pytest.fail("invalid budget must not reach recorder"),
    )
    with pytest.raises(ValueError):
        scene_job.execute_job(
            {"family": "family.a"}, factory, p, 3, "attempt.a", tmp_path / "result"
        )
    assert not (tmp_path / "result").exists()


def test_slow_host_response_timeout(tmp_path, fixture_wrapper):
    import time

    p = tmp_path / "expert.py"
    p.write_text("def run(actions):\n    actions.observe()")

    class Slow(Actions):
        def observe(self):
            time.sleep(0.15)
            return {}

    with pytest.raises(TimeoutError):
        scene_execution.run_expert(p, Slow(), timeout_s=0.1, max_calls=1)

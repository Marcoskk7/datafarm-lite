"""Weak PRoot runtime validation; real PRoot cases require its executable."""

import json
import os
import sys
import sysconfig
import time
import subprocess
import shutil
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
import pytest
from datafarm.adapters.agent_harness.scene_execution import run_expert
from datafarm.adapters.sandbox.scene_proot import (
    validate_runtime,
    expert_command,
    runtime_record,
)
from datafarm.entry import scene_job
from datafarm.expert.adapter import validate_proot_rollout
from datafarm.expert.util import ConfigError
from datafarm.expert.rollout import build_job_script, AttemptSpec

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="PRoot runtime is Linux-only"
)
REAL_PROOT = pytest.mark.skipif(
    shutil.which("proot") is None, reason="optional PRoot executable not installed"
)


def runtime():
    # Without PRoot, python is only an executable stand-in for configuration tests.
    # REAL_PROOT cases never use it as an isolation backend.
    return {
        "mode": "proot",
        "acknowledge_weak_isolation": True,
        "executable": shutil.which("proot") or str(Path(sys.executable).resolve()),
        "python": str(Path(sys.executable).resolve()),
        "stdlib": sysconfig.get_path("stdlib"),
    }


class Actions:
    def __init__(self):
        self.calls = []

    def observe(self):
        self.calls.append("observe")
        return {"robot": {"q": [0.25]}}

    def move_tcp(self, x):
        self.calls.append(("tcp", x))
        return {"status": "completed"}

    def move_gripper(self, x):
        self.calls.append(("gripper", x))
        return {"status": "completed"}


@pytest.mark.parametrize("cls", [None, "Expert"])
@REAL_PROOT
def test_real_proot_stream(tmp_path, cls, monkeypatch):
    monkeypatch.setattr(
        "datafarm.adapters.agent_harness.scene_execution.probe_available",
        lambda _: (False, "bwrap unavailable"),
    )
    source = "def run(actions):\n    s=actions.observe()\n    actions.move_tcp({'x':s['robot']['q'][0]})\n    actions.move_gripper({'width_m':.1})\n"
    if cls:
        source = "class Expert:\n    " + source.replace("\n", "\n    ").replace(
            "def run(actions)", "def run(self,actions)"
        )
    p = tmp_path / "expert.py"
    p.write_text(source)
    a = Actions()
    run_expert(
        p,
        a,
        timeout_s=8,
        max_calls=3,
        runtime=runtime(),
        class_name=cls,
        expected_source=p.read_bytes(),
    )
    assert a.calls == ["observe", ("tcp", {"x": 0.25}), ("gripper", {"width_m": 0.1})]
    assert p.read_text() == source


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"acknowledge_weak_isolation": False},
        {"require_readonly": True},
        {"mode": "none"},
        {"executable": "relative"},
        {"python": "/missing/python"},
        {"stdlib": "/missing/lib"},
        {"run_as": {"uid": 0, "gid": 0}},
        {"run_as": {"uid": True, "gid": 1000}},
        {
            "run_as": {
                "uid": (os.getuid() or 1000) + 1,
                "gid": (os.getgid() or 1000) + 1,
            }
        },
    ],
)
def test_configuration_rejected(change):
    cfg = runtime()
    cfg.update(change)
    if change == {}:
        cfg.pop("acknowledge_weak_isolation")
    with pytest.raises((ValueError, OSError)):
        validate_runtime(cfg)


@pytest.mark.parametrize(
    "source",
    [
        "def run(actions):\n    open('/private')",
        "def run(actions):\n    actions._host",
        "def run(actions):\n    import os",
        "def run(actions):\n    raise ValueError('error')",
    ],
)
@REAL_PROOT
def test_expert_rejection(tmp_path, source):
    p = tmp_path / "expert.py"
    p.write_text(source)
    with pytest.raises((ValueError, RuntimeError)):
        run_expert(p, Actions(), timeout_s=8, max_calls=2, runtime=runtime())


@REAL_PROOT
def test_budget_and_eof(tmp_path):
    p = tmp_path / "expert.py"
    p.write_text("def run(actions):\n    actions.observe()\n    actions.observe()")
    with pytest.raises(RuntimeError, match="REQUEST_REJECTED"):
        run_expert(p, Actions(), timeout_s=8, max_calls=1, runtime=runtime())
    p.write_text("def run(actions):\n    while True:\n        pass")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run_expert(p, Actions(), timeout_s=1, max_calls=1, runtime=runtime())
    assert time.monotonic() - started < 8
    p.write_text("def run(actions): pass")
    with pytest.raises(ValueError, match="snapshot"):
        run_expert(
            p,
            Actions(),
            timeout_s=8,
            max_calls=1,
            runtime=runtime(),
            expected_source=b"different",
        )
    with pytest.raises(ValueError):
        run_expert(p, Actions(), timeout_s=901, max_calls=1, runtime=runtime())


@REAL_PROOT
def test_snapshot_change_detected(tmp_path):
    from datafarm.adapters.agent_harness import scene_worker

    with pytest.raises(RuntimeError, match="SNAPSHOT_CHANGED"):
        with expert_command(
            runtime(), b"def run(actions): pass", scene_worker.__file__, timeout_s=5
        ) as argv:
            config = json.loads(Path(argv[-1]).read_text())
            target = (
                Path(config["argv"][config["argv"].index("-r") + 1]) / "work/expert.py"
            )
            target.chmod(0o644)
            target.write_text("changed")


def test_metadata_no_host_paths():
    r = runtime_record(runtime())
    assert r["security_boundary"] is False and r["guest_user"] == "nonroot"
    assert not any("/" in str(v) for v in r.values())


def trusted_raw():
    return {
        "isolation": {"rollout": {"mode": "proot", "acknowledge_weak_isolation": True}},
        "verifier": {
            "judge_bundle": {"directory": "/trusted/judge", "sha256": "f" * 64}
        },
        "rollout": {
            "trusted_scene": {
                "entry": "datafarm.entry.scene_job",
                "configuration_sha256": "a" * 64,
                "factory_sha256": "b" * 64,
            },
            "sim": {
                "argv": [
                    "/usr/bin/python3",
                    "-m",
                    "datafarm.entry.scene_job",
                    "--factory",
                    "/trusted/factory.py",
                    "--factory-sha256",
                    "b" * 64,
                    "--config",
                    "/trusted/config.json",
                    "--config-sha256",
                    "a" * 64,
                    "--expert",
                    "{expert_file}:Expert",
                    "--seed",
                    "{seed}",
                    "--attempt-id",
                    "{attempt_id}",
                    "--output",
                    "{run_dir}/native",
                ],
                "requires_output": "native/episode-ref.json",
            },
            "verdict": {
                "schema": "judge-result/1",
                "file": "verdict.json",
                "success": "success",
            },
        },
    }


@pytest.mark.parametrize(
    "mutation",
    ["ack", "fallback", "command", "factory_pin", "config_pin", "duplicate", "judge"],
)
def test_outer_rejects_arbitrary_proot_job(mutation):
    raw = trusted_raw()
    if mutation == "ack":
        raw["isolation"]["rollout"]["acknowledge_weak_isolation"] = False
    if mutation == "fallback":
        raw["isolation"]["rollout"]["fallback"] = "none"
    if mutation == "command":
        raw["rollout"]["sim"]["argv"][2] = "untrusted.module"
    if mutation == "factory_pin":
        raw["rollout"]["sim"]["argv"][6] = "c" * 64
    if mutation == "config_pin":
        raw["rollout"]["sim"]["argv"][10] = "c" * 64
    if mutation == "duplicate":
        raw["rollout"]["sim"]["argv"][7] = "--factory"
    if mutation == "judge":
        raw["verifier"] = {}
    with pytest.raises(ConfigError):
        validate_proot_rollout(raw)


def test_outer_job_labels_weak(tmp_path):
    raw = trusted_raw()
    validate_proot_rollout(raw)

    class Adapter:
        def __init__(self):
            self.raw = raw
            self.rollout = raw["rollout"]

        def isolation(self, name):
            return raw["isolation"][name]

        def shell_paths(self, p):
            return p

        def x(self, v, **kw):
            kw.pop("keep_env", None)
            return (
                [s.format(**kw) for s in v] if isinstance(v, list) else v.format(**kw)
            )

    spec = AttemptSpec("attempt.a", "dev", 1, "Expert", tmp_path, "job.a")
    script = build_job_script(Adapter(), spec, masked=["/trusted/judge"])
    assert "XH_BACKEND=proot" in script and "ISO=weak" in script
    assert "bwrap --" not in script
    scriptfile = tmp_path / "job.sh"
    scriptfile.write_text(script)
    subprocess.run(["bash", "-n", str(scriptfile)], check=True)


def test_job_freeze_checks_before_after(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "datafarm.adapters.agent_harness.scene_execution.probe_available",
        lambda _: (True, "protocol fixture"),
    )
    monkeypatch.setattr(
        "datafarm.adapters.agent_harness.scene_execution.wrap", lambda spec, cmd: cmd
    )
    p = tmp_path / "expert.py"
    p.write_text("def run(actions):\n    actions.observe()")
    checks = []
    ref = {"kind": "expert", "sha256": "a" * 64, "media_type": "text/x-python"}

    def factory(config, seed):
        return {
            "store": SimpleNamespace(load_bytes=lambda _: p.read_bytes()),
            "expert_ref": ref,
            "task_ref": {},
            "scene_ref": {},
            "task_contract_ref": {},
            "expert_budget": {"timeout_s": 8, "max_calls": 1},
        }

    monkeypatch.setattr(
        "datafarm.judge.contract_binding.verify_contract",
        lambda *a, **k: checks.append("freeze"),
    )

    def record(**kw):
        assert "execution_runtime" not in kw
        a = Actions()
        kw["expert_executor"](a, p.read_bytes())
        assert a.calls == ["observe"]
        return {"episode_ref": {"sha256": "b" * 64}, "execution_status": "completed"}

    monkeypatch.setattr(scene_job, "record_scene_episode", record)
    cfg = {
        "family": "family.a",
        "judge_freeze": {"directory": "/trusted/freeze", "sha256": "f" * 64},
    }
    scene_job.execute_job(cfg, factory, p, 1, "attempt.a", tmp_path / "out")
    assert len(checks) == 4
    assert json.loads((tmp_path / "out/initial-state.json").read_text())["valid"]


def test_changed_freeze_no_accepted_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "datafarm.adapters.agent_harness.scene_execution.probe_available",
        lambda _: (True, "protocol fixture"),
    )
    monkeypatch.setattr(
        "datafarm.adapters.agent_harness.scene_execution.wrap", lambda spec, cmd: cmd
    )
    p = tmp_path / "expert.py"
    p.write_text("def run(actions): pass")
    ref = {"kind": "expert", "sha256": "a" * 64, "media_type": "text/x-python"}

    def factory(config, seed):
        return {
            "store": SimpleNamespace(load_bytes=lambda _: p.read_bytes()),
            "expert_ref": ref,
            "task_ref": {},
            "scene_ref": {},
            "task_contract_ref": {},
            "expert_budget": {"timeout_s": 8, "max_calls": 1},
        }

    calls = []

    def verify(*a, **kw):
        calls.append(1)
        if len(calls) > 2:
            raise ValueError("Frozen judge dependency changed")

    monkeypatch.setattr("datafarm.judge.contract_binding.verify_contract", verify)

    def record(**kw):
        kw["expert_executor"](Actions(), p.read_bytes())
        return {"episode_ref": {}}

    monkeypatch.setattr(scene_job, "record_scene_episode", record)
    cfg = {
        "family": "family.a",
        "judge_freeze": {"directory": "/trusted/freeze", "sha256": "f" * 64},
    }
    with pytest.raises(ValueError, match="Frozen"):
        scene_job.execute_job(cfg, factory, p, 1, "attempt.a", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_nonreading_child_reply_deadline(tmp_path, monkeypatch):
    import subprocess, time

    p = tmp_path / "expert.py"
    p.write_text("def run(actions): pass")
    child = tmp_path / "nonreading.py"
    child.write_text(
        'import sys,time\nsys.stdout.write(\'{"method":"observe","argument":null}\\n\');sys.stdout.flush();time.sleep(20)'
    )
    from datafarm.adapters.agent_harness import scene_execution as execution

    monkeypatch.setattr(
        execution,
        "probe_available",
        lambda _: (True, "explicit protocol-only subprocess fixture"),
    )
    monkeypatch.setattr(
        execution, "wrap", lambda spec, cmd: [sys.executable, str(child)]
    )

    class Large(Actions):
        def observe(self):
            return {"data": "x" * 500000}

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run_expert(p, Large(), timeout_s=0.5, max_calls=1)
    assert time.monotonic() - started < 5


def test_scene_cli_config_digest_required(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"expert_runtime": runtime()}))
    factory = tmp_path / "factory.py"
    factory.write_text(
        'raise RuntimeError("must not execute before config verification")'
    )
    from datafarm.kernel.io import sha256_file

    with pytest.raises(SystemExit) as error:
        scene_job.main(
            [
                "--config",
                str(p),
                "--factory",
                str(factory),
                "--factory-sha256",
                sha256_file(factory),
                "--expert",
                "none",
                "--seed",
                "1",
                "--attempt-id",
                "attempt.a",
                "--output",
                str(tmp_path / "out"),
            ]
        )
    assert error.value.code == 2

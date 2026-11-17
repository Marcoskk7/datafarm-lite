"""scene-author sandbox: the per-round spec, single wrapping, fail-closed selection, the explicit opt-out, and a
real proot round (skipped when proot is not installed)."""
import json
import os
from pathlib import Path
import shutil
import stat
import sys

import pytest

from datafarm.adapters.agent_harness.backends import make_agent_backend
from datafarm.adapters.agent_harness.call_log import read_calls
from datafarm.entry import scene_author_cli, scene_author_sandbox
from datafarm.entry.scene_author_cli import AgentSessions, load_config
from datafarm.entry.scene_author_sandbox import REPO_READABLE, AuthoringSandbox, sandbox_config
from datafarm.expert.util import ConfigError
from datafarm.scene.authoring import LAYOUT_FILE

# The sandbox adapter package may be absent from an installed/older tree (the CLI then refuses with a
# ConfigError); skip this module rather than failing collection with an ImportError/AttributeError.
sandbox_pkg = pytest.importorskip("datafarm.adapters.sandbox")
_missing = [n for n in ("SandboxSpec", "ProbeResult", "IsolationUnavailable", "run_probe", "registry")
            if not hasattr(sandbox_pkg, n)]
if _missing:
    pytest.skip(f"datafarm.adapters.sandbox lacks {_missing} (the sandbox refactor is not installed)",
                allow_module_level=True)
IsolationUnavailable = sandbox_pkg.IsolationUnavailable
ProbeResult = sandbox_pkg.ProbeResult
registry = sandbox_pkg.registry

ROOT = Path(__file__).resolve().parents[1]
PROOT = os.environ.get("DATAFARM_PROOT_BIN") or shutil.which("proot")
BASE_PYTHON = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def proot_only(mode: str) -> tuple[bool, str]:
    return (mode == "proot", "stub: only proot")


def under(path: str, roots) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def make_sandbox(tmp_path, **cfg):
    work = tmp_path / "work"
    (work / "workspace").mkdir(parents=True)
    return AuthoringSandbox(sandbox_config({"proot": "/opt/stub/proot", **cfg}), repo=ROOT, work=work,
                            hidden=["/data/simready-cache", "/data/robot"], home="/home/u",
                            available=proot_only), work


def test_round_spec_reads_only_the_allowed_paths_and_hides_the_rest(tmp_path):
    sandbox, work = make_sandbox(tmp_path, python=BASE_PYTHON)
    workspace = work / "workspace"
    spec, paths = sandbox.spec(1, workspace, "/opt/agent/bin/claude")

    assert spec.mode == "proot" and spec.base == "allowlist" and spec.ro_soft  # proot, no run_as
    snapshot = str(work / "sandbox" / "repo")
    assert spec.ro == (snapshot, str(Path(BASE_PYTHON).parents[1]), "/opt/agent/bin/claude")
    assert spec.rw == (str(workspace), str(work / "sandbox" / "rounds" / "001" / "home"))
    assert spec.binds == () and spec.cwd == str(workspace)
    copied = sorted(p.relative_to(snapshot).as_posix() for p in Path(snapshot).rglob("*") if p.is_file())
    assert {"docs/ir/request.md", "docs/ir/scene-layout.md", "tools/simready.py",
            "configs/scene-roles.json"} <= set(copied)
    assert all(any(c == r or c.startswith(r + "/") for r in REPO_READABLE) for c in copied)
    assert not any(c.startswith(("examples", "runs", "verification", ".git")) for c in copied)
    mounted = list(spec.ro) + list(spec.rw) + list(spec.system_ro)
    for hidden in [ROOT, ROOT / "examples" / "scene", ROOT / "runs", ROOT / "verification", ROOT / ".git",
                   work / "sessions", work / "attempts", work / "prepared", Path("/data/simready-cache"),
                   Path("/data/robot"), Path("/home/u/.claude")]:
        assert not under(str(hidden), mounted), hidden
    for name in ("examples", "runs", "verification", ".git", "prompts"):
        assert str(ROOT / name) in paths["masked"]
    assert "/home/u/.claude" in paths["masked"] and "/home/u/.ssh" in paths["masked"]
    assert paths["writable"][0] == str(workspace)  # the probe checks the workspace is writable
    assert "/data/simready-cache" in paths["masked"] and "/home/u/.claude.json" in paths["masked"]
    assert sandbox.simready_command == f"{BASE_PYTHON} {snapshot}/tools/simready.py"

    _, second = sandbox.spec(2, workspace, "/opt/agent/bin/claude")
    assert str(work / "sandbox" / "rounds" / "001") in second["masked"]


def test_cache_and_robot_package_are_visible_read_only_and_not_masked(tmp_path):
    cache, robot = tmp_path / "cache", tmp_path / "robot"
    cache.mkdir()
    robot.mkdir()
    work = tmp_path / "work"
    (work / "workspace").mkdir(parents=True)
    sandbox = AuthoringSandbox(sandbox_config({"python": BASE_PYTHON}), repo=ROOT, work=work, home="/home/u",
                               cache=cache, robot_package=robot, available=proot_only)
    spec, paths = sandbox.spec(1, work / "workspace", "/opt/agent/bin/claude")
    # the role prompt tells the agent to run simready.py and to trust host/asset-records.json: both must be visible
    assert str(cache) in spec.ro and str(robot) in spec.ro
    assert str(cache) not in paths["masked"] and str(robot) not in paths["masked"]
    assert str(cache) in paths["readonly"] and str(robot) in paths["readonly"]
    assert str(ROOT / "examples") in paths["masked"]  # the repo itself stays hidden


def test_missing_sandbox_package_is_a_config_error_and_none_still_opts_out(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scene_author_sandbox, "SANDBOX_IMPORT_ERROR",
                        ImportError("No module named 'datafarm.adapters.sandbox'"))
    work = tmp_path / "work"
    with pytest.raises(ConfigError, match="datafarm.adapters.sandbox is unavailable"):
        AuthoringSandbox(sandbox_config({}), repo=ROOT, work=work)
    # an explicit opt-out must keep working without the adapter package
    assert AuthoringSandbox(sandbox_config({"mode": "none"}), repo=ROOT, work=work).mode == "none"

    config = tmp_path / "agent.json"
    config.write_text(json.dumps({"schema": "datafarm.scene-author/1", "agent": {"backend": "fake", "plan": "x"}}))
    code = scene_author_cli.main(["--request", str(ROOT / "examples/request/plate-drawer-airbot.yaml"),
                                  "--work", str(work), "--cache", str(tmp_path), "--robot-package", str(tmp_path),
                                  "--agent-config", str(config), "--max-rounds", "1"])
    err = capsys.readouterr().err
    assert code == 2 and "ConfigError" in err and "datafarm.adapters.sandbox is unavailable" in err
    assert "ImportError" not in err
    assert not (work / "sessions").exists()


def test_config_validation_and_defaults(tmp_path):
    assert sandbox_config(None)["mode"] == "bwrap" and sandbox_config(None)["fallback"] == ["proot"]
    for bad in ({"mode": "unshare"}, {"fallback": ["unshare"]}, {"surprise": 1}, {"extra_ro": ["rel"]},
                {"python": "python3"}):
        with pytest.raises(ConfigError):
            sandbox_config(bad)
    template = json.loads((ROOT / "configs" / "scene-author.template.json").read_text())
    path = tmp_path / "a.json"
    path.write_text(json.dumps({**template, "price_table": None}))
    config, _ = load_config(path)
    assert config["sandbox"]["mode"] == "bwrap" and config["sandbox"]["fallback"] == ["proot"]
    path.write_text(json.dumps({**template, "price_table": None, "sandbx": {}}))
    with pytest.raises(ValueError, match="unknown keys"):
        load_config(path)


def test_no_backend_fails_closed_with_a_named_error(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    with pytest.raises(IsolationUnavailable, match="no usable isolation"):
        AuthoringSandbox(sandbox_config({}), repo=ROOT, work=work, available=lambda m: (False, "absent"))

    monkeypatch.setattr(registry, "probe_available", lambda mode, spec=None: (False, "absent"))
    config = tmp_path / "agent.json"
    config.write_text(json.dumps({"schema": "datafarm.scene-author/1", "agent": {"backend": "fake", "plan": "x"}}))
    code = scene_author_cli.main(["--request", str(ROOT / "examples/request/plate-drawer-airbot.yaml"),
                                  "--work", str(work), "--cache", str(tmp_path), "--robot-package", str(tmp_path),
                                  "--agent-config", str(config), "--max-rounds", "1"])
    err = capsys.readouterr().err
    assert code == 2 and "IsolationUnavailable" in err and '"mode": "none"' in err
    assert not (work / "sessions").exists()


def fake_sessions(tmp_path, sandbox, work, plan_steps, python=sys.executable, script=None):
    plan = tmp_path / "fake-plan.json"
    plan.write_text(json.dumps({"sessions": [plan_steps], "cost_per_session": 0.01}))
    cfg = {"backend": "fake", "model": "fake-model", "plan": str(plan), "python": python}
    if script:
        cfg["script"] = script
    backend = make_agent_backend(cfg, {})
    return AgentSessions(backend=backend, config={"wall_s_per_round": 60}, work=work,
                         call_log=work / "agent-calls.jsonl", run_id="t",
                         env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, sandbox=sandbox)


def test_opt_out_runs_unwrapped_and_records_none(tmp_path):
    work = tmp_path / "work"
    (work / "workspace").mkdir(parents=True)
    sandbox = AuthoringSandbox(sandbox_config({"mode": "none"}), repo=ROOT, work=work)
    sessions = fake_sessions(tmp_path, sandbox, work, [{"op": "write", "path": LAYOUT_FILE, "text": "{}"}])
    turn = sessions(1, "prompt", work / "workspace")
    call = read_calls(work / "agent-calls.jsonl")[0]
    assert turn.status == "ok" and call.extra["isolation"]["mode"] == "none"
    assert "without OS isolation" in call.extra["isolation"]["opt_out"]
    assert not (work / "sandbox").exists()


STUB = """#!{python}
import json, os, sys
with open({log!r}, "a") as h:
    h.write(json.dumps(sys.argv) + "\\n")
args, cwd, i = sys.argv[1:], "/", 0
while args[i].startswith("-"):
    if args[i] == "-w":
        cwd = args[i + 1]
    i += 2
os.chdir(cwd)
os.execv(args[i], args[i:])
"""


def test_stub_backend_wraps_once_and_records_the_isolation_summary(tmp_path, monkeypatch):
    log = tmp_path / "stub.log"
    stub = tmp_path / "proot"
    stub.write_text(STUB.format(python=sys.executable, log=str(log)))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(scene_author_sandbox, "run_probe", lambda spec, **kw: ProbeResult(
        spec.mode, True, [], [], [], [], ["HIDDEN x"], ro_soft=spec.ro_soft))
    work = tmp_path / "work"
    (work / "workspace").mkdir(parents=True)
    sandbox = AuthoringSandbox(sandbox_config({"mode": "proot", "fallback": [], "proot": str(stub)}), repo=ROOT,
                               work=work, available=proot_only)
    sessions = fake_sessions(tmp_path, sandbox, work, [{"op": "write", "path": LAYOUT_FILE, "text": "{}"}])
    turn = sessions(1, "prompt", work / "workspace")

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == str(stub) and argv.count("-r") == 1 and argv.count("--plan") == 1
    assert sum(a == str(stub) for a in argv) == 1
    assert argv[argv.index("-w") + 1] == str(work / "workspace")
    assert turn.status == "ok" and (work / "workspace" / LAYOUT_FILE).read_text() == "{}"
    iso = read_calls(work / "agent-calls.jsonl")[0].extra["isolation"]
    assert iso["mode"] == "proot" and iso["base"] == "allowlist" and iso["ro_soft"] is True and iso["ok"] is True
    assert iso["readonly_enforced"] is False  # proot without run_as has no read-only bind
    assert iso["guest_root"] == "empty (allowlist)" and iso["leaks"] == 0
    assert (work / iso["spec"]).is_file() and (work / iso["probe"]).is_file()


def test_failed_probe_stops_before_the_agent_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(scene_author_sandbox, "run_probe", lambda spec, **kw: ProbeResult(
        spec.mode, False, [str(ROOT / "examples")], [], [], [], [], ro_soft=True))
    work = tmp_path / "work"
    (work / "workspace").mkdir(parents=True)
    sandbox = AuthoringSandbox(sandbox_config({"mode": "proot", "fallback": [], "proot": "/nonexistent/proot"}),
                               repo=ROOT, work=work, available=proot_only)
    sessions = fake_sessions(tmp_path, sandbox, work, [{"op": "write", "path": LAYOUT_FILE, "text": "{}"}])
    turn = sessions(1, "prompt", work / "workspace")
    call = read_calls(work / "agent-calls.jsonl")[0]
    assert turn.status == "aborted" and turn.fatal and "probe failed" in turn.fatal
    assert call.error_type == "isolation_probe" and call.extra["isolation"]["leaks"] == 1
    assert sandbox.refusal is not None and sandbox.refusal["leaks"] == 1  # main turns this into exit 2
    assert not (work / "workspace" / LAYOUT_FILE).exists()


def test_symlinked_layout_is_refused(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / LAYOUT_FILE).symlink_to(ROOT / "examples" / "scene" / "plate-drawer-airbot.json")
    assert scene_author_cli.refuse_links(workspace) == [LAYOUT_FILE]
    assert not (workspace / LAYOUT_FILE).exists()


@pytest.mark.skipif(not PROOT or sys.platform != "linux", reason="proot not installed (set DATAFARM_PROOT_BIN)")
def test_real_proot_round_hides_the_repo_and_writes_the_workspace(tmp_path):
    work = tmp_path / "work"
    workspace = work / "workspace"
    workspace.mkdir(parents=True)
    script = tmp_path / "agent" / "fake_agent.py"
    script.parent.mkdir()
    shutil.copy(ROOT / "src/datafarm/adapters/fakes/fake_agent.py", script)
    sandbox = AuthoringSandbox(sandbox_config({"mode": "proot", "fallback": [], "proot": PROOT,
                                               "python": BASE_PYTHON, "extra_ro": [str(tmp_path / "agent")]}),
                               repo=ROOT, work=work)
    probe_paths = [str(ROOT / "examples" / "scene"), str(ROOT / "verification"), str(ROOT / ".git"),
                   str(work / "sessions"), str(ROOT / "docs" / "ir" / "scene-layout.md")]
    (tmp_path / "agent" / "plan.json").write_text("{}")
    sessions = fake_sessions(tmp_path, sandbox, work, [{"op": "probe", "paths": probe_paths},
                                                       {"op": "write", "path": LAYOUT_FILE, "text": "{}"}],
                             python=BASE_PYTHON, script=str(script))
    sessions.backend.cfg["plan"] = str(tmp_path / "agent" / "plan.json")
    shutil.copy(tmp_path / "fake-plan.json", tmp_path / "agent" / "plan.json")
    turn = sessions(1, "prompt", workspace)

    call = read_calls(work / "agent-calls.jsonl")[0]
    assert turn.status == "ok", (work / "sessions" / "session-001.stderr.log").read_text()
    assert call.extra["isolation"]["mode"] == "proot" and call.extra["isolation"]["ok"] is True
    seen = json.loads((workspace / "isolation-probe.json").read_text())
    assert all(seen[p].startswith("hidden") for p in probe_paths[:4]), seen
    assert seen[probe_paths[4]] == "readable"  # the snapshot is mounted at WORK/sandbox/repo, not the repo path
    assert (workspace / LAYOUT_FILE).read_text() == "{}"

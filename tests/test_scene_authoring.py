"""Agent-authored scene layout loop: host gates decide acceptance, every attempt is kept, diagnostics feed back."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from datafarm.scene.authoring import (ATTEMPTS_FILE, LAYOUT_FILE, REPORT_FILE, REPORT_SCHEMA, AgentTurn,
                                      CheckResult, PrepareError, author_layout, check_diagnostics)
from datafarm.scene.conformance import load_role_rules
from datafarm.simulation.robot_binding import discover_urdf
from tests.test_scene_layout import URDF, records as toy_records

ROOT = Path(__file__).resolve().parents[1]
REQUEST = ROOT / "examples" / "request" / "plate-drawer-airbot.yaml"
RULES = load_role_rules(ROOT / "configs" / "scene-roles.json")
ROBOT_ASSET = "robot.airbot_g2_left"
DESCRIPTOR = discover_urdf(URDF)

# Toy assets from test_scene_layout (table 1.0x0.8 m top at 0.05, cabinet with a 0.4 m drawer, 0.2 m plate) and the
# toy 6-DOF arm, renamed to the asset_id the role rules require for the request robot.
GOOD = {
    "schema": "datafarm.scene-layout/1", "scene_id": "scene.toy",
    "assets": [
        {"asset_id": "object.table_a", "role": "background", "simready": "table_a", "body": "kinematic"},
        {"asset_id": "object.cabinet_a", "role": "receptacle", "simready": "cabinet_a", "body": "articulation",
         "anchor_body": "Root/body"},
        {"asset_id": "object.plate_a", "role": "task_object", "simready": "plate_a", "body": "rigid"},
    ],
    "nodes": [
        {"node_id": "object.table", "asset_id": "object.table_a", "semantic_tag": "table", "xy_m": [0.0, 0.0],
         "on": "floor"},
        {"node_id": "object.plate", "asset_id": "object.plate_a", "semantic_tag": "plate", "xy_m": [0.2, 0.25],
         "on": "object.table", "randomization": {"position_jitter_m": 0.02}},
        {"node_id": "object.cabinet", "asset_id": "object.cabinet_a", "semantic_tag": "drawer", "xy_m": [0.8, 0.0],
         "yaw_deg": 90, "on": "floor", "joint_state": {"drawer_joint": 0.2}},
    ],
    "robot": {"node_id": "robot.arm", "asset_id": ROBOT_ASSET, "package_asset": "airbot_single",
              "on": "object.table", "xy_m": [0.35, 0.0], "arms": ["left"], "gripper": "g2", "tcp_link": "end_link",
              "tcp_frame_id": "tcp.left", "tool_approach_axis": "x"},
    "reach_targets": [
        {"name": "plate_above", "node": "object.plate", "local_m": [0.0, 0.0, 0.08], "yaw_samples": 4},
        {"name": "drawer_release", "node": "object.cabinet", "joint": "drawer_joint", "local_m": [0.0, 0.0, 0.65],
         "yaw_samples": 4},
    ],
    "probes": [{"name": "plate_in_drawer", "asset_id": "object.plate_a", "node": "object.cabinet",
                "joint": "drawer_joint", "local_m": [0.0, 0.0, 0.62], "expect_rest_z_min_m": 0.58,
                "expect_rest_z_max_m": 0.66}],
    "collision_groups": {"task": {"entities": ["object.plate"],
                                  "contact_with": ["object.table", "robot.arm", "object.cabinet"],
                                  "forbidden_with": []}},
}


def pool() -> dict:
    found = toy_records()
    robot = found.pop("robot.toy")
    robot["asset_id"] = ROBOT_ASSET
    return {"table_a": found["object.table_a"], "cabinet_a": found["object.cabinet_a"],
            "plate_a": found["object.plate_a"], "robot": robot}


class FakePrepare:
    """Builds records only for the assets the layout declares; unknown SimReady names fail like a missing cache."""

    def __init__(self):
        self.calls = []
        self.pool = pool()

    def __call__(self, layout_path: Path, prep_dir: Path) -> dict:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        self.calls.append((layout_path, prep_dir))
        out = {}
        for asset in layout["assets"]:
            if asset["simready"] not in self.pool:
                raise PrepareError(f"{asset['simready']}: no simready-fetch.json in cache")
            out[asset["asset_id"]] = {**deepcopy(self.pool[asset["simready"]]), "asset_id": asset["asset_id"]}
        out[layout["robot"]["asset_id"]] = deepcopy(self.pool["robot"])
        return out


class ScriptedAgent:
    """Writes the scripted content per round (None = writes nothing); records prompts it was given."""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def __call__(self, round_index: int, prompt: str, workspace: Path) -> AgentTurn:
        self.prompts.append(prompt)
        content = self.script[round_index - 1]
        if content is not None:
            text = content if isinstance(content, str) else json.dumps(content)
            (workspace / LAYOUT_FILE).write_text(text, encoding="utf-8")
        return AgentTurn(status="ok", call_ids=(f"call-r{round_index}",))


def passing_check(measured=None, new_contacts=None):
    summary = {"drift": {"max_drift_m": 0.0, "max_end_speed_mps": 0.0, "max_joint_drift": 0.0,
                         "max_passive_joint_drift": 0.0, "stable": True},
               "penetration": {"max_depth_m": 0.0, "ok": True},
               "probes": {"ok": True, "total": 1, "failed": []},
               "new_contacts": new_contacts or {},
               "measured_joint_state": measured or {}}
    return CheckResult(ran=True, ok=True, exit_code=0, summary=summary, diagnostics=(), path=None)


def _passing_check(layout_path, prep_dir):
    return passing_check()


def run(tmp_path, agent, prepare=None, max_rounds=3, docs_root=None, run_check=_passing_check,
        kinematic_only=False):
    return author_layout(request_path=REQUEST, work=tmp_path / "work", rules=RULES, instructions="ROLE TEXT",
                         max_rounds=max_rounds, run_agent=agent, prepare_assets=prepare or FakePrepare(),
                         load_descriptor=lambda layout, records: DESCRIPTOR, docs_root=docs_root,
                         run_check=run_check, kinematic_only=kinematic_only)


def attempts(tmp_path):
    lines = (tmp_path / "work" / ATTEMPTS_FILE).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def without_randomization():
    layout = deepcopy(GOOD)
    del layout["nodes"][1]["randomization"]
    return layout


def test_round_prompt_names_the_sandboxed_repository_snapshot(tmp_path):
    snapshot = tmp_path / "work" / "sandbox" / "repo"
    agent = ScriptedAgent([GOOD])
    run(tmp_path, agent, docs_root=snapshot)
    line = ("The repository itself is not present; the repository files you may read are a read-only snapshot at "
            f"{snapshot}; the IR documents are under it.")
    assert line in agent.prompts[0]

    plain = tmp_path / "plain"
    plain_agent = ScriptedAgent([GOOD])
    run(plain, plain_agent)  # sandbox opted out: the repo root is readable, so no snapshot line
    assert "repository itself is not present" not in plain_agent.prompts[0]
    assert "read-only snapshot" not in plain_agent.prompts[0]


def test_conformance_failure_feeds_round_two_and_fixed_layout_is_accepted(tmp_path):
    agent = ScriptedAgent([without_randomization(), GOOD])
    prepare = FakePrepare()
    result = run(tmp_path, agent, prepare)

    assert result.accepted
    rows = attempts(tmp_path)
    assert [r["round"] for r in rows] == [1, 2]
    assert [r["accepted"] for r in rows] == [False, True]
    first = rows[0]
    assert first["conformance"]["complete"] and not first["conformance"]["ok"]
    assert first["plan"]["ok"]  # geometry was fine; only the request contract failed
    problem = "node 'object.plate' (task_object): missing randomization (position_jitter_m / yaw_range_deg)"
    assert f"conformance: {problem}" in first["diagnostics"]
    assert problem in agent.prompts[1] and problem not in agent.prompts[0]
    assert "first round" in agent.prompts[0]
    assert rows[1]["diagnostics"] == []
    assert rows[1]["conformance"] == {"ok": True, "complete": True, "problems": [], "skipped": []}
    assert [r["agent"]["call_ids"] for r in rows] == [["call-r1"], ["call-r2"]]

    # Same asset set in both rounds: prepared once and reused.
    assert len(prepare.calls) == 1
    assert [r["prepare"]["ran"] for r in rows] == [True, False]
    assert rows[1]["prepare"]["reused"]

    work = tmp_path / "work"
    kept = [json.loads((work / r["layout"]["kept"]).read_text()) for r in rows]
    assert kept == [without_randomization(), GOOD]
    assert rows[0]["layout"]["sha256"] != rows[1]["layout"]["sha256"]
    assert json.loads(result.layout_path.read_text()) == GOOD

    report = json.loads((work / REPORT_FILE).read_text())
    assert report == result.report
    assert report["schema"] == REPORT_SCHEMA and report["accepted"] and report["stop_reason"] == "accepted"
    assert report["accepted_layout"]["round"] == 2
    assert report["agent_call_ids"] == ["call-r1", "call-r2"]
    assert any("check" in line and "settle" in line for line in report["verified"])
    assert all("not run" not in line for line in report["not_verified"])
    assert report["next_step"] is None  # physics-gated acceptance needs no manual GPU step
    assert rows[1]["check"]["ran"] and rows[1]["check"]["ok"] and rows[1]["check"]["exit_code"] == 0
    # The host exposes the evidence the agent may read next round, and the plan sits next to prepared.json for check.
    assert (work / "workspace" / "host" / "asset-records.json").is_file()
    assert json.loads((work / rows[1]["prepare"]["dir"] / "plan.json").read_text())["ok"]


def test_joint_sweep_rejection_feeds_round_two_and_clear_layout_is_accepted(tmp_path):
    # Round 1 opens the drawer into the table (root box clear, displaced box not); the kinematic pre-gate rejects it
    # before any GPU check, and round 2 moves the cabinet back to the accepted geometry.
    intruding = deepcopy(GOOD)
    intruding["nodes"][2]["xy_m"] = [0.0, -0.75]
    agent = ScriptedAgent([intruding, GOOD])
    result = run(tmp_path, agent, FakePrepare())

    rows = attempts(tmp_path)
    assert [r["round"] for r in rows] == [1, 2]
    assert [r["accepted"] for r in rows] == [False, True]
    assert result.accepted

    problem = "object.cabinet drawer_joint at 0.2 m moves into object.table"
    first = rows[0]
    assert not first["plan"]["ok"]
    assert any(problem in line for line in first["diagnostics"])
    assert problem not in agent.prompts[0]
    assert problem in agent.prompts[1]
    assert rows[1]["plan"]["ok"] and rows[1]["diagnostics"] == []

    work = tmp_path / "work"
    swept = json.loads((work / "attempts" / "001" / "plan.json").read_text())["footprint"]["joint_sweep"]
    clear = json.loads((work / "attempts" / "002" / "plan.json").read_text())["footprint"]["joint_sweep"]
    assert swept["ok"] is False
    assert clear["ok"] is True
    assert clear["checked"] == [{"node": "object.cabinet", "joint": "drawer_joint", "travel_m": 0.2}]


def test_exhausted_rounds_are_not_accepted_and_report_says_so(tmp_path):
    unreachable = deepcopy(GOOD)
    unreachable["reach_targets"].append({"name": "beyond", "node": "object.table", "local_m": [3.0, 0.0, 0.1],
                                         "yaw_samples": 2})
    agent = ScriptedAgent([unreachable, unreachable])
    result = run(tmp_path, agent, max_rounds=2)

    assert not result.accepted and result.layout_path is None
    assert not (tmp_path / "work" / LAYOUT_FILE).exists()
    rows = attempts(tmp_path)
    assert len(rows) == 2 and not any(r["accepted"] for r in rows)
    assert all(r["conformance"]["ok"] and not r["plan"]["ok"] for r in rows)
    assert any(line.startswith("plan: reach target beyond unreachable") for line in rows[1]["diagnostics"])
    report = result.report
    assert report["accepted"] is False and report["stop_reason"] == "max_rounds"
    assert report["rounds"] == 2 and report["accepted_layout"] is None
    assert report["final"]["diagnostics"] == rows[1]["diagnostics"]
    assert report["next_step"].startswith("not accepted")


def test_missing_and_invalid_layout_are_recorded_attempts_and_loop_continues(tmp_path):
    agent = ScriptedAgent([None, '{"schema": "datafarm.scene-layout/1", "nodes": [', GOOD])
    prepare = FakePrepare()
    result = run(tmp_path, agent, prepare)

    assert result.accepted
    rows = attempts(tmp_path)
    assert [r["round"] for r in rows] == [1, 2, 3]
    assert rows[0]["layout"] == {"present": False, "valid": False, "sha256": None, "kept": None, "changed": None}
    assert rows[0]["diagnostics"] == [f"{LAYOUT_FILE} was not found in the workspace; write the layout there"]
    assert rows[1]["layout"]["present"] and not rows[1]["layout"]["valid"]
    assert rows[1]["diagnostics"][0].startswith(f"{LAYOUT_FILE} is not valid JSON")
    assert (tmp_path / "work" / rows[1]["layout"]["kept"]).read_text().endswith('"nodes": [')
    assert rows[0]["conformance"] is None and rows[1]["conformance"] is None
    assert not rows[0]["prepare"]["ran"] and not rows[1]["prepare"]["ran"]
    assert len(prepare.calls) == 1  # only the valid layout reached the host prepare
    assert "was not found in the workspace" in agent.prompts[1]
    assert "is not valid JSON" in agent.prompts[2]


def test_prepare_failure_is_a_diagnostic_and_a_new_asset_set_is_prepared_again(tmp_path):
    missing = deepcopy(GOOD)
    missing["assets"][2]["simready"] = "plate_missing"
    agent = ScriptedAgent([missing, GOOD])
    prepare = FakePrepare()
    result = run(tmp_path, agent, prepare)

    assert result.accepted
    rows = attempts(tmp_path)
    assert not rows[0]["prepare"]["ok"] and "plate_missing" in rows[0]["prepare"]["error"]
    assert rows[0]["conformance"]["complete"] is False  # no records: joint checks skipped, never accepted
    assert "plan not run: no asset records for this layout" in rows[0]["diagnostics"]
    assert len(prepare.calls) == 2
    assert rows[0]["prepare"]["asset_set_sha256"] != rows[1]["prepare"]["asset_set_sha256"]


def test_rerun_resumes_after_accept_without_new_sessions_and_refuses_other_request(tmp_path):
    run(tmp_path, ScriptedAgent([GOOD]))
    again = ScriptedAgent([])
    result = run(tmp_path, again)
    assert result.accepted and again.prompts == [] and len(attempts(tmp_path)) == 1

    other = tmp_path / "other.yaml"
    other.write_text(REQUEST.read_text().replace("plate-drawer-airbot-001", "plate-drawer-airbot-002"))
    with pytest.raises(ValueError, match="different request"):
        author_layout(request_path=other, work=tmp_path / "work", rules=RULES, instructions="", max_rounds=2,
                      run_agent=again, prepare_assets=FakePrepare(), load_descriptor=lambda *_: DESCRIPTOR,
                      run_check=passing_check())


def test_entry_session_runs_agent_backend_and_logs_one_call_per_round(tmp_path):
    from datafarm.adapters.agent_harness.backends import make_agent_backend
    from datafarm.adapters.agent_harness.call_log import read_calls
    from datafarm.entry.scene_author_cli import AgentSessions
    from datafarm.entry.scene_author_sandbox import AuthoringSandbox, sandbox_config

    plan = tmp_path / "fake-plan.json"
    plan.write_text(json.dumps({"sessions": [[{"op": "write", "path": LAYOUT_FILE,
                                               "text": json.dumps(without_randomization())}],
                                             [{"op": "write", "path": LAYOUT_FILE, "text": json.dumps(GOOD)}]],
                                "cost_per_session": 0.01}))
    backend = make_agent_backend({"backend": "fake", "model": "fake-model", "plan": str(plan),
                                  "python": sys.executable}, {})
    home = tmp_path / "home"
    home.mkdir()
    # This test covers the session/call-ledger wiring, not isolation: opt out explicitly.
    sandbox = AuthoringSandbox(sandbox_config({"mode": "none"}), repo=ROOT, work=tmp_path / "work")
    sessions = AgentSessions(backend=backend, config={"wall_s_per_round": 60}, work=tmp_path / "work",
                             call_log=tmp_path / "work" / "agent-calls.jsonl", run_id="t",
                             env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, sandbox=sandbox)
    result = run(tmp_path, sessions)

    assert result.accepted
    calls = read_calls(tmp_path / "work" / "agent-calls.jsonl")
    rows = attempts(tmp_path)
    assert [c.call_id for c in calls] == [cid for r in rows for cid in r["agent"]["call_ids"]]
    assert [c.status for c in calls] == ["ok", "ok"] and [c.attempt for c in calls] == [1, 2]
    assert all(c.component == "scene-author/session" for c in calls)


PASSIVE_JOINT = {"name": "joint_part_01_joint_00", "actuated": False, "declared": 0.42, "start": 0.42,
                 "measured": 0.4609, "drift_m": 0.0409, "limits": [0.0, 0.5], "within_limits": True,
                 "moved": True, "new_contacts": {}, "ok": True}


def check_document(*, stable=True, penetration_ok=True, pairs=None, joint=None, probes=None, measured=None,
                   new_contacts=None):
    joint = dict(PASSIVE_JOINT if joint is None else joint)
    probes = probes or []
    document = {
        "initial_joint_state": {"object.cabinet": [0.42]},
        "settle": {"ran": True, "steps": 240, "max_drift_m": 0.0, "stable": stable,
                   "max_end_speed_mps": 0.0, "max_joint_drift": 0.0,
                   "max_passive_joint_drift": joint["drift_m"],
                   "tolerances": {"drift_m": 0.005, "speed_mps": 0.01, "joint_active": 0.005,
                                  "joint_passive": 0.005},
                   "nodes": {"object.cabinet": {"joint_checks": [joint]}},
                   "measured_joint_state": measured if measured is not None
                   else {"object.cabinet": {"joint_part_01_joint_00": joint["measured"]}},
                   "new_contacts": new_contacts or {}},
        "penetration": {"ran": True, "ok": penetration_ok,
                        "max_depth_m": max([float((p or {}).get("depth_m") or 0.0)
                                            for p in (pairs or {}).values()], default=0.0),
                        "tolerance_m": 0.003, "contact_count": len(pairs or {}), "pairs": pairs or {}},
        "probes_ok": all(p.get("ok") for p in probes), "probes": probes,
    }
    return document


def check_runner(tmp_path, document, exit_code=0):
    from datafarm.entry.scene_author_cli import CHECK_DEFAULTS, check_with_tool
    script = tmp_path / "fake-check"
    script.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document)
    script.write_text(f"#!{sys.executable}\n"
                      "import json, pathlib, sys\n"
                      "work = pathlib.Path(sys.argv[sys.argv.index('--work') + 1])\n"
                      f"document = json.loads({payload!r})\n"
                      "(work / 'check.json').write_text(json.dumps(document))\n"
                      f"sys.exit({exit_code})\n", encoding="utf-8")
    script.chmod(0o755)
    return check_with_tool(python=str(script), tools=ROOT / "tools", env_script=None, timeout_s=60,
                           tolerances=CHECK_DEFAULTS)


def test_check_gate_accepts_only_when_the_check_command_passes(tmp_path):
    failing = run(tmp_path / "failing", ScriptedAgent([GOOD]), max_rounds=1,
                  run_check=check_runner(tmp_path / "failing", check_document(stable=False), exit_code=1))
    assert not failing.accepted
    row = attempts(tmp_path / "failing")[0]
    assert row["check"]["ran"] and not row["check"]["ok"] and row["check"]["exit_code"] == 1
    assert row["check"]["path"].endswith("check.json")
    assert any(line.startswith("check:") for line in row["diagnostics"])

    passing = run(tmp_path / "passing", ScriptedAgent([GOOD]), max_rounds=1,
                  run_check=check_runner(tmp_path / "passing", check_document()))
    assert passing.accepted
    assert attempts(tmp_path / "passing")[0]["check"]["ok"]


def test_check_failure_diagnostics_are_one_per_problem_and_reach_the_next_prompt(tmp_path):
    pairs = {"object.cabinet | object.table": {"depth_m": 0.0619, "a": "object.cabinet", "b": "object.table",
                                               "body_a": "part_01_obj_00", "body_b": "sm_table"}}
    probes = [{"name": "plate_in_drawer", "ok": False, "rest_m": [0.3, 0.0, 0.51],
               "expected_rest_z_m": [0.55, 0.85], "support_node": "object.cabinet",
               "touching": [], "end_speed_mps": 0.0}]
    document = check_document(stable=False, penetration_ok=False, pairs=pairs, probes=probes)
    agent = ScriptedAgent([GOOD, GOOD])
    result = run(tmp_path, agent, max_rounds=2,
                 run_check=check_runner(tmp_path, document, exit_code=1))

    assert not result.accepted
    rows = attempts(tmp_path)
    lines = rows[0]["diagnostics"]
    assert ("check: penetration object.cabinet part_01_obj_00 vs object.table sm_table "
            "depth 61.9 mm > 3.0 mm") in lines
    assert "check: probe plate_in_drawer failed: rest z 0.51 m outside [0.55, 0.85]" in lines
    assert "check: passive joint object.cabinet.joint_part_01_joint_00 moved 0.0409 m " \
           "(limits 0..0.5, 0 new contacts) - recorded as measured initial state" in lines
    for line in lines:
        assert line in agent.prompts[1]


def test_kinematic_only_accepts_without_check_and_records_it(tmp_path):
    result = run(tmp_path, ScriptedAgent([GOOD]), kinematic_only=True, run_check=None)
    assert result.accepted
    row = attempts(tmp_path)[0]
    assert row["check"] == {"ran": False, "ok": False, "mode": "kinematic-only", "exit_code": None,
                            "summary": None, "diagnostics": [], "path": None, "error": None}
    report = result.report
    assert report["kinematic_only"] is True
    assert report["final"]["check"]["mode"] == "kinematic-only"
    assert any("check" in line and "not run" in line for line in report["not_verified"])
    assert report["next_step"].startswith("kinematic-only")
    with pytest.raises(ValueError, match="kinematic-only"):
        run(tmp_path, ScriptedAgent([]))


def test_missing_check_interpreter_refuses_before_any_agent_session(tmp_path, capsys):
    from datafarm.entry import scene_author_cli
    work = tmp_path / "work"
    plan = tmp_path / "fake-plan.json"
    plan.write_text(json.dumps({"sessions": [], "cost_per_session": 0.01}))
    config = tmp_path / "agent.json"
    config.write_text(json.dumps({"schema": "datafarm.scene-author/1",
                                  "sandbox": {"mode": "none"},
                                  "agent": {"backend": "fake", "model": "fake-model", "plan": str(plan),
                                            "python": sys.executable}}))
    code = scene_author_cli.main(["--request", str(REQUEST), "--work", str(work), "--cache", str(tmp_path),
                                  "--robot-package", str(tmp_path), "--agent-config", str(config),
                                  "--check-python", str(tmp_path / "no-such-isaac-python")])
    assert code == 2
    assert "no check interpreter" in capsys.readouterr().err
    assert not (work / "sessions").exists() and not (work / ATTEMPTS_FILE).exists()


def test_passive_joint_drift_with_no_new_contacts_is_recorded_as_measured_state(tmp_path):
    document = check_document()
    result = run(tmp_path, ScriptedAgent([GOOD]), run_check=check_runner(tmp_path, document))
    assert result.accepted
    row = attempts(tmp_path)[0]
    assert row["check"]["ok"]
    assert row["check"]["summary"]["measured_joint_state"] == {"object.cabinet": {"joint_part_01_joint_00": 0.4609}}
    assert row["check"]["summary"]["new_contacts"] == {}


def test_passive_joint_drift_with_a_new_contact_fails(tmp_path):
    added = {"object.cabinet | object.plate": {"depth_m": 0.004, "a": "object.cabinet", "b": "object.plate",
                                               "body_a": "part_01_obj_00", "body_b": "plate"}}
    joint = {**PASSIVE_JOINT, "new_contacts": added, "ok": False}
    document = check_document(stable=False, joint=joint, new_contacts=added)
    result = run(tmp_path, ScriptedAgent([GOOD]), max_rounds=1,
                 run_check=check_runner(tmp_path, document, exit_code=1))
    assert not result.accepted
    lines = attempts(tmp_path)[0]["diagnostics"]
    assert any("1 new contacts" in line and "object.cabinet | object.plate" in line for line in lines)


def test_check_diagnostics_lists_each_problem_once():
    pairs = {"object.cabinet | object.table": {"depth_m": 0.0619, "a": "object.cabinet", "b": "object.table",
                                               "body_a": "part_01_obj_00", "body_b": "sm_table"}}
    document = check_document(stable=False, penetration_ok=False, pairs=pairs)
    lines = check_diagnostics(document)
    assert sum(line.startswith("check: penetration ") for line in lines) == 1
    assert sum(line.startswith("check: passive joint ") for line in lines) == 1


def test_added_contacts_reports_only_new_pairs_with_depth():
    from datafarm.adapters.sim.isaac_scene import added_contacts
    initial = {"pairs": {"object.plate | object.table": {"depth_m": 0.0, "a": "object.plate",
                                                          "b": "object.table"}}}
    final = {"pairs": {"object.plate | object.table": {"depth_m": 0.001, "a": "object.plate",
                                                        "b": "object.table"},
                       "object.cabinet | object.plate": {"depth_m": 0.004, "a": "object.cabinet",
                                                         "b": "object.plate"}}}
    added = added_contacts(initial, final)
    assert list(added) == ["object.cabinet | object.plate"]
    assert added["object.cabinet | object.plate"]["depth_m"] == 0.004


def test_check_config_defaults_and_unknown_keys():
    from datafarm.entry.scene_author_cli import CHECK_DEFAULTS, check_config
    assert check_config(None) == CHECK_DEFAULTS
    assert check_config({"joint_tol_active": 0.002})["joint_tol_active"] == 0.002
    with pytest.raises(ValueError, match="unknown keys"):
        check_config({"settle": 2.0})
    with pytest.raises(ValueError, match="positive number"):
        check_config({"timeout_s": -1})


def test_check_interpreter_keeps_the_venv_entry_symlink(tmp_path):
    """Linux venv 的 `bin/python` 是指向基础解释器的符号链接：入口一旦被 resolve，venv 的 site-packages 就丢了。"""
    from datafarm.entry.scene_author_cli import resolve_check_interpreter
    base = tmp_path / "cpython-3.12.16" / "bin" / "python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("", encoding="utf-8")
    base.chmod(0o755)
    entry = tmp_path / "venv" / "bin" / "python"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(base)
    assert resolve_check_interpreter(str(entry)) == str(entry)


def test_contact_body_labels_are_portable_scene_refs(tmp_path):
    """接触行两端要能落盘：Newton 的 `/World/...` 刚体标签必须编码成场景引用 `usd:/World/...`。"""
    from datafarm.adapters.sim.isaac_scene import body_ref, penetration_summary
    from datafarm.kernel.schema import SchemaError
    from datafarm.kernel.store import ArtifactStore
    table = "/World/envs/env_0/ObjectTable/Geometry/sm_table_metal_stainless_a03_obj_00"
    block = "/World/envs/env_0/ObjectBlock0/Geometry/primitives_block_blue_a01_obj_00"
    assert body_ref(table) == "usd:" + table
    assert body_ref("world") == "world"
    row = {"a": "object.table", "b": "object.block.0", "body_a": table, "body_b": block, "dist_m": -0.004}
    store = ArtifactStore(tmp_path / "store")
    with pytest.raises(SchemaError, match="absolute filesystem path"):
        store.put_json("scene.check-evidence", {"penetration": penetration_summary([row], 0.003)})
    stored = store.put_json("scene.check-evidence", {"penetration": penetration_summary(
        [{**row, "body_a": body_ref(table), "body_b": body_ref(block)}], 0.003)})
    assert store.load(stored)["penetration"]["max_depth_m"] == 0.004

"""Agent-authored scene layout loop: host gates decide acceptance, every attempt is kept, diagnostics feed back."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from datafarm.scene.authoring import (ATTEMPTS_FILE, LAYOUT_FILE, REPORT_FILE, REPORT_SCHEMA, AgentTurn,
                                      PrepareError, author_layout)
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


def run(tmp_path, agent, prepare=None, max_rounds=3, docs_root=None):
    return author_layout(request_path=REQUEST, work=tmp_path / "work", rules=RULES, instructions="ROLE TEXT",
                         max_rounds=max_rounds, run_agent=agent, prepare_assets=prepare or FakePrepare(),
                         load_descriptor=lambda layout, records: DESCRIPTOR, docs_root=docs_root)


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
    assert any("check" in line and "GPU" in line for line in report["not_verified"])
    assert f"--work {work / rows[1]['prepare']['dir']}" in report["next_step"]
    # The host exposes the evidence the agent may read next round, and the plan sits next to prepared.json for check.
    assert (work / "workspace" / "host" / "asset-records.json").is_file()
    assert json.loads((work / rows[1]["prepare"]["dir"] / "plan.json").read_text())["ok"]


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
                      run_agent=again, prepare_assets=FakePrepare(), load_descriptor=lambda *_: DESCRIPTOR)


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

"""Judge input is a field-preserving projection of the recorded Episode; no state renames."""
from copy import deepcopy
import unittest

from datafarm.judge.protocol import EPISODE_SCHEMA, JudgeError, project_episode, validate_episode
from test_ir_episode_state import STATE

NODES = list(STATE["entities"])
DT = 1 / 120


def recorded(steps=2):
    states = []
    for k in range(steps + 1):
        s = deepcopy(STATE)
        s.update(control_step=k, time_s=k * DT)
        s["contacts"]["control_step"] = k
        states.append(s)
    actions = [{"control_step": k, "requested_joint_targets": {"left_joint1": 0.0},
                "applied_joint_targets": {"left_joint1": 0.0}, "application": "actuator_readback",
                "command": {"call_id": "call.0001"}} for k in range(steps)]
    return {"profile": "measured-scene-episode/1",
            "setup": {"control_dt_s": DT, "node_ids": NODES, "source_sha256": {"x": "0" * 64}},
            "actions": actions, "state_trace": states, "contacts": [s["contacts"] for s in states],
            "events": [], "frame_map": [], "replay": {"state_playback": "unverified", "action_replay": "unverified"},
            "termination": {"status": "completed", "reason_code": "EXPERT_RETURNED", "trace_complete": True},
            "judgement": "unverified"}


class JudgeEpisodeProjectionTests(unittest.TestCase):
    def test_projection_keeps_state_trace_bytes_and_drops_provenance(self):
        source = recorded()
        episode = project_episode(source)
        self.assertEqual(episode["schema_version"], EPISODE_SCHEMA)
        self.assertEqual(episode["state_trace"], source["state_trace"])
        self.assertNotIn("setup", episode)
        self.assertEqual(set(episode["actions"][0]), {"control_step", "requested_joint_targets", "applied_joint_targets"})
        validate_episode(episode, {"conditions": []})
        document = {"schema": "datafarm.episode-ir", "payload": source}
        self.assertEqual(project_episode(document), episode)

    def test_validation_uses_the_ir_state_contract(self):
        episode = project_episode(recorded())
        broken = deepcopy(episode)
        broken["state_trace"][1]["robot"]["tcp_pose"] = broken["state_trace"][1]["robot"].pop("tcp")
        with self.assertRaisesRegex(JudgeError, r"state_trace\[1\]"):
            validate_episode(broken, {"conditions": []})
        gap = deepcopy(episode)
        gap["state_trace"][2]["control_step"] = 3
        with self.assertRaisesRegex(JudgeError, "clock gap"):
            validate_episode(gap, {"conditions": []})
        with self.assertRaisesRegex(JudgeError, "N actions"):
            validate_episode({**episode, "actions": episode["actions"][:1]}, {"conditions": []})
        with self.assertRaisesRegex(JudgeError, "Unsupported"):
            validate_episode({**episode, "schema_version": "judge-episode/1"}, {"conditions": []})
        with self.assertRaisesRegex(JudgeError, "measured-scene-episode/1"):
            project_episode({**recorded(), "profile": "other"})

    def test_evidence_fields_address_state_trace_paths(self):
        episode = project_episode(recorded())
        validate_episode(episode, {"conditions": [{"evidence_fields": ["state_trace.robot.tcp", "state_trace.entities"]}]})
        missing_tcp = deepcopy(episode)
        for state in missing_tcp["state_trace"]:
            del state["robot"]["tcp"]
        with self.assertRaisesRegex(JudgeError, "state_trace.robot.tcp"):
            validate_episode(missing_tcp, {"conditions": [{"evidence_fields": ["state_trace.robot.tcp"]}]})


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(__import__("sys").platform.startswith("linux"),
                     "judge worker installs RLIMIT_AS, which macOS refuses for any value")
class JudgeWorkerCapacityTests(unittest.TestCase):
    """真实抓放 episode 投影后 35-72 MiB（lab30 实测）；worker 必须真的跑判定，而不是因资源上限返回 error。"""

    def test_a_long_recorded_episode_is_evaluated(self):
        from datafarm.judge.execution import run_source
        source = recorded(steps=1)
        state = source["state_trace"][0]
        # 每条 state 约 20 KB，与实测同量级；2000 步 ≈ 40 MiB 规范 JSON。
        state["contacts"] = {**state["contacts"], "availability": "available", "coverage": "unknown",
                             "rows": [{"a": NODES[0], "b": None, "body_a": "usd:/World/x", "body_b": "world",
                                       "dist_m": 0.001 * i} for i in range(150)]}
        episode = project_episode(recorded_long(state, 2000))
        contract = {"conditions": [{"id": "steps", "evidence_fields": ["state_trace.control_step"]}]}
        src = ("def evaluate(episode, task_contract, scene_metadata):\n"
               "    n = len(episode['state_trace'])\n"
               "    return result({'steps': n == 2001}, {'n': n}, 'counted')\n")
        verdict = run_source(src, episode, contract, {})
        self.assertEqual(verdict["outcome"], "success", verdict)


def recorded_long(state, steps):
    value = recorded(steps=0)
    states = []
    for k in range(steps + 1):
        s = deepcopy(state)
        s.update(control_step=k, time_s=k * DT)
        s["contacts"] = {**s["contacts"], "control_step": k}
        states.append(s)
    value["state_trace"], value["contacts"] = states, [s["contacts"] for s in states]
    value["actions"] = [{"control_step": k, "requested_joint_targets": {"left_joint1": 0.0},
                         "applied_joint_targets": {"left_joint1": 0.0}, "application": "actuator_readback",
                         "command": {"call_id": "call.0001"}} for k in range(steps)]
    return value

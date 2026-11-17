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

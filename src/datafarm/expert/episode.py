"""Immutable full-cycle measured episodes; recording is not task acceptance."""
from __future__ import annotations

from copy import deepcopy
import math
from collections.abc import Mapping

from datafarm.kernel.artifacts import ArtifactDocument, ArtifactRef, IRKind
from datafarm.kernel.identifiers import logical_id, storage_key
from datafarm.kernel.schema import canonical_json_bytes, validate_ref
from datafarm.kernel.io import is_sha256

PROFILE = "measured-scene-episode/1"
UNITS = {"length": "m", "angle": "rad", "time": "s"}


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def validate_state(state, node_ids, step, dt):
    _require(isinstance(state, Mapping) and state.get("control_step") == step
             and type(state["control_step"]) is int, "state clock gap")
    _require(state.get("units") == UNITS and state.get("frame_id") == "world", "state units/frame")
    _require(type(state.get("time_s")) in (int, float) and
             math.isclose(state["time_s"], step * dt, rel_tol=0, abs_tol=1e-9), "state time mismatch")
    _require(set(state.get("entities", {})) == set(node_ids), "incomplete entity coverage")
    for node, reading in state["entities"].items():
        _require(isinstance(reading, Mapping) and reading.get("availability") == "available", "entity state unavailable")
        for key, count in (("position_m", 3), ("quaternion_xyzw", 4),
                           ("linear_velocity_m_s", 3), ("angular_velocity_rad_s", 3)):
            vector = reading.get(key)
            _require(isinstance(vector, list) and len(vector) == count and
                     all(type(v) in (int, float) and math.isfinite(v) for v in vector), "invalid entity vector: " + key)
        _require(math.isclose(math.hypot(*reading["quaternion_xyzw"]), 1, abs_tol=1e-6), "unnormalized quaternion")
        links = reading.get("links", {})
        _require(isinstance(links, dict), "invalid links")
        for reading_pose in links.values():
            for key, size in (("position_m", 3), ("quaternion_xyzw", 4)):
                vector = reading_pose.get(key)
                _require(isinstance(vector, list) and len(vector) == size and all(type(v) in (int, float) and math.isfinite(v) for v in vector), "invalid link pose")
            _require(math.isclose(math.hypot(*reading_pose["quaternion_xyzw"]), 1, abs_tol=1e-6), "invalid link quaternion")
        joints = reading.get("joints", [])
        _require(isinstance(joints, list) and len({j["name"] for j in joints}) == len(joints), "invalid joint coverage")
        for joint in joints:
            _require(joint.get("type") in ("revolute", "prismatic") and joint.get("unit") ==
                     ("m" if joint["type"] == "prismatic" else "rad"), "invalid joint type/unit")
            _require(all(type(joint.get(k)) in (int, float) and math.isfinite(joint[k]) for k in ("q", "dq")), "invalid q/dq")
    canonical_json_bytes(state)
    return deepcopy(dict(state))


def validate_episode(payload):
    _require(payload.get("profile") == PROFILE, "unsupported episode profile")
    setup = payload["setup"]
    for key in ("episode_id", "task_id", "task_revision", "attempt_id"):
        logical_id(setup[key], label=key)
    for key in ("request_ref", "task_ref", "scene_ref", "runtime_ref", "expert_ref"):
        validate_ref(setup[key], key)
    validate_ref(setup["original_request_ref"], "original_request_ref")
    _require(isinstance(setup["source_sha256"], dict) and bool(setup["source_sha256"]) and
             all(isinstance(n, str) and is_sha256(v) for n, v in setup["source_sha256"].items()), "source pin missing")
    for reference in setup["asset_refs"]:
        validate_ref(reference, "asset_ref")
    if setup.get("task_contract_ref") is not None:
        validate_ref(setup["task_contract_ref"], "task_contract_ref")
    dt = setup["control_dt_s"]
    _require(type(dt) in (int, float) and math.isfinite(dt) and dt > 0, "invalid dt")
    nodes = setup["node_ids"]
    _require(isinstance(nodes, list) and bool(nodes) and len(set(nodes)) == len(nodes), "invalid nodes")
    for node in nodes:
        logical_id(node, label="node_id")
    states, actions = payload["state_trace"], payload["actions"]
    _require(isinstance(states, list) and states, "initial measured state missing")
    _require(isinstance(actions, list), "actions must be a list")
    for i, state in enumerate(states):
        validate_state(state, nodes, i, dt)
    entity_signature = {n: ([j["name"] for j in states[0]["entities"][n]["joints"]],
                            set(states[0]["entities"][n]["links"])) for n in nodes}
    for state in states:
        _require(entity_signature == {n: ([j["name"] for j in state["entities"][n]["joints"]],
                                            set(state["entities"][n]["links"])) for n in nodes}, "joint/link coverage changed")
    for i, action in enumerate(actions):
        _require(action.get("control_step") == i and type(action["control_step"]) is int, "action clock gap")
        _require(isinstance(action.get("requested_joint_targets"), dict) and
                 isinstance(action.get("applied_joint_targets"), dict), "requested/applied targets missing")
        _require(action.get("application") == "actuator_readback", "unmeasured actuator input")
        for targets in (action["requested_joint_targets"], action["applied_joint_targets"]):
            _require(all(isinstance(n, str) and n and type(v) in (int, float) and math.isfinite(v) for n, v in targets.items()), "invalid actuator targets")
    _require(isinstance(payload["termination"].get("trace_complete"), bool), "trace completeness must be boolean")
    _require(payload.get("judgement") == "unverified", "recorder cannot certify a Judge result")
    status = payload["termination"]["status"]
    _require(status in ("completed", "failed", "aborted", "error", "timeout"), "invalid termination")
    if payload["termination"]["trace_complete"]:
        _require(len(states) == len(actions) + 1, "expected N inputs and N+1 states")
    else:
        _require(status != "completed" and len(states) in (len(actions), len(actions)+1), "invalid partial trace")
    _require(len(payload["contacts"]) == len(states), "contact samples missing")
    for i, sample in enumerate(payload["contacts"]):
        _require(sample == states[i].get("contacts"), "contact sample differs from source state")
        _require(sample["control_step"] == i and sample.get("sampling") == "cycle_end_snapshot", "contact clock/sampling")
        _require(sample.get("availability") in ("available", "unavailable"), "invalid contact availability")
        _require(sample.get("coverage") in ("unknown", "complete", "truncated"), "contact coverage missing")
        if sample["availability"] == "unavailable":
            _require(sample.get("rows") is None, "unavailable contact is not empty contact")
    _require(payload["frame_map"] == [], "recording entry does not render frames")
    _require(payload["replay"] == {"state_playback": "unverified", "action_replay": "unverified"}, "cannot self-certify replay")
    canonical_json_bytes(payload)
    return deepcopy(payload)


class EpisodeRecorder:
    """One reset-qualified initial state, one record per attempted owned physics step."""
    def __init__(self, setup, *, max_steps, max_bytes):
        _require(type(max_steps) is int and max_steps > 0 and type(max_bytes) is int and max_bytes > 0, "explicit recording caps required")
        self.setup = deepcopy(setup)
        self.max_steps, self.max_bytes = max_steps, max_bytes
        self.states, self.actions, self.events = [], [], []
        self.closed = False
        self.rejected_measurements = []

    def _size(self, value):
        _require(len(canonical_json_bytes(value)) <= self.max_bytes, "episode capture byte budget exhausted")

    def initial(self, state):
        _require(not self.closed and not self.states, "initial state already captured")
        value = validate_state(state, self.setup["node_ids"], 0, self.setup["control_dt_s"])
        self._size(value)
        self.states.append(value)

    def rejected_measurement(self, value):
        """Retain noncanonical raw readings separately; never round/repair them."""
        self.rejected_measurements.append(deepcopy(value))

    def event(self, value):
        _require(not self.closed, "episode closed")
        self._size({"setup": self.setup, "actions": self.actions, "states": self.states, "events": [*self.events, value]})
        self.events.append(deepcopy(value))

    def ensure_capacity(self, requested, metadata):
        """Refuse a known step/byte budget exhaustion before target application."""
        _require(not self.closed and len(self.states) == len(self.actions)+1 and len(self.actions) < self.max_steps, "recording step unavailable")
        self._size({"setup": self.setup, "actions": self.actions, "states": self.states,
                    "events": self.events, "next_input": {"requested": requested, "command": metadata}})

    def input(self, requested, applied, metadata):
        _require(not self.closed and len(self.states) == len(self.actions)+1 and len(self.actions) < self.max_steps, "recording step unavailable")
        row = {"control_step": len(self.actions), "requested_joint_targets": deepcopy(requested),
               "applied_joint_targets": deepcopy(applied), "application": "actuator_readback", "command": deepcopy(metadata)}
        self._size({"actions": [*self.actions, row], "states": self.states, "events": self.events})
        self.actions.append(row)

    def measured(self, state):
        _require(not self.closed and len(self.actions) == len(self.states), "no corresponding applied input")
        value = validate_state(state, self.setup["node_ids"], len(self.states), self.setup["control_dt_s"])
        self._size({"actions": self.actions, "states": [*self.states, value], "events": self.events})
        self.states.append(value)

    def finish(self, status, reason_code):
        _require(not self.closed, "episode already closed")
        payload = {"profile": PROFILE, "setup": deepcopy(self.setup), "actions": deepcopy(self.actions),
                   "state_trace": deepcopy(self.states), "contacts": [deepcopy(s["contacts"]) for s in self.states],
                   "events": deepcopy(self.events), "frame_map": [],
                   "replay": {"state_playback": "unverified", "action_replay": "unverified"},
                   "termination": {"status": status, "reason_code": reason_code,
                                   "trace_complete": len(self.states) == len(self.actions)+1}, "judgement": "unverified"}
        checked = validate_episode(payload)
        # Final outcome metadata has a bounded reserve; never discard raw states to fit it.
        _require(len(canonical_json_bytes(checked)) <= self.max_bytes + 65536, "episode seal reserve exhausted")
        self.closed = True
        return checked

    def document(self, payload, *, created_at):
        checked = validate_episode(payload)
        return ArtifactDocument(kind=IRKind.EPISODE, artifact_id=self.setup["episode_id"],
            created_at=created_at, producer="b-scene-recorder",
            predecessors=(), evidence=tuple(ArtifactRef.from_dict(self.setup[k]) for k in
                ("request_ref", "original_request_ref", "task_ref", "scene_ref", "runtime_ref", "expert_ref")) +
                ((ArtifactRef.from_dict(self.setup["task_contract_ref"]),) if self.setup.get("task_contract_ref") is not None else ()), payload=checked)


def episode_identity(task_id, task_revision, attempt_id):
    return storage_key("episode", {"task_id": logical_id(task_id), "task_revision": logical_id(task_revision),
                                   "attempt_id": logical_id(attempt_id)})

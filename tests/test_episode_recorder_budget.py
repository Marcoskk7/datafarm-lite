"""EpisodeRecorder 的字节上限：增量计数必须与旧的整条重序列化结果逐字节一致（lab30：旧实现 O(N²)，100 步 ~100 s）。"""
import pytest

from datafarm.expert.episode import EpisodeRecorder
from datafarm.kernel.schema import canonical_json_bytes

SETUP = {"node_ids": ["object.plate"], "control_dt_s": 0.5, "episode_id": "e"}


def _state(step):
    return {"control_step": step, "time_s": step * 0.5, "units": {"length": "m", "angle": "rad", "time": "s"},
            "frame_id": "world",
            "entities": {"object.plate": {"availability": "available", "asset_id": "a", "position_m": [0.1 * step, 0, 0],
                                          "quaternion_xyzw": [0, 0, 0, 1], "linear_velocity_m_s": [0, 0, 0],
                                          "angular_velocity_rad_s": [0, 0, 0], "joints": [], "links": {}}},
            "robot": {"joint_names": ["j"], "q": [0.01 * step], "dq": [0.0]},
            "contacts": {"control_step": step, "sampling": "cycle_end_snapshot", "availability": "unavailable",
                         "rows": None, "coverage": "unknown"}}


def _old_sizes(rec, event, requested, row, state):
    """旧实现在各检查点序列化的整条对象长度。"""
    return {"event": len(canonical_json_bytes({"setup": rec.setup, "actions": rec.actions, "states": rec.states,
                                               "events": [*rec.events, event]})),
            "capacity": len(canonical_json_bytes({"setup": rec.setup, "actions": rec.actions, "states": rec.states,
                                                  "events": rec.events,
                                                  "next_input": {"requested": requested, "command": {"c": 1}}})),
            "input": len(canonical_json_bytes({"actions": [*rec.actions, row], "states": rec.states, "events": rec.events})),
            "measured": len(canonical_json_bytes({"actions": rec.actions, "states": [*rec.states, state],
                                                  "events": rec.events}))}


def test_incremental_budget_equals_full_serialization_at_the_exact_boundary():
    rec = EpisodeRecorder(SETUP, max_steps=10, max_bytes=10**9)
    rec.initial(_state(0))
    for k in range(3):
        event = {"kind": "control_request", "control_step": k}
        requested = {"j": 0.1 * k}
        row = {"control_step": k, "requested_joint_targets": requested, "applied_joint_targets": requested,
               "application": "actuator_readback", "command": {"c": 1}}
        sizes = _old_sizes(rec, event, requested, row, _state(k + 1))
        for name, call in (("event", lambda: rec.event(event)),):
            rec.max_bytes = sizes[name] - 1
            with pytest.raises(ValueError, match="byte budget"):
                call()
            rec.max_bytes = sizes[name]
            call()
        sizes = _old_sizes(rec, event, requested, row, _state(k + 1))
        rec.max_bytes = sizes["capacity"] - 1
        with pytest.raises(ValueError, match="byte budget"):
            rec.ensure_capacity(requested, {"c": 1})
        rec.max_bytes = sizes["capacity"]
        rec.ensure_capacity(requested, {"c": 1})
        rec.max_bytes = sizes["input"] - 1
        with pytest.raises(ValueError, match="byte budget"):
            rec.input(requested, requested, {"c": 1})
        rec.max_bytes = sizes["input"]
        rec.input(requested, requested, {"c": 1})
        sizes = _old_sizes(rec, event, requested, row, _state(k + 1))
        rec.max_bytes = sizes["measured"] - 1
        with pytest.raises(ValueError, match="byte budget"):
            rec.measured(_state(k + 1))
        rec.max_bytes = sizes["measured"]
        rec.measured(_state(k + 1))


def test_budget_check_cost_does_not_grow_with_trace_length():
    """每步检查只序列化新条目：录 400 步后，再记一步的序列化量与第 1 步同量级。"""
    import datafarm.expert.episode as module
    calls = []
    original = module.canonical_json_bytes

    def counting(value):
        data = original(value)
        calls.append(len(data))
        return data

    rec = EpisodeRecorder(SETUP, max_steps=1000, max_bytes=10**9)
    rec.initial(_state(0))
    module.canonical_json_bytes = counting
    try:
        for k in range(400):
            calls.clear()
            rec.ensure_capacity({"j": 0.0}, {"c": 1})
            rec.input({"j": 0.0}, {"j": 0.0}, {"c": 1})
            rec.measured(_state(k + 1))
            rec.event({"kind": "control_applied", "control_step": k + 1})
            if k == 0:
                first = sum(calls)
        last = sum(calls)
    finally:
        module.canonical_json_bytes = original
    assert last < 2 * first

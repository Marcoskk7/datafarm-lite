"""临时 HDF5 导出（tmp(B)）：`datafarm.episode-hdf5/1`，见 docs/reference/hdf5-export.md。

临时补丁，待 B 线同学确认：仓库此前没有 HDF5 导出器，集成链
（request -> scene -> expert -> recorded Episode -> judge -> render -> HDF5 -> admission）
卡在 HDF5 这一步。这里按 docs/reference/b-control-interfaces.md §8 的最小布局落盘一个 Episode，只做**打包**：数组是录制值的 float64/int64 原样拷贝，不重采样、不取整、不改动作语义。

已知限制（不在本模块解决）：
- 只导出一个 Episode 一个文件；§8 允许逻辑组跨多个文件，多成员 manifest 由 B 线的数据集包负责。
- 只支持 `joint_position_target` 控制模式；其它模式必须新增导出约定。
- robot 段没有关节类型字段，`joint_types` 只能来自 provenance 的关节描述或 state_trace 里的实体关节；
  两者都没有时逐关节写 `unknown` 并在 `meta.joint_types_source` 说明。
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from datafarm.expert.episode import validate_episode
from datafarm.kernel.io import sha256_file
from datafarm.kernel.schema import SchemaError, canonical_json_bytes, validate_ref

SCHEMA = "datafarm.episode-hdf5/1"
CONTROL_MODE = "joint_position_target"
UNVERIFIED = "unverified"
UNKNOWN_JOINT_TYPE = "unknown"
FRAMES_NOT_RENDERED = "not_rendered"
FRAMES_RENDERED = "rendered"
STRING = h5py.string_dtype(encoding="utf-8")
FALLBACK_ROBOT_ID = "robot"


def _fail(message: str) -> None:
    raise ValueError(message)


def _text(value: Any, label: str) -> str:
    """规范 JSON 文本：可移植性由 kernel 检查（不含宿主路径/密钥），键序稳定。"""
    try:
        return canonical_json_bytes(value).decode("utf-8")
    except SchemaError as exc:
        _fail(f"{label} is not portable JSON: {exc}")


def _group_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "/" in value:
        _fail(f"{label} {value!r} cannot be used as an HDF5 group name")
    return value


# ---------------------------------------------------------------- 载荷与派生


def _episode_payload(episode_document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(episode_document, Mapping):
        _fail("episode_document must be an episode document or payload")
    if "state_trace" in episode_document:
        payload = episode_document
    elif isinstance(episode_document.get("payload"), Mapping):
        payload = episode_document["payload"]
    else:
        _fail("episode_document has no payload")
    # 先过录制器合同：profile、N/N+1、contacts 逐条相等、frame_map 为空等都在这里把关。
    return validate_episode(deepcopy(dict(payload)))


def _control_names(actions: Sequence[Mapping[str, Any]], setup: Mapping[str, Any]) -> list[str]:
    """控制通道 = applied 目标键（执行器全量读回），排序固定；逐行 applied 缺通道直接失败（不补值）。

    requested 只含本周期实际下发的通道（录制器实测：move_tcp 只请求手臂 6 关节，move_gripper 只请求主关节），
    必须是 applied 通道的子集；未请求的位置由 `requested_mask` 标为 False，`requested` 该位置写 0.0 且不可解读。
    """
    sets = []
    for index, action in enumerate(actions):
        requested, applied = action["requested_joint_targets"], action["applied_joint_targets"]
        if not set(requested) <= set(applied):
            _fail(f"action {index}: requested channels {sorted(set(requested) - set(applied))} "
                  "are not applied actuator channels")
        sets.append(set(applied))
    first = sets[0] if sets else set()
    for index, names in enumerate(sets):
        if names != first:
            _fail(f"action {index}: control channel set differs from action 0 "
                  f"(missing {sorted(first - names)}, extra {sorted(names - first)})")
    declared = setup.get("control_names")
    if declared is not None and set(declared) != first:
        _fail(f"setup.control_names {sorted(set(declared))} does not match applied target channels "
              f"{sorted(first)}")
    return sorted(first)


def _requested_rows(actions: Sequence[Mapping[str, Any]], control_names: Sequence[str]) -> tuple[list, list]:
    values = [[float(action["requested_joint_targets"].get(joint, 0.0)) for joint in control_names] for action in actions]
    mask = [[joint in action["requested_joint_targets"] for joint in control_names] for action in actions]
    return values, mask


def _robot_node_id(states: Sequence[Mapping[str, Any]], joint_names: Sequence[str],
                   provenance: Mapping[str, Any]) -> str:
    """机器人实体节点 = 关节名与 robot.joint_names 完全一致的那个 entity；可用 provenance 指定。"""
    explicit = provenance.get("robot_node_id")
    if explicit is not None:
        if explicit not in states[0]["entities"]:
            _fail(f"provenance.robot_node_id {explicit!r} is not a scene node")
        return _group_name(explicit, "robot_node_id")
    wanted = list(joint_names)
    entities = states[0]["entities"]
    for node_id in sorted(entities):
        names = [joint["name"] for joint in entities[node_id]["joints"]]
        if names == wanted:
            return _group_name(node_id, "robot node id")
    for node_id in sorted(entities):
        names = {joint["name"] for joint in entities[node_id]["joints"]}
        if wanted and set(wanted) == names:
            return _group_name(node_id, "robot node id")
    return FALLBACK_ROBOT_ID


def _joint_types(joint_names: Sequence[str], states: Sequence[Mapping[str, Any]],
                 robot_node_id: str, provenance: Mapping[str, Any]) -> tuple[list[str], str]:
    """robot 段没有类型字段：优先 provenance 的关节描述，其次同源实体关节，否则写 unknown。"""
    descriptor = provenance.get("robot_joint_types")
    if descriptor is not None:
        if isinstance(descriptor, Mapping):
            mapping = dict(descriptor)
        elif isinstance(descriptor, Sequence) and not isinstance(descriptor, (str, bytes)):
            mapping = {}
            for item in descriptor:
                if isinstance(item, Mapping):
                    mapping[item["name"]] = item.get("type")
                elif isinstance(item, str):
                    mapping[len(mapping)] = item
            mapping = {str(k): v for k, v in mapping.items()}
            if list(mapping) == [str(i) for i in range(len(joint_names))]:
                mapping = dict(zip(joint_names, mapping.values()))
        else:
            _fail("provenance.robot_joint_types must be an object or a list")
        missing = [name for name in joint_names if name not in mapping]
        if missing:
            _fail(f"provenance.robot_joint_types misses joints {missing}")
        types = [mapping[name] for name in joint_names]
        if any(value not in ("revolute", "prismatic") for value in types):
            _fail(f"provenance.robot_joint_types has invalid types {types}")
        return [str(value) for value in types], "provenance"
    if robot_node_id in states[0]["entities"]:
        observed = {joint["name"]: joint["type"] for joint in states[0]["entities"][robot_node_id]["joints"]}
        if all(name in observed for name in joint_names):
            return [observed[name] for name in joint_names], "state_trace_entity"
    return [UNKNOWN_JOINT_TYPE] * len(joint_names), "unknown"


def _vector(state: Mapping[str, Any], node_id: str, key: str) -> list[float]:
    return list(state["entities"][node_id][key])


def _pose(state: Mapping[str, Any], node_id: str) -> list[float]:
    entity = state["entities"][node_id]
    return [float(v) for v in entity["position_m"]] + [float(v) for v in entity["quaternion_xyzw"]]


def _optional_robot_field(states: Sequence[Mapping[str, Any]], key: str, label: str) -> bool:
    present = [key in state["robot"] for state in states]
    if any(present) and not all(present):
        _fail(f"robot.{label} is present in some states but not all")
    return all(present)


# ---------------------------------------------------------------- 写入


def _float_array(group: h5py.Group, name: str, rows: Sequence[Sequence[float]]) -> None:
    group.create_dataset(name, data=np.asarray(rows, dtype=np.float64))


def _int_array(group: h5py.Group, name: str, values: Sequence[int]) -> None:
    group.create_dataset(name, data=np.asarray(values, dtype=np.int64))


def _string_array(group: h5py.Group, name: str, values: Sequence[str]) -> None:
    group.create_dataset(name, data=np.asarray(list(values), dtype=object), dtype=STRING)


def _write_meta(root: h5py.File, payload: Mapping[str, Any], *, robot_node_id: str,
                joint_types: list[str], joint_types_source: str, control_names: Sequence[str],
                judge_result: Any, provenance: Mapping[str, Any], frames_status: str) -> None:
    setup = payload["setup"]
    meta = root.create_group("meta")
    meta.attrs["schema"] = SCHEMA
    meta.attrs["profile"] = payload["profile"]
    meta.attrs["episode_id"] = setup["episode_id"]
    meta.attrs["task_id"] = setup["task_id"]
    meta.attrs["task_revision"] = setup["task_revision"]
    meta.attrs["attempt_id"] = setup["attempt_id"]
    meta.attrs["control_dt_s"] = np.float64(setup["control_dt_s"])
    meta.attrs["control_mode"] = CONTROL_MODE
    meta.attrs["real_ready"] = np.bool_(False)
    meta.attrs["frames"] = frames_status
    meta.attrs["robot_node_id"] = robot_node_id
    meta.attrs["joint_types_source"] = joint_types_source
    meta.attrs["steps_states"] = np.int64(len(payload["state_trace"]))
    meta.attrs["steps_actions"] = np.int64(len(payload["actions"]))
    meta.attrs["source_episode_sha256"] = provenance["episode_ref"]["sha256"]
    meta.attrs["judge_result_json"] = _text(UNVERIFIED if judge_result is None else judge_result,
                                            "judge_result")
    meta.attrs["termination_json"] = _text(payload["termination"], "termination")
    meta.attrs["provenance_json"] = _text(dict(provenance), "provenance")
    _string_array(meta, "joint_names", payload["state_trace"][0]["robot"]["joint_names"])
    _string_array(meta, "joint_types", joint_types)
    _string_array(meta, "control_names", list(control_names))


def _write_state(root: h5py.File, payload: Mapping[str, Any], *, robot_node_id: str) -> None:
    states = payload["state_trace"]
    state = root.create_group("state")
    _int_array(state, "control_step", [entry["control_step"] for entry in states])

    robot = state.create_group("robots").create_group(robot_node_id)
    _float_array(robot, "q", [entry["robot"]["q"] for entry in states])
    _float_array(robot, "dq", [entry["robot"]["dq"] for entry in states])
    if _optional_robot_field(states, "tcp", "tcp"):
        _float_array(robot, "tcp_pose_xyzw", [
            [float(v) for v in entry["robot"]["tcp"]["position_m"]]
            + [float(v) for v in entry["robot"]["tcp"]["quaternion_xyzw"]] for entry in states])
    if _optional_robot_field(states, "gripper", "gripper"):
        _float_array(robot, "gripper_width_m", [[float(entry["robot"]["gripper"]["width_m"])] for entry in states])

    entities = state.create_group("entities")
    for node_id in states[0]["entities"]:
        group = entities.create_group(_group_name(node_id, "entity node id"))
        group.attrs["asset_id"] = states[0]["entities"][node_id]["asset_id"]
        _float_array(group, "pose_xyzw", [_pose(entry, node_id) for entry in states])
        _float_array(group, "linear_velocity_m_s", [_vector(entry, node_id, "linear_velocity_m_s") for entry in states])
        _float_array(group, "angular_velocity_rad_s", [_vector(entry, node_id, "angular_velocity_rad_s") for entry in states])
        joints = states[0]["entities"][node_id]["joints"]
        if joints:
            joint_group = group.create_group("joints")
            for index, joint in enumerate(joints):
                name = _group_name(joint["name"], f"entity {node_id} joint name")
                leaf = joint_group.create_group(name)
                leaf.attrs["type"] = joint["type"]
                leaf.attrs["unit"] = joint["unit"]
                _float_array(leaf, "q", [[float(entry["entities"][node_id]["joints"][index]["q"])] for entry in states])
                _float_array(leaf, "dq", [[float(entry["entities"][node_id]["joints"][index]["dq"])] for entry in states])


def _write_action(root: h5py.File, payload: Mapping[str, Any], *, robot_node_id: str,
                  control_names: Sequence[str]) -> None:
    actions = payload["actions"]
    group = root.create_group("action")
    _int_array(group, "interval_steps", [[index, index + 1] for index in range(len(actions))])
    robot = group.create_group("robots").create_group(robot_node_id)
    requested, mask = _requested_rows(actions, control_names)
    _float_array(robot, "requested", requested)
    robot.create_dataset("requested_mask", data=np.asarray(mask, dtype=np.bool_))
    _float_array(robot, "applied", [[float(action["applied_joint_targets"][joint]) for joint in control_names]
                                    for action in actions])
    _string_array(group, "call_id", [str(action.get("command", {}).get("call_id", "")) for action in actions])


def _write_records(parent: h5py.Group, name: str, records: Sequence[Any], label: str) -> None:
    _string_array(parent, name, [_text(record, f"{label}[{index}]") for index, record in enumerate(records)])


def _frame_groups(manifest: Mapping[str, Any], state_count: int) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    streams = manifest.get("streams")
    if not isinstance(streams, list) or not streams:
        _fail("render manifest has no streams")
    frame_map = manifest.get("frame_map")
    if not isinstance(frame_map, list):
        _fail("render manifest has no frame_map")
    known = set()
    for index, stream in enumerate(streams):
        for key in ("camera_id", "variant_id", "video", "intrinsics", "extrinsics", "frame_count"):
            if key not in stream:
                _fail(f"render manifest streams[{index}] misses {key}")
        _group_name(stream["camera_id"], "camera_id")
        _group_name(stream["variant_id"], "variant_id")
        key = (stream["camera_id"], stream["variant_id"])
        if key in known:
            _fail(f"render manifest repeats stream {key}")
        known.add(key)
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {key: [] for key in known}
    for index, frame in enumerate(frame_map):
        key = (frame.get("camera_id"), frame.get("variant_id"))
        if key not in groups:
            _fail(f"render manifest frame_map[{index}] has no matching stream {key}")
        step = frame.get("control_step")
        if not isinstance(step, int) or isinstance(step, bool) or not 0 <= step < state_count:
            _fail(f"render manifest frame_map[{index}].control_step {step!r} is outside [0, {state_count})")
        if not isinstance(frame.get("frame_index"), int):
            _fail(f"render manifest frame_map[{index}].frame_index must be an integer")
        if not isinstance(frame.get("status"), str) or not frame["status"]:
            _fail(f"render manifest frame_map[{index}].status must be a non-empty string")
        groups[key].append(frame)
    for stream in streams:
        key = (stream["camera_id"], stream["variant_id"])
        if len(groups[key]) != stream["frame_count"]:
            _fail(f"stream {key} declares {stream['frame_count']} frames but frame_map has {len(groups[key])}")
        if sorted(frame["frame_index"] for frame in groups[key]) != list(range(len(groups[key]))):
            _fail(f"stream {key} frame_index is not contiguous from 0")
    return groups


def _write_frames(root: h5py.File, manifest: Mapping[str, Any], state_count: int) -> dict[str, dict[str, int]]:
    groups = _frame_groups(manifest, state_count)
    frames = root.create_group("frames")
    frames.attrs["render_spec_json"] = _text(manifest.get("render_spec"), "render_spec")
    frames.attrs["state_playback_json"] = _text(manifest.get("state_playback"), "state_playback")
    counts: dict[str, dict[str, int]] = {}
    for (camera_id, variant_id), rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda frame: frame["frame_index"])
        group = frames.create_group(camera_id).create_group(variant_id)
        group.attrs["camera_id"] = camera_id
        group.attrs["variant_id"] = variant_id
        group.attrs["frame_count"] = np.int64(len(rows))
        _int_array(group, "control_step", [frame["control_step"] for frame in rows])
        _int_array(group, "frame_index", [frame["frame_index"] for frame in rows])
        _string_array(group, "status", [frame["status"] for frame in rows])
        for key, field in (("video_ref_json", "image"), ("intrinsics_ref_json", "intrinsics"),
                          ("extrinsics_ref_json", "extrinsics")):
            values = []
            for frame in rows:
                reference = frame.get(field)
                if reference is None:
                    values.append("null")
                else:
                    validate_ref(reference, f"frames.{camera_id}.{variant_id}.{field}")
                    values.append(_text(reference, f"frames.{camera_id}.{variant_id}.{field}"))
            _string_array(group, key, values)
        counts.setdefault(camera_id, {})[variant_id] = len(rows)
    return counts


def export_episode(path: str | Path, *, episode_document: Mapping[str, Any], judge_result: Any = None,
                   render_manifest: Mapping[str, Any] | None = None,
                   provenance: Mapping[str, Any]) -> dict[str, Any]:
    """把一个已录制的 Episode 写成单个 HDF5 文件；返回不含宿主路径的摘要。"""
    payload = _episode_payload(episode_document)
    if not isinstance(provenance, Mapping):
        _fail("provenance must be an object")
    if "episode_ref" not in provenance:
        _fail("provenance must bind the source episode as provenance.episode_ref")
    validate_ref(provenance["episode_ref"], "provenance.episode_ref")
    provenance = dict(provenance)

    setup = payload["setup"]
    states, actions = payload["state_trace"], payload["actions"]
    if setup.get("control_mode") not in (None, CONTROL_MODE):
        _fail(f"unsupported control mode {setup['control_mode']!r}; only {CONTROL_MODE} is exported")
    control_names = _control_names(actions, setup)
    joint_names = list(states[0]["robot"]["joint_names"])
    robot_node_id = _robot_node_id(states, joint_names, provenance)
    joint_types, joint_types_source = _joint_types(joint_names, states, robot_node_id, provenance)
    frame_counts = None
    if render_manifest is not None:
        if not isinstance(render_manifest, Mapping):
            _fail("render_manifest must be an object")
        # 帧引用必须指向本 episode 的步号；manifest 自带 episode ref 时顺带核对。
        if render_manifest.get("episode") is not None:
            validate_ref(render_manifest["episode"], "render_manifest.episode")
            if render_manifest["episode"] != provenance["episode_ref"]:
                _fail("render_manifest.episode does not bind the exported episode ref")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(handle)
    try:
        with h5py.File(temporary, "w") as root:
            _write_meta(root, payload, robot_node_id=robot_node_id, joint_types=joint_types,
                        joint_types_source=joint_types_source, control_names=control_names,
                        judge_result=judge_result, provenance=provenance,
                        frames_status=FRAMES_NOT_RENDERED if render_manifest is None else FRAMES_RENDERED)
            _write_state(root, payload, robot_node_id=robot_node_id)
            _write_action(root, payload, robot_node_id=robot_node_id, control_names=control_names)
            _write_records(root.create_group("contacts"), "records", payload["contacts"], "contacts")
            _write_records(root.create_group("events"), "records", payload["events"], "events")
            if render_manifest is not None:
                frame_counts = _write_frames(root, render_manifest, len(states))
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

    return {
        "schema": SCHEMA,
        "episode_id": setup["episode_id"],
        "source_episode_sha256": provenance["episode_ref"]["sha256"],
        "real_ready": False,
        "control_mode": CONTROL_MODE,
        "states": len(states),
        "actions": len(actions),
        "entities": list(states[0]["entities"]),
        "robot_node_id": robot_node_id,
        "joint_names": joint_names,
        "joint_types": joint_types,
        "joint_types_source": joint_types_source,
        "control_names": control_names,
        "contacts": len(payload["contacts"]),
        "events": len(payload["events"]),
        "frames": FRAMES_NOT_RENDERED if render_manifest is None else {"status": FRAMES_RENDERED,
                                                                      "streams": frame_counts},
        "file_sha256": sha256_file(destination),
    }


# ---------------------------------------------------------------- 读取


def _strings(dataset: h5py.Dataset) -> list[str]:
    return [str(value) for value in dataset.asstr()[...]]


def _attr_string(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def read_episode(path: str | Path) -> dict[str, Any]:
    """把导出的 HDF5 读回为普通 dict（数组仍是 numpy，浮点逐位不变）。"""
    out: dict[str, Any] = {"meta": {}, "state": {"robots": {}, "entities": {}},
                           "action": {"robots": {}}, "contacts": [], "events": [], "frames": {}}
    with h5py.File(path, "r") as root:
        meta = root["meta"]
        for key, value in meta.attrs.items():
            out["meta"][key] = _attr_string(value) if isinstance(value, (str, bytes)) else value
        out["meta"]["joint_names"] = _strings(meta["joint_names"])
        out["meta"]["joint_types"] = _strings(meta["joint_types"])
        out["meta"]["control_names"] = _strings(meta["control_names"])
        out["meta"]["judge_result"] = json.loads(out["meta"]["judge_result_json"])
        out["meta"]["termination"] = json.loads(out["meta"]["termination_json"])
        out["meta"]["provenance"] = json.loads(out["meta"]["provenance_json"])

        state = root["state"]
        out["state"]["control_step"] = state["control_step"][()]
        for name, group in state["robots"].items():
            robot = {"q": group["q"][()], "dq": group["dq"][()]}
            for key in ("tcp_pose_xyzw", "gripper_width_m"):
                if key in group:
                    robot[key] = group[key][()]
            out["state"]["robots"][name] = robot
        for node_id, group in state["entities"].items():
            entity = {key: group[key][()] for key in
                      ("pose_xyzw", "linear_velocity_m_s", "angular_velocity_rad_s")}
            entity["asset_id"] = _attr_string(group.attrs["asset_id"])
            entity["joints"] = {}
            if "joints" in group:
                for name, leaf in group["joints"].items():
                    entity["joints"][name] = {"type": _attr_string(leaf.attrs["type"]),
                                              "unit": _attr_string(leaf.attrs["unit"]),
                                              "q": leaf["q"][()], "dq": leaf["dq"][()]}
            out["state"]["entities"][node_id] = entity

        action = root["action"]
        out["action"]["interval_steps"] = action["interval_steps"][()]
        out["action"]["call_id"] = _strings(action["call_id"])
        for name, group in action["robots"].items():
            out["action"]["robots"][name] = {"requested": group["requested"][()],
                                             "requested_mask": group["requested_mask"][()],
                                             "applied": group["applied"][()]}
        if "contacts" in root:
            out["contacts"] = [json.loads(text) for text in _strings(root["contacts"]["records"])]
        if "events" in root:
            out["events"] = [json.loads(text) for text in _strings(root["events"]["records"])]
        if "frames" in root:
            frames = root["frames"]
            out["frames"]["render_spec"] = json.loads(_attr_string(frames.attrs["render_spec_json"]))
            out["frames"]["state_playback"] = json.loads(_attr_string(frames.attrs["state_playback_json"]))
            out["frames"]["streams"] = {}
            for camera_id, camera in frames.items():
                out["frames"]["streams"][camera_id] = {}
                for variant_id, group in camera.items():
                    out["frames"]["streams"][camera_id][variant_id] = {
                        key: _strings(group[key]) if key.endswith("_json") else group[key][()]
                        for key in ("control_step", "frame_index", "status",
                                    "video_ref_json", "intrinsics_ref_json", "extrinsics_ref_json")}
    return out


# ---------------------------------------------------------------- 校验


def _expect_array(root: h5py.File, path: str, expected: Any, dtype: Any, problems: list[str]) -> None:
    item = root.get(path)
    if item is None:
        problems.append(f"missing dataset {path}")
        return
    if not isinstance(item, h5py.Dataset):
        problems.append(f"{path} is not a dataset")
        return
    if item.dtype != np.dtype(dtype):
        problems.append(f"{path} dtype {item.dtype} != {np.dtype(dtype)}")
    actual = item[()]
    expected = np.asarray(expected, dtype=dtype)
    if actual.shape != expected.shape:
        problems.append(f"{path} shape {actual.shape} != {expected.shape}")
    elif not np.array_equal(actual, expected):
        problems.append(f"{path} values differ from the episode payload")


def _expect_shape(root: h5py.File, path: str, shape: tuple[int, ...], dtype: Any, problems: list[str]) -> None:
    item = root.get(path)
    if item is None:
        problems.append(f"missing dataset {path}")
        return
    if not isinstance(item, h5py.Dataset):
        problems.append(f"{path} is not a dataset")
        return
    if item.dtype != np.dtype(dtype):
        problems.append(f"{path} dtype {item.dtype} != {np.dtype(dtype)}")
    if item.shape != shape:
        problems.append(f"{path} shape {item.shape} != {shape}")


def _expect_strings(root: h5py.File, path: str, expected: Sequence[str], problems: list[str]) -> None:
    item = root.get(path)
    if item is None:
        problems.append(f"missing dataset {path}")
        return
    if not isinstance(item, h5py.Dataset):
        problems.append(f"{path} is not a dataset")
        return
    info = h5py.check_string_dtype(item.dtype)
    if info is None or info.encoding != "utf-8":
        problems.append(f"{path} is not UTF-8 strings ({item.dtype})")
        return
    try:
        actual = _strings(item)
    except (ValueError, TypeError) as exc:
        problems.append(f"{path} cannot be read as UTF-8: {exc}")
        return
    if actual != list(expected):
        problems.append(f"{path} values differ from the episode payload")


def validate_hdf5(path: str | Path, episode_payload: Mapping[str, Any] | None = None) -> list[str]:
    """检查布局/类型/形状；给出 episode payload 时逐数组核对（float64 逐位相等）。空列表表示通过。"""
    problems: list[str] = []
    try:
        root = h5py.File(path, "r")
    except OSError as exc:
        return [f"not an HDF5 file: {exc}"]
    with root:
        meta = root.get("meta")
        if not isinstance(meta, h5py.Group):
            return ["missing group /meta"]
        if _attr_string(meta.attrs.get("schema")) != SCHEMA:
            problems.append(f"meta.schema must be {SCHEMA}")
        if _attr_string(meta.attrs.get("control_mode")) != CONTROL_MODE:
            problems.append(f"meta.control_mode must be {CONTROL_MODE}")
        if bool(meta.attrs.get("real_ready", True)):
            problems.append("meta.real_ready must be false: simulation output is not real-ready data")
        for key in ("episode_id", "task_id", "task_revision", "attempt_id", "control_dt_s", "frames",
                    "robot_node_id", "joint_types_source", "judge_result_json", "termination_json",
                    "provenance_json"):
            if key not in meta.attrs:
                problems.append(f"meta.{key} attribute missing")
        if problems:
            return problems

        for key in ("joint_names", "joint_types", "control_names"):
            if not isinstance(meta.get(key), h5py.Dataset):
                problems.append(f"missing dataset meta/{key}")
        if problems:
            return problems
        control_names = _strings(meta["control_names"])
        joint_names = _strings(meta["joint_names"])
        joint_types = _strings(meta["joint_types"])
        if len(joint_types) != len(joint_names):
            problems.append("meta.joint_types length != meta.joint_names length")

        step_dataset = root.get("state/control_step")
        if not isinstance(step_dataset, h5py.Dataset):
            problems.append("missing dataset state/control_step")
            return problems
        if step_dataset.dtype != np.int64:
            problems.append(f"state/control_step dtype {step_dataset.dtype} != int64")
        steps = step_dataset[()]
        if steps.ndim != 1:
            problems.append("state/control_step must be one dimensional")
            return problems
        states = len(steps)
        if list(steps) != list(range(states)):
            problems.append("state/control_step must be contiguous from 0")
        actions = states - 1

        interval = root.get("action/interval_steps")
        if not isinstance(interval, h5py.Dataset):
            problems.append("missing dataset action/interval_steps")
            return problems
        if interval.dtype != np.int64:
            problems.append(f"action/interval_steps dtype {interval.dtype} != int64")
        rows = interval[()]
        if rows.shape != (actions, 2):
            problems.append(f"action/interval_steps shape {rows.shape} != {(actions, 2)}")
            actions = rows.shape[0] if rows.ndim == 2 else 0
        else:
            for index, row in enumerate(rows):
                if list(row) != [index, index + 1]:
                    problems.append(f"action/interval_steps[{index}] is {list(row)}, expected {[index, index + 1]}")
        if states != actions + 1:
            problems.append(f"expected N+1 states for N actions, got {states} states and {actions} intervals")

        robot_id = _attr_string(meta.attrs["robot_node_id"])
        robot_path = f"state/robots/{robot_id}"
        for name in ("q", "dq"):
            _expect_shape(root, f"{robot_path}/{name}", (states, len(joint_names)), np.float64, problems)
        for name, width in (("tcp_pose_xyzw", 7), ("gripper_width_m", 1)):
            if root.get(f"{robot_path}/{name}") is not None:
                _expect_shape(root, f"{robot_path}/{name}", (states, width), np.float64, problems)

        entities = root.get("state/entities")
        if not isinstance(entities, h5py.Group) or not len(entities):
            problems.append("missing group /state/entities")
        else:
            for node_id, entity in entities.items():
                if not isinstance(entity, h5py.Group):
                    problems.append(f"state/entities/{node_id} is not a group")
                    continue
                _expect_shape(root, f"state/entities/{node_id}/pose_xyzw", (states, 7), np.float64, problems)
                for key in ("linear_velocity_m_s", "angular_velocity_rad_s"):
                    _expect_shape(root, f"state/entities/{node_id}/{key}", (states, 3), np.float64, problems)
                if "joints" in entity:
                    for joint, leaf in entity["joints"].items():
                        for key in ("q", "dq"):
                            _expect_shape(root, f"state/entities/{node_id}/joints/{joint}/{key}",
                                          (states, 1), np.float64, problems)

        action_robot = f"action/robots/{robot_id}"
        for name in ("requested", "applied"):
            _expect_shape(root, f"{action_robot}/{name}", (actions, len(control_names)), np.float64, problems)
        _expect_shape(root, f"{action_robot}/requested_mask", (actions, len(control_names)), np.bool_, problems)
        call_id = root.get("action/call_id")
        if not isinstance(call_id, h5py.Dataset) or call_id.shape != (actions,) or \
                h5py.check_string_dtype(call_id.dtype) is None:
            problems.append("action/call_id must be a UTF-8 string dataset with one row per action")

        contacts = root.get("contacts/records")
        if not isinstance(contacts, h5py.Dataset) or contacts.shape != (states,):
            problems.append("contacts/records must hold one UTF-8 JSON record per state")
        else:
            for index, text in enumerate(_strings(contacts)):
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    problems.append(f"contacts/records[{index}] is not JSON")
                    continue
                if record.get("availability") == "unavailable" and record.get("rows") is not None:
                    problems.append(f"contacts/records[{index}] is unavailable but rows is not null")
                if record.get("availability") == "available" and not isinstance(record.get("rows"), list):
                    problems.append(f"contacts/records[{index}] is available but rows is not a list")
                if record.get("control_step") != index:
                    problems.append(f"contacts/records[{index}] control_step {record.get('control_step')} != {index}")
        events = root.get("events/records")
        if not isinstance(events, h5py.Dataset) or h5py.check_string_dtype(events.dtype) is None:
            problems.append("events/records must be a UTF-8 JSON dataset")

        frames_status = _attr_string(meta.attrs["frames"])
        if frames_status == FRAMES_NOT_RENDERED:
            if "frames" in root:
                problems.append("meta.frames is not_rendered but /frames exists")
        elif frames_status == FRAMES_RENDERED:
            frames = root.get("frames")
            if not isinstance(frames, h5py.Group) or not len(frames):
                problems.append("meta.frames is rendered but /frames has no stream")
            else:
                for camera_id, camera in frames.items():
                    if not isinstance(camera, h5py.Group):
                        continue
                    for variant_id, group in camera.items():
                        counts = {key: len(group[key]) for key in
                                  ("control_step", "frame_index", "status",
                                   "video_ref_json", "intrinsics_ref_json", "extrinsics_ref_json")
                                  if isinstance(group.get(key), h5py.Dataset)}
                        if len(set(counts.values())) != 1:
                            problems.append(f"frames/{camera_id}/{variant_id} leaf lengths differ: {counts}")
                        steps_of_frames = group["control_step"][()]
                        if any(not 0 <= int(step) < states for step in steps_of_frames):
                            problems.append(f"frames/{camera_id}/{variant_id} control_step out of range")
                        statuses = _strings(group["status"])
                        if len(statuses) != len(steps_of_frames):
                            problems.append(f"frames/{camera_id}/{variant_id} status length mismatch")
        else:
            problems.append(f"meta.frames must be {FRAMES_NOT_RENDERED} or {FRAMES_RENDERED}")

        if episode_payload is not None:
            _compare_payload(root, episode_payload, problems, states=states, robot_id=robot_id,
                             control_names=control_names)
    return problems


def _compare_payload(root: h5py.File, payload: Mapping[str, Any], problems: list[str], *,
                     states: int, robot_id: str, control_names: Sequence[str]) -> None:
    source = validate_episode(deepcopy(dict(payload)))
    trace, actions = source["state_trace"], source["actions"]
    if len(trace) != states:
        problems.append(f"payload has {len(trace)} states but file has {states}")
        return
    _expect_array(root, "state/control_step", [entry["control_step"] for entry in trace], np.int64, problems)
    _expect_strings(root, "meta/joint_names", trace[0]["robot"]["joint_names"], problems)
    if actions:
        applied = sorted(actions[0]["applied_joint_targets"])
        _expect_strings(root, "meta/control_names", _control_names(actions, source["setup"]), problems)
        if list(control_names) != applied:
            problems.append(f"meta/control_names {list(control_names)} != applied channels {applied}")

    _expect_array(root, f"state/robots/{robot_id}/q", [entry["robot"]["q"] for entry in trace], np.float64, problems)
    _expect_array(root, f"state/robots/{robot_id}/dq", [entry["robot"]["dq"] for entry in trace], np.float64, problems)
    if all("tcp" in entry["robot"] for entry in trace):
        _expect_array(root, f"state/robots/{robot_id}/tcp_pose_xyzw",
                      [[float(v) for v in entry["robot"]["tcp"]["position_m"]]
                       + [float(v) for v in entry["robot"]["tcp"]["quaternion_xyzw"]] for entry in trace],
                      np.float64, problems)
    if all("gripper" in entry["robot"] for entry in trace):
        _expect_array(root, f"state/robots/{robot_id}/gripper_width_m",
                      [[float(entry["robot"]["gripper"]["width_m"])] for entry in trace], np.float64, problems)

    for node_id in trace[0]["entities"]:
        _expect_array(root, f"state/entities/{node_id}/pose_xyzw", [_pose(entry, node_id) for entry in trace],
                      np.float64, problems)
        for key in ("linear_velocity_m_s", "angular_velocity_rad_s"):
            _expect_array(root, f"state/entities/{node_id}/{key}", [_vector(entry, node_id, key) for entry in trace],
                          np.float64, problems)
        for index, joint in enumerate(trace[0]["entities"][node_id]["joints"]):
            for name in ("q", "dq"):
                _expect_array(root, f"state/entities/{node_id}/joints/{joint['name']}/{name}",
                              [[float(entry["entities"][node_id]["joints"][index][name])] for entry in trace],
                              np.float64, problems)

    requested, mask = _requested_rows(actions, control_names)
    _expect_array(root, f"action/robots/{robot_id}/requested", requested, np.float64, problems)
    _expect_array(root, f"action/robots/{robot_id}/requested_mask", mask, np.bool_, problems)
    _expect_array(root, f"action/robots/{robot_id}/applied",
                  [[float(action["applied_joint_targets"][joint]) for joint in control_names] for action in actions],
                  np.float64, problems)
    _expect_strings(root, "action/call_id",
                    [str(action.get("command", {}).get("call_id", "")) for action in actions], problems)
    _expect_strings(root, "contacts/records", [_text(record, "contacts") for record in source["contacts"]], problems)
    _expect_strings(root, "events/records", [_text(record, "events") for record in source["events"]], problems)

    if "frames" in root and source["frame_map"]:
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for frame in source["frame_map"]:
            grouped.setdefault((frame["camera_id"], frame["variant_id"]), []).append(frame)
        for (camera_id, variant_id), rows in grouped.items():
            rows = sorted(rows, key=lambda frame: frame["frame_index"])
            prefix = f"frames/{camera_id}/{variant_id}"
            _expect_array(root, f"{prefix}/control_step", [frame["control_step"] for frame in rows],
                          np.int64, problems)
            _expect_array(root, f"{prefix}/frame_index", [frame["frame_index"] for frame in rows],
                          np.int64, problems)
            _expect_strings(root, f"{prefix}/status", [frame["status"] for frame in rows], problems)

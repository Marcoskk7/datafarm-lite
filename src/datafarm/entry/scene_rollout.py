"""Trusted composition root for request-bound full-scene recording (no task registry)."""
from __future__ import annotations
from copy import deepcopy
import importlib
from pathlib import Path

from datafarm.adapters.sim.scene_runtime import load_scene, initialize
from datafarm.adapters.sim.scene_measurements import SceneMeasurements
from datafarm.entry.robot_control import bind_isaac_tcp_control
from datafarm.expert.episode import EpisodeRecorder, episode_identity
from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.io import sha256_file, utc_now
from datafarm.task.request_profile import validate_task_payload


class SceneActions:
    """Narrow expert port, not the host, scene, store, Judge or reset authority."""
    def __init__(self, api, measurements, host):
        self._api, self._measurements, self._host = api, measurements, host
        self._results = []

    @property
    def results(self):
        return deepcopy(self._results)

    def observe(self):
        if not self._host.healthy or self._host.step_outcome_unknown:
            raise ValueError("host requires requalification")
        return self._measurements.snapshot(self._host.control_step, self._host.dt_s)

    def move_tcp(self, request):
        value = self._api.move_tcp(request)
        self._results.append(deepcopy(value))
        return value

    def move_gripper(self, request):
        value = self._api.move_gripper(request)
        self._results.append(deepcopy(value))
        return value


def record_scene_episode(*, store, task_ref, scene_ref, prepared, asset_roots,
                         api, simulation, scene, control_profile, runtime_ref, expert_ref,
                         attempt_id, reset, settle_steps, expert, max_bytes,
                         task_contract_ref=None, randomization=None, cancelled=None, contact_query=None, expert_executor=None, execution_runtime=None):
    """Use already initialized host resources and an explicitly supplied expert callable.

    Host must qualify reset/resources; Python attribute visibility is NOT an expert
    sandbox. A broker/OS boundary remains required for untrusted programs. This
    API neither launches a model nor certifies task success or replay.
    """
    if (callable(expert) == callable(expert_executor)) or api._tcp_motion_service is not None:
        raise ValueError("fresh API binding and explicit expert callable required")
    resolved = load_scene(store, scene_ref, prepared, asset_roots=asset_roots)
    task = store.load(ArtifactRef.from_dict(task_ref))
    payload = validate_task_payload(task["payload"])
    if payload["scene_id"] != resolved["graph"]["scene_id"]:
        raise ValueError("task is bound to a different scene")
    robot_node = next(n for n in resolved["graph"]["nodes"] if n["semantic_tag"] == "robot")
    from datafarm.adapters.sim.isaac_scene import entity
    if entity(scene, robot_node["node_id"]) is not api.robot:
        raise ValueError("API robot is not the bound scene entity")
    from datafarm.simulation.robot_binding import verify_binding
    verify_binding(api.binding)
    source = resolved["records"][robot_node["asset_id"]]
    urdfs = [f["artifact"]["sha256"] for f in source["files"] if f["format"] == "urdf"]
    if urdfs != [api.binding["descriptor"]["source_sha256"]]:
        raise ValueError("API descriptor is not bound to the A robot URDF")
    runtime = store.load(ArtifactRef.from_dict(runtime_ref))
    source_runtime = store.load(ArtifactRef.from_dict(resolved["graph"]["runtime_profile"]))
    if runtime != source_runtime or runtime.get("physics_dt_s") != simulation.get_physics_dt():
        raise ValueError("runtime differs from source scene or physics clock")
    expert_bytes = store.load_bytes(ArtifactRef.from_dict(expert_ref))
    import inspect
    if expert_executor is None and (not inspect.isfunction(expert) or Path(inspect.getfile(expert)).read_bytes() != expert_bytes):
        raise ValueError("expert callable must come from the exact pinned source file")
    if task["schema"] != "datafarm.task-ir" or task["artifact_id"] != payload["task_revision"] or scene_ref not in task["evidence"]:
        raise ValueError("task revision or scene digest mismatch")
    known = {n["node_id"] for n in resolved["graph"]["nodes"]}
    if any(r["node_id"] not in known for r in payload["roles"]):
        raise ValueError("task role is not in the bound scene")
    if len(task["predecessors"]) != 1 or task["predecessors"][0]["kind"] != "task-request":
        raise ValueError("one normalized request predecessor required")
    request_ref = task["predecessors"][0]
    from datafarm.task.request_profile import validate_request
    request = validate_request(store.load(ArtifactRef.from_dict(request_ref)))
    store.load_bytes(ArtifactRef.from_dict(request["original_request_ref"]))
    if request["request_id"] != payload["request_id"]:
        raise ValueError("request identity differs from task")
    if task_contract_ref is not None:
        from datafarm.judge.contract_binding import verify_contract
        verify_contract(store, task_contract_ref, task_ref=task_ref, scene_ref=scene_ref)
    from datafarm.kernel.identifiers import logical_id
    from datafarm.kernel.schema import canonical_json_bytes
    logical_id(attempt_id, label="attempt_id")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("recording byte cap required")
    canonical_json_bytes(randomization)
    if randomization not in (None, {}):
        raise ValueError("randomization application requires an independently bound source scene revision")
    from datafarm.simulation.tcp_control import ControlProfile
    validated_profile = ControlProfile.from_dict(control_profile) if isinstance(control_profile, dict) else control_profile
    if not isinstance(validated_profile, ControlProfile) or validated_profile.dt_s != simulation.get_physics_dt():
        raise ValueError("control profile must bind the physics clock")
    setup = {"episode_id": episode_identity(payload["task_id"], payload["task_revision"], attempt_id),
             "task_id": payload["task_id"], "task_revision": payload["task_revision"], "attempt_id": attempt_id,
             "request_ref": deepcopy(request_ref), "original_request_ref": deepcopy(request["original_request_ref"]),
             "task_ref": deepcopy(task_ref), "scene_ref": deepcopy(scene_ref), "asset_refs": resolved["asset_refs"],
             "runtime_ref": deepcopy(runtime_ref), "expert_ref": deepcopy(expert_ref),
             "task_contract_ref": deepcopy(task_contract_ref), "randomization": deepcopy(randomization),
             "control_dt_s": simulation.get_physics_dt(), "node_ids": [n["node_id"] for n in resolved["graph"]["nodes"]],
             "initialization": {"reset": "host_owned", "settle_steps": settle_steps, "state_restore": "unverified"}, "control_profile": control_profile.to_dict() if hasattr(control_profile, "to_dict") else deepcopy(control_profile),
             "source_sha256": {n: sha256_file(importlib.import_module(n).__file__) for n in
                  ("datafarm.entry.scene_rollout", "datafarm.adapters.sim.scene_runtime",
                   "datafarm.adapters.sim.scene_measurements", "datafarm.adapters.sim.isaac_control",
                   "datafarm.adapters.sim.isaac_ik", "datafarm.entry.robot_control", "datafarm.simulation.isaac_robot",
                   "datafarm.adapters.sim.isaac_scene", "datafarm.adapters.sim.usd_assets",
                   "datafarm.simulation.tcp_control", "datafarm.expert.episode",
                   "datafarm.adapters.agent_harness.scene_execution", "datafarm.adapters.agent_harness.scene_worker",
                   "datafarm.adapters.sandbox.proot", "datafarm.adapters.sandbox.proot_exec",
                   "datafarm.adapters.sandbox.scene_proot")}}
    if execution_runtime is not None:
        if (not isinstance(execution_runtime,dict) or execution_runtime.get('mode') != 'proot'
                or execution_runtime.get('security_boundary') is not False
                or execution_runtime.get('acknowledge_weak_isolation') is not True):
            raise ValueError('explicit weak PRoot execution metadata required')
        setup['execution_runtime'] = deepcopy(execution_runtime)
    profile = control_profile.to_dict() if hasattr(control_profile, "to_dict") else control_profile
    recorder = EpisodeRecorder(setup, max_steps=profile["max_total_control_steps"], max_bytes=max_bytes)
    measurements = SceneMeasurements(scene, resolved["graph"], resolved["records"], api, contact_query=contact_query, rejected_sink=recorder.rejected_measurement)
    from datafarm.kernel.schema import canonical_json_bytes as encode_setup
    if len(encode_setup(setup)) > max_bytes:
        raise ValueError("episode setup exceeds byte cap")
    try:
        initialization = initialize(simulation, scene, resolved, reset=reset, settle_steps=settle_steps)
        recorder.setup["initialization"] = initialization
        recorder.initial(measurements.snapshot(0, simulation.get_physics_dt()))
    except Exception:
        failure = {"episode_id": setup["episode_id"], "task_ref": deepcopy(task_ref),
                   "scene_ref": deepcopy(scene_ref), "reason_code": "INITIALIZATION_OR_STATE_ERROR",
                   "source_sha256": setup["source_sha256"], "trace_complete": False}
        if recorder.rejected_measurements:
            import json
            raw = json.dumps(recorder.rejected_measurements, ensure_ascii=False, allow_nan=True).encode("utf-8")
            failure["raw_failure_ref"] = store.put_bytes("episode.rejected-measurements", raw,
                media_type="application/vnd.datafarm.raw-measurements+json").to_dict()
        reference = store.put_json("episode.initialization-failure", failure)
        return {"episode_ref": None, "failure_ref": reference.to_dict(), "episode_id": setup["episode_id"],
                "execution_status": "error", "trace_complete": False, "task_success": None,
                "judgement": "unverified", "action_replay": "unverified"}
    frame_ids = {f["frame_id"] for f in resolved["graph"]["frames"]} | set(setup["node_ids"])
    if any(role["frame_id"] not in frame_ids for role in payload["roles"]):
        raise ValueError("task role frame not bound to scene")
    frames = {name: (lambda n=name: measurements.resolve_frame(n, service.host.control_step, service.host.dt_s))
              for name in frame_ids if name != "world"}
    status, reason = "error", "RUNTIME_BINDING_ERROR"
    actions = None
    try:
        service = bind_isaac_tcp_control(api, simulation, control_profile, frame_resolvers=frames,
            scene=scene, measurements=measurements, recorder=recorder,
            event_sink=recorder.event, cancelled=cancelled)
        actions = SceneActions(api, measurements, service.host)
        status, reason = "completed", "EXPERT_RETURNED"
        expert_executor(actions, expert_bytes) if expert_executor is not None else expert(actions)
        unsuccessful = next((v for v in actions.results if v["status"] != "completed"), None)
        if unsuccessful is not None:
            status = unsuccessful["status"] if unsuccessful["status"] in ("error", "aborted", "timeout") else "failed"
            reason = unsuccessful["reason_code"]
        if service.host.step_outcome_unknown or not service.host.healthy:
            status, reason = "error", "HOST_REQUALIFICATION_REQUIRED"
        elif cancelled is not None and service.host.cancelled():
            status, reason = "aborted", "CANCELLED"
    except TimeoutError:
        status, reason = "timeout", "EXPERT_WALL_BUDGET"
        if actions is not None and (service.host.step_outcome_unknown or not service.host.healthy):
            status, reason = "error", "HOST_REQUALIFICATION_REQUIRED"
    except Exception:
        status, reason = "error", "EXPERT_OR_RUNTIME_ERROR" if actions is not None else "RUNTIME_BINDING_ERROR"
        # Private traceback data must not enter portable evidence.
    try:
        recorder.event({"kind": "expert_execution_end", "primitive_results": actions.results if actions is not None else [],
                        "status": status, "reason_code": reason})
    except ValueError:
        status, reason = "error", "RECORDING_BUDGET_EXHAUSTED"
    result = recorder.finish(status, reason)
    if recorder.rejected_measurements:
        import json
        raw = json.dumps(recorder.rejected_measurements, ensure_ascii=False, allow_nan=True).encode("utf-8")
        result["raw_failure_ref"] = store.put_bytes("episode.rejected-measurements", raw,
            media_type="application/vnd.datafarm.raw-measurements+json").to_dict()
    reference = store.put_document(recorder.document(result, created_at=utc_now()))
    return {"episode_ref": reference.to_dict(), "episode_id": setup["episode_id"],
            "execution_status": status, "trace_complete": result["termination"]["trace_complete"],
            "task_success": None, "judgement": "unverified", "action_replay": "unverified"}

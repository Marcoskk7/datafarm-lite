"""Restore and replay measured state using initialized Isaac resources, no renderer."""
from copy import deepcopy
import threading
import math
from datafarm.adapters.sim.isaac_scene import entity
from datafarm.expert.episode import validate_state


def tensor(value, like):
    import torch
    like = like.torch if hasattr(like, "torch") else like
    return torch.tensor([value], dtype=like.dtype, device=like.device)


class IsaacReplayHost:
    def __init__(self, *, setup, scene, simulation, api, measurements, reset, store, prepared, asset_roots):
        self.setup = deepcopy(setup)
        self.scene, self.simulation, self.api, self.measurements, self.reset = scene, simulation, api, measurements, reset
        self.store, self.prepared, self.asset_roots = store, prepared, asset_roots
        self.dt = setup["control_dt_s"]
        self.owned = self.pending = False
        self.steps = 0
        self.unhealthy = False
        if simulation.get_physics_dt() != self.dt:
            raise ValueError("replay physics dt differs")

    def qualification(self):
        return "source_bound_runtime"

    def source_binding(self):
        from datafarm.adapters.sim.scene_runtime import load_scene
        from datafarm.kernel.artifacts import ArtifactRef
        from datafarm.kernel.io import sha256_file
        from datafarm.simulation.robot_binding import verify_binding
        resolved = load_scene(self.store, self.setup["scene_ref"], self.prepared, asset_roots=self.asset_roots)
        graph = resolved["graph"]
        if self.setup["node_ids"] != [n["node_id"] for n in graph["nodes"]] or self.setup["asset_refs"] != resolved["asset_refs"]:
            raise ValueError("replay source scene/assets differ")
        runtime = self.store.load(ArtifactRef.from_dict(self.setup["runtime_ref"]))
        if runtime != self.store.load(ArtifactRef.from_dict(graph["runtime_profile"])) or runtime.get("physics_dt_s") != self.simulation.get_physics_dt():
            raise ValueError("replay runtime differs")
        if self.measurements.scene is not self.scene or self.measurements.api is not self.api or self.measurements.graph != graph or self.measurements.records != resolved["records"]:
            raise ValueError("measurement port not bound to source scene")
        robot = next(n for n in graph["nodes"] if n["semantic_tag"] == "robot")
        if entity(self.scene, robot["node_id"]) is not self.api.robot:
            raise ValueError("API does not own scene robot")
        verify_binding(self.api.binding)
        hashes = [f["artifact"]["sha256"] for f in resolved["records"][robot["asset_id"]]["files"] if f["format"] == "urdf"]
        if hashes != [self.api.binding["descriptor"]["source_sha256"]]:
            raise ValueError("API descriptor/source differs")
        import importlib
        for name, digest in self.setup["source_sha256"].items():
            if sha256_file(importlib.import_module(name).__file__) != digest:
                raise ValueError("recording runtime implementation differs")
        self.store.load_bytes(ArtifactRef.from_dict(self.setup["expert_ref"]))
        return deepcopy(self.setup)

    def acquire(self):
        if self.unhealthy or self.owned or not self.api._motion_lock.acquire(blocking=False):
            return False
        self.owned = True
        self.api._command_owner = threading.get_ident()
        return True

    def release(self):
        self._check_owner()
        self.api._command_owner = None
        self.owned = False
        self.api._motion_lock.release()

    def _check_owner(self):
        if not self.owned or self.api._command_owner != threading.get_ident():
            raise ValueError("exclusive replay owner required")

    def _ready(self):
        self._check_owner()
        if self.unhealthy or self.simulation.get_physics_dt() != self.dt:
            raise ValueError("replay host needs requalification")

    def restore(self, state):
        self._ready()
        self.unhealthy = True
        validate_state(state, self.setup["node_ids"], 0, self.dt)
        self.reset()
        for node, reading in state["entities"].items():
            handle = entity(self.scene, node)
            handle.write_root_pose_to_sim_index(root_pose=tensor(reading["position_m"] + reading["quaternion_xyzw"], handle.data.root_pos_w))
            handle.write_root_velocity_to_sim_index(root_velocity=tensor(reading["linear_velocity_m_s"] + reading["angular_velocity_rad_s"], handle.data.root_pos_w))
            if reading["joints"]:
                if list(handle.joint_names) != [j["name"] for j in reading["joints"]]:
                    raise ValueError("replay joint order differs")
                q = tensor([j["q"] for j in reading["joints"]], handle.data.joint_pos)
                handle.write_joint_position_to_sim_index(position=q)
                handle.write_joint_velocity_to_sim_index(velocity=tensor([j["dq"] for j in reading["joints"]], handle.data.joint_vel))
                handle.set_joint_position_target_index(target=q)
        self.scene.write_data_to_sim()
        self.simulation.forward()
        self.scene.update(self.dt)
        self.steps = 0
        self.pending = False
        self.unhealthy = False

    def apply(self, targets):
        self._ready()
        if self.pending:
            raise ValueError("unconsumed replay input")
        from datafarm.simulation.robot_binding import verify_binding
        verify_binding(self.api.binding)
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in targets.values()):
            raise ValueError("invalid replay targets")
        if set(targets) != set(self.api.binding["joint_names"]):
            raise ValueError("all recorded robot actuator inputs required")
        names = self.api.binding["joint_names"]
        value = tensor([targets[n] for n in names], self.api.robot.data.joint_pos)
        self.unhealthy = True
        self.api.robot.actuators.target_command.set_position_index(value=value, joint_ids=list(range(len(names))))
        self.scene.write_data_to_sim()
        value = self.api.robot.actuators.target_command.position
        value = value.torch if hasattr(value, "torch") else value
        values = value.detach().cpu().tolist()[0]
        readback = {n: values[self.api.binding["joint_index"][n]] for n in targets}
        if readback != targets:
            raise ValueError("recorded actuator input cannot be restored exactly")
        self.pending = True
        self.unhealthy = False
        return readback

    def step(self):
        self._ready()
        if not self.pending:
            raise ValueError("replay step has no applied input")
        try:
            self.simulation.step(render=False)
            self.scene.update(self.dt)
            self.steps += 1
            self.pending = False
        except Exception:
            self.unhealthy = True
            raise

    def snapshot(self, step):
        self._ready()
        if self.pending or step != self.steps:
            raise ValueError("replay snapshot clock differs")
        try:
            return self.measurements.snapshot(step, self.dt)
        except Exception:
            self.unhealthy = True
            raise

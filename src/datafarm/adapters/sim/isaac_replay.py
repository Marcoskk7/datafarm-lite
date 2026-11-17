"""Restore and replay measured state using initialized Isaac resources, no renderer."""
from copy import deepcopy
from datafarm.adapters.sim.isaac_scene import entity
from datafarm.expert.episode import validate_state


def tensor(value, like):
    import torch
    like = like.torch if hasattr(like, "torch") else like
    return torch.tensor([value], dtype=like.dtype, device=like.device)


class IsaacReplayHost:
    def __init__(self, *, setup, scene, simulation, api, measurements, reset):
        self.setup = deepcopy(setup)
        self.scene, self.simulation, self.api, self.measurements, self.reset = scene, simulation, api, measurements, reset
        self.dt = setup["control_dt_s"]
        self.unhealthy = False
        if simulation.get_physics_dt() != self.dt:
            raise ValueError("replay physics dt differs")

    def source_binding(self):
        return deepcopy(self.setup)

    def restore(self, state):
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

    def apply(self, targets):
        import math
        from datafarm.simulation.robot_binding import verify_binding
        verify_binding(self.api.binding)
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in targets.values()):
            raise ValueError("invalid replay targets")
        if set(targets) != set(self.api.binding["joint_names"]):
            raise ValueError("all recorded robot actuator inputs required")
        names = self.api.binding["joint_names"]
        value = tensor([targets[n] for n in names], self.api.robot.data.joint_pos)
        self.api.robot.actuators.target_command.set_position_index(value=value, joint_ids=list(range(len(names))))
        self.scene.write_data_to_sim()
        value = self.api.robot.actuators.target_command.position
        value = value.torch if hasattr(value, "torch") else value
        values = value.detach().cpu().tolist()[0]
        return {n: values[self.api.binding["joint_index"][n]] for n in targets}

    def step(self):
        if self.unhealthy or self.simulation.get_physics_dt() != self.dt:
            raise ValueError("replay host needs requalification")
        try:
            self.simulation.step(render=False)
            self.scene.update(self.dt)
        except Exception:
            self.unhealthy = True
            raise

    def snapshot(self, step):
        return self.measurements.snapshot(step, self.dt)

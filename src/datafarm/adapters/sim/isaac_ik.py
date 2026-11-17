"""Isaac Lab 原生 Newton 微分 IK；只计算建议，不推进或重置物理。"""
from copy import deepcopy
import hashlib
import importlib.metadata
import math
from pathlib import Path

from datafarm.simulation.robot_binding import BindingError, digest, verify_binding
from datafarm.simulation.tcp_control import ControlFault, pose


def _tensor(value):
    return value.torch if hasattr(value, "torch") else value


def control_chain(binding):
    """从 TCP 的实际 link 回溯串联链；不按 joint 数组位置选择手臂。"""
    verify_binding(binding)
    tcp = binding["semantics"].get("tcp")
    if tcp is None:
        raise BindingError("CAPABILITY_UNAVAILABLE: a bound TCP is required")
    descriptor = binding["descriptor"]
    parents = {j["child"]: j for j in descriptor["joints"]}
    frame, names, seen = tcp["link"], [], set()
    while frame != descriptor["root_link"]:
        if frame in seen or frame not in parents:
            raise BindingError("Invalid TCP chain")
        seen.add(frame)
        joint = parents[frame]
        if joint["type"] != "fixed":
            if joint["mimic"] is not None:
                raise BindingError("CAPABILITY_UNAVAILABLE: moving mimic on TCP chain")
            if joint["name"] not in binding["position_driven_joints"]:
                raise BindingError("CAPABILITY_UNAVAILABLE: TCP chain lacks an active position drive")
            names.append(joint["name"])
        frame = joint["parent"]
    if not names:
        raise BindingError("CAPABILITY_UNAVAILABLE: no controlled TCP joints")
    return tuple(reversed(names))


class IsaacLabIKBackend:
    """按运行实例校验的单环境后端；使用世界坐标下的 link/TCP Jacobian。"""

    def __init__(self, api, profile):
        # 重依赖仅在显式绑定时加载；包导入不启动应用、不加载模型。
        import torch
        from isaaclab_newton.controllers.differential_ik import NewtonDifferentialIKController
        from isaaclab_newton.controllers.differential_ik_cfg import NewtonDifferentialIKControllerCfg

        self.api, self.robot, self.profile = api, api.robot, profile
        self.joint_names = control_chain(api.binding)
        if set(profile.joint_speed_limits) != set(self.joint_names):
            raise BindingError("Profile must name exactly the TCP control chain")
        self._binding_hash = api.binding["binding_sha256"]
        self._joint_ids = [api.binding["joint_index"][n] for n in self.joint_names]
        self._body_idx = api.binding["body_names"].index(api.binding["semantics"]["tcp"]["link"])
        if not isinstance(self.robot.is_fixed_base, bool) or self.robot.num_base_dofs not in (0, 6):
            raise BindingError("Unsupported runtime base layout")
        if self.robot.is_fixed_base != (self.robot.num_base_dofs == 0):
            raise BindingError("Runtime base layout mismatch")
        if self.robot.is_fixed_base and api.binding["body_names"][0] != api.binding["descriptor"]["root_link"]:
            raise BindingError("Fixed root body ordering must be verified")
        self._fixed_base, self._base_dofs = self.robot.is_fixed_base, self.robot.num_base_dofs
        self._row = self._body_idx-1 if self.robot.is_fixed_base else self._body_idx
        self._columns = [i+self.robot.num_base_dofs for i in self._joint_ids]
        q = _tensor(self.robot.data.joint_pos)
        self._device = q.device
        self._offset = torch.tensor([api.binding["semantics"]["tcp"]["position_m"]], device=q.device, dtype=q.dtype)
        joints = {j["name"]: j for j in api.binding["descriptor"]["joints"]}
        velocity = _tensor(self.robot.data.joint_vel_limits)
        if tuple(velocity.shape) != tuple(q.shape):
            raise BindingError("Runtime velocity limits shape mismatch")
        self.joint_speed_limits = {}
        for name, i in zip(self.joint_names, self._joint_ids):
            v = float(velocity[0, i])
            if not math.isfinite(v) or v <= 0:
                raise BindingError("Runtime velocity limit unavailable: " + name)
            self.joint_speed_limits[name] = min(v, joints[name]["limits"]["velocity"])
        self._limits = _tensor(self.robot.data.joint_pos_limits).clone()
        self._vel_limits = velocity.clone()
        self._stiffness = _tensor(self.robot.data.joint_stiffness).clone()
        self._damping = _tensor(self.robot.data.joint_damping).clone()
        self._effort = _tensor(self.robot.data.joint_effort_limits).clone()
        n = len(api.binding["joint_names"])
        for values in (self._stiffness, self._damping, self._effort):
            if tuple(values.shape) != (1, n) or not torch.isfinite(values).all():
                raise BindingError("Invalid runtime drive parameters")
        for i in self._joint_ids:
            if self._stiffness[0, i] <= 0 or self._damping[0, i] < 0 or self._effort[0, i] <= 0:
                raise BindingError("Invalid active position drive")
        use_limits = profile.joint_limit_avoidance_gain > 0
        lower, upper = [], []
        for name in self.joint_names:
            bounds = api.binding["effective_position_limits"][name]
            if bounds is None:
                if use_limits or profile.joint_limit_margin:
                    raise BindingError("Limit avoidance requires finite authored bounds")
                lower.append(-math.inf); upper.append(math.inf)
            else:
                low, high = bounds[0]+profile.joint_limit_margin, bounds[1]-profile.joint_limit_margin
                if low >= high:
                    raise BindingError("Joint limit margin excludes the chain")
                lower.append(low); upper.append(high)
        self._lower, self._upper = lower, upper
        self._controllers = {}
        for mode in ("position", "pose"):
            cfg = NewtonDifferentialIKControllerCfg(
                bandwidth=profile.bandwidth_s_inv, damping=profile.damping,
                axis_weight=(1, 1, 1, 0, 0, 0) if mode == "position" else (1, 1, 1, 1, 1, 1),
                use_joint_limit_avoidance=use_limits,
                joint_limit_avoidance_gain=profile.joint_limit_avoidance_gain,
                joint_limit_avoidance_margin=profile.joint_limit_margin,
                null_space_damping=profile.damping if use_limits else None)
            controller = NewtonDifferentialIKController(cfg, 1, len(self.joint_names), str(q.device))
            if use_limits:
                controller.set_joint_pos_limits(torch.tensor(lower, device=q.device), torch.tensor(upper, device=q.device))
            self._controllers[mode] = controller
        import inspect
        import newton.controllers
        from datafarm.simulation import isaac_robot
        from . import isaac_control
        sources = {"native_controller": Path(inspect.getfile(NewtonDifferentialIKController)),
                   "native_config": Path(inspect.getfile(NewtonDifferentialIKControllerCfg)),
                   "newton_controller": Path(inspect.getfile(newton.controllers.ControllerDifferentialIKModelFree)),
                   "robot_api": Path(isaac_robot.__file__), "host_adapter": Path(isaac_control.__file__),
                   "adapter": Path(__file__), "control_contract": Path(inspect.getfile(type(profile)))}
        manifest = dict(schema_version="tcp-ik-binding/1", robot_binding_sha256=self._binding_hash,
                        profile_sha256=digest(profile.to_dict()), joint_names=list(self.joint_names),
                        tcp=deepcopy(api.binding["semantics"]["tcp"]), solver="damped_least_squares",
                        pose_convention="world_xyzw", base_dofs_commanded=False,
                        runtime_parameters={"position_limits": {n: api.binding["effective_position_limits"][n] for n in self.joint_names},
                                            "velocity_limits": self._vel_limits.detach().cpu().tolist(),
                                            "stiffness": self._stiffness.detach().cpu().tolist(),
                                            "damping": self._damping.detach().cpu().tolist(),
                                            "effort_limits": self._effort.detach().cpu().tolist()},
                        runtime_versions={n: importlib.metadata.version(n) for n in
                                          ("isaaclab", "isaaclab_newton", "newton", "warp-lang", "torch")},
                        sources={n: hashlib.sha256(p.read_bytes()).hexdigest() for n, p in sources.items()})
        self.manifest = dict(manifest, sha256=digest(manifest))
        self._profile_hash = digest(profile.to_dict())
        self._manifest_hash = digest(self.manifest)
        self._kinematics()  # 构造时核验接口和形状，不等到第一条动作才猜 Jacobian。

    def _kinematics(self):
        import torch
        from isaaclab.utils.math import quat_apply
        verify_binding(self.api.binding)
        if (digest(self.profile.to_dict()) != self._profile_hash or digest(self.manifest) != self._manifest_hash):
            raise ControlFault("ASSET_MISMATCH")
        if (self.api.binding["binding_sha256"] != self._binding_hash
                or self.robot.is_fixed_base != self._fixed_base or self.robot.num_base_dofs != self._base_dofs
                or self.robot.joint_names != self.api.binding["joint_names"]
                or self.robot.body_names != self.api.binding["body_names"]):
            raise ControlFault("ASSET_MISMATCH")
        for field, expected in (("joint_pos_limits", self._limits), ("joint_vel_limits", self._vel_limits),
                                ("joint_stiffness", self._stiffness), ("joint_damping", self._damping),
                                ("joint_effort_limits", self._effort)):
            if not torch.equal(_tensor(getattr(self.robot.data, field)), expected):
                raise ControlFault("ASSET_MISMATCH")
        data = self.robot.data
        q, jac = _tensor(data.joint_pos), _tensor(data.body_link_jacobian_w)
        n, b = len(self.api.binding["joint_names"]), len(self.api.binding["body_names"])
        if (tuple(q.shape) != (1, n) or tuple(jac.shape) != (1, b-int(self.robot.is_fixed_base), 6, n+self.robot.num_base_dofs)
                or self._row < 0 or q.device != self._device):
            raise ControlFault("ASSET_MISMATCH")
        body_quat = _tensor(data.body_quat_w)
        if tuple(body_quat.shape) != (1, b, 4) or not torch.isfinite(body_quat).all():
            raise ControlFault("NONFINITE_STATE")
        quat = body_quat[:, self._body_idx]
        if not torch.allclose(torch.linalg.vector_norm(quat, dim=-1), torch.ones(1, device=q.device), atol=1e-6, rtol=0):
            raise ControlFault("NONFINITE_STATE")
        jac = jac[:, self._row].index_select(-1, torch.tensor(self._columns, device=q.device)).clone()
        # J_v(TCP) = J_v(link) + omega x R_world_link p_link_tcp。
        offset_w = quat_apply(quat, self._offset)
        jac[:, :3] += torch.linalg.cross(jac[:, 3:], offset_w[:, :, None], dim=1)
        if not torch.isfinite(jac).all() or not torch.isfinite(q).all():
            raise ControlFault("NONFINITE_STATE")
        measured = pose(self.api.observe()["tcp_pose"])
        current = torch.tensor([[*measured["position_m"], *measured["quaternion_xyzw"]]], device=q.device, dtype=torch.float32)
        return current, jac.contiguous(), q[:, self._joint_ids].to(torch.float32).contiguous()

    def compute(self, reference, mode):
        import torch
        target = pose(reference)
        if target["frame_id"] != "world" or mode not in self._controllers:
            raise ControlFault("INVALID_ARGUMENT")
        current, jac, q = self._kinematics()
        desired = torch.tensor([[*target["position_m"], *target["quaternion_xyzw"]]], device=q.device, dtype=torch.float32)
        try:
            output = self._controllers[mode].compute(current, desired, jac, q, self.profile.dt_s).clone()
        except Exception:
            raise ControlFault("IK_BACKEND_ERROR") from None
        if tuple(output.shape) != tuple(q.shape) or not torch.isfinite(output).all():
            raise ControlFault("IK_OUTPUT_ERROR")
        values = output[0].detach().cpu().tolist()
        if any(not low <= v <= high for low, v, high in zip(self._lower, values, self._upper)):
            raise ControlFault("JOINT_LIMIT")
        return dict(zip(self.joint_names, values))

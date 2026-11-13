"""Isaac Lab 3.0 live binding. Only this boundary knows tensor/actuator APIs.

No reset, simulation advance or gain changes: the owning runtime retains those
responsibilities. Targets are staged, not reported as achieved motion.
"""
from copy import deepcopy
import math

from .robot_binding import BindingError, bind_robot, digest, gripper_target, validate_targets, verify_binding


def tensor(value):
    return value.torch if hasattr(value, 'torch') else value


class IsaacRobotAPI:
    def __init__(self, articulation, descriptor, profile=None):
        self.robot = articulation
        self.binding = bind_robot(descriptor, profile, list(articulation.joint_names),
                                  list(articulation.body_names))
        q = tensor(articulation.data.joint_pos)
        if q.shape[0] != 1:
            raise BindingError('Initial API supports exactly one environment')
        limits = tensor(articulation.data.joint_pos_limits).detach().cpu().tolist()[0]
        joints = {j['name']: j for j in descriptor['joints']}
        for name, index in self.binding['joint_index'].items():
            expected = joints[name]['limits']['position']
            actual = limits[index]
            if (len(actual) != 2 or any(math.isnan(v) for v in actual)
                    or (expected is not None and any(not math.isfinite(a) or abs(a-b) > 1e-5
                                                    for a, b in zip(actual, expected)))):
                raise BindingError('Runtime/source position limits differ: ' + name)
        parameters = {'position_limits': limits}
        for key in ('joint_stiffness', 'joint_damping', 'joint_effort_limits'):
            value = getattr(articulation.data, key, None)
            parameters[key] = tensor(value).detach().cpu().tolist()[0] if value is not None else None
        stiffness = parameters['joint_stiffness']
        driven = [n for n in self.binding['controllable_joints']
                  if stiffness is not None and stiffness[self.binding['joint_index'][n]] > 0]
        configured = {n for group in self.binding['groups'].values() for n in group}
        gripper = self.binding['profile'].get('gripper')
        if gripper:
            configured.add(gripper['master_joint'])
        if not configured.issubset(driven):
            raise BindingError('Profile requests a joint without an active position drive')
        self.binding['position_driven_joints'] = driven
        self.binding['capabilities']['joint_position_target'] = bool(driven)
        self.binding['runtime_parameters'] = parameters
        self.binding.pop('binding_sha256')
        self.binding['binding_sha256'] = digest(self.binding)

    def capabilities(self):
        verify_binding(self.binding)
        return deepcopy(self.binding['capabilities'])

    def observe(self):
        verify_binding(self.binding)
        q = tensor(self.robot.data.joint_pos).detach().cpu().tolist()[0]
        dq = tensor(self.robot.data.joint_vel).detach().cpu().tolist()[0]
        if len(q) != len(self.binding['joint_names']) or len(dq) != len(q):
            raise BindingError('Runtime joint state dimensions changed')
        if not all(math.isfinite(x) for x in q + dq):
            raise BindingError('NONFINITE_STATE')
        result = {'joint_names': self.binding['joint_names'][:], 'q': q, 'dq': dq,
                  'contacts': {'availability': 'unavailable'}}
        tcp = self.binding['profile'].get('tcp')
        if tcp is not None:
            # Lab 3.0 body poses are XYZW, verified against the local runtime.
            import torch
            from isaaclab.utils.math import combine_frame_transforms
            i = self.binding['body_names'].index(tcp['link'])
            pos = tensor(self.robot.data.body_pos_w)[:, i]
            quat = tensor(self.robot.data.body_quat_w)[:, i]
            offset = torch.tensor([tcp['position_m']], device=pos.device, dtype=pos.dtype)
            rotation = torch.tensor([tcp['quaternion_xyzw']], device=quat.device, dtype=quat.dtype)
            p, q_tcp = combine_frame_transforms(pos, quat, offset, rotation)
            if not torch.isfinite(p).all() or not torch.isfinite(q_tcp).all():
                raise BindingError('NONFINITE_STATE: TCP')
            result['tcp_pose'] = dict(frame_id='world', position_m=p[0].tolist(),
                                      quaternion_xyzw=q_tcp[0].tolist())
        return result

    def set_joint_targets(self, targets):
        targets = validate_targets(self.binding, targets)
        if not set(targets).issubset(self.binding['position_driven_joints']):
            raise BindingError('CAPABILITY_UNAVAILABLE: no active position drive')
        import torch
        names = list(targets)
        ids = [self.binding['joint_index'][n] for n in names]
        q = tensor(self.robot.data.joint_pos)
        values = torch.tensor([[targets[n] for n in names]], device=q.device, dtype=q.dtype)
        self.robot.actuators.target_command.set_position_index(value=values.contiguous(), joint_ids=ids)
        return dict(status='staged', targets=targets)

    def set_gripper(self, width_m):
        return self.set_joint_targets(gripper_target(self.binding, width_m))

    def move_tcp(self, *args, **kwargs):
        raise BindingError('CAPABILITY_UNAVAILABLE: IK/motion planner not bound')

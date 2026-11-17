"""Isaac Lab 3.0 的运行边界，负责张量读取和执行器目标暂存。

复位、物理步进和驱动增益由宿主持有，本类不擅自修改。返回 staged 只表示
已暂存位置目标，不代表机器人已经到达、轨迹无碰撞或任务已经成功。
"""
from copy import deepcopy
import math
import threading

from .robot_binding import BindingError, bind_robot, digest, gripper_target, validate_targets, verify_binding


def tensor(value):
    """统一原生 Torch 张量与带 .torch 属性的运行时张量包装。"""
    return value.torch if hasattr(value, 'torch') else value


class IsaacRobotAPI:
    """将已初始化的单环境 articulation 与 URDF 绑定，只开放已验证的能力。"""

    def __init__(self, articulation, descriptor, profile=None):
        """核对实际名称/限位并读取驱动参数，不靠模型声明猜测有效执行器。"""
        self.robot = articulation
        self._tcp_motion_service = None
        self._motion_lock = threading.Lock()
        self._command_owner = None
        self.binding = bind_robot(descriptor, profile, list(articulation.joint_names),
                                  list(articulation.body_names))
        q = tensor(articulation.data.joint_pos)
        if q.shape[0] != 1:
            raise BindingError('Initial API supports exactly one environment')
        # 模型用于提供定义，运行实例用于核对导入结果；两者不一致时拒绝继续控制。
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
        # 当前采用位置控制。独立关节仍须具有正 stiffness，才能开放位置目标接口。
        # 这里只读取既有配置，不为获得能力而自动调高增益或改变 effort 限制。
        stiffness = parameters['joint_stiffness']
        driven = [n for n in self.binding['controllable_joints']
                  if stiffness is not None and stiffness[self.binding['joint_index'][n]] > 0]
        configured = {n for group in self.binding['groups'].values() for n in group}
        gripper = self.binding['semantics'].get('gripper')
        if gripper:
            # 模型自动发现了夹爪，不等于当前场景配置了它的位置驱动。
            # 自动候选缺驱动时关闭动作能力；显式配置缺驱动则视为配置错误。
            if gripper['master_joint'] not in driven and gripper['selection_source'] == 'model':
                self.binding['capabilities']['set_gripper'] = False
                self.binding['semantic_diagnostics'].append(dict(component='gripper', reason='no_active_position_drive'))
            else:
                configured.add(gripper['master_joint'])
        if not configured.issubset(driven):
            raise BindingError('Profile requests a joint without an active position drive')
        self.binding['position_driven_joints'] = driven
        self.binding['capabilities']['joint_position_target'] = bool(driven)
        self.binding['runtime_parameters'] = parameters
        # 驱动能力和实际参数也属于绑定身份，追加后重新计算摘要。
        self.binding.pop('binding_sha256')
        self.binding['binding_sha256'] = digest(self.binding)

    def capabilities(self):
        """返回能力快照，调用方修改返回值不会改变已绑定的控制权限。"""
        verify_binding(self.binding)
        result = deepcopy(self.binding['capabilities'])
        if self._tcp_motion_service is not None:
            control = self._tcp_motion_service.capabilities()
            result.update(move_tcp=control['move_tcp'], collision_checked_motion=False, tcp_control=control)
        return result

    def observe(self):
        """读取当前实测状态，不推进物理；缺失接触接口时明确标记 unavailable。"""
        verify_binding(self.binding)
        q = tensor(self.robot.data.joint_pos).detach().cpu().tolist()[0]
        dq = tensor(self.robot.data.joint_vel).detach().cpu().tolist()[0]
        if len(q) != len(self.binding['joint_names']) or len(dq) != len(q):
            raise BindingError('Runtime joint state dimensions changed')
        if not all(math.isfinite(x) for x in q + dq):
            raise BindingError('NONFINITE_STATE')
        result = {'joint_names': self.binding['joint_names'][:], 'q': q, 'dq': dq,
                  'contacts': {'availability': 'unavailable'}}
        gripper = self.binding['semantics'].get('gripper')
        if gripper is not None:
            actual = q[self.binding['joint_index'][gripper['master_joint']]]
            width = (actual - gripper['joint_offset']) / gripper['joint_per_m']
            # 显式 profile 默认由实测主关节反算；模型推导优先使用两根手指的实测值，
            # 避免主从运动暂未同步时把理想 mimic 关系当作实际夹爪开度。
            measured = gripper.get('width_observation')
            if measured is not None:
                width = measured['offset_m'] + sum(c*q[self.binding['joint_index'][n]]
                                                    for n, c in measured['joint_coefficients'].items())
            # 保留越界读数并单独给出有效性，不把碰撞/数值异常裁剪成合法观测。
            result['gripper'] = dict(width_m=width, width_range_m=gripper['width_range_m'],
                                      state_source='measured_finger_joints' if measured else 'measured_master_joint',
                                      verification=gripper.get('verification', 'explicit_profile'),
                                      within_defined_range=gripper['width_range_m'][0]-1e-8 <= width <= gripper['width_range_m'][1]+1e-8)
        tcp = self.binding['semantics'].get('tcp')
        if tcp is not None:
            # 本机 Lab 3.0 的 body pose 已核对为 XYZW；不能沿用其他版本的 WXYZ 假设。
            import torch
            from isaaclab.utils.math import combine_frame_transforms
            i = self.binding['body_names'].index(tcp['link'])
            pos = tensor(self.robot.data.body_pos_w)[:, i]
            quat = tensor(self.robot.data.body_quat_w)[:, i]
            offset = torch.tensor([tcp['position_m']], device=pos.device, dtype=pos.dtype)
            rotation = torch.tensor([tcp['quaternion_xyzw']], device=quat.device, dtype=quat.dtype)
            # T_world_tcp = T_world_body · T_body_tcp；偏置已由模型固定链解析到该 body。
            p, q_tcp = combine_frame_transforms(pos, quat, offset, rotation)
            if not torch.isfinite(p).all() or not torch.isfinite(q_tcp).all():
                raise BindingError('NONFINITE_STATE: TCP')
            result['tcp_pose'] = dict(frame_id='world', position_m=p[0].tolist(),
                                      quaternion_xyzw=q_tcp[0].tolist())
        return result

    def set_joint_targets(self, targets):
        """运动期间拒绝其他调用者插入目标；不把并发请求排队成隐式新动作。"""
        if not self._motion_lock.acquire(blocking=False):
            raise BindingError('CONTROL_BUSY')
        try:
            return self._stage_joint_targets(targets)
        finally:
            self._motion_lock.release()

    def _stage_joint_targets(self, targets):
        """校验并按运行时索引暂存位置目标；插值、限速与步进由宿主执行。"""
        targets = validate_targets(self.binding, targets)
        if not set(targets).issubset(self.binding['position_driven_joints']):
            raise BindingError('CAPABILITY_UNAVAILABLE: no active position drive')
        import torch
        names = list(targets)
        # 使用具名映射，不能假定 URDF 中的关节顺序与运行时张量列顺序相同。
        ids = [self.binding['joint_index'][n] for n in names]
        q = tensor(self.robot.data.joint_pos)
        values = torch.tensor([[targets[n] for n in names]], device=q.device, dtype=q.dtype)
        self.robot.actuators.target_command.set_position_index(value=values.contiguous(), joint_ids=ids)
        return dict(status='staged', targets=targets)

    def set_gripper(self, width_m):
        """将目标开度换算为独立主关节位置，经同一位置目标入口下发。"""
        if not self.capabilities()['set_gripper']:
            raise BindingError('CAPABILITY_UNAVAILABLE: gripper definition or active position drive required')
        return self.set_joint_targets(gripper_target(self.binding, width_m))

    def bind_tcp_controller(self, controller):
        """宿主专用注入端口；静态 robot-binding/2 不因此伪称已验证运动能力。"""
        if not self._motion_lock.acquire(blocking=False):
            raise BindingError('CONTROL_BUSY')
        try:
            verify_binding(self.binding)
            if self._tcp_motion_service is not None:
                raise BindingError('TCP controller is already bound')
            if (getattr(controller, 'binding_sha256', None) != self.binding['binding_sha256']
                    or not callable(getattr(controller, 'move_tcp', None))
                    or not callable(getattr(controller, 'capabilities', None))):
                raise BindingError('TCP controller binding mismatch')
            controller.capabilities()
            self._tcp_motion_service = controller
        finally:
            self._motion_lock.release()

    def move_tcp(self, request):
        """委托给有界宿主闭环；未绑定时仍明确拒绝，不把目标暂存当作到达。"""
        if self._tcp_motion_service is None:
            raise BindingError('CAPABILITY_UNAVAILABLE: IK/motion planner not bound')
        return self._tcp_motion_service.move_tcp(request)

    def move_gripper(self, request):
        """Bounded measured gripper motion through the same owned physics clock."""
        if self._tcp_motion_service is None:
            raise BindingError('CAPABILITY_UNAVAILABLE: motion host not bound')
        return self._tcp_motion_service.move_gripper(request)

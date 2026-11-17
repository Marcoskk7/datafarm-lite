"""显式组合 Isaac Lab TCP 控制，不在 import 时创建模拟器或运行任务。"""
import math

from datafarm.adapters.sim.isaac_control import IsaacControlHost
from datafarm.adapters.sim.isaac_ik import IsaacLabIKBackend
from datafarm.simulation.isaac_robot import IsaacRobotAPI
from datafarm.simulation.robot_binding import BindingError
from datafarm.simulation.tcp_control import ControlProfile, TCPMotionService


def bind_isaac_tcp_control(api: IsaacRobotAPI, simulation, control_profile, *,
                           frame_resolvers=None, event_sink=None, cancelled=None):
    """绑定已初始化 articulation/模拟器；调用方保证独占同一世界的步进。"""
    if not isinstance(api, IsaacRobotAPI):
        raise BindingError("Expected the initialized Isaac Robot API")
    if api._tcp_motion_service is not None:
        raise BindingError("TCP control is already bound; create a new binding for a new runtime")
    profile = ControlProfile.from_dict(control_profile) if isinstance(control_profile, dict) else control_profile
    if not isinstance(profile, ControlProfile):
        raise BindingError("Expected a versioned control profile")
    host = IsaacControlHost(api, simulation, frame_resolvers=frame_resolvers,
                            event_sink=event_sink, cancelled=cancelled)
    if not math.isclose(host.dt_s, profile.dt_s, rel_tol=0, abs_tol=1e-12):
        raise BindingError("Control dt must equal the explicitly owned physics step")
    backend = IsaacLabIKBackend(api, profile)
    service = TCPMotionService(api.binding, backend, host, profile)
    api.bind_tcp_controller(service)
    return service

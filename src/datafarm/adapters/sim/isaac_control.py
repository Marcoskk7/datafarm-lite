"""单环境串行宿主：应用关节目标、步进、刷新并保留动作/实测事件。"""
from copy import deepcopy
import math
import threading

from datafarm.simulation.robot_binding import BindingError
from datafarm.simulation.robot_semantics import compose
from datafarm.simulation.tcp_control import ControlFault, pose


class IsaacControlHost:
    """不创建场景或后台循环；调用方交付已初始化的模拟器和独占步进权限。"""

    def __init__(self, api, simulation, *, frame_resolvers=None, event_sink=None, cancelled=None, scene=None, measurements=None, recorder=None):
        self.api, self.simulation = api, simulation
        if (scene is None) != (measurements is None) or (recorder is not None and scene is None):
            raise BindingError("Full scene, measurement and recording ports must be supplied together")
        self.scene, self.measurements, self.recorder = scene, measurements, recorder
        self.latest_scene_state = None
        if scene is not None and not all(callable(getattr(scene, n, None)) for n in ("write_data_to_sim", "update")):
            raise BindingError("Full scene stepping ports unavailable")
        self.frames = dict(frame_resolvers or {})
        if "world" in self.frames or not all(isinstance(n, str) and n and callable(v) for n, v in self.frames.items()):
            raise BindingError("Frame resolvers must be named callables; world cannot be overridden")
        if event_sink is not None and not callable(event_sink):
            raise BindingError("Event sink must be callable")
        if cancelled is not None and not callable(cancelled):
            raise BindingError("Cancellation probe must be callable")
        self._sink, self._cancel = event_sink, cancelled
        self.dt_s = float(simulation.get_physics_dt())
        if not math.isfinite(self.dt_s) or self.dt_s <= 0:
            raise BindingError("Invalid physics step duration")
        for obj, methods in ((simulation, ("step",)), (api.robot, ("write_data_to_sim", "update"))):
            if not all(callable(getattr(obj, m, None)) for m in methods):
                raise BindingError("Initialized host stepping resources are required")
        self.control_step = self.step_attempts = 0
        self.command_pending = self.step_outcome_unknown = False
        self.healthy = True
        self.latest_observation = None

    def acquire(self):
        if not self.api._motion_lock.acquire(blocking=False):
            return False
        self.api._command_owner = threading.get_ident()
        return True

    def release(self):
        self.api._command_owner = None
        self.api._motion_lock.release()

    def emit(self, event):
        if self._sink is not None:
            try:
                self._sink(deepcopy(event))
            except Exception:
                self.healthy = False
                raise ControlFault("EVENT_SINK_ERROR") from None

    def cancelled(self):
        if self._cancel is None:
            return False
        try:
            value = self._cancel()
            if not isinstance(value, bool):
                raise TypeError()
            return value
        except Exception:
            raise ControlFault("CANCEL_PROBE_ERROR") from None

    def observe(self):
        if not self.healthy or self.step_outcome_unknown:
            raise ControlFault("HOST_REQUALIFICATION_REQUIRED")
        try:
            if self.measurements is None:
                self.latest_observation = self.api.observe()
            else:
                self.latest_scene_state = self.measurements.snapshot(self.control_step, self.dt_s)
                self.latest_observation = self.latest_scene_state["robot"]
            return deepcopy(self.latest_observation)
        except BindingError as exc:
            code = "NONFINITE_STATE" if str(exc).startswith("NONFINITE_STATE") else "ASSET_MISMATCH"
            raise ControlFault(code) from None
        except Exception:
            raise ControlFault("STATE_READ_ERROR") from None

    def resolve_pose(self, target):
        target = pose(target)
        if target["frame_id"] == "world":
            return target
        resolver = self.frames.get(target["frame_id"])
        if resolver is None:
            raise ControlFault("UNKNOWN_FRAME")
        try:
            frame = pose(resolver())
            if frame["frame_id"] != "world":
                raise ValueError()
            resolved = compose(frame, target)
            return pose(dict(frame_id="world", **resolved))
        except Exception:
            raise ControlFault("FRAME_RESOLUTION_ERROR") from None

    def begin(self, names):
        if not self.healthy or self.step_outcome_unknown:
            raise ControlFault("HOST_REQUALIFICATION_REQUIRED")
        try:
            value = self.api.robot.actuators.target_command.position
            value = value.torch if hasattr(value, "torch") else value
            values = value.detach().cpu().tolist()
            if len(values) != 1 or len(values[0]) != len(self.api.binding["joint_names"]):
                raise ValueError()
            return {n: values[0][self.api.binding["joint_index"][n]] for n in names}
        except Exception:
            raise ControlFault("TARGET_READ_ERROR") from None

    def step(self, targets, event):
        if self.api._command_owner != threading.get_ident() or not self.healthy:
            raise ControlFault("CONTROL_OWNERSHIP_ERROR")
        if set(event) & {"kind", "control_step", "applied", "measured"}:
            raise ControlFault("INVALID_EVENT_FIELDS")
        self.latest_observation = None
        if not math.isclose(float(self.simulation.get_physics_dt()), self.dt_s, rel_tol=0, abs_tol=1e-12):
            raise ControlFault("CONTROL_DT_CHANGED")
        if self.recorder is not None:
            try:
                self.recorder.ensure_capacity(targets, event)
            except ValueError:
                raise ControlFault("RECORDING_BUDGET_EXHAUSTED") from None
        self.emit(dict(kind="control_request", control_step=self.control_step,
                       applied=targets, **event))
        try:
            self.api._stage_joint_targets(targets)
            self.command_pending = True
            if self.scene is None:
                self.api.robot.write_data_to_sim()
            else:
                self.scene.write_data_to_sim()
            applied = self.begin(self.api.binding["joint_names"])
            if self.recorder is not None:
                self.recorder.input(targets, applied, event)
        except Exception:
            self.healthy = False
            raise ControlFault("TARGET_APPLY_ERROR") from None
        self.step_attempts += 1
        self.latest_observation = None
        try:
            self.simulation.step(render=False)
        except Exception:
            # 异常可能发生在物理推进之后：不重复步进，不捏造确定的末状态。
            self.step_outcome_unknown = True
            self.healthy = False
            self.emit(dict(kind="control_step_error", control_step=self.control_step,
                           step_outcome_unknown=True, **event))
            raise ControlFault("SIMULATION_ERROR") from None
        self.control_step += 1
        self.command_pending = False
        try:
            if self.scene is None:
                self.api.robot.update(self.dt_s)
            else:
                self.scene.update(self.dt_s)
            measured = self.observe()
            if self.recorder is not None:
                self.recorder.measured(self.latest_scene_state)
        except Exception:
            self.healthy = False
            raise ControlFault("STATE_REFRESH_ERROR") from None
        self.emit(dict(kind="control_applied", control_step=self.control_step,
                       applied=targets, measured=measured, **event))
        return measured

    def hold(self, names):
        """只暂存实测关节位置，不擅自多走一步；未知物理状态下不能承诺停住。"""
        try:
            if self.step_outcome_unknown or not self.healthy:
                return dict(status="unavailable", reason_code="HOST_REQUALIFICATION_REQUIRED", stepped=False)
            measured = self.api.observe()
            targets = {n: measured["q"][self.api.binding["joint_index"][n]] for n in names}
            self.api._stage_joint_targets(targets)
            self.command_pending = True
            return dict(status="staged", targets=targets, stepped=False, physical_stop_verified=False)
        except Exception:
            self.healthy = False
            return dict(status="error", reason_code="HOLD_ERROR", stepped=False)

    def joint_speed_limit(self, name):
        """Read actual drive speed limits; no inferred unbounded gripper rate."""
        value = self.api.robot.data.joint_vel_limits
        value = value.torch if hasattr(value, "torch") else value
        speed = float(value[0, self.api.binding["joint_index"][name]])
        if not math.isfinite(speed) or speed <= 0:
            raise ControlFault("RUNTIME_SPEED_LIMIT_UNAVAILABLE")
        return speed

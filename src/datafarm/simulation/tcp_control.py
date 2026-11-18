"""通用 TCP 微分控制契约；不导入仿真器、不持有隐藏任务或判定器。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import math
import time
from typing import Mapping
from uuid import uuid4

from .robot_binding import BindingError, digest, finite, validate_targets, verify_binding

PROFILE_SCHEMA = "tcp-control-profile/1"
REQUEST_SCHEMA = "tcp-motion-request/1"
RESULT_SCHEMA = "tcp-motion-result/1"


class ControlFault(BindingError):
    """可公开的控制错误码；不把后端 traceback、文件或宿主信息暴露给策略。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def number(value, *, positive=False):
    value = finite(value)
    if value < 0 or (positive and value == 0):
        raise BindingError("Expected a positive/nonnegative control value")
    return value


def integer(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BindingError("Expected a positive integer budget")
    return value


def pose(value):
    if not isinstance(value, Mapping) or set(value) != {"frame_id", "position_m", "quaternion_xyzw"}:
        raise ControlFault("INVALID_ARGUMENT")
    frame = value["frame_id"]
    if not isinstance(frame, str) or not frame:
        raise ControlFault("INVALID_ARGUMENT")
    try:
        p, q = list(value["position_m"]), list(value["quaternion_xyzw"])
        if len(p) != 3 or len(q) != 4:
            raise ValueError()
        p, q = [finite(x) for x in p], [finite(x) for x in q]
        if not math.isclose(math.hypot(*q), 1.0, rel_tol=0, abs_tol=1e-6):
            raise ValueError()
    except (ValueError, TypeError):
        raise ControlFault("INVALID_ARGUMENT") from None
    return dict(frame_id=frame, position_m=p, quaternion_xyzw=q)


def pose_error(current, target):
    dp = math.dist(current["position_m"], target["position_m"])
    # q 和 -q 表示同一旋转；先限制舍入误差，再取最短旋转角。
    a, b = current["quaternion_xyzw"], target["quaternion_xyzw"]
    dot = abs(sum(x*y for x, y in zip(a, b))) / (math.hypot(*a)*math.hypot(*b))
    return dp, 2*math.acos(min(1.0, dot))


def bounded_reference(current, target, linear_step, angular_step, mode):
    distance, angle = pose_error(current, target)
    fraction = min(1.0, linear_step/distance) if distance else 1.0
    pos = [x+fraction*(y-x) for x, y in zip(current["position_m"], target["position_m"])]
    a, b = current["quaternion_xyzw"], target["quaternion_xyzw"]
    if sum(x*y for x, y in zip(a, b)) < 0:
        b = [-x for x in b]
    if mode == "position":
        quat = a[:]
    else:
        f = min(1.0, angular_step/angle) if angle else 1.0
        half = angle/2
        if half < 1e-7:
            quat = [(1-f)*x+f*y for x, y in zip(a, b)]
        else:
            quat = [(math.sin((1-f)*half)*x+math.sin(f*half)*y)/math.sin(half)
                    for x, y in zip(a, b)]
        norm = math.hypot(*quat)
        quat = [x/norm for x in quat]
    return dict(frame_id="world", position_m=pos, quaternion_xyzw=quat)


@dataclass(frozen=True)
class ControlProfile:
    """宿主显式给定的最大速度、容差和预算，不从测试任务推断全局默认值。"""

    dt_s: float
    bandwidth_s_inv: float
    damping: float
    max_linear_speed_m_s: float
    max_angular_speed_rad_s: float
    joint_speed_limits: dict[str, float]
    position_tolerance_m: float
    orientation_tolerance_rad: float
    settle_steps: int
    max_control_steps: int
    max_total_control_steps: int
    max_calls: int
    max_wall_time_s: float
    reference_speed_fraction: float = 1.0
    joint_limit_avoidance_gain: float = 0.0
    joint_limit_margin: float = 0.0

    def __post_init__(self):
        for field in ("dt_s", "bandwidth_s_inv", "damping", "max_linear_speed_m_s",
                      "max_angular_speed_rad_s", "position_tolerance_m",
                      "orientation_tolerance_rad", "max_wall_time_s"):
            number(getattr(self, field), positive=True)
        for field in ("settle_steps", "max_control_steps", "max_total_control_steps", "max_calls"):
            integer(getattr(self, field))
        if self.settle_steps > self.max_control_steps or self.max_control_steps > self.max_total_control_steps:
            raise BindingError("Inconsistent control budgets")
        fraction = number(self.reference_speed_fraction, positive=True)
        if fraction > 1:
            raise BindingError("Reference speed fraction must be in (0, 1]")
        number(self.joint_limit_avoidance_gain); number(self.joint_limit_margin)
        speeds = self.joint_speed_limits
        if not isinstance(speeds, dict) or not speeds or not all(isinstance(n, str) and n for n in speeds):
            raise BindingError("Expected named joint speed limits")
        object.__setattr__(self, "joint_speed_limits", {n: number(v, positive=True) for n, v in speeds.items()})

    def to_dict(self):
        return dict(schema_version=PROFILE_SCHEMA, **asdict(self))

    @classmethod
    def from_dict(cls, value):
        value = deepcopy(value)
        if not isinstance(value, dict) or value.pop("schema_version", None) != PROFILE_SCHEMA:
            raise BindingError("Unsupported control profile version")
        try:
            return cls(**value)
        except TypeError:
            raise BindingError("Invalid control profile fields") from None


class TCPMotionService:
    """串行有界运动服务；IK 只建议目标，只有注入的宿主可以推进物理。"""

    def __init__(self, binding, backend, host, profile: ControlProfile):
        verify_binding(binding)
        self.binding, self.backend, self.host, self.profile = binding, backend, host, profile
        self._profile_hash = digest(profile.to_dict())
        self._backend_hash = digest(backend.manifest)
        self.binding_sha256 = binding["binding_sha256"]
        self.joint_names = tuple(backend.joint_names)
        if set(profile.joint_speed_limits) != set(self.joint_names):
            raise BindingError("Control profile must name exactly the selected chain joints")
        self._joint_speeds = {
            n: min(profile.joint_speed_limits[n], backend.joint_speed_limits[n]) for n in self.joint_names
        }
        self.calls = self.total_steps = 0
        self._moving = False

    def capabilities(self):
        verify_binding(self.binding)
        if (digest(self.profile.to_dict()) != self._profile_hash
                or digest(self.backend.manifest) != self._backend_hash):
            raise BindingError("Control profile/backend identity was modified")
        return dict(move_tcp=getattr(self.host, "healthy", True), modes=["position", "pose"], collision_checked_motion=False,
                    backend="isaaclab_newton_differential_ik", control_binding=deepcopy(self.backend.manifest),
                    remaining_calls=max(0, self.profile.max_calls-self.calls),
                    remaining_control_steps=max(0, self.profile.max_total_control_steps-self.total_steps))

    def _request(self, value):
        if not isinstance(value, dict):
            raise ControlFault("INVALID_ARGUMENT")
        allowed = {"schema_version", "target_pose", "mode", "max_control_steps", "settle_steps",
                   "linear_speed_m_s", "angular_speed_rad_s", "joint_speed_limits",
                   "position_tolerance_m", "orientation_tolerance_rad", "max_wall_time_s",
                   "require_collision_checked"}
        if set(value)-allowed or value.get("schema_version") != REQUEST_SCHEMA:
            raise ControlFault("INVALID_ARGUMENT")
        cfg = self.profile
        r = deepcopy(value)
        r["target_pose"] = pose(r.get("target_pose"))
        r.setdefault("mode", "pose")
        if r["mode"] not in ("position", "pose"):
            raise ControlFault("INVALID_ARGUMENT")
        collision = r.get("require_collision_checked", False)
        if not isinstance(collision, bool):
            raise ControlFault("INVALID_ARGUMENT")
        if collision:
            raise ControlFault("CAPABILITY_UNAVAILABLE")
        limits = {"linear_speed_m_s": cfg.max_linear_speed_m_s,
                  "angular_speed_rad_s": cfg.max_angular_speed_rad_s,
                  "position_tolerance_m": cfg.position_tolerance_m,
                  "orientation_tolerance_rad": cfg.orientation_tolerance_rad,
                  "max_wall_time_s": cfg.max_wall_time_s}
        for k, maximum in limits.items():
            r[k] = number(r.get(k, maximum), positive=True)
            if r[k] > maximum:
                raise ControlFault("INVALID_ARGUMENT")
        r["max_control_steps"] = integer(r.get("max_control_steps", cfg.max_control_steps))
        r["settle_steps"] = integer(r.get("settle_steps", cfg.settle_steps))
        if not cfg.settle_steps <= r["settle_steps"] <= r["max_control_steps"] <= cfg.max_control_steps:
            raise ControlFault("INVALID_ARGUMENT")
        speeds = r.get("joint_speed_limits", {})
        if not isinstance(speeds, dict) or set(speeds)-set(self.joint_names):
            raise ControlFault("INVALID_ARGUMENT")
        r["joint_speed_limits"] = {n: number(speeds.get(n, v), positive=True) for n, v in self._joint_speeds.items()}
        if any(r["joint_speed_limits"][n] > v for n, v in self._joint_speeds.items()):
            raise ControlFault("INVALID_ARGUMENT")
        return r

    def _state(self, value, speeds):
        if not isinstance(value, dict):
            raise ControlFault("ASSET_MISMATCH")
        if (value.get("joint_names") != self.binding["joint_names"]
                or len(value.get("q", [])) != len(self.binding["joint_names"])
                or len(value.get("dq", [])) != len(self.binding["joint_names"])):
            raise ControlFault("ASSET_MISMATCH")
        try:
            for x in value["q"] + value["dq"]:
                finite(x)
            p = pose(value["tcp"])
        except (KeyError, TypeError, ValueError):
            raise ControlFault("NONFINITE_STATE") from None
        if p["frame_id"] != "world":
            raise ControlFault("ASSET_MISMATCH")
        for name in self.joint_names:
            i = self.binding["joint_index"][name]
            limits = self.binding["effective_position_limits"][name]
            if limits is not None and not limits[0] <= value["q"][i] <= limits[1]:
                raise ControlFault("JOINT_LIMIT")
            if abs(value["dq"][i]) > speeds[name]+1e-6:
                raise ControlFault("SPEED_LIMIT")
        return value

    def _targets(self, proposed, state, previous, speeds):
        if not isinstance(proposed, dict) or set(proposed) != set(self.joint_names):
            raise ControlFault("IK_OUTPUT_ERROR")
        try:
            proposed = validate_targets(self.binding, proposed)
        except BindingError:
            raise ControlFault("IK_OUTPUT_ERROR") from None
        applied, limited = {}, []
        for name, q in proposed.items():
            # 限速针对连续执行器参考，不是把跟踪误差限制成一周期位移。
            # 后者会消除位置驱动抵抗重力所需的误差，导致目标跟着机器人下沉。
            delta = speeds[name]*self.profile.dt_s
            low, high = previous[name]-delta, previous[name]+delta
            applied[name] = min(high, max(low, q))
            if applied[name] != q:
                limited.append(name)
        validate_targets(self.binding, applied)
        return applied, limited

    def move_tcp(self, request):
        # 非阻塞获取：并发请求不能等前一个运动结束后悄悄变成新运动。
        if not self.host.acquire():
            return self._rejected("CONTROL_BUSY")
        if self._moving:
            self.host.release()
            return self._rejected("CONTROL_BUSY")
        self._moving = True
        try:
            return self._move(request)
        finally:
            self._moving = False
            self.host.release()

    def _rejected(self, code):
        return dict(schema_version=RESULT_SCHEMA, call_id=uuid4().hex, status="rejected", reason_code=code,
                    start_step=self.host.control_step, end_step=self.host.control_step,
                    step_attempts=0, collision_checked=False,
                    control_binding_sha256=self.backend.manifest["sha256"])

    def _move(self, request):
        before = after = resolved = r = None
        start = self.host.control_step
        call_id, started = uuid4().hex, time.monotonic()
        attempts = self.host.step_attempts
        status, reason, stable = "error", "CONTROL_ERROR", 0
        try:
            self.host.latest_observation = None
            self.capabilities()
            if not getattr(self.host, "healthy", True):
                raise ControlFault("HOST_REQUALIFICATION_REQUIRED")
            if self.calls >= self.profile.max_calls or self.total_steps >= self.profile.max_total_control_steps:
                raise ControlFault("BUDGET_EXHAUSTED")
            # 无效调用也消耗调用预算，但不会消耗物理步数。
            self.calls += 1
            r = self._request(request)
            if r["max_control_steps"] > self.profile.max_total_control_steps-self.total_steps:
                raise ControlFault("BUDGET_EXHAUSTED")
            resolved = pose(self.host.resolve_pose(r["target_pose"]))
            if resolved["frame_id"] != "world":
                raise ControlFault("FRAME_RESOLUTION_ERROR")
            before = after = self._state(self.host.observe(), r["joint_speed_limits"])
            previous = self.host.begin(self.joint_names)
            if not isinstance(previous, dict) or set(previous) != set(self.joint_names):
                raise ControlFault("TARGET_READ_ERROR")
            self._targets(previous, before, previous, r["joint_speed_limits"])
            reference = deepcopy(before["tcp"])
            self.host.emit(dict(kind="motion_start", call_id=call_id, control_step=start,
                                requested=r, resolved_target=resolved, before=before,
                                control_binding_sha256=self.backend.manifest["sha256"]))
            for _ in range(r["max_control_steps"]):
                if self.host.cancelled():
                    status, reason = "aborted", "CANCELLED"
                    break
                if time.monotonic()-started >= r["max_wall_time_s"]:
                    status, reason = "timeout", "WALL_TIME_LIMIT"
                    break
                state = self._state(self.host.observe(), r["joint_speed_limits"])
                # 参考按控制时间推进；不能每步跟着下沉的实测状态重置误差。
                reference = bounded_reference(reference, resolved,
                                              r["linear_speed_m_s"]*self.profile.dt_s*self.profile.reference_speed_fraction,
                                              r["angular_speed_rad_s"]*self.profile.dt_s*self.profile.reference_speed_fraction,
                                              r["mode"])
                proposed = self.backend.compute(reference, r["mode"])
                applied, limited = self._targets(proposed, state, previous, r["joint_speed_limits"])
                if self.host.cancelled():
                    status, reason = "aborted", "CANCELLED"
                    break
                if time.monotonic()-started >= r["max_wall_time_s"]:
                    status, reason = "timeout", "WALL_TIME_LIMIT"
                    break
                after = None  # 步进/刷新失败时不能把旧状态标为这一步的 after。
                after = self.host.step(applied, dict(call_id=call_id, requested_ik=proposed,
                                                    reference=reference, rate_limited_joints=limited, before=state))
                previous = applied
                self._state(after, r["joint_speed_limits"])
                dp, dr = pose_error(state["tcp"], after["tcp"])
                if (dp/self.profile.dt_s > r["linear_speed_m_s"]+1e-6
                        or dr/self.profile.dt_s > r["angular_speed_rad_s"]+1e-6):
                    raise ControlFault("SPEED_LIMIT")
                dp, dr = pose_error(after["tcp"], resolved)
                reached = dp <= r["position_tolerance_m"] and (r["mode"] == "position" or dr <= r["orientation_tolerance_rad"])
                stable = stable+1 if reached else 0
                if self.host.cancelled():
                    status, reason = "aborted", "CANCELLED"
                    break
                if time.monotonic()-started >= r["max_wall_time_s"]:
                    status, reason = "timeout", "WALL_TIME_LIMIT"
                    break
                if stable >= r["settle_steps"]:
                    status, reason = "completed", "TARGET_REACHED"
                    break
            else:
                status, reason = "timeout", "LOCAL_IK_NONCONVERGENCE"
        except ControlFault as exc:
            reason = exc.code
            if reason in {"INVALID_ARGUMENT", "UNKNOWN_FRAME", "CAPABILITY_UNAVAILABLE", "BUDGET_EXHAUSTED"}:
                status = "rejected"
            elif reason in {"JOINT_LIMIT", "SPEED_LIMIT", "TRACKING_ERROR"}:
                status = "failed"
            else:
                status = "error"
        except (TypeError, ValueError):
            status, reason = ("rejected", "INVALID_ARGUMENT") if r is None else ("error", "CONTROL_ERROR")
        except Exception:
            status, reason = "error", "CONTROL_ERROR"
        finally:
            self.total_steps += self.host.step_attempts-attempts
        # 刷新/事件写入在真实步进之后失败时保留已读到的 after，而不是伪造旧状态。
        if after is None and self.host.latest_observation is not None:
            after = deepcopy(self.host.latest_observation)
        hold = None
        if status != "completed" and (self.host.step_attempts != attempts or self.host.command_pending):
            hold = self.host.hold(self.joint_names)  # 只暂存实测位置，不添加预算之外的物理步。
        result = dict(schema_version=RESULT_SCHEMA, call_id=call_id, status=status, reason_code=reason,
                      start_step=start, end_step=self.host.control_step,
                      step_attempts=self.host.step_attempts-attempts,
                      step_outcome_unknown=self.host.step_outcome_unknown,
                      requested=r, resolved_target=resolved, before=before, after=after,
                      settled_steps=stable, hold=hold, collision_checked=False,
                      control_binding_sha256=self.backend.manifest["sha256"])
        try:
            self.host.emit(dict(kind="motion_end", **result))
        except ControlFault:
            result["status"], result["reason_code"] = "error", "EVENT_SINK_ERROR"
            result["hold"] = self.host.hold(self.joint_names)
        return result

    def move_gripper(self, request):
        """Width convergence with explicit safety limits; staging alone is not completion."""
        from .robot_binding import gripper_target
        start = time.monotonic()
        if not isinstance(request, dict) or set(request) != {"schema_version", "width_m", "speed_m_s", "tolerance_m", "settle_steps", "max_control_steps"} or request["schema_version"] != "gripper-motion-request/1":
            raise ControlFault("INVALID_ARGUMENT")
        self.capabilities()
        cfg = self.profile
        speed, tolerance = number(request["speed_m_s"], positive=True), number(request["tolerance_m"], positive=True)
        settle, limit = integer(request["settle_steps"]), integer(request["max_control_steps"])
        if speed > cfg.max_linear_speed_m_s or tolerance > cfg.position_tolerance_m or not cfg.settle_steps <= settle <= limit <= cfg.max_control_steps:
            raise ControlFault("INVALID_ARGUMENT")
        target = gripper_target(self.binding, request["width_m"])
        name = next(iter(target))
        if name not in self.binding["position_driven_joints"]:
            raise ControlFault("CAPABILITY_UNAVAILABLE")
        descriptor = next(j for j in self.binding["descriptor"]["joints"] if j["name"] == name)
        max_joint_speed = min(descriptor["limits"]["velocity"], self.host.joint_speed_limit(name), cfg.joint_speed_limits.get(name, descriptor["limits"]["velocity"]))
        if not self.host.acquire():
            raise ControlFault("CONTROL_BUSY")
        self.calls += 1
        stable = stuck = 0
        width = None
        # 夹爪被物体挡住：实测开度不在容差内，且开度与关节速度都已停止变化。
        stall_width_m = max(1e-5, 0.05*speed*cfg.dt_s)
        stall_joint_speed = 0.05*max_joint_speed
        status, reason = "timeout", "GRIPPER_NONCONVERGENCE"
        try:
            if self.calls > cfg.max_calls:
                status, reason = "rejected", "CALL_BUDGET"
            else:
                previous = self.host.observe()
                reference = self.host.begin([name])[name]
                for _ in range(limit):
                    if self.host.cancelled():
                        status, reason = "aborted", "CANCELLED"
                        break
                    if self.total_steps >= cfg.max_total_control_steps or time.monotonic()-start >= cfg.max_wall_time_s:
                        reason = "CONTROL_BUDGET"
                        break
                    step = min(speed*abs(self.binding["semantics"]["gripper"]["joint_per_m"]), max_joint_speed)*cfg.dt_s
                    reference += max(-step, min(step, target[name]-reference))
                    before = self.host.step_attempts
                    try:
                        current = self.host.step({name: reference}, {"primitive": "gripper", "width_m": request["width_m"]})
                    finally:
                        self.total_steps += self.host.step_attempts-before
                    i = current["joint_names"].index(name)
                    validate_targets(self.binding, {name: current["q"][i]})
                    if abs(current["dq"][i]) > max_joint_speed+1e-6:
                        raise ControlFault("SPEED_LIMIT")
                    if abs(current["gripper"]["width_m"]-previous["gripper"]["width_m"]) > speed*cfg.dt_s+1e-6:
                        raise ControlFault("SPEED_LIMIT")
                    width = current["gripper"]["width_m"]
                    reached = abs(width-request["width_m"]) <= tolerance
                    stable = stable+1 if reached else 0
                    # 只在指令参考已到达目标后判定，避免加速起步阶段误判为 blocked。
                    stalled = (not reached and abs(reference-target[name]) <= 1e-9
                               and abs(width-previous["gripper"]["width_m"]) <= stall_width_m
                               and abs(current["dq"][i]) <= stall_joint_speed)
                    stuck = stuck+1 if stalled else 0
                    previous = current
                    if self.host.cancelled():
                        status, reason = "aborted", "CANCELLED"
                        break
                    if stable >= settle:
                        status, reason = "completed", "SETTLED"
                        break
                    if stuck >= settle:
                        # 不是执行错误：是否构成有效抓取由任务 Judge 依据开度/接触证据判断。
                        status, reason = "blocked", "GRIPPER_BLOCKED"
                        break
        except ControlFault as exc:
            status, reason = ("failed" if exc.code in ("SPEED_LIMIT", "JOINT_LIMIT") else "error"), exc.code
        except Exception:
            status, reason = "error", "GRIPPER_CONTROL_ERROR"
        finally:
            if status not in ("completed", "blocked"):
                self.host.hold([name])
            self.host.release()
        return {"schema_version": "gripper-motion-result/1", "status": status, "reason_code": reason,
                "settled_steps": stable, "stalled_steps": stuck, "width_m": width,
                "control_step": self.host.control_step,
                "step_outcome_unknown": self.host.step_outcome_unknown}

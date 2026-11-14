"""批量 supervisor 与其 task 实现之间共享的契约：policy、callable 结果、``BatchTask`` 与发起异常。

``orchestration.batch`` 的控制器和 ``adapters.process.subprocess_task`` 都只通过本模块交换
这些类型，因此适配器无需导入编排层。本模块仅依赖标准库和 ``datafarm.kernel``。
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from datafarm.kernel import io

POLICY_SCHEMA = "datafarm.batch-policy/1.0.0"

POLL_STATUSES = ("running", "finished", "crashed", "unknown")
CRASH_KINDS = ("transient", "provider_down", "permanent", "capacity")
DECISIONS = ("pass", "reject", "abstain")
LEASE_STATES = ("alive", "dead", "unknown")


class ProviderDown(Exception):
    """当 provider 端点不可用时由 ``launch`` 抛出；没有启动任何东西。"""


class LaunchNotStarted(Exception):
    """当没有启动任何东西时由 ``launch`` 抛出；按临时崩溃计数。"""


class LaunchDeferred(Exception):
    """当容量不可用时由 ``launch`` 抛出；没有启动任何东西，也不消耗预算。"""


class LaunchOutcomeUnknown(Exception):
    """当 try 可能已启动、也可能未启动时由 ``launch`` 抛出。"""


# -- policy ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class BatchPolicy:
    """循环控制 policy。每个边界都显式给出；``from_dict`` 拒绝未知键。"""

    # 预算
    max_attempts_per_item: int = 8  # 已完成（已验证）的 try = 每个 item 的 seed 数
    max_crash_retries_per_item: int = 8  # 崩溃后的重新发起；绝不消耗 seed
    max_concurrency: int = 10
    max_rounds: int = 12  # try 代数硬上限（纵深防御）
    zero_progress_rounds: int = 3  # 连续无新准入的已关闭轮次；0 = 关闭
    max_total_cost: float | None = None
    launch_cost_estimate: float = 0.0  # 发起前为每个 in-flight try 预留这么多成本
    max_wall_seconds: float | None = None  # 活跃时间；provider 宕机暂停不计入
    # 崩溃
    crash_retry_delay_s: float = 30.0  # 崩溃重试前 per-item 延迟，每次崩溃翻倍
    crash_retry_delay_cap_s: float = 900.0
    crash_burst_items: int = 5  # 时间窗内不同 item 的崩溃构成 burst；0 = 关闭
    crash_burst_window_s: float = 600.0
    burst_backoff_s: float = 60.0  # 首次全局退避；burst 重复出现时翻倍
    burst_backoff_cap_s: float = 1800.0
    # provider 可用性
    provider_probe_interval_s: float = 120.0
    max_provider_pause_s: float = 6 * 3600.0
    # 存活状态
    max_consecutive_unknown: int = 20  # 暂停前单个 try 存活性 unknown 的轮询次数
    lost_confirmations: int = 2  # 连续死亡 lease 探针（无存活证据）= 丢失
    # 流式校验
    systematic_min_items: int = 8  # 检查运行前所需的不同 item 数；0 = 关闭
    systematic_window: int = 20  # 考虑最近多少个不同 item
    systematic_share: float = 0.6  # 单个 gate 失败占比达到该值即停止发起
    max_verify_errors: int = 3  # 验证器异常达到该次数后 try 计为 abstain
    # 循环机制
    poll_interval_s: float = 30.0
    max_tick_gap_s: float = 600.0  # 更长的间隔（supervisor 宕机）不计入预算
    drain_on_stop: bool = True  # 进入 stopped_* 状态前等待 in-flight 的 try
    seed_base: int = 0
    seed_modulus: int = 2_147_483_647

    def __post_init__(self) -> None:
        positive_ints = ("max_attempts_per_item", "max_concurrency", "max_rounds",
                         "max_consecutive_unknown", "lost_confirmations", "systematic_window",
                         "max_verify_errors")
        non_negative_ints = ("max_crash_retries_per_item", "zero_progress_rounds",
                             "crash_burst_items", "systematic_min_items", "seed_base")
        non_negative_floats = ("launch_cost_estimate", "crash_retry_delay_s",
                               "crash_retry_delay_cap_s", "crash_burst_window_s", "burst_backoff_s",
                               "burst_backoff_cap_s", "provider_probe_interval_s",
                               "max_provider_pause_s", "poll_interval_s", "max_tick_gap_s")
        for name in positive_ints:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"policy.{name} must be an integer >= 1, got {value!r}")
        for name in non_negative_ints:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"policy.{name} must be an integer >= 0, got {value!r}")
        for name in non_negative_floats:
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or value < 0):
                raise ValueError(f"policy.{name} must be a number >= 0, got {value!r}")
        for name in ("max_total_cost", "max_wall_seconds"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value <= 0):
                raise ValueError(f"policy.{name} must be null or a number > 0, got {value!r}")
        if not 0 < float(self.systematic_share) <= 1:
            raise ValueError("policy.systematic_share must be in (0, 1]")
        if self.systematic_min_items > self.systematic_window:
            raise ValueError("policy.systematic_min_items cannot exceed systematic_window")
        if isinstance(self.seed_modulus, bool) or not isinstance(self.seed_modulus, int) \
                or self.seed_modulus < 2:
            raise ValueError("policy.seed_modulus must be an integer >= 2")
        if self.poll_interval_s <= 0:
            raise ValueError("policy.poll_interval_s must be > 0")
        if not isinstance(self.drain_on_stop, bool):
            raise ValueError("policy.drain_on_stop must be a boolean")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "BatchPolicy":
        data = dict(data or {})
        schema = data.pop("schema", POLICY_SCHEMA)
        if schema != POLICY_SCHEMA:
            raise ValueError(f"unsupported policy schema {schema!r}; expected {POLICY_SCHEMA}")
        data.pop("description", None)
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - names)
        if unknown:
            raise ValueError(f"unknown policy keys {unknown}; allowed: {sorted(names)}")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": POLICY_SCHEMA, **dataclasses.asdict(self)}

    def sha256(self) -> str:
        return io.sha256_json(self.to_dict(), allow_nan=True)

    def replace(self, **changes: Any) -> "BatchPolicy":
        return dataclasses.replace(self, **changes)


# -- callable 结果 -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Poll:
    """``poll(handle)`` 报告的内容。``running`` 要求有存活的正面证据。"""

    status: str
    result: Any = None
    detail: str | None = None
    kind: str = "transient"  # crashed 时：transient | provider_down | permanent
    cost: float | None = None

    def __post_init__(self) -> None:
        if self.status not in POLL_STATUSES:
            raise ValueError(f"poll status must be one of {POLL_STATUSES}, got {self.status!r}")
        if self.kind not in CRASH_KINDS:
            raise ValueError(f"crash kind must be one of {CRASH_KINDS}, got {self.kind!r}")

    @classmethod
    def coerce(cls, value: Any) -> "Poll":
        if isinstance(value, Poll):
            return value
        if isinstance(value, str):
            return cls(value)
        if isinstance(value, Mapping):
            allowed = {f.name for f in dataclasses.fields(cls)}
            unknown = set(value) - allowed
            if unknown:
                raise ValueError(f"unknown poll keys {sorted(unknown)}")
            return cls(**dict(value))
        if isinstance(value, tuple) and value:
            return cls(*value)
        raise TypeError(f"cannot interpret poll result {value!r}")


@dataclass(frozen=True)
class Verdict:
    """``verify(item, result)`` 报告的内容。``abstain`` 是一种一等公民的未准入。"""

    decision: str
    reason: str | None = None
    gate: str | None = None
    final: bool = False  # 最终拒绝会结束该 item（rejected_final）
    evidence: Any = None

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"verdict must be one of {DECISIONS}, got {self.decision!r}")

    @classmethod
    def passed(cls, evidence: Any = None) -> "Verdict":
        return cls("pass", evidence=evidence)

    @classmethod
    def rejected(cls, reason: str, gate: str | None = None, *, final: bool = False,
                 evidence: Any = None) -> "Verdict":
        return cls("reject", reason=reason, gate=gate, final=final, evidence=evidence)

    @classmethod
    def abstained(cls, reason: str, gate: str | None = None) -> "Verdict":
        return cls("abstain", reason=reason, gate=gate)

    @classmethod
    def coerce(cls, value: Any) -> "Verdict":
        if isinstance(value, Verdict):
            return value
        if isinstance(value, str):
            return cls(value)
        if isinstance(value, Mapping):
            allowed = {f.name for f in dataclasses.fields(cls)}
            unknown = set(value) - allowed
            if unknown:
                raise ValueError(f"unknown verdict keys {sorted(unknown)}")
            return cls(**dict(value))
        if isinstance(value, tuple) and value:
            return cls(*value)
        raise TypeError(f"cannot interpret verdict {value!r}")


def coerce_lease(value: Any) -> str:
    if value is True:
        return "alive"
    if value is False:
        return "dead"
    if value is None:
        return "unknown"
    if isinstance(value, str) and value in LEASE_STATES:
        return value
    raise ValueError(f"lease probe must return one of {LEASE_STATES}, got {value!r}")


@dataclass
class BatchTask:
    """task 特有行为，以 callable 注入。

    ``launch(item, attempt, seed)`` 启动一次 try，并返回可 JSON 序列化的 handle。若它还接受
    ``launch_id`` 关键字参数，则会收到本次发起的确定性 id（``<item>.aNN.tNN``）；同一 try
    的崩溃重试会获得新的 ``tNN`` 后缀。

    ``poll(handle)`` 返回 ``running | finished | crashed | unknown``（或 :class:`Poll`）。

    ``verify(item, result)`` 返回 ``pass | reject | abstain``（或 :class:`Verdict`）。

    可选：``lease_probe(handle) -> alive|dead|unknown``、``cost(result) -> float``、
    ``reconcile(item, attempt, seed, launch_id) -> handle | None | "unknown"``（查找 intent
    已持久化但 handle 未持久化的发起）、``provider_probe() -> up|down|unknown``
    以及 ``seed_for(item, attempt) -> int``。
    """

    launch: Callable[..., Any]
    poll: Callable[[Any], Any]
    verify: Callable[[str, Any], Any]
    lease_probe: Callable[[Any], Any] | None = None
    cost: Callable[[Any], Any] | None = None
    reconcile: Callable[[str, int, int, str], Any] | None = None
    provider_probe: Callable[[], Any] | None = None
    seed_for: Callable[[str, int], int] | None = None


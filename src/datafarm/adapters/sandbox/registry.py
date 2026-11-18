"""沙箱后端的注册、探测与选择；``wrap`` 按 mode 分发到对应后端。"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence

from datafarm.adapters.sandbox import bwrap, proot, unshare
from datafarm.adapters.sandbox.model import IsolationUnavailable, SandboxSpec

BACKENDS = {"bwrap": bwrap, "proot": proot, "unshare": unshare}

_AVAILABLE: dict[str, tuple[bool, str]] = {}


def probe_available(mode: str, spec: SandboxSpec | None = None) -> tuple[bool, str]:
    """此宿主能否创建该沙箱？按 (mode, 二进制路径) 缓存。

    proot 的探测需要知道配置的二进制路径；``spec`` 缺省时回退到
    环境变量 PROOT 或 PATH。"""

    if mode == "none":
        return True, "no isolation requested"
    key = mode
    if mode == "proot" and spec is not None and spec.proot_bin:
        key = mode + ":" + spec.proot_bin
    if key in _AVAILABLE:
        return _AVAILABLE[key]
    backend = BACKENDS.get(mode)
    if backend is None:
        result = (False, f"unknown isolation mode {mode!r}")
    elif mode == "proot":
        result = backend.probe(spec.proot_bin if spec is not None else None)
    else:
        result = backend.probe()
    _AVAILABLE[key] = result
    return result


def _fallback_modes(fallback: str | Sequence[str] | None) -> list[str]:
    if fallback is None:
        return []
    if isinstance(fallback, str):
        return [fallback]
    return [str(mode) for mode in fallback]


def resolve_spec(spec: SandboxSpec, *, fallback: str | Sequence[str] | None, allow_degraded: bool,
                 available: Callable[[str], tuple[bool, str]] | None = None) -> SandboxSpec:
    """选择可用模式：首选模式，否则按顺序尝试回退；都不可用则失败关闭。"""

    if available is None:
        available = lambda mode: probe_available(mode, spec=spec)  # noqa: E731
    reasons = []
    modes: list[str] = []
    for mode in [spec.mode, *_fallback_modes(fallback)]:
        if mode and mode not in modes:
            modes.append(mode)
    for mode in modes:
        ok, why = available(mode)
        if not ok:
            reasons.append(f"{mode}: {why}")
            continue
        if mode == "unshare" and spec.base == "allowlist":
            if not allow_degraded:
                reasons.append("unshare: allowlist spec needs allow_degraded_fallback")
                continue
            return spec.replace(mode="unshare", base="root_ro", degraded=True)
        if mode == "proot":
            # proot 没有只读 bind：未切换用户（无法 chown 到沙箱 uid）时
            # 只读目录实际可写，标记 ro_soft 让探针与审计如实记录降级。
            return spec.replace(mode="proot", ro_soft=spec.run_as is None)
        return spec.replace(mode=mode)
    raise IsolationUnavailable("no usable isolation: " + "; ".join(reasons))


def wrap(spec: SandboxSpec, command: Sequence[str]) -> list[str]:
    if spec.mode == "none":
        return list(command)
    backend = BACKENDS.get(spec.mode)
    if backend is None:
        raise IsolationUnavailable(f"unknown isolation mode {spec.mode!r}")
    prepare = getattr(backend, "prepare", None)
    if prepare is not None:
        prepare(spec)
    return backend.argv(spec, command)

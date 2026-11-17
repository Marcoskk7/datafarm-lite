"""scene-author 每轮 agent 会话的 OS 沙箱（`datafarm.adapters.sandbox` 的 allowlist 语义，与 expert harness 相同）。

guest 从空根开始，只出现：系统目录（`DEFAULT_SYSTEM_RO`）、`/dev`、`/proc`、后端提供的私有 `/tmp`；本轮工作区与
本轮私有 HOME（读写）；`WORK/sandbox/repo/` 下角色允许的仓库文件快照（只读意图）；agent CLI 程序与跑
`tools/simready.py` 的解释器前缀（只读意图）；配置 `extra_ro` 显式列出的路径；构造时给出的 SimReady 缓存与机器人包
（只读意图——角色指令让 agent 跑 `simready.py search/probe` 并信 `host/asset-records.json`）。其余一切——仓库本体
（含 `examples/`、`runs/`、`verification/`、`.git`、prompts、主机代码）、`WORK` 下的会话记录/尝试/prepare 产物、
往轮 HOME、真实 HOME 里的 agent 配置与凭据目录——在 guest 里不存在。缓存/机器人包只在**未**绑定时进遮蔽清单（即
构造参数为 None、调用方把它们放进 `hidden`），因此“绑定”与“断言不存在”互斥，不会自相矛盾。

用快照而不是 bind 仓库：proot 没有只读 bind（`ro_soft`），直接 bind 时 agent 能改写主机随后 import 的源码。
所有挂载都是同路径的 `ro`/`rw`，不用改名 bind（proot 后端只取 bind 源路径）。每轮开始前在同一个沙箱里跑探针，
任何应隐藏路径可见、工作区不可写都会让本轮不启动 agent，且工具以 2 退出。`ro_soft` 时摘要写明只读不是强制
（`readonly_enforced: false`），绝不把 proot 做不到的只读说成强制。
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from datafarm.expert.util import ConfigError
from datafarm.kernel.io import atomic_write_json

# 唯一的 sandbox 导入点：适配器包（用户尚未提交的重构）缺失时不让 ImportError 冒出去，
# 而是记下原因，由 AuthoringSandbox 在真正需要隔离时抛 ConfigError（工具以 2 退出）。
try:
    from datafarm.adapters.sandbox import (DEFAULT_SYSTEM_RO, SandboxSpec, make_empty_file, resolve_spec,
                                           run_probe, wrap)
except ImportError as _exc:  # pragma: no cover - 只在缺包时走到
    SANDBOX_IMPORT_ERROR: ImportError | None = _exc
    DEFAULT_SYSTEM_RO = ()  # type: ignore[assignment]
else:
    SANDBOX_IMPORT_ERROR = None

SANDBOX_PACKAGE = "datafarm.adapters.sandbox"

MODES = ("bwrap", "proot", "none")
FALLBACK_MODES = ("bwrap", "proot")  # unshare 会把 allowlist 退化成 root_ro denylist（整个仓库可见），不接受
DEFAULTS: dict[str, Any] = {"mode": "bwrap", "fallback": ["proot"], "proot": None, "python": None, "extra_ro": []}
# 角色允许读的仓库文件（相对仓库根）：两份 IR 文档、闸门规则表、`tools/simready.py` 及其 import 闭包。
REPO_READABLE = ("docs/ir/request.md", "docs/ir/scene-layout.md", "configs/scene-roles.json", "tools/simready.py",
                 "src/datafarm/__init__.py", "src/datafarm/adapters/__init__.py", "src/datafarm/adapters/assets",
                 "src/datafarm/kernel")
# 探针逐项确认 guest 里不存在的仓库路径（仓库本体整个不 bind，这里列最要紧的几项）。
REPO_HIDDEN = ("examples", "runs", "verification", ".git", "AGENTS.md", "prompts", "src", "tools", "configs",
               "bindings.json")
WORK_HIDDEN = ("sessions", "attempts", "prepared", "attempts.jsonl", "agent-calls.jsonl",
               "scene-authoring-report.json")
HOME_HIDDEN = (".claude", ".claude.json", ".codex", ".ssh", ".config", ".npmrc", ".bash_history")
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"


def sandbox_config(raw: Any) -> dict[str, Any]:
    """校验 agent 配置的 `sandbox` 段并补默认值。缺省 = bwrap，回退 proot；`{"mode": "none"}` 是显式退出。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError('sandbox must be an object, e.g. {"mode": "none"} to opt out')
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"sandbox: unknown keys {unknown}; allowed {sorted(DEFAULTS)}")
    cfg = {**DEFAULTS, **raw}
    if cfg["mode"] not in MODES:
        raise ConfigError(f"sandbox.mode must be one of {MODES}")
    fallback = cfg["fallback"]
    if not isinstance(fallback, list) or any(m not in FALLBACK_MODES for m in fallback):
        raise ConfigError(f"sandbox.fallback must be a list drawn from {FALLBACK_MODES}")
    for key in ("proot", "python"):
        if cfg[key] is not None and (not isinstance(cfg[key], str) or not cfg[key]):
            raise ConfigError(f"sandbox.{key} must be null or a non-empty string")
    if cfg["python"] is not None and not cfg["python"].startswith("/"):
        raise ConfigError("sandbox.python must be an absolute path")
    extra = cfg["extra_ro"]
    if not isinstance(extra, list) or any(not isinstance(p, str) or not p.startswith(("/", "${")) for p in extra):
        raise ConfigError("sandbox.extra_ro must be a list of absolute paths")
    return cfg


def _expand(value: str) -> str:
    if "${" not in value:
        return value
    from datafarm.config import expand, load_bindings
    return expand(value, load_bindings())


def _covered(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def require_sandbox() -> None:
    """缺 `datafarm.adapters.sandbox` 包时抛 ConfigError（点名缺的包），而不是 ImportError。"""
    if SANDBOX_IMPORT_ERROR is not None:
        raise ConfigError(
            f"{SANDBOX_PACKAGE} is unavailable: the OS-sandbox adapter package is missing or incomplete "
            f"({SANDBOX_IMPORT_ERROR}). Restore that package, or opt out explicitly with "
            f'{{"sandbox": {{"mode": "none"}}}} in the agent config.')


class AuthoringSandbox:
    """一次 authoring 运行的沙箱；`spec()` 每轮生成一个新 spec。`mode == "none"` 时不包装、不探针。"""

    def __init__(self, cfg: Mapping[str, Any], *, repo: Path, work: Path, hidden: Sequence[str | Path] = (),
                 home: str | None = None, cache: str | Path | None = None,
                 robot_package: str | Path | None = None,
                 available: Callable[[str], tuple[bool, str]] | None = None) -> None:
        self.cfg = dict(cfg)
        self.mode = self.cfg["mode"]
        self.repo = Path(repo).resolve()
        self.work = Path(work).resolve()
        self.root = self.work / "sandbox"
        self.snapshot = self.root / "repo"
        self.available = available
        self.refusal: dict[str, Any] | None = None
        # 缓存与机器人包在 guest 里可见（角色要用 simready.py / 读机器人包）；给出即只读 bind，不给则留在遮蔽清单。
        self.cache = None if cache is None else str(Path(cache).resolve())
        self.robot_package = None if robot_package is None else str(Path(robot_package).resolve())
        self.bound = [p for p in (self.cache, self.robot_package) if p]
        self.proot_bin = _expand(self.cfg["proot"]) if self.cfg["proot"] else ""
        if not self.proot_bin and os.environ.get("DATAFARM_PROOT_BIN"):
            self.proot_bin = os.environ["DATAFARM_PROOT_BIN"]
        home = home if home is not None else os.environ.get("HOME", "")
        self.hidden = [str(p) for p in hidden] + ([str(Path(home) / n) for n in HOME_HIDDEN] if home else [])
        if self.mode == "none":
            self.python = sys.executable
            self.tools = self.repo / "tools"
            return
        require_sandbox()
        base = self.cfg["python"] or getattr(sys, "_base_executable", None) or sys.executable
        self.python = os.path.realpath(base)
        self.tools = self.snapshot / "tools"
        # 选不出后端就在任何会话之前失败关闭（IsolationUnavailable）。
        self._resolve(SandboxSpec(mode=self.mode, base="allowlist", proot_bin=self.proot_bin))

    @property
    def simready_command(self) -> str:
        return f"{self.python} {self.tools / 'simready.py'}"

    def _resolve(self, spec: SandboxSpec) -> SandboxSpec:
        return resolve_spec(spec, fallback=self.cfg["fallback"], allow_degraded=False, available=self.available)

    def none_summary(self) -> dict[str, Any]:
        """显式退出：与沙箱摘要同形的字段 + 明白写出“没有隔离”，便于和 expert harness 的记录对比。"""
        return {"mode": "none", "base": "none", "guest_root": "host", "ok": True, "degraded": False,
                "ro_soft": False, "readonly_enforced": False, "share_net": True, "readonly_binds": 0,
                "checked_masks": 0, "leaks": 0, "umounted": 0, "writable_readonly": 0,
                "unwritable_workspace": 0, "error": None, "spec": None, "probe": None, "leaked_paths": [],
                "opt_out": "agent config sandbox.mode = none: the agent ran without OS isolation"}

    def _refresh_snapshot(self) -> None:
        if self.snapshot.exists():
            shutil.rmtree(self.snapshot)
        for rel in REPO_READABLE:
            src, dest = self.repo / rel, self.snapshot / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, dest)

    def spec(self, round_index: int, workspace: Path, program: str) -> tuple[SandboxSpec, dict[str, Any]]:
        """本轮已 resolve 的 spec 与探针路径清单；重建仓库快照与本轮 HOME。"""
        rdir = self.root / "rounds" / f"{round_index:03d}"
        home = rdir / "home"
        home.mkdir(parents=True, exist_ok=True)
        self._refresh_snapshot()
        system = list(DEFAULT_SYSTEM_RO)
        ro = [str(self.snapshot)]
        prefix = str(Path(self.python).parents[1])
        if not _covered(prefix, system):
            ro.append(prefix)
        for path in dict.fromkeys((program, os.path.realpath(program))):
            if not _covered(path, system + ro):
                ro.append(path)
        ro += [_expand(p) for p in self.cfg["extra_ro"]]
        for path in self.bound:  # SimReady 缓存 / 机器人包：可见但只读意图
            if path not in ro and not _covered(path, system):
                ro.append(path)
        spec = SandboxSpec(mode=self.mode, base="allowlist", ro=tuple(ro), rw=(str(workspace), str(home)),
                           share_net=True, cwd=str(workspace), empty_file=make_empty_file(rdir),
                           proot_bin=self.proot_bin)
        hidden = [str(self.repo / rel) for rel in REPO_HIDDEN]
        hidden += [str(self.work / name) for name in WORK_HIDDEN]
        hidden += [str(self.root / "rounds" / f"{i:03d}") for i in range(1, round_index)]
        hidden += self.hidden
        # 绑定与遮蔽互斥：已 bind 的路径不再进遮蔽清单（否则探针会断言一个有意可见的路径不存在）。
        hidden = [p for p in dict.fromkeys(hidden) if not _covered(p, ro)]
        paths = {"dir": rdir, "home": home, "masked": hidden,
                 "readonly": [p for p in ro if os.path.isdir(p)], "writable": [str(workspace), str(home), "/tmp"]}
        return self._resolve(spec), paths

    def env(self, base: Mapping[str, str], home: Path, backend_name: str) -> dict[str, str]:
        """guest 环境：HOME/PATH/TMPDIR/agent 配置目录换成本轮的；凭据变量照旧只按名字转发。"""
        drop = ("HOME", "PATH", "TMPDIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
        env = {k: v for k, v in base.items() if k not in drop}
        env.update(HOME=str(home), PATH=SANDBOX_PATH, TMPDIR="/tmp")
        if backend_name == "claude":
            env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        return env

    def prepare_round(self, round_index: int, workspace: Path, command: Sequence[str], base_env: Mapping[str, str],
                      backend_name: str) -> tuple[list[str] | None, dict[str, str], dict[str, Any]]:
        """(包装后的 argv, env, 隔离摘要)。探针失败时 argv 为 None：本轮不得启动 agent。"""
        command = list(command)
        program = command[0] if os.path.isabs(command[0]) else shutil.which(command[0], path=base_env.get("PATH"))
        if not program:
            raise ConfigError(f"agent program {command[0]!r} not found on PATH")
        command[0] = program
        spec, paths = self.spec(round_index, workspace, program)
        env = self.env(base_env, paths["home"], backend_name)
        probe = run_probe(spec, masked=paths["masked"], readonly_dirs=paths["readonly"],
                          writable_dirs=paths["writable"], env={"PATH": SANDBOX_PATH})
        rdir: Path = paths["dir"]
        atomic_write_json(rdir / "sandbox-spec.json", spec.to_dict())
        atomic_write_json(rdir / "isolation-probe.json", probe.to_dict())
        summary = {"mode": spec.mode, "base": spec.base, "guest_root": "empty (allowlist)", "ok": probe.ok,
                   "degraded": spec.degraded, "ro_soft": spec.ro_soft,
                   # proot 未切换用户时没有只读 bind：只读只是意图，不是强制，摘要不得声称强制。
                   "readonly_enforced": not spec.ro_soft and spec.mode != "none",
                   "share_net": spec.share_net, "readonly_binds": len(paths["readonly"]),
                   "checked_masks": len(paths["masked"]), "leaks": len(probe.leaks),
                   "umounted": len(probe.umounted), "writable_readonly": len(probe.writable_readonly),
                   "unwritable_workspace": len(probe.unwritable_workspace), "error": probe.error,
                   "spec": (rdir / "sandbox-spec.json").relative_to(self.work).as_posix(),
                   "probe": (rdir / "isolation-probe.json").relative_to(self.work).as_posix()}
        summary["leaked_paths"] = list(probe.leaks)
        if not probe.ok:
            self.refusal = dict(summary)  # 失败关闭：main 见到它就退出 2（agent 未启动）
            return None, env, summary
        return wrap(spec, command), env, summary

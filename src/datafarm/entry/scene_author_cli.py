"""``tools/scene_author.py``：由 coding agent 编写 scene-layout，主机闸门逐轮判定（见 docs/reference/scene-assembly.md）。

组合根：把 agent harness（`make_agent_backend` + `SessionRunner` + 调用账本）接成 `run_agent`，每轮会话
包在 `scene_author_sandbox.AuthoringSandbox` 的 OS 沙箱里；把 `tools/scene_assemble.py prepare` 子进程接成
`prepare_assets`，把机器人包 URDF 接成描述符，再交给 `datafarm.scene.authoring.author_layout`。
退出码：0 接受，1 未接受，2 用法、配置错误或没有可用沙箱（未显式 `sandbox.mode = none`）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from datafarm.adapters.agent_harness.backends import make_agent_backend
from datafarm.adapters.agent_harness.call_log import AgentCallRecord, append_call, new_call_id
from datafarm.adapters.agent_harness.session import AgentBackend, SessionRunner, SessionSpec, classify_errors
from datafarm.adapters.assets.simready import FETCH_MANIFEST, endpoint_from_env
from datafarm.entry.scene_author_sandbox import AuthoringSandbox, sandbox_config
from datafarm.entry.scene_files import load_prepared, robot_descriptor
from datafarm.expert.util import ConfigError, HarnessError
from datafarm.instructions import build_context, repository_root
from datafarm.kernel.io import atomic_write_json, sha256_file, sha256_text, utc_now
from datafarm.scene.authoring import (HOST_DIR, LAYOUT_FILE, AgentTurn, CheckError, CheckResult, PrepareError,
                                      RunCheck, author_layout, check_diagnostics, check_summary)
from datafarm.scene.conformance import load_role_rules

ROLE = "scene-author"
COMPONENT = "scene-author/session"
CONFIG_SCHEMA = "datafarm.scene-author/1"
CONFIG_KEYS = frozenset({"schema", "notes", "agent", "price_table", "wall_s_per_round", "max_budget_usd_per_round",
                         "env_passthrough", "sandbox", "check"})
CHECK_DEFAULTS: dict[str, Any] = {
    "python": None, "env_script": None, "timeout_s": 1500.0,
    "settle_s": 2.0, "probe_s": 2.0, "drift_tol": 0.005, "speed_tol": 0.01,
    "joint_tol_active": 0.02, "joint_tol_passive": 0.005, "penetration_tol": 0.003,
    "nconmax": 4096, "njmax": 8192,
}
CHECK_KEYS = frozenset(CHECK_DEFAULTS) | {"notes"}
CHECK_NUMERIC = ("timeout_s", "settle_s", "probe_s", "drift_tol", "speed_tol", "joint_tol_active",
                 "joint_tol_passive", "penetration_tol", "nconmax", "njmax")


def check_config(raw: Any) -> dict[str, Any]:
    """校验并补全 agent 配置的 `check` 节；容差的唯一来源就是这里，再由 CLI 显式转成工具参数。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("'check' must be an object")
    unknown = sorted(set(raw) - CHECK_KEYS)
    if unknown:
        raise ValueError(f"check: unknown keys {unknown}")
    out = {key: raw.get(key, default) for key, default in CHECK_DEFAULTS.items()}
    for key in CHECK_NUMERIC:
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
            raise ValueError(f"check.{key} must be a positive number, got {value!r}")
    for key in ("python", "env_script"):
        if out[key] is not None and (not isinstance(out[key], str) or not out[key]):
            raise ValueError(f"check.{key} must be a non-empty string or null")
    return out


def resolve_check_interpreter(python: str | None) -> str | None:
    """`--check-python` / `check.python` / `$ISAAC_PYTHON` → 可执行文件的绝对路径；找不到返回 None。"""
    if not python:
        return None
    path = Path(python).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        # 保留 symlink 入口：Linux venv 的 bin/python 指向基础解释器，resolve 后 CPython 找不到 pyvenv.cfg。
        return os.path.abspath(os.fspath(path))
    if os.sep not in python:
        found = shutil.which(python)
        if found:
            return found
    return None


def load_config(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"{path}: schema must be {CONFIG_SCHEMA}")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown keys {unknown}")
    if not isinstance(config.get("agent"), Mapping) or not config["agent"].get("backend"):
        raise ValueError(f"{path}: needs an 'agent' object with a backend")
    config["sandbox"] = sandbox_config(config.get("sandbox"))
    config["check"] = check_config(config.get("check"))
    prices: dict[str, Any] = {}
    if config.get("price_table"):
        prices = json.loads((path.parent / config["price_table"]).read_text(encoding="utf-8")).get("models", {})
    return config, prices


class AgentSessions:
    """一轮一个新 agent 会话（不 resume：上一轮的诊断和主机证据都在 prompt 与工作区里）。"""

    def __init__(self, *, backend: AgentBackend, config: Mapping[str, Any], work: Path, call_log: Path,
                 run_id: str, env: Mapping[str, str], sandbox: AuthoringSandbox) -> None:
        self.backend = backend
        self.wall_s = float(config.get("wall_s_per_round", 1800))
        self.max_budget = config.get("max_budget_usd_per_round")
        self.sessions = work / "sessions"
        self.call_log = call_log
        self.run_id = run_id
        self.env = dict(env)
        self.sandbox = sandbox

    def __call__(self, round_index: int, prompt: str, workspace: Path) -> AgentTurn:
        backend = self.backend
        new_id = None if backend.name in ("codex", "codex-metered") else str(uuid.uuid4())
        spec = SessionSpec(index=round_index, prompt=prompt, cwd=str(workspace), new_session_id=new_id,
                           max_budget_usd=self.max_budget)
        self.sessions.mkdir(parents=True, exist_ok=True)
        transcript = self.sessions / f"session-{round_index:03d}.jsonl"
        stderr_path = self.sessions / f"session-{round_index:03d}.stderr.log"
        started_at = utc_now()
        with backend.session(spec, self.sessions):
            parser = backend.parser()
            argv, env = backend.command(spec), {**self.env, **backend.env()}
            if self.sandbox.mode == "none":
                isolation = self.sandbox.none_summary()
            else:
                argv, env, isolation = self.sandbox.prepare_round(round_index, workspace, argv, env, backend.name)
            if argv is None:
                return self._refused(round_index, prompt, started_at, new_id, isolation)
            info = SessionRunner().run(argv, env=env, cwd=str(workspace),
                                       stdin_text=prompt if backend.uses_stdin_prompt else None,
                                       transcript=transcript, stderr_path=stderr_path, wall_s=self.wall_s,
                                       parser=parser)
        refused = refuse_links(workspace)
        parsed = backend.settle(parser.finish())
        _, auth_error, error_type = classify_errors(parsed, info.stderr_tail, info.exit_code)
        if info.stop_reason == "wall_cap":
            status = "timeout"
        elif error_type == "max_budget":
            status = "budget_exhausted"
        elif error_type:
            status = "error"
        else:
            status = "ok"
        call = AgentCallRecord(
            call_id=new_call_id(), component=COMPONENT, backend=backend.call_backend,
            model=parsed.model or backend.model, started_at=started_at, ended_at=utc_now(), status=status,
            prompt_sha256=sha256_text(prompt),
            response_sha256=sha256_file(transcript) if transcript.exists() else None,
            session_id=parsed.session_id or new_id, input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens, cache_read_tokens=parsed.cache_read_tokens,
            cache_write_tokens=parsed.cache_write_tokens, cost_usd=parsed.cost_usd, cost_source=parsed.cost_source,
            turns=parsed.turns, latency_s=round(info.wall_s, 3), error_type=error_type, run_id=self.run_id,
            attempt=round_index,
            extra={"stop_reason": info.stop_reason, "exit_code": info.exit_code, "tool_uses": parsed.tool_uses,
                   "transcript": str(transcript), "isolation": isolation, "refused_links": refused})
        append_call(self.call_log, call)
        fatal = "agent CLI authentication failed" if auth_error else None
        return AgentTurn(status=status, call_ids=(call.call_id,), fatal=fatal)

    def _refused(self, round_index: int, prompt: str, started_at: str, new_id: str | None,
                 isolation: Mapping[str, Any]) -> AgentTurn:
        """探针失败：不启动 agent，记一条 aborted 调用并停止循环（失败关闭）。"""
        call = AgentCallRecord(
            call_id=new_call_id(), component=COMPONENT, backend=self.backend.call_backend, model=self.backend.model,
            started_at=started_at, ended_at=utc_now(), status="aborted", prompt_sha256=sha256_text(prompt),
            session_id=new_id, error_type="isolation_probe", run_id=self.run_id, attempt=round_index,
            extra={"isolation": dict(isolation)})
        append_call(self.call_log, call)
        return AgentTurn(status="aborted", call_ids=(call.call_id,),
                         fatal=f"sandbox probe failed before the agent ran: {dict(isolation)}")


def refuse_links(workspace: Path) -> list[str]:
    """主机随后会读 `scene-layout.json`、写 `host/`：二者若被 agent 换成符号链接，主机就会读到/写到 guest 外的
    主机路径（例如把 `examples/scene/` 的布局当成本轮产物）。删掉这些链接并记录，本轮按缺文件处理。"""
    removed = []
    for name in (LAYOUT_FILE, HOST_DIR):
        path = workspace / name
        if path.is_symlink():
            path.unlink()
            removed.append(name)
    return removed


def check_with_tool(*, python: str, tools: Path, env_script: str | None, timeout_s: float,
                    tolerances: Mapping[str, Any]) -> RunCheck:
    """主机物理闸门：用 Isaac Lab 解释器跑 `tools/scene_assemble.py check`，读它写的 `check.json`。

    布局参数是本轮留存的 `attempts/<n>/scene-layout.json`（与 `plan.json.spec_sha256` 记录的字节相同），
    工作目录是同一 prepared 目录。0 = 通过，1 = 检查失败，其他退出码或缺失 `check.json` 是 CheckError。
    """
    def value(key: str) -> str:
        return f"{float(tolerances[key]):g}"

    def run(layout_path: Path, prep_dir: Path) -> CheckResult:
        argv = [python, str(tools / "scene_assemble.py"), "check", "--spec", str(layout_path),
                "--work", str(prep_dir), "--headless",
                "--settle-s", value("settle_s"), "--probe-s", value("probe_s"),
                "--drift-tol", value("drift_tol"), "--speed-tol", value("speed_tol"),
                "--joint-tol-active", value("joint_tol_active"),
                "--joint-tol-passive", value("joint_tol_passive"),
                "--penetration-tol", value("penetration_tol"),
                "--nconmax", str(int(tolerances["nconmax"])), "--njmax", str(int(tolerances["njmax"]))]
        if env_script:
            argv = ["bash", "-c", 'source "$0" || exit $?; shift; exec "$@"', env_script, *argv]
        check_path = prep_dir / "check.json"
        check_path.unlink(missing_ok=True)  # never read a previous round's evidence
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise CheckError(f"tools/scene_assemble.py check timed out after {timeout_s:g} s") from exc
        except OSError as exc:
            raise CheckError(f"could not start the check interpreter {python!r}: {exc}") from exc
        log = (completed.stdout or "") + (completed.stderr or "")
        (prep_dir / "check.log").write_text(log, encoding="utf-8")
        document = None
        if check_path.is_file():
            try:
                document = json.loads(check_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise CheckError(f"check.json is not valid JSON: {exc}") from exc
        if document is None or completed.returncode not in (0, 1):
            tail = log[-1500:]
            raise CheckError(f"tools/scene_assemble.py check exit {completed.returncode} without check.json; "
                             f"log tail:\n{tail}")
        diagnostics = tuple(check_diagnostics(document))
        if completed.returncode != 0 and not diagnostics:
            diagnostics = (f"check: tools/scene_assemble.py check exit {completed.returncode}",)
        return CheckResult(ran=True, ok=completed.returncode == 0, exit_code=completed.returncode,
                           summary=check_summary(document), diagnostics=diagnostics, path=check_path)

    return run


def prepare_with_tool(*, python: str, cache: Path, robot_package: Path, endpoint: str):
    """主机 prepare：缓存里缺的 SimReady 资产先 `tools/simready.py fetch`，再跑 `tools/scene_assemble.py prepare`
    子进程；任一步失败时把日志尾部放进 `PrepareError`。"""
    tools = repository_root() / "tools"

    def run(label: str, argv: list[str], log: Path) -> None:
        with open(log, "ab") as handle:
            result = subprocess.run(argv, stdout=handle, stderr=subprocess.STDOUT, check=False)
        if result.returncode != 0:
            tail = log.read_bytes()[-1500:].decode("utf-8", "replace")
            raise PrepareError(f"{label} exit {result.returncode}; log tail:\n{tail}")

    def prepare(layout_path: Path, prep_dir: Path) -> dict:
        prep_dir.mkdir(parents=True, exist_ok=True)
        log = prep_dir / "prepare.log"
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        missing = sorted({a["simready"] for a in layout["assets"]
                          if not (cache / a["simready"] / FETCH_MANIFEST).is_file()})
        if missing:
            run("simready fetch", [sys.executable, str(tools / "simready.py"), "--endpoint", endpoint, "fetch",
                                   *missing, "--dest", str(cache)], log)
        run("scene_assemble prepare", [python, str(tools / "scene_assemble.py"), "prepare", "--spec",
                                       str(layout_path), "--cache", str(cache), "--robot-package",
                                       str(robot_package), "--work", str(prep_dir)], log)
        return load_prepared(prep_dir)[2]

    return prepare


def descriptor_from_package(robot_package: Path):
    def load(layout: Mapping[str, Any], records: Mapping[str, Any]) -> dict:
        return robot_descriptor(robot_package, layout["robot"]["package_asset"])[0]

    return load


def agent_env(passthrough: list[str], endpoint: str) -> dict[str, str]:
    """agent 进程的环境：基本变量 + 配置列出的凭据变量（只按名字转发，从不写进文件或 argv）。"""
    keep = ["PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "TERM", *passthrough]
    env = {name: os.environ[name] for name in dict.fromkeys(keep) if name in os.environ}
    env["HF_ENDPOINT"] = endpoint
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--request", type=Path, required=True, help="datafarm.request/1 YAML")
    parser.add_argument("--work", type=Path, required=True, help="authoring run directory (resumable)")
    parser.add_argument("--cache", type=Path, required=True, help="SimReady fetch cache used by prepare")
    parser.add_argument("--robot-package", type=Path, required=True)
    parser.add_argument("--agent-config", type=Path, required=True, help="see configs/scene-author.template.json")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--rules", type=Path, default=repository_root() / "configs" / "scene-roles.json")
    parser.add_argument("--prepare-python", default=sys.executable,
                        help="interpreter with pxr + coacd for tools/scene_assemble.py prepare")
    parser.add_argument("--check-python", default=None,
                        help="Isaac Lab interpreter for tools/scene_assemble.py check (default $ISAAC_PYTHON, then "
                             "the config's check.python); without it the run refuses unless --kinematic-only")
    parser.add_argument("--check-timeout-s", type=float, default=None,
                        help="wall limit for the check subprocess (default the config's check.timeout_s)")
    parser.add_argument("--check-env-script", default=None,
                        help="bash script sourced before check (e.g. an Isaac Lab activation script)")
    parser.add_argument("--kinematic-only", action="store_true",
                        help="accept without the physics check; recorded in attempts.jsonl and the report")
    parser.add_argument("--endpoint", default=endpoint_from_env(), help="SimReady/Hugging Face endpoint for the agent")
    parser.add_argument("--call-log", type=Path, help="agent-call ledger (default WORK/agent-calls.jsonl)")
    args = parser.parse_args(argv)
    try:
        config, prices = load_config(args.agent_config)
        rules = load_role_rules(args.rules)
        backend = make_agent_backend(config["agent"], prices)
    except (ConfigError, OSError, ValueError, KeyError) as exc:
        print(f"scene_author: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    check_cfg = config["check"]
    check_timeout_s = args.check_timeout_s if args.check_timeout_s is not None else check_cfg["timeout_s"]
    check_env_script = args.check_env_script or check_cfg["env_script"]
    check_python = args.check_python or check_cfg["python"] or os.environ.get("ISAAC_PYTHON")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    try:
        sandbox = AuthoringSandbox(config["sandbox"], repo=repository_root(), work=work, cache=args.cache,
                                   robot_package=args.robot_package)
    except ConfigError as exc:  # 缺 sandbox 适配器包：点名缺的包，退出 2（不是 ImportError）
        print(f"scene_author: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except HarnessError as exc:  # 含 IsolationUnavailable：失败关闭
        print(f"scene_author: {type(exc).__name__}: {exc}. Install a sandbox backend (bwrap or proot; "
              f"set sandbox.proot or DATAFARM_PROOT_BIN) or opt out explicitly with \"sandbox\": "
              f"{{\"mode\": \"none\"}} in the agent config.", file=sys.stderr)
        return 2
    if check_timeout_s <= 0:
        print("scene_author: --check-timeout-s must be positive", file=sys.stderr)
        return 2
    if check_env_script and not Path(check_env_script).is_file():
        print(f"scene_author: check env script {check_env_script!r} does not exist", file=sys.stderr)
        return 2
    if not args.kinematic_only:
        resolved = resolve_check_interpreter(check_python)
        if resolved is None:
            print("scene_author: no check interpreter (--check-python, $ISAAC_PYTHON or the config's check.python); "
                  "the physics gate is mandatory - install Isaac Lab, point at its Python, or pass --kinematic-only "
                  "to accept without physics verification (recorded, never silent)", file=sys.stderr)
            return 2
        check_python = resolved
    context = build_context(ROLE)
    atomic_write_json(work / "agent-instructions.json", context["receipt"])
    sessions = AgentSessions(backend=backend, config=config, work=work,
                             call_log=args.call_log or work / "agent-calls.jsonl", run_id=work.name,
                             env=agent_env(list(config.get("env_passthrough") or []), args.endpoint),
                             sandbox=sandbox)
    result = author_layout(
        request_path=args.request, work=work, rules=rules, instructions=context["messages"][0]["content"],
        max_rounds=args.max_rounds, run_agent=sessions,
        prepare_assets=prepare_with_tool(python=args.prepare_python, cache=args.cache,
                                         robot_package=args.robot_package, endpoint=args.endpoint),
        load_descriptor=descriptor_from_package(args.robot_package),
        run_check=None if args.kinematic_only else check_with_tool(
            python=check_python, tools=repository_root() / "tools", env_script=check_env_script,
            timeout_s=check_timeout_s, tolerances=check_cfg),
        kinematic_only=args.kinematic_only,
        simready_command=f"{sandbox.simready_command} --endpoint {args.endpoint}",
        # 沙箱里仓库本体不可见：agent 只能读 WORK/sandbox/repo 下的快照，prompt 里点名它。
        docs_root=None if sandbox.mode == "none" else sandbox.snapshot)
    print(json.dumps({"accepted": result.accepted, "report": str(result.report_path),
                      "layout": None if result.layout_path is None else str(result.layout_path),
                      "rounds": result.report["rounds"], "stop_reason": result.report["stop_reason"],
                      "next_step": result.report["next_step"]}, indent=1, ensure_ascii=False))
    if sandbox.refusal is not None:  # 可见性探针发现泄漏：本轮没启动 agent，工具失败关闭
        print("scene_author: isolation probe failed; the agent was not started: "
              f"{json.dumps(sandbox.refusal, ensure_ascii=False)}", file=sys.stderr)
        return 2
    return 0 if result.accepted else 1

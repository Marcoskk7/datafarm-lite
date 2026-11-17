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
from datafarm.scene.authoring import HOST_DIR, LAYOUT_FILE, AgentTurn, PrepareError, author_layout
from datafarm.scene.conformance import load_role_rules

ROLE = "scene-author"
COMPONENT = "scene-author/session"
CONFIG_SCHEMA = "datafarm.scene-author/1"
CONFIG_KEYS = frozenset({"schema", "notes", "agent", "price_table", "wall_s_per_round", "max_budget_usd_per_round",
                         "env_passthrough", "sandbox"})


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
        simready_command=f"{sandbox.simready_command} --endpoint {args.endpoint}")
    print(json.dumps({"accepted": result.accepted, "report": str(result.report_path),
                      "layout": None if result.layout_path is None else str(result.layout_path),
                      "rounds": result.report["rounds"], "stop_reason": result.report["stop_reason"],
                      "next_step": result.report["next_step"]}, indent=1, ensure_ascii=False))
    if sandbox.refusal is not None:  # 可见性探针发现泄漏：本轮没启动 agent，工具失败关闭
        print("scene_author: isolation probe failed; the agent was not started: "
              f"{json.dumps(sandbox.refusal, ensure_ascii=False)}", file=sys.stderr)
        return 2
    return 0 if result.accepted else 1

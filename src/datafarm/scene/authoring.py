"""由 coding agent 编写 `scene-layout.json`，主机用确定性闸门逐轮判定（1b）。

每一轮：拼 prompt（角色指令 + request + 上一轮诊断）→ 运行 agent → 读 `workspace/scene-layout.json`
→ 资产集合变化时由主机 prepare → 符合性检查 + `run_plan` → 追加一条 attempt 记录。接受与否只由
主机判定：符合性 `ok` 且 `complete`（prepare 之后才有 AssetRecord），并且 `plan.ok`。agent 的自述不参与。

本模块是纯业务逻辑：agent 会话、资产 prepare、机器人描述符都由调用方注入（`entry/scene_author_cli.py`
把它们接到 agent harness 和 `tools/scene_assemble.py prepare` 上）。不运行 `check`（Isaac Lab，GPU），
接受的布局仍须在 GPU 主机上跑 `check`。
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datafarm.ir.common import IRError
from datafarm.kernel.io import append_jsonl, atomic_write_bytes, atomic_write_json, sha256_bytes, sha256_json, utc_now
from datafarm.request.schema import load_request
from datafarm.scene import layout as scene_layout
from datafarm.scene import planning
from datafarm.scene.conformance import check_conformance

REPORT_SCHEMA = "datafarm.scene-authoring-report/1"
ATTEMPT_SCHEMA = "datafarm.scene-authoring-attempt/1"
LAYOUT_FILE = "scene-layout.json"
REPORT_FILE = "scene-authoring-report.json"
ATTEMPTS_FILE = "attempts.jsonl"
HOST_DIR = "host"
HOST_FILES = ("asset-records.json", "plan.json", "last-attempt.json")
NOT_VERIFIED = (
    "check (Isaac Lab settle, penetration and placement probes) was not run: run "
    "tools/scene_assemble.py check on a GPU host before using this layout",
    "reachability is kinematic only: no collision-aware planning and no grasp feasibility",
)


class PrepareError(RuntimeError):
    """主机侧 prepare（拉取 + AssetRecord）失败；作为诊断回给 agent，不终止循环。"""


@dataclass(frozen=True)
class AgentTurn:
    """一轮 agent 会话的摘要。`status` 取 `datafarm.agent-call/1` 的状态；`fatal` 非空时停止循环。"""

    status: str
    call_ids: tuple[str, ...] = ()
    fatal: str | None = None


@dataclass(frozen=True)
class AuthoringResult:
    accepted: bool
    layout_path: Path | None
    report_path: Path
    report: dict[str, Any]


RunAgent = Callable[[int, str, Path], AgentTurn]
PrepareAssets = Callable[[Path, Path], Mapping[str, Mapping[str, Any]]]
LoadDescriptor = Callable[[Mapping[str, Any], Mapping[str, Mapping[str, Any]]], Mapping[str, Any]]


def asset_set_key(layout: Mapping[str, Any]) -> str:
    """prepare 的输入摘要：`assets[]` 中 prepare 读取的字段 + 机器人 `asset_id`/`package_asset`。

    布局形状不对（缺 `assets`/`robot` 或字段）时抛 `ValueError`，调用方把它当诊断。
    """
    assets = layout.get("assets")
    robot = layout.get("robot")
    if not isinstance(assets, list) or not all(isinstance(a, Mapping) for a in assets):
        raise ValueError("layout 'assets' must be a list of objects")
    if not isinstance(robot, Mapping):
        raise ValueError("layout 'robot' must be an object")
    entries = []
    for index, asset in enumerate(assets):
        missing = [key for key in ("asset_id", "role", "simready", "body") if not isinstance(asset.get(key), str)]
        if missing:
            raise ValueError(f"assets[{index}] is missing string fields {missing}")
        if asset["body"] == "articulation" and not isinstance(asset.get("anchor_body"), str):
            raise ValueError(f"assets[{index}] ({asset['asset_id']}): body 'articulation' needs anchor_body")
        entries.append({key: asset.get(key) for key in ("asset_id", "role", "simready", "body", "anchor_body")})
    for key in ("asset_id", "package_asset"):
        if not isinstance(robot.get(key), str):
            raise ValueError(f"layout robot.{key} must be a string")
    return sha256_json({"assets": sorted(entries, key=lambda e: e["asset_id"]),
                        "robot": {"asset_id": robot["asset_id"], "package_asset": robot["package_asset"]}})


def _read_layout(path: Path) -> tuple[bytes | None, dict[str, Any] | None, list[str]]:
    """读 agent 写的布局；缺失、非 JSON、非对象、schema 不对都是诊断，不抛异常。"""
    if not path.is_file():
        return None, None, [f"{LAYOUT_FILE} was not found in the workspace; write the layout there"]
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        return raw, None, [f"{LAYOUT_FILE} is not valid JSON: {exc}"]
    if not isinstance(document, dict):
        return raw, None, [f"{LAYOUT_FILE} must be a JSON object"]
    if document.get("schema") != scene_layout.LAYOUT_SCHEMA:
        return raw, None, [f"{LAYOUT_FILE} schema must be {scene_layout.LAYOUT_SCHEMA!r}, "
                           f"got {document.get('schema')!r}"]
    return raw, document, []


def round_prompt(*, instructions: str, request_text: str, round_index: int, max_rounds: int,
                 workspace: Path, simready_command: str, previous: Mapping[str, Any] | None) -> str:
    """一轮的完整 prompt。上一轮的诊断原样逐行附上；第一轮没有诊断。"""
    if previous is None:
        feedback = "This is the first round: there are no host diagnostics yet."
    else:
        lines = previous.get("diagnostics") or ["(no diagnostics recorded)"]
        feedback = (f"Round {previous['round']} was NOT accepted. Host diagnostics, one per line:\n"
                    + "\n".join(f"- {line}" for line in lines)
                    + f"\nYour previous file is still at {LAYOUT_FILE}; edit it to fix every line above.")
    return (
        f"{instructions}\n\n"
        f"--- SCENE AUTHORING ROUND {round_index} of {max_rounds} ---\n"
        f"Workspace (your working directory): {workspace}\n"
        f"Write the layout to {workspace / LAYOUT_FILE}. The host reads only that file after you stop.\n"
        f"Search and probe SimReady assets with: {simready_command} search ... / probe ...\n"
        f"Host-written files you may read (never edit): {HOST_DIR}/asset-records.json (AssetRecords of the "
        f"last prepared asset set), {HOST_DIR}/plan.json (last plan: resolved poses, footprint, reachability), "
        f"{HOST_DIR}/last-attempt.json (last host verdict). They are absent until the host has produced them.\n"
        f"The request is also at {workspace / 'request.yaml'}.\n\n"
        f"<request>\n{request_text}</request>\n\n"
        f"{feedback}\n"
    )


def _load_attempts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_host_files(host: Path, *, records: Mapping[str, Any] | None, plan: Mapping[str, Any] | None,
                      attempt: Mapping[str, Any]) -> None:
    host.mkdir(parents=True, exist_ok=True)
    for name in HOST_FILES:
        (host / name).unlink(missing_ok=True)
    if records is not None:
        atomic_write_json(host / "asset-records.json", dict(records))
    if plan is not None:
        atomic_write_json(host / "plan.json", dict(plan))
    atomic_write_json(host / "last-attempt.json", dict(attempt))


def author_layout(*, request_path: str | Path, work: str | Path, rules: Mapping[str, Any], instructions: str,
                  max_rounds: int, run_agent: RunAgent, prepare_assets: PrepareAssets,
                  load_descriptor: LoadDescriptor, simready_command: str = "python tools/simready.py"
                  ) -> AuthoringResult:
    """跑到接受或 `max_rounds` 轮为止；已有 `attempts.jsonl` 时从下一轮续跑，已接受则直接返回。

    `run_agent(round, prompt, workspace)` 运行一次 agent 会话；`prepare_assets(layout_path, prep_dir)`
    为布局的资产集合产出 AssetRecord（按 asset_id，含机器人），失败抛 `PrepareError`；
    `load_descriptor(layout, records)` 返回机器人描述符（robot-descriptor/2）。
    """
    if max_rounds < 1:
        raise ValueError("max_rounds must be >= 1")
    request_path = Path(request_path)
    work = Path(work)
    request_bytes = request_path.read_bytes()
    request = load_request(request_path)
    workspace = work / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(workspace / "request.yaml", request_bytes)
    attempts_path = work / ATTEMPTS_FILE
    attempts = _load_attempts(attempts_path)
    request_sha256 = sha256_bytes(request_bytes)
    foreign = [a["round"] for a in attempts if a.get("request_sha256") != request_sha256]
    if foreign:
        raise ValueError(f"{attempts_path} holds rounds {foreign} for a different request; use a new --work")
    prepared: dict[str, Mapping[str, Mapping[str, Any]]] = {}
    stop_reason = "max_rounds"

    if attempts and attempts[-1]["accepted"]:
        stop_reason = "accepted"
    else:
        for round_index in range(len(attempts) + 1, max_rounds + 1):
            attempt, fatal = _run_round(
                round_index=round_index, max_rounds=max_rounds, request=request,
                request_text=request_bytes.decode("utf-8"), work=work, workspace=workspace, rules=rules,
                instructions=instructions, simready_command=simready_command,
                previous=attempts[-1] if attempts else None, prepared=prepared, run_agent=run_agent,
                prepare_assets=prepare_assets, load_descriptor=load_descriptor)
            attempt["request_sha256"] = request_sha256
            append_jsonl(attempts_path, attempt)
            attempts.append(attempt)
            if attempt["accepted"]:
                stop_reason = "accepted"
                break
            if fatal:
                stop_reason = "agent_fatal"
                break

    accepted = bool(attempts) and attempts[-1]["accepted"]
    layout_path: Path | None = None
    if accepted:
        layout_path = work / LAYOUT_FILE
        shutil.copyfile(work / attempts[-1]["layout"]["kept"], layout_path)
    report = _report(request_path=request_path, request_bytes=request_bytes, request_id=request.request_id,
                     work=work, attempts=attempts, max_rounds=max_rounds, stop_reason=stop_reason,
                     layout_path=layout_path)
    report_path = work / REPORT_FILE
    atomic_write_json(report_path, report)
    return AuthoringResult(accepted=accepted, layout_path=layout_path, report_path=report_path, report=report)


def _run_round(*, round_index: int, max_rounds: int, request: Any, request_text: str, work: Path,
               workspace: Path, rules: Mapping[str, Any], instructions: str, simready_command: str,
               previous: Mapping[str, Any] | None, prepared: dict[str, Mapping[str, Mapping[str, Any]]],
               run_agent: RunAgent, prepare_assets: PrepareAssets, load_descriptor: LoadDescriptor
               ) -> tuple[dict[str, Any], str | None]:
    attempt_dir = work / "attempts" / f"{round_index:03d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    prompt = round_prompt(instructions=instructions, request_text=request_text, round_index=round_index,
                          max_rounds=max_rounds, workspace=workspace, simready_command=simready_command,
                          previous=previous)
    atomic_write_bytes(attempt_dir / "prompt.txt", prompt.encode("utf-8"))
    started_at = utc_now()
    turn = run_agent(round_index, prompt, workspace)
    agent_ended_at = utc_now()

    diagnostics: list[str] = []
    if turn.status != "ok":
        diagnostics.append(f"agent session ended with status '{turn.status}'")
    raw, layout, problems = _read_layout(workspace / LAYOUT_FILE)
    diagnostics.extend(problems)
    layout_info: dict[str, Any] = {"present": raw is not None, "valid": layout is not None,
                                   "sha256": None, "kept": None, "changed": None}
    if raw is not None:
        kept = attempt_dir / LAYOUT_FILE
        atomic_write_bytes(kept, raw)
        layout_info.update(sha256=sha256_bytes(raw), kept=kept.relative_to(work).as_posix())
        previous_sha = ((previous or {}).get("layout") or {}).get("sha256")
        layout_info["changed"] = previous is None or layout_info["sha256"] != previous_sha

    prepare_info: dict[str, Any] = {"asset_set_sha256": None, "ran": False, "reused": False, "ok": False,
                                    "dir": None, "error": None}
    records: Mapping[str, Mapping[str, Any]] | None = None
    if layout is not None:
        try:
            key = asset_set_key(layout)
        except ValueError as exc:
            key = None
            prepare_info["error"] = str(exc)
            diagnostics.append(f"prepare not run: {exc}")
        if key is not None:
            prep_dir = work / "prepared" / key[:16]
            prepare_info.update(asset_set_sha256=key, dir=prep_dir.relative_to(work).as_posix())
            if key in prepared:
                records, prepare_info["reused"] = prepared[key], True
            else:
                prepare_info["ran"] = True
                try:
                    records = prepared[key] = dict(prepare_assets(attempt_dir / LAYOUT_FILE, prep_dir))
                except PrepareError as exc:
                    prepare_info["error"] = str(exc)
                    diagnostics.append(f"host prepare failed for this asset set: {exc}")
            prepare_info["ok"] = records is not None

    conformance = None
    if layout is not None:
        try:
            report = check_conformance(request, layout, records, rules)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            conformance = {"ok": False, "complete": False, "error": f"{type(exc).__name__}: {exc}",
                           "problems": [], "skipped": []}
            diagnostics.append(f"conformance could not read the layout: {conformance['error']}")
        else:
            conformance = {"ok": report.ok, "complete": report.complete, "problems": list(report.problems),
                           "skipped": list(report.skipped)}
            diagnostics.extend(f"conformance: {line}" for line in report.problems)
            if records is None:
                diagnostics.extend(f"conformance skipped: {line}" for line in report.skipped)
    plan_info: dict[str, Any] = {"ran": False, "ok": False, "diagnostics": [], "error": None}
    plan = None
    if layout is not None and records is not None:
        plan_info["ran"] = True
        try:
            descriptor = load_descriptor(layout, records)
            plan = planning.run_plan(layout, records, descriptor, spec_sha256=layout_info["sha256"])
        except (AttributeError, IRError, KeyError, TypeError, ValueError) as exc:
            plan_info["error"] = f"{type(exc).__name__}: {exc}"
            diagnostics.append(f"plan failed: {plan_info['error']}")
        else:
            plan_info.update(ok=bool(plan["ok"]), diagnostics=planning.plan_diagnostics(plan))
            diagnostics.extend(f"plan: {line}" for line in plan_info["diagnostics"])
            atomic_write_json(attempt_dir / "plan.json", plan)
            # 与 `scene_assemble.py plan` 写在同一 prepare 目录：`check --work <dir>` 可直接使用。
            atomic_write_json(work / prepare_info["dir"] / "plan.json", plan)
    elif layout is not None:
        diagnostics.append("plan not run: no asset records for this layout")

    accepted = bool(conformance and conformance["ok"] and conformance["complete"] and plan_info["ok"])
    attempt = {
        "schema": ATTEMPT_SCHEMA, "round": round_index, "started_at": started_at,
        "agent_ended_at": agent_ended_at, "ended_at": utc_now(),
        "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
        "agent": {"status": turn.status, "call_ids": list(turn.call_ids), "fatal": turn.fatal},
        "layout": layout_info, "prepare": prepare_info, "conformance": conformance, "plan": plan_info,
        "diagnostics": diagnostics, "accepted": accepted,
    }
    _write_host_files(workspace / HOST_DIR, records=records, plan=plan, attempt=attempt)
    return attempt, turn.fatal


def _report(*, request_path: Path, request_bytes: bytes, request_id: str, work: Path,
            attempts: list[dict[str, Any]], max_rounds: int, stop_reason: str,
            layout_path: Path | None) -> dict[str, Any]:
    last = attempts[-1] if attempts else None
    accepted = bool(last and last["accepted"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "request": {"path": str(request_path), "sha256": sha256_bytes(request_bytes), "request_id": request_id},
        "accepted": accepted,
        "stop_reason": stop_reason,
        "rounds": len(attempts),
        "max_rounds": max_rounds,
        "attempts_log": ATTEMPTS_FILE,
        "agent_call_ids": [cid for a in attempts for cid in a["agent"]["call_ids"]],
        "accepted_layout": None,
        "final": None if last is None else {key: last[key] for key in
                                             ("round", "layout", "prepare", "conformance", "plan", "diagnostics")},
        "verified": ["request conformance with AssetRecords (complete)", "plan: footprint and kinematic reachability"],
        "not_verified": list(NOT_VERIFIED),
        "next_step": None,
    }
    if accepted and layout_path is not None:
        prep_dir = last["prepare"]["dir"]
        report["accepted_layout"] = {"path": layout_path.relative_to(work).as_posix(),
                                     "sha256": last["layout"]["sha256"], "round": last["round"],
                                     "prepared_dir": prep_dir}
        report["next_step"] = (f"on a GPU host: python tools/scene_assemble.py check --spec {layout_path} "
                               f"--work {work / prep_dir} --headless")
    else:
        report["next_step"] = "not accepted: inspect the final diagnostics and attempts, then rerun with more rounds"
    return report

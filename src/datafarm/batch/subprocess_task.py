"""由 JSON 任务配置构建的、由 subprocess 支撑的 :class:`~datafarm.batch.supervisor.BatchTask`。

供 ``tools/supervise_batch.py`` 使用，使每任务 supervisor 可以变成一个配置文件。一次 attempt
就是一条命令，通过 ``batch_exec.py`` 在其独立 session、独立目录
``<work_dir>/attempts/<launch_id>/`` 中运行（包含 ``log.txt``、``attempt.json``、``handle.json``、
``started.json``、``exit.json`` 以及命令自身写入的任何内容）。

配置（JSON，schema ``datafarm.batch-task-config/1.0.0``）：:

    {
      "schema": "datafarm.batch-task-config/1.0.0",
      "batch_id": "my-task-seedance100",
      "items": ["000", "001"],                  # 或 "items_file": "items.json"
      "work_dir": "batch",                       # 相对路径相对配置文件所在目录解析
      "launch": {"argv": ["python", "render.py", "--index", "{item}", "--seed", "{seed}",
                          "--out", "{attempt_dir}"], "cwd": ".", "env": {"KEY": "value"}},
      "result": {"path": "{attempt_dir}/result.json", "required_keys": ["verdict"],
                 "match": {"attempt": "{attempt}"},
                 "incomplete_values": {"status": ["generated_needs_semantic_review"]}},
      "verify": {"result_status": {"key": "status", "pass": ["admitted"],
                                   "reject": {"rejected_seam": "seam"}, "final": []}},
      "crash_classes": [{"pattern": "bounded upload attempts exhausted", "kind": "transient"},
                        {"pattern": "503 Service Unavailable", "kind": "provider_down"}],
      "lease_probe": {"argv": ["..."], "timeout_s": 30},
      "unresolved_submission_marker": "remote-submit-pending",
      "provider_probe": {"argv": ["..."], "timeout_s": 30},
      "cost": {"per_finished_attempt": 1.0},
      "seed": {"mode": "linear", "base": 0, "item_stride": 1, "attempt_stride": 100000},
      "ledger": {"column": "render/generate"},
      "policy": {"max_attempts_per_item": 8}
    }

模板字段：``{item} {item_index} {attempt} {seed} {launch_id} {attempt_dir} {work_dir}
{result_path}`` 以及来自 ``item_vars``（或 ``item_vars_file``）的逐条目变量 ``{vars[name]}``，
例如 per-variant prompt；字面大括号写作 ``{{`` 和 ``}}``。``verify`` 要么是
``result_status``（映射 result JSON 的某个 status 字段），要么是 ``argv``（一个 verifier 命令，
其 stdout 上最后一行 JSON 为 ``{"decision": "pass|reject|abstain", "reason": ..., "gate": ...}``）。
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from datafarm.runtime import expand, load_bindings
from datafarm.batch.supervisor import (
    CRASH_KINDS,
    BatchPolicy,
    BatchTask,
    LaunchDeferred,
    LaunchNotStarted,
    LaunchOutcomeUnknown,
    Poll,
    Verdict,
    write_json_atomic,
)
from datafarm.common import read_json_or_none

TASK_CONFIG_SCHEMA = "datafarm.batch-task-config/1.0.0"
TEMPLATE_FIELDS = frozenset({"item", "item_index", "attempt", "seed", "launch_id", "attempt_dir",
                             "work_dir", "result_path", "vars"})
TOP_LEVEL_KEYS = frozenset({"schema", "batch_id", "description", "items", "items_file",
                            "item_vars", "item_vars_file", "work_dir", "launch", "result",
                            "verify", "crash_classes", "lease_probe", "provider_probe", "cost",
                            "seed", "ledger", "policy", "unresolved_submission_marker",
                            "upstream_batch_state"})
PROVIDER_DOWN_EXIT = 75  # EX_TEMPFAIL，与 EmbodiedSWE 的 MODEL_DOWN 约定一致
LOG_TAIL_BYTES = 16384


class TaskConfigError(ValueError):
    pass


def _fields(template: str) -> set[str]:
    """模板字段的根名称（``{vars[prompt]}`` -> ``vars``）。"""

    try:
        names = [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]
    except ValueError as exc:
        raise TaskConfigError(f"bad template {template!r}: {exc}") from exc
    return {re.split(r"[.\[]", name, maxsplit=1)[0] for name in names}


def _check_templates(values: Sequence[str], where: str, allowed: frozenset[str]) -> None:
    for value in values:
        if not isinstance(value, str):
            raise TaskConfigError(f"{where} entries must be strings, got {value!r}")
        unknown = _fields(value) - allowed
        if unknown:
            raise TaskConfigError(f"{where}: unknown template fields {sorted(unknown)} in "
                                  f"{value!r}; allowed {sorted(allowed)}")


def _command_spec(spec: Any, where: str, allowed: frozenset[str] = TEMPLATE_FIELDS) -> dict:
    if not isinstance(spec, Mapping) or not isinstance(spec.get("argv"), list) or not spec["argv"]:
        raise TaskConfigError(f"{where} needs a non-empty 'argv' list")
    unknown = set(spec) - {"argv", "cwd", "env", "timeout_s"}
    if unknown:
        raise TaskConfigError(f"{where}: unknown keys {sorted(unknown)}")
    timeout = spec.get("timeout_s")
    if timeout is not None and (isinstance(timeout, bool) or
                                not isinstance(timeout, (int, float)) or
                                not math.isfinite(timeout) or timeout <= 0):
        raise TaskConfigError(f"{where}.timeout_s must be a finite positive number")
    _check_templates(spec["argv"], f"{where}.argv", allowed)
    if spec.get("cwd") is not None:
        _check_templates([spec["cwd"]], f"{where}.cwd", allowed)
    env = spec.get("env") or {}
    if not isinstance(env, Mapping):
        raise TaskConfigError(f"{where}.env must be an object")
    _check_templates(list(env.values()), f"{where}.env", allowed)
    return dict(spec)


def load_task_config(path: str | Path) -> tuple[dict[str, Any], Path, list[str]]:
    """加载并校验任务配置；返回 (config, base_dir, items)。"""

    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise TaskConfigError("task config must be a JSON object")
    config = expand(config, load_bindings())
    if config.get("schema") != TASK_CONFIG_SCHEMA:
        raise TaskConfigError(f"task config schema must be {TASK_CONFIG_SCHEMA!r}")
    unknown = set(config) - TOP_LEVEL_KEYS
    if unknown:
        raise TaskConfigError(f"unknown task config keys {sorted(unknown)}")
    base_dir = path.parent
    if ("items" in config) == ("items_file" in config):
        raise TaskConfigError("give exactly one of 'items' or 'items_file'")
    if "items" in config:
        raw_items = config["items"]
    else:
        loaded = json.loads((base_dir / config["items_file"]).read_text(encoding="utf-8"))
        raw_items = loaded.get("items") if isinstance(loaded, dict) else loaded
    if not isinstance(raw_items, list) or not raw_items:
        raise TaskConfigError("items must be a non-empty list")
    items = []
    for value in raw_items:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TaskConfigError(f"items must be strings or integers, got {value!r}")
        items.append(str(value))
    if "item_vars" in config and "item_vars_file" in config:
        raise TaskConfigError("give at most one of 'item_vars' or 'item_vars_file'")
    item_vars = config.get("item_vars")
    if "item_vars_file" in config:
        item_vars = json.loads((base_dir / config["item_vars_file"]).read_text(encoding="utf-8"))
    if item_vars is not None:
        if not isinstance(item_vars, dict) or not all(isinstance(v, dict)
                                                      for v in item_vars.values()):
            raise TaskConfigError("item_vars maps each item id to an object of variables")
        missing = [item for item in items if item not in item_vars]
        if missing:
            raise TaskConfigError(f"item_vars lacks {len(missing)} item(s), e.g. {missing[:3]}")
        config = {**config, "item_vars": item_vars}
    if not isinstance(config.get("work_dir"), str):
        raise TaskConfigError("work_dir (string) is required")
    marker = config.get("unresolved_submission_marker")
    if marker is not None and (not isinstance(marker, str) or not marker or
                               Path(marker).name != marker or marker in (".", "..")):
        raise TaskConfigError("unresolved_submission_marker must be a filename in attempt_dir")
    _command_spec(config.get("launch"), "launch")
    upstream = config.get("upstream_batch_state")
    if upstream is not None:
        if not isinstance(upstream, str):
            raise TaskConfigError("upstream_batch_state must be a JSON state path")
        _check_templates([upstream], "upstream_batch_state", TEMPLATE_FIELDS)
    result = config.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("path"), str):
        raise TaskConfigError("result.path is required")
    unknown = set(result) - {"path", "required_keys", "match", "incomplete_values"}
    if unknown:
        raise TaskConfigError(f"result: unknown keys {sorted(unknown)}")
    _check_templates([result["path"]], "result.path", TEMPLATE_FIELDS - {"result_path"})
    _check_templates(list((result.get("match") or {}).values()), "result.match", TEMPLATE_FIELDS)
    incomplete = result.get("incomplete_values") or {}
    if not isinstance(incomplete, Mapping) or not all(
            isinstance(values, list) for values in incomplete.values()):
        raise TaskConfigError("result.incomplete_values maps a key to a list of values")
    verify = config.get("verify")
    if not isinstance(verify, Mapping) or len(set(verify) & {"argv", "result_status"}) != 1:
        raise TaskConfigError("verify needs exactly one of 'argv' or 'result_status'")
    if "argv" in verify:
        _command_spec(verify, "verify")
    else:
        status = verify["result_status"]
        if not isinstance(status, Mapping) or not isinstance(status.get("key"), str):
            raise TaskConfigError("verify.result_status.key is required")
        unknown = set(status) - {"key", "pass", "reject", "final"}
        if unknown:
            raise TaskConfigError(f"verify.result_status: unknown keys {sorted(unknown)}")
    for entry in config.get("crash_classes") or []:
        if not isinstance(entry, Mapping) or entry.get("kind") not in CRASH_KINDS:
            raise TaskConfigError(f"crash_classes entries need a pattern and kind in {CRASH_KINDS}")
        re.compile(entry["pattern"])
    for name in ("lease_probe", "provider_probe"):
        if config.get(name) is not None:
            _command_spec(config[name], name)
    cost = config.get("cost")
    if cost is not None and (not isinstance(cost, Mapping) or len(
            set(cost) & {"per_finished_attempt", "result_key"}) != 1 or
            set(cost) - {"per_finished_attempt", "result_key"}):
        raise TaskConfigError("cost needs exactly one of per_finished_attempt or result_key")
    if cost is not None and "per_finished_attempt" in cost:
        value = cost["per_finished_attempt"]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0):
            raise TaskConfigError("cost.per_finished_attempt must be finite and non-negative")
    if cost is not None and "result_key" in cost and (not isinstance(cost["result_key"], str)
                                                       or not cost["result_key"]):
        raise TaskConfigError("cost.result_key must be a non-empty string")
    seed = config.get("seed") or {"mode": "hash"}
    if seed.get("mode") not in ("hash", "linear"):
        raise TaskConfigError("seed.mode must be 'hash' or 'linear'")
    if seed["mode"] == "linear" and set(seed) - {"mode", "base", "item_stride", "attempt_stride",
                                                  "modulus"}:
        raise TaskConfigError("unknown seed keys")
    ledger = config.get("ledger")
    if ledger is not None and (not isinstance(ledger, Mapping) or set(ledger) - {"column"}):
        raise TaskConfigError("ledger accepts only {'column': ...}")
    BatchPolicy.from_dict(config.get("policy"))  # 对错误的 policy key 尽早失败
    return config, base_dir, items


def _start_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return int(text.rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError):
        return None


def _proc_state(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
        return text.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def pid_alive(pid: int | None, start_ticks: int | None = None) -> bool | None:
    """已知时返回 True/False；进程无法探测时返回 None。"""

    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return None
    if start_ticks is not None:
        current = _start_ticks(int(pid))
        if current is not None and current != start_ticks:
            return False  # 该 pid 已被另一个进程复用
    if _proc_state(int(pid)) == "Z":
        return False
    return True


def _log_tail(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _last_error_line(tail: str) -> str:
    lines = [line.strip() for line in tail.replace("\r", "\n").splitlines() if line.strip()]
    for line in reversed(lines):
        if "Error" in line or "Exception" in line or "error" in line:
            return line[:300]
    return lines[-1][:300] if lines else "no output"


class SubprocessTask:
    """以分离子进程的方式启动、轮询、校验和调和 attempts。"""

    def __init__(self, config: Mapping[str, Any], *, base_dir: str | Path, items: Sequence[str],
                 host: str | None = None, python: str | None = None) -> None:
        self.config = config
        self.base_dir = Path(base_dir)
        self.work_dir = (self.base_dir / config["work_dir"]).resolve()
        self.attempts_dir = self.work_dir / "attempts"
        self.items = list(items)
        self.index = {item: position for position, item in enumerate(self.items)}
        self.host = host or socket.gethostname()
        self.python = python or sys.executable
        self.exec_path = Path(__file__).resolve().with_name("batch_exec.py")
        self.crash_classes = [(re.compile(entry["pattern"]), entry["kind"])
                              for entry in config.get("crash_classes") or []]
        self._children: dict[str, subprocess.Popen] = {}

    # -- BatchTask 接线 ----------------------------------------------------------------------
    def task(self) -> BatchTask:
        return BatchTask(
            launch=self.launch, poll=self.poll, verify=self.verify, reconcile=self.reconcile,
            lease_probe=self.lease_probe if self.config.get("lease_probe") else None,
            provider_probe=self.provider_probe if self.config.get("provider_probe") else None,
            cost=self.cost if self.config.get("cost") else None,
            seed_for=self.linear_seed_for() if (self.config.get("seed") or {}).get("mode")
            == "linear" else None,
        )

    def linear_seed_for(self) -> Callable[[str, int], int]:
        spec = self.config["seed"]
        base = int(spec.get("base", 0))
        item_stride = int(spec.get("item_stride", 1))
        attempt_stride = int(spec.get("attempt_stride", 100_000))
        modulus = int(spec.get("modulus", 2_147_483_647))

        def seed_for(item: str, attempt: int) -> int:
            number = int(item) if item.lstrip("-").isdigit() else self.index[item]
            return (base + number * item_stride + (attempt - 1) * attempt_stride) % modulus

        return seed_for

    # -- 模板 --------------------------------------------------------------------------------
    def attempt_dir(self, launch_id: str) -> Path:
        return self.attempts_dir / launch_id

    def _unresolved_submission(self, handle: Mapping[str, Any]) -> bool:
        marker = self.config.get("unresolved_submission_marker")
        return bool(marker and (Path(handle["attempt_dir"]) / marker).exists())

    def values(self, item: str, attempt: int, seed: int, launch_id: str) -> dict[str, Any]:
        values = {"item": item, "item_index": self.index[item], "attempt": attempt, "seed": seed,
                  "launch_id": launch_id, "attempt_dir": str(self.attempt_dir(launch_id)),
                  "work_dir": str(self.work_dir),
                  "vars": dict((self.config.get("item_vars") or {}).get(item, {}))}
        values["result_path"] = self._path(self.config["result"]["path"].format_map(values))
        return values

    def _path(self, value: str) -> str:
        path = Path(value)
        return str(path if path.is_absolute() else (self.base_dir / path).resolve())

    def command(self, spec: Mapping[str, Any], values: Mapping[str, Any]) -> tuple[list[str],
                                                                                     str, dict]:
        argv = [str(part).format_map(values) for part in spec["argv"]]
        cwd = self._path(spec["cwd"].format_map(values)) if spec.get("cwd") else str(self.base_dir)
        env = dict(os.environ)
        env.update({key: str(value).format_map(values)
                    for key, value in (spec.get("env") or {}).items()})
        return argv, cwd, env

    def _handle(self, values: Mapping[str, Any], pid: int | None,
                start_ticks: int | None) -> dict[str, Any]:
        return {"launch_id": values["launch_id"], "attempt_dir": values["attempt_dir"],
                "pid": pid, "start_ticks": start_ticks, "host": self.host,
                "result_path": values["result_path"], "values": dict(values)}

    # -- 启动 / 调和 -------------------------------------------------------------------------
    def launch(self, item: str, attempt: int, seed: int, *, launch_id: str) -> dict[str, Any]:
        values = self.values(item, attempt, seed, launch_id)
        directory = Path(values["attempt_dir"])
        if directory.exists() and any(directory.iterdir()):
            raise LaunchOutcomeUnknown(f"attempt directory already populated: {directory}")
        upstream = self.config.get("upstream_batch_state")
        if upstream:
            # 前置条件等待仅限本条目，不消耗 attempt，
            # 且不得触发 provider 级熔断。
            state = read_json_or_none(Path(self._path(upstream.format_map(values))))
            slot = (state or {}).get("item_state", {}).get(item, {})
            if slot.get("state") != "admitted":
                raise LaunchDeferred(f"upstream item {item} not admitted")
        argv, cwd, env = self.command(self.config["launch"], values)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            write_json_atomic(directory / "attempt.json", {
                "item": item, "attempt": attempt, "seed": seed, "launch_id": launch_id,
                "argv": argv, "cwd": cwd, "host": self.host, "created_at": time.time()})
            log = open(directory / "log.txt", "ab")
        except OSError as exc:
            raise LaunchNotStarted(f"cannot prepare {directory}: {exc}") from exc
        try:
            timeout = self.config["launch"].get("timeout_s")
            wrapper_argv = [self.python, str(self.exec_path), str(directory)]
            if timeout is not None:
                wrapper_argv += ["--timeout-s", str(timeout)]
            wrapper_argv += ["--", *argv]
            process = subprocess.Popen(
                wrapper_argv, cwd=cwd, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        except OSError as exc:
            raise LaunchNotStarted(f"cannot start attempt wrapper: {exc}") from exc
        finally:
            log.close()
        self._children[launch_id] = process
        handle = self._handle(values, process.pid, _start_ticks(process.pid))
        write_json_atomic(directory / "handle.json", handle)
        return handle

    def reconcile(self, item: str, attempt: int, seed: int, launch_id: str) -> Any:
        values = self.values(item, attempt, seed, launch_id)
        directory = Path(values["attempt_dir"])
        if not directory.exists():
            return None
        handle = read_json_or_none(directory / "handle.json")
        if isinstance(handle, dict):
            return handle
        started = read_json_or_none(directory / "started.json")
        if isinstance(started, dict) or (directory / "exit.json").exists():
            pid = started.get("pid") if isinstance(started, dict) else None
            return self._handle(values, pid, None)
        return "unknown"

    # -- 轮询 --------------------------------------------------------------------------------
    def _classify(self, tail: str) -> str:
        for pattern, kind in self.crash_classes:
            if pattern.search(tail):
                return kind
        return "transient"

    def _check_result(self, handle: Mapping[str, Any]) -> tuple[str | None, Any]:
        path = Path(handle["result_path"])
        if not path.is_file():
            return f"no result file at {path}", None
        spec = self.config["result"]
        data = read_json_or_none(path) if path.suffix == ".json" else None
        if path.suffix == ".json" and not isinstance(data, dict):
            return f"result {path} is not a JSON object", None
        missing = [key for key in spec.get("required_keys") or [] if key not in (data or {})]
        if missing:
            return f"incomplete result (missing {missing})", None
        for key, values in (spec.get("incomplete_values") or {}).items():
            if (data or {}).get(key) in values:
                return f"incomplete result ({key}={(data or {}).get(key)!r} is provisional)", None
        for key, template in (spec.get("match") or {}).items():
            expected = str(template).format_map(handle["values"])
            actual = (data or {}).get(key)
            if str(actual) != expected:
                return f"stale result ({key}={actual!r}, expected {expected!r})", None
        return None, {"path": str(path), "data": data, "values": handle["values"]}

    def poll(self, handle: Mapping[str, Any]) -> Poll:
        child = self._children.get(handle["launch_id"])
        if child is not None and child.poll() is not None:
            self._children.pop(handle["launch_id"], None)
        directory = Path(handle["attempt_dir"])
        exit_record = read_json_or_none(directory / "exit.json")
        if self._unresolved_submission(handle) and exit_record is not None:
            return Poll("unknown", detail="remote submission outcome unresolved; "
                                          "reconcile marker and provider before retry")
        if isinstance(exit_record, dict) and exit_record.get("timed_out") is True:
            return Poll("unknown", detail=f"attempt exceeded {exit_record.get('timeout_s')}s; "
                                          "child was stopped, remote effects need reconciliation")
        if exit_record is None:
            if handle.get("host") not in (None, self.host):
                return Poll("unknown", detail=f"attempt runs on {handle['host']}; cannot probe "
                                              f"from {self.host}")
            alive = pid_alive(handle.get("pid"), handle.get("start_ticks"))
            if alive:
                return Poll("running")
            exit_record = read_json_or_none(directory / "exit.json")  # 它可能刚刚被写入
            if exit_record is None:
                if alive is None:
                    return Poll("unknown", detail="attempt process cannot be probed")
                if self._unresolved_submission(handle):
                    return Poll("unknown", detail="attempt process gone with unresolved "
                                                  "remote submission marker")
                tail = _log_tail(directory / "log.txt")
                return Poll("crashed", kind=self._classify(tail),
                            detail=f"attempt process gone without an exit record: "
                                   f"{_last_error_line(tail)}")
        if self._unresolved_submission(handle):
            return Poll("unknown", detail="remote submission outcome unresolved; "
                                          "reconcile marker and provider before retry")
        code = int(exit_record.get("returncode", -1))
        if code == 0:
            problem, result = self._check_result(handle)
            if problem is None:
                return Poll("finished", result=result)
            return Poll("crashed", detail=f"exit 0 but {problem}")
        tail = _log_tail(directory / "log.txt")
        return Poll("crashed", kind=self._classify(tail),
                    detail=f"exit {code}: {_last_error_line(tail)}")

    # -- 校验 --------------------------------------------------------------------------------
    def verify(self, item: str, result: Mapping[str, Any]) -> Verdict:
        spec = self.config["verify"]
        if "result_status" in spec:
            mapping = spec["result_status"]
            status = (result.get("data") or {}).get(mapping["key"])
            if status in (mapping.get("pass") or []):
                return Verdict.passed(evidence=result["path"])
            gate = (mapping.get("reject") or {}).get(status)
            if gate is not None:
                return Verdict.rejected(str(status), gate,
                                        final=status in (mapping.get("final") or []),
                                        evidence=result["path"])
            return Verdict.abstained(f"unmapped result status {status!r}", gate="unmapped_status")
        argv, cwd, env = self.command(spec, result["values"])
        completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                   timeout=float(spec.get("timeout_s") or 3600))
        if completed.returncode != 0:
            raise RuntimeError(f"verifier exit {completed.returncode}: "
                               f"{_last_error_line(completed.stderr or completed.stdout)}")
        for line in reversed(completed.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return Verdict.coerce(json.loads(line))
                except (ValueError, TypeError):
                    continue
        raise RuntimeError(f"verifier exit {completed.returncode} without a JSON verdict: "
                           f"{_last_error_line(completed.stderr)}")

    # -- 可选探测 ----------------------------------------------------------------------------
    def _probe(self, spec: Mapping[str, Any], values: Mapping[str, Any]) -> tuple[int, str]:
        argv, cwd, env = self.command(spec, values)
        completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                   timeout=float(spec.get("timeout_s") or 60))
        return completed.returncode, completed.stdout.strip()

    def lease_probe(self, handle: Mapping[str, Any]) -> str:
        """仅当探测命令成功且明确给出时才返回 alive/dead；否则返回 unknown。"""

        if self._unresolved_submission(handle):
            return "unknown"  # 没有 task ID 时，lease 查询为否并不能证明什么
        exit_record = read_json_or_none(Path(handle["attempt_dir"]) / "exit.json")
        if isinstance(exit_record, dict) and exit_record.get("timed_out") is True:
            return "unknown"  # 本地子进程已停止并不证明远程 job 已停止
        try:
            code, output = self._probe(self.config["lease_probe"], handle["values"])
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        answer = output.split()[0] if output else ""
        return answer if code == 0 and answer in ("alive", "dead") else "unknown"

    def provider_probe(self) -> str:
        values: dict[str, Any] = {name: "" for name in TEMPLATE_FIELDS}
        values.update(work_dir=str(self.work_dir), vars={})
        try:
            code, output = self._probe(self.config["provider_probe"], values)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        if code == PROVIDER_DOWN_EXIT or output.startswith("down"):
            return "down"
        if code == 0:
            return "up"
        return "unknown"

    def cost(self, result: Mapping[str, Any]) -> float:
        spec = self.config["cost"]
        if "per_finished_attempt" in spec:
            return float(spec["per_finished_attempt"])
        value = (result.get("data") or {}).get(spec["result_key"])
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0):
            raise ValueError(f"missing or invalid cost in result key {spec['result_key']!r}")
        return float(value)


__all__ = ["SubprocessTask", "TASK_CONFIG_SCHEMA", "TaskConfigError", "load_task_config",
           "pid_alive"]

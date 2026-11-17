"""Task-Expert 编写评测套件：tasks x models x repeats，按 holdout 评分。

每个 cell 是一轮完整的编写（prepare -> sessions -> holdout evaluation）。
当某一轮最终 expert 的 holdout 通过率达到套件阈值时，该轮即视为 *verified*。
对每个 (task, backend, model)，报告给出：

* 轮级 ``pass@k`` = P（k 个独立轮中至少一轮 verified）与
  ``pass^k`` = P（全部 k 轮均 verified），使用无偏估计
  ``1 - C(n-c, k)/C(n, k)`` 与 ``C(c, k)/C(n, k)``，其中 n 为已评测轮数、c 为 verified 轮数；
* 平均 holdout 通过率、每个 verified expert 的成本（已知成本 / verified 轮数）、活跃
  wall time、计入的 rollout 数、基础设施失败次数；
* violations：tamper、isolation、concurrency、budget、holdout-seed 及非开发用
  seed 请求、审计与不安全路径拒绝。

没有可用 adapter 的任务会列出其缺失的确切接口，而不是
伪造。
"""

from __future__ import annotations

import collections
import math
import statistics
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from datafarm.expert.adapter import ADAPTER_SCHEMA, TaskAdapter
from datafarm.expert.layout import RoundPaths
from datafarm.expert.state import RoundState
from datafarm.expert.util import (Clock, ConfigError, HarnessError, atomic_write_json, atomic_write_text)
from datafarm.kernel.io import is_sha256, read_json, sha256_json, utc_now
from datafarm.kernel.identifiers import (
    IDENTITY_PROFILE, logical_id, require_identity_profile, storage_key, verify_storage_key,
)

SUITE_SCHEMA = "datafarm.expert-harness.suite/1"
PLAN_SCHEMA = "datafarm.expert-harness.suite-plan/1"
REPORT_SCHEMA = "datafarm.expert-harness.suite-report/1"
SUITE_SCHEMA_V2 = "datafarm.expert-harness.suite/2"
PLAN_SCHEMA_V2 = "datafarm.expert-harness.suite-plan/2"
REPORT_SCHEMA_V2 = "datafarm.expert-harness.suite-report/2"


def _validate_plan(plan: Mapping[str, Any], out: Path) -> None:
    if not isinstance(plan, Mapping):
        raise HarnessError("suite plan must be a JSON object")
    if plan.get("schema") == PLAN_SCHEMA:
        return
    if plan.get("schema") != PLAN_SCHEMA_V2:
        raise HarnessError("unsupported suite plan schema")
    try:
        require_identity_profile(plan)
        logical_id(plan.get("suite_id"), label="suite_id")
        if not is_sha256(plan.get("suite_sha256")):
            raise ValueError("suite digest is missing")
        specification = plan.get("suite_specification")
        if (not isinstance(specification, Mapping)
                or specification.get("schema") != SUITE_SCHEMA_V2
                or specification.get("suite_id") != plan["suite_id"]
                or sha256_json(specification) != plan["suite_sha256"]
                or plan["k"] != specification.get("k", [1])
                or plan["verified_pass_rate"] != specification.get("verified_pass_rate", 0.5)):
            raise ValueError("suite specification or scoring policy differs from its digest")
        tasks = {task["task_id"]: task for task in specification["tasks"]}
        models = {(model["backend"], model["model"]) for model in specification["models"]}
        expected = {"suite_id": plan["suite_id"], "suite_sha256": plan["suite_sha256"]}
        verify_storage_key(plan.get("plan_id"), "suite", plan.get("identity"), expected)
        seen = set()
        if not isinstance(plan.get("cells"), list) or any(not isinstance(c, Mapping) for c in plan["cells"]):
            raise ValueError("suite cells must be a list of objects")
        for cell in plan["cells"]:
            identity = cell.get("identity")
            if not isinstance(identity, Mapping):
                raise ValueError("cell identity is missing")
            expected = {"suite_id": plan["suite_id"], "suite_sha256": plan["suite_sha256"],
                        "task_id": logical_id(cell.get("task_id"), label="task_id"),
                        "adapter_sha256": cell.get("adapter_sha256"),
                        "backend": cell.get("backend"), "model": cell.get("model"),
                        "repeat": cell.get("repeat")}
            verify_storage_key(cell.get("cell_id"), "cell", identity, expected)
            if cell["cell_id"] in seen:
                raise ValueError("duplicate suite cell")
            seen.add(cell["cell_id"])
            if cell["task_id"] not in tasks:
                raise ValueError("cell task is not in the bound suite")
            unavailable = expected["backend"] is None
            if unavailable:
                if (cell.get("status") != "unavailable" or cell.get("round_dir") is not None
                        or any(expected[k] is not None for k in ("model", "repeat"))):
                    raise ValueError("invalid unavailable cell")
            else:
                if (not is_sha256(expected["adapter_sha256"])
                        or expected["backend"] not in ("claude", "codex", "fake")
                        or not isinstance(expected["model"], str) or not expected["model"]
                        or type(expected["repeat"]) is not int or expected["repeat"] < 1
                        or cell.get("status") not in ("planned", "done", "failed")
                        or (expected["backend"], expected["model"]) not in models
                        or expected["repeat"] > specification["repeats"]):
                    raise ValueError("invalid planned cell")
                target = (out / "rounds" / cell["cell_id"]).resolve()
                if target.parent != out.resolve() / "rounds" or cell.get("round_dir") != str(target):
                    raise ValueError("cell round directory is outside its bound storage")
        for task_id in tasks:
            cells = [cell for cell in plan["cells"] if cell["task_id"] == task_id]
            unavailable_cells = [cell for cell in cells if cell["status"] == "unavailable"]
            if unavailable_cells:
                if len(cells) != 1:
                    raise ValueError("unavailable task has extra cells")
            elif {(cell["backend"], cell["model"], cell["repeat"]) for cell in cells} != {
                    (backend, model, repeat) for backend, model in models
                    for repeat in range(1, specification["repeats"] + 1)}:
                raise ValueError("suite cell denominator is incomplete")
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError(f"invalid suite plan identity: {exc}") from exc


def _bind_cells(plan: dict[str, Any], out: Path) -> None:
    plan.update(schema=PLAN_SCHEMA_V2, identity_profile=IDENTITY_PROFILE,
                identity={"suite_id": plan["suite_id"], "suite_sha256": plan["suite_sha256"]})
    plan["plan_id"] = storage_key("suite", plan["identity"])
    for cell in plan["cells"]:
        identity = {"suite_id": plan["suite_id"], "suite_sha256": plan["suite_sha256"],
                    "task_id": cell["task_id"], "adapter_sha256": cell.get("adapter_sha256"),
                    "backend": cell.get("backend"), "model": cell.get("model"), "repeat": cell.get("repeat")}
        cell.update(identity=identity, cell_id=storage_key("cell", identity))
        if cell["status"] != "unavailable":
            cell["round_dir"] = str((out / "rounds" / cell["cell_id"]).resolve())
    _validate_plan(plan, out)


def pass_at_k(n: int, c: int, k: int) -> float | None:
    if n < k or k <= 0:
        return None
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float | None:
    if n < k or k <= 0:
        return None
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def load_suite(path: str | Path) -> dict[str, Any]:
    suite_path = Path(path)
    suite = read_json(suite_path)
    if not isinstance(suite, dict) or suite.get("schema") not in (SUITE_SCHEMA, SUITE_SCHEMA_V2):
        raise ConfigError("unsupported suite schema")
    if suite["schema"] == SUITE_SCHEMA_V2:
        try:
            if (not isinstance(suite.get("tasks"), list) or not isinstance(suite.get("models"), list)
                    or any(not isinstance(t, Mapping) for t in suite["tasks"])
                    or any(not isinstance(m, Mapping) for m in suite["models"])):
                raise ValueError("suite tasks and models must be lists of objects")
            require_identity_profile(suite)
            logical_id(suite.get("suite_id"), label="suite_id")
            ids = [logical_id(task.get("task_id"), label="task_id") for task in suite.get("tasks", [])]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate suite task_id")
            models = [(model["backend"], model["model"]) for model in suite.get("models", [])]
            if (any(not isinstance(b, str) or not isinstance(m, str) or not m for b, m in models)
                    or len(models) != len(set(models))):
                raise ValueError("duplicate or invalid suite model")
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigError(str(exc)) from exc
    if not suite.get("tasks") or not suite.get("models"):
        raise ConfigError("a suite needs tasks and models")
    if type(suite.get("repeats")) is not int or suite["repeats"] < 1:
        raise ConfigError("repeats must be a positive integer")
    for task in suite["tasks"]:
        if not task.get("adapter") and not (task.get("unavailable") or {}).get("missing_interfaces"):
            raise ConfigError(f"task {task.get('task_id')} has neither an adapter nor missing_interfaces")
    for model in suite["models"]:
        if model.get("backend") not in ("claude", "codex", "fake") or not model.get("model"):
            raise ConfigError(f"invalid suite model {model!r}")
    suite["_dir"] = str(suite_path.resolve().parent)
    suite["_sha256"] = sha256_json({k: v for k, v in suite.items() if not k.startswith("_")})
    return suite


def plan_suite(suite: Mapping[str, Any], out_dir: str | Path, *, now: float,
               _write: bool = True) -> dict[str, Any]:
    out = Path(out_dir)
    versioned = suite["schema"] == SUITE_SCHEMA_V2
    if versioned and _write and ((out / "plan.json").exists() or (out / "plan.json").is_symlink()):
        raise HarnessError("suite plan already exists; resume it or choose a new directory")
    cells = []
    for task in suite["tasks"]:
        task_id = task["task_id"]
        if not task.get("adapter"):
            cells.append({"cell_id": f"{task_id}__unavailable", "task_id": task_id, "status": "unavailable",
                          **({k: v for k, v in task.get("unavailable", {}).items()
                              if k in ("missing_interfaces", "existing")} if versioned
                             else task.get("unavailable", {}))})
            continue
        adapter_path = (Path(suite["_dir"]) / task["adapter"]).resolve()
        adapter = TaskAdapter.load(adapter_path)
        if not versioned or adapter.raw["schema"] != ADAPTER_SCHEMA or adapter.task_id != task_id:
            raise ConfigError("new adapter/1 requires suite/2 with an identical task ID")
        if not adapter.available:
            cells.append({"cell_id": f"{task_id}__unavailable", "task_id": task_id, "status": "unavailable",
                          "missing_interfaces": adapter.raw.get("missing_interfaces"),
                          **({"adapter_sha256": adapter.sha256} if versioned else {})})
            continue
        for model in suite["models"]:
            for repeat in range(1, suite["repeats"] + 1):
                cell_id = f"{task_id}__{model['backend']}-{model['model']}__r{repeat}"
                cells.append({"cell_id": cell_id, "task_id": task_id, "status": "planned",
                              "adapter": str(adapter_path), "backend": model["backend"], "model": model["model"],
                              "repeat": repeat, "holdout_seeds": task.get("holdout_seeds"),
                              **({"adapter_sha256": adapter.sha256} if versioned else {}),
                              "round_dir": str(out / "rounds" / cell_id)})
    plan = {"schema": PLAN_SCHEMA, "suite_id": suite["suite_id"], "suite_sha256": suite["_sha256"],
            "created_at": utc_now(now), "verified_pass_rate": suite.get("verified_pass_rate", 0.5),
            "k": suite.get("k", [1]), "cells": cells,
            "counts": dict(collections.Counter(c["status"] for c in cells))}
    if versioned:
        plan["suite_specification"] = {k: v for k, v in suite.items() if not k.startswith("_")}
        _bind_cells(plan, out)
    if _write:
        out.mkdir(parents=True, exist_ok=True)
        atomic_write_json(out / "plan.json", plan)
    return plan


def round_metrics(round_dir: str | Path, verified_pass_rate: float) -> dict[str, Any]:
    paths = RoundPaths(Path(round_dir))
    state = RoundState.load(paths.state)
    data = state.data
    round_json = read_json(paths.round_json)
    from datafarm.expert.round import validate_round_identity

    validate_round_identity(round_json)
    if data["round_id"] != round_json["round_id"]:
        raise HarnessError("round metrics state identity differs from its prepared record")
    budgets = round_json["budgets"]
    holdout = read_json(paths.holdout_results) if paths.holdout_results.exists() else None
    final = (holdout or {}).get("final") or {}
    rate = final.get("pass_rate")
    counters = data["counters"]
    active = float(data.get("active_wall_s_final") or 0.0)
    cost = float(data.get("cost_usd_known") or 0.0)
    kinds = collections.Counter(v["kind"] for v in data.get("violations", []))
    iso = (data.get("isolation") or {}).get("agent") or {}
    rollout_iso = sum(1 for a in data["attempts"].values()
                      if (a.get("outcome") or {}).get("outcome_class") in ("isolation_failed", "isolation_unavailable"))
    violations = {
        "tamper": int(data["status"] == "invalid_tamper" or not data["manifest"].get("ok", True)),
        "isolation": int(not iso.get("ok", False)) + rollout_iso + int(counters.get("unisolated_rollouts", 0) > 0),
        "concurrency": int(int(data.get("max_in_flight_observed") or 0) > budgets["max_concurrent_rollouts"])
        + kinds.get("concurrency", 0) + kinds.get("late_finish", 0),
        "budget": int(counters.get("counted", 0) > budgets["max_counted_rollouts"])
        + int(budgets.get("max_usd") is not None and cost > budgets["max_usd"])
        + int(active > budgets["total_wall_s"] + 900),
        "holdout_seed_requests": counters.get("holdout_seed", 0),
        "non_dev_seed_requests": counters.get("non_dev_seed", 0),
        "audit_rejections": counters.get("audit_rejected", 0),
        "unsafe_paths": counters.get("unsafe_path", 0),
    }
    return {"round_id": data["round_id"], "status": data["status"], "evaluated": holdout is not None,
            "verified": None if rate is None else rate >= verified_pass_rate, "holdout_pass_rate": rate,
            "holdout_passed": final.get("passed"), "holdout_scored": final.get("scored"),
            "cost_usd_known": round(cost, 4), "sessions_unknown_cost": data.get("sessions_unknown_cost", 0),
            "sessions": len(data["sessions"]), "active_wall_s": active,
            "counted_rollouts": counters.get("counted", 0), "infra_failures": counters.get("infra", 0),
            "dev_successes": counters.get("successes", 0), "violations": violations,
            "curve": (holdout or {}).get("curve")}


def report_suite(out_dir: str | Path, *, now: float) -> dict[str, Any]:
    out = Path(out_dir)
    plan = read_json(out / "plan.json")
    _validate_plan(plan, out)
    threshold = float(plan.get("verified_pass_rate", 0.5))
    ks = plan.get("k", [1])
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for cell in plan["cells"]:
        if cell["status"] != "done":
            continue
        if plan["schema"] == PLAN_SCHEMA_V2:
            from datafarm.expert.round import validate_round_identity

            record = read_json(RoundPaths(Path(cell["round_dir"])).round_json)
            validate_round_identity(record)
            if (record["schema"] != "datafarm.expert-harness.round/2"
                    or record["task_id"] != cell["task_id"]
                    or record["identity"]["nonce"] != cell["cell_id"]):
                raise HarnessError("suite cell and evaluated round identities differ")
        metrics = round_metrics(cell["round_dir"], threshold)
        cell["metrics"] = metrics
        groups[(cell["task_id"], cell["backend"], cell["model"])].append(metrics)
    rows = []
    for (task_id, backend, model), items in sorted(groups.items()):
        evaluated = [m for m in items if m["evaluated"]]
        n = len(evaluated)
        c = sum(1 for m in evaluated if m["verified"])
        total_cost = sum(m["cost_usd_known"] for m in items)
        rates = [m["holdout_pass_rate"] for m in evaluated if m["holdout_pass_rate"] is not None]
        violations: collections.Counter[str] = collections.Counter()
        for m in items:
            violations.update({k: v for k, v in m["violations"].items() if v})
        rows.append({
            "task_id": task_id, "backend": backend, "model": model, "rounds": len(items), "evaluated": n,
            "verified": c, **{f"pass@{k}": pass_at_k(n, c, k) for k in ks},
            **{f"pass^{k}": pass_hat_k(n, c, k) for k in ks},
            "mean_holdout_pass_rate": None if not rates else round(statistics.fmean(rates), 4),
            "cost_usd_known_total": round(total_cost, 4),
            "cost_per_verified_expert": None if c == 0 else round(total_cost / c, 4),
            "sessions_unknown_cost": sum(m["sessions_unknown_cost"] for m in items),
            "median_active_wall_s": statistics.median([m["active_wall_s"] for m in items]) if items else None,
            "mean_counted_rollouts": round(statistics.fmean([m["counted_rollouts"] for m in items]), 2) if items else None,
            "infra_failures": sum(m["infra_failures"] for m in items),
            "violations": dict(violations),
        })
    unavailable = [{"task_id": c["task_id"], "missing_interfaces": c.get("missing_interfaces"),
                    "existing": c.get("existing")} for c in plan["cells"] if c["status"] == "unavailable"]
    report = {"schema": REPORT_SCHEMA, "suite_id": plan["suite_id"], "suite_sha256": plan["suite_sha256"],
              "generated_at": utc_now(now), "verified_pass_rate": threshold, "k": ks, "rows": rows,
              "unavailable_tasks": unavailable,
              "cells": [{k: c.get(k) for k in ("cell_id", "status", "error")} for c in plan["cells"]]}
    if plan["schema"] == PLAN_SCHEMA_V2:
        report.update(schema=REPORT_SCHEMA_V2, identity_profile=IDENTITY_PROFILE,
                      identity=plan["identity"], plan_id=plan["plan_id"],
                      cells=[{k: c.get(k) for k in ("cell_id", "identity", "status", "error")}
                             for c in plan["cells"]])
    atomic_write_json(out / "report.json", report)
    atomic_write_text(out / "report.md", report_markdown(report))
    return report


def report_markdown(report: Mapping[str, Any]) -> str:
    ks = report["k"]
    head = ["task", "backend/model", "rounds", "verified"] + [f"pass@{k}" for k in ks] + \
        [f"pass^{k}" for k in ks] + ["mean holdout", "$/verified", "median wall", "rollouts", "violations"]
    lines = [f"# Task-expert eval suite `{report['suite_id']}`", "",
             f"Generated {report['generated_at']}; a round is verified when its final expert passes at least "
             f"{report['verified_pass_rate']:.0%} of the hidden holdout seeds.", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]

    def fmt(value: Any) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    for row in report["rows"]:
        cells = [row["task_id"], f"{row['backend']}/{row['model']}", row["rounds"], f"{row['verified']}/{row['evaluated']}"]
        cells += [fmt(row[f"pass@{k}"]) for k in ks] + [fmt(row[f"pass^{k}"]) for k in ks]
        cells += [fmt(row["mean_holdout_pass_rate"]), fmt(row["cost_per_verified_expert"]),
                  fmt(row["median_active_wall_s"]), fmt(row["mean_counted_rollouts"]),
                  ", ".join(f"{k}={v}" for k, v in row["violations"].items()) or "none"]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    if report["unavailable_tasks"]:
        lines += ["", "## Declared but unavailable tasks", ""]
        for item in report["unavailable_tasks"]:
            lines.append(f"- **{item['task_id']}**: missing " + "; ".join(item.get("missing_interfaces") or []))
    return "\n".join(lines) + "\n"


def run_suite(suite_path: str | Path, out_dir: str | Path, *, clock: Clock,
              fake: Callable[[TaskAdapter, dict[str, Any], Path, str | None], tuple[TaskAdapter, str]] | None = None,
              approve_agent_spend: bool = False, approve_gpu: bool = False, only: Sequence[str] = (),
              isolation: str | None = None, fast: bool = False) -> dict[str, Any]:
    """``fake``（由入口层提供，例如 ``entry.expert_dryrun.fake_suite_cells()``）把每个 cell 换成
    fake agent + fake simulator：``fake(adapter, cell, out, isolation) -> (adapter, holdout_file)``。"""
    from datafarm.expert.holdout import evaluate_holdout
    from datafarm.expert.round import RoundRunner, harness_code_sha256, manifest_entries, prepare_round, rollout_masks
    from datafarm.expert.rollout import make_backend

    suite = load_suite(suite_path)
    out = Path(out_dir)
    runtime = Clock()  # 轮次运行真实进程：预算与 holdout 截止时间依赖真实时间
    plan = read_json(out / "plan.json") if (out / "plan.json").exists() else plan_suite(suite, out, now=clock.now())
    _validate_plan(plan, out)
    expected_schema = PLAN_SCHEMA_V2 if suite["schema"] == SUITE_SCHEMA_V2 else PLAN_SCHEMA
    if plan["schema"] != expected_schema or plan["suite_id"] != suite["suite_id"]:
        raise HarnessError("suite and plan versions or identities differ")
    if plan["suite_sha256"] != suite["_sha256"]:
        raise HarnessError("the suite file changed since it was planned; plan into a new directory")
    if plan["schema"] == PLAN_SCHEMA_V2:
        expected = plan_suite(suite, out, now=clock.now(), _write=False)
        fields = ("cell_id", "identity", "task_id", "adapter", "adapter_sha256", "backend", "model",
                  "repeat", "holdout_seeds", "round_dir", "missing_interfaces", "existing")
        actual_cells = [{k: c.get(k) for k in fields} for c in plan["cells"]]
        expected_cells = [{k: c.get(k) for k in fields} for c in expected["cells"]]
        if (actual_cells != expected_cells or plan["k"] != expected["k"]
                or plan["verified_pass_rate"] != expected["verified_pass_rate"]):
            raise HarnessError("planned denominator, inputs or scoring policy differ from the suite")
    for cell in plan["cells"]:
        if cell["status"] != "planned" or (only and cell["cell_id"] not in only):
            continue
        try:
            overrides = {"agent": {"backend": cell["backend"], "model": cell["model"]}}
            if suite.get("budget_overrides"):
                overrides["budgets"] = suite["budget_overrides"]
            if plan["schema"] == PLAN_SCHEMA_V2:
                task = next(t for t in suite["tasks"] if t["task_id"] == cell["task_id"])
                expected_path = (Path(suite["_dir"]) / task["adapter"]).resolve()
                original = TaskAdapter.load(expected_path)
                if (cell["adapter"] != str(expected_path) or original.sha256 != cell["adapter_sha256"]
                        or original.task_id != cell["task_id"]
                        or cell.get("holdout_seeds") != task.get("holdout_seeds")):
                    raise HarnessError("planned adapter or holdout input changed; create a new plan")
            adapter = TaskAdapter.load(cell["adapter"], overrides=overrides)
            holdout_file = cell.get("holdout_seeds")
            if fake:
                adapter, holdout_file = fake(adapter, cell, out, isolation)
            if plan["schema"] == PLAN_SCHEMA_V2 and (
                    adapter.raw["schema"] != ADAPTER_SCHEMA or adapter.task_id != cell["task_id"]):
                raise HarnessError("runtime adapter changed the planned logical identity")
            paths = RoundPaths(Path(cell["round_dir"]))
            prepare_round(adapter, paths.root, clock=runtime, holdout_seeds_path=holdout_file,
                          round_id=cell["cell_id"] if plan["schema"] == PLAN_SCHEMA_V2 else f"XS-{cell['cell_id']}"[:120],
                          overrides=None if fake else overrides)
            runner_kwargs = {"tick_interval_s": 0.1, "poll_interval_s": 0.2, "term_grace_s": 3.0,
                             "drain_timeout_s": 120.0} if fake or fast else {}
            RoundRunner(paths, adapter, clock=runtime, approve_agent_spend=approve_agent_spend,
                        approve_gpu=approve_gpu, isolation_override=isolation if fake else None,
                        **runner_kwargs).run()
            kind = adapter.rollout.get("backend")
            if kind == "command" and not approve_gpu:
                raise HarnessError("holdout evaluation submits GPU jobs; pass --approve-gpu")
            backend = make_backend(adapter, kind=kind, masked=rollout_masks(adapter, paths, purpose="holdout"),
                                   isolation_override=isolation if fake else None)
            evaluate_holdout(paths, adapter, backend, clock=runtime, poll_s=0.2 if fake else 15.0,
                             manifest_entries=manifest_entries(adapter, paths),
                             manifest_extra={"harness_code_sha256": harness_code_sha256()})
            cell["status"] = "done"
        except HarnessError as exc:
            cell["status"] = "failed"
            cell["error"] = str(exc)[:500]
        atomic_write_json(out / "plan.json", plan)
    return report_suite(out, now=clock.now())

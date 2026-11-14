#!/usr/bin/env python3
"""按 recipe 在 tasks x episodes x modes 上构建包，并以轮转（round-robin）方式将 ticket 入队。

  python h3_enqueue.py [--run-dir <run>] --recipe <run>/recipes/<name>.json --tasks <task-a> <task-b> \
      --modes sim2real [--episodes <id> ...] [--seed-offset 0 1 ... 7] [--priority 100] [--per-chunk] [--build-slots 2]
Bundle 从 <run>/tasks/<task>/ 读取，包写入 <run>/pkgs/<recipe>/<task>/<tag>，ticket 写入 <run>/queue/。
--episodes 选择 bundle 的 episode id（默认：全部）。OOD episode 会循环使用 bundle 的 ood_variants（episode k ->
variant k mod n）。--per-chunk 为每个尚未渲染的 chunk 写一个 ticket，从而多个 worker 可以共享同一个包（各 chunk
独立渲染）。已存在的包会被复用（不重建）；已完成/运行中的 ticket 不会重新入队。
构建顺序：第一个 --seed-offset 的包先于其他包构建，以便填满 run 的 HED 缓存（以
white chunk 为键），后续 seed 便复用该缓存，而不是重新计算相同的 HED 视频。每次构建都以 nice 10
运行，并占用 --build-slots 个 run 级槽位之一（<run>/cache/build-slots，flock，由并发 enqueue 调用共享）：一次
构建会运行 4 个 HED 进程 x 16 线程，而不加节流地构建大量 episode x seed 曾把一台 180 核登录节点
的负载推到约 2500。
--gate <name>（生产环境）仅当该 recipe 与 task 的 h3_gate.py gate 已放行所有选中的
episode 时才执行：先是 2 个 prefix，两个 prefix 均通过盲审后是 canary，canary 通过后才是其余部分。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import h3_paths

HERE = Path(__file__).resolve().parent


def bundle_file(run: Path, task: str, recipe: dict) -> Path:
    name = f"bundle-{recipe['bundle_variant']}.json" if recipe.get("bundle_variant") else "bundle.json"
    return run / "tasks" / task / name


@contextlib.contextmanager
def build_slot(run: Path, slots: int, poll: float = 5.0):
    """构建期间占用 `slots` 个 run 级构建槽位之一（对 <run>/cache/build-slots/slot-<i> 加 flock）。"""
    d = run / "cache" / "build-slots"
    d.mkdir(parents=True, exist_ok=True)
    while True:
        for i in range(max(1, slots)):
            f = open(d / f"slot-{i}", "a+")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                continue
            try:
                yield i
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()
            return
        time.sleep(poll)


def chunk_tickets(ticket: dict) -> list[tuple[str, dict]]:
    """按 chunk 返回（名称后缀, ticket）对，仅限尚未有输出的 chunk；每个 ticket 指向 manifest 的单 job 副本。"""
    package = Path(ticket["package"])
    manifest = json.loads((package / "job-manifest.json").read_text())
    out = []
    for job in manifest["jobs"]:
        if (Path(ticket["output_root"]) / "chunks" / job["chunk_id"] / "segment-manifest.json").exists():
            continue
        path = package / f"job-manifest.{job['chunk_id']}.json"
        path.write_text(json.dumps({**manifest, "jobs": [job]}, indent=2))
        out.append((f"-c{int(job['chunk_index']):02d}", {**ticket, "manifest": str(path)}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", required=True)
    ap.add_argument("--tasks", nargs="+", required=True)
    ap.add_argument("--episodes", nargs="*", default=[], help="bundle episode ids (default: all)")
    ap.add_argument("--modes", nargs="+", required=True, choices=["sim2real", "ood"])
    ap.add_argument("--seed-offset", type=int, nargs="+", default=[0],
                    help="one or more seed offsets; the first is built first and fills the HED cache")
    ap.add_argument("--build-slots", type=int, default=int(os.environ.get("H3_BUILD_SLOTS", "2")),
                    help="run-wide concurrent package builds (default env H3_BUILD_SLOTS or 2)")
    ap.add_argument("--priority", type=int, default=100, help="lower runs first (ticket filename prefix)")
    ap.add_argument("--per-chunk", action="store_true", help="one ticket per chunk instead of one per package")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gate", help="h3_gate.py gate name: enqueue only episodes it has released")
    h3_paths.add_run_dir_arg(ap)
    args = ap.parse_args()
    run = h3_paths.run_dir(args.run_dir)
    python = h3_paths.python_exe()
    recipe = json.loads(Path(args.recipe).read_text())
    if recipe.get("backend") != "control-hybrid":
        raise SystemExit("recipe backend must be 'control-hybrid'")
    recipe_name = recipe["name"]
    items = []
    for task in args.tasks:
        bundle = json.loads(bundle_file(run, task, recipe).read_text())
        episodes = [e for e in bundle["episodes"] if not args.episodes or e["id"] in args.episodes]
        variants = [v["name"] for v in bundle["ood_variants"]] if "ood" in args.modes else []
        for k, ep in enumerate(episodes):
            for mode in args.modes:
                variant = variants[k % len(variants)] if mode == "ood" else None
                for offset in args.seed_offset:
                    tag = f"{ep['id']}-{mode}" + (f"-{variant}" if variant else "") + (f"-s{offset}" if offset else "")
                    items.append({"task": task, "episode": ep["id"], "mode": mode, "variant": variant,
                                  "seed_offset": offset, "out": run / "pkgs" / recipe_name / task / tag, "rank": k,
                                  "tag": tag})
    if args.gate:
        if len(args.tasks) != 1:
            raise SystemExit("--gate covers one task")
        import h3_gate
        h3_gate.check_enqueue(run, args.gate, recipe_name, args.tasks[0], sorted({it["episode"] for it in items}))
    # 轮转顺序：episode rank，然后 mode，然后 task，然后 seed
    items.sort(key=lambda it: (it["rank"], args.modes.index(it["mode"]), args.tasks.index(it["task"]),
                               args.seed_offset.index(it["seed_offset"])))

    def build(it):
        if (it["out"] / "ticket.json").exists():
            return it, "exists"
        cmd = ["nice", "-n", "10", python, str(HERE / "h3_package.py"), "build",
               "--bundle", str(bundle_file(run, it["task"], recipe)), "--recipe", str(Path(args.recipe).resolve()),
               "--episode", it["episode"], "--mode", it["mode"], "--seed-offset", str(it["seed_offset"]),
               "--out", str(it["out"]), "--run-dir", str(run)]
        if it["variant"]:
            cmd += ["--variant", it["variant"]]
        with build_slot(run, args.build_slots):
            r = subprocess.run(cmd, capture_output=True, text=True)
        return it, ("built" if r.returncode == 0 else "FAILED: " + r.stderr[-800:])

    if args.dry_run:
        for it in items:
            print(it["tag"], it["task"])
        return 0
    first = [it for it in items if it["seed_offset"] == args.seed_offset[0]]
    later = [it for it in items if it["seed_offset"] != args.seed_offset[0]]
    with cf.ThreadPoolExecutor(4) as ex:   # 先构建第一个 seed：其构建会填满后续 seed 复用的 HED 缓存
        done = dict(zip(map(id, first), ex.map(build, first)))
        done.update(zip(map(id, later), ex.map(build, later)))
    results = [done[id(it)] for it in items]
    q = run / "queue"
    known = set()
    for f in list(q.glob("pending/*/*.json")) + [f for sub in ("running", "done") for f in (q / sub).glob("*.json")]:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        known.add((d.get("ticket") or d).get("package"))
    for n, (it, status) in enumerate(results):
        if status.startswith("FAILED"):
            print(it["task"], it["tag"], status)
            continue
        ticket = json.loads((it["out"] / "ticket.json").read_text())
        stem = f"{args.priority:03d}-{n:03d}-{recipe_name}-{it['task']}-{it['tag']}"
        if ticket["package"] in known:
            print("skip (already queued/done)", stem)
            continue
        dest = q / "pending" / ticket["profile"]
        dest.mkdir(parents=True, exist_ok=True)
        for suffix, sub in (chunk_tickets(ticket) if args.per_chunk else [("", ticket)]):
            (dest / f"{stem}{suffix}.json").write_text(json.dumps(sub, indent=2))
            print(status, f"{stem}{suffix}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""为已完成的 H3 package 打分并打包：assemble -> 冻结的 EVAL-v1 评分 -> lite 评审 pack（每次调用执行一遍）。

处理给定的 --packages，否则处理每个 <run>/queue/done ticket 以及每个 derived（仅 assembly）package；已完成的
package 会被跳过，因此可以重复运行。评分器是本 checkout 冻结的 evaluations/render-h3/h3eval.py（EVAL-v1），用 H3_PYTHON
和 --dino ${H3_EVAL_DINO} 运行。h3_review_prep.py 需要这里写出的 pack。

  python h3_score_and_pack.py [--run-dir <run>] [--workers 6] [--packages <pkg dir> ...]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
from pathlib import Path

import h3_paths
from h3_assemble import assemble

EVAL_THREADS = 16


def run_eval(*args: str) -> None:
    cmd = [h3_paths.python_exe(), str(h3_paths.eval_script()), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, EVAL_THREADS=str(EVAL_THREADS)))
    if r.returncode:
        raise RuntimeError(" ".join(cmd)[:300] + "\n" + r.stderr[-1500:])


def score(generated: str, carrier: str, out: Path, task: str, run: Path) -> None:
    pool = run / "eval/realpool" / f"{task}.npz"
    run_eval("score", "--generated", generated, "--carrier", carrier, "--out", str(out), "--white-cos", "--dino",
             h3_paths.eval_dino(), *(["--realpool", str(pool)] if pool.exists() else []))


def bundle_references(pkg: dict) -> list[str]:
    b = json.loads(Path(pkg["bundle"]).read_text())
    refs = (b.get("per_episode_references") or {}).get(pkg["episode"]) or b["real_references"]
    return [r["path"] for r in refs]


def chunks_done(pkg_dir: Path) -> bool:
    manifest = json.loads((pkg_dir / "job-manifest.json").read_text())
    return all((pkg_dir / "render/chunks" / job["chunk_id"] / "segment-manifest.json").is_file()
               for job in manifest["jobs"])


def process(pkg_dir: Path, run: Path) -> str:
    if (pkg_dir / "score.json").exists() and (pkg_dir / "pack/manifest.json").exists():
        return f"skip {pkg_dir.name}"
    pkg = json.loads((pkg_dir / "package.json").read_text())
    if not chunks_done(pkg_dir):
        return f"waiting {pkg_dir.name}"
    if not (pkg_dir / "assembled.json").exists():
        assemble(pkg_dir)
    assembled = json.loads((pkg_dir / "assembled.json").read_text())
    score(assembled["final"], assembled["carrier_clip"], pkg_dir / "score.json", pkg["task"], run)
    references = [arg for r in bundle_references(pkg) for arg in ("--ref", r)]
    run_eval("pack", "--generated", assembled["final"], "--carrier", assembled["carrier_clip"], "--out",
             str(pkg_dir / "pack"), *references)
    return f"ok {pkg_dir}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--packages", nargs="*", default=[], type=Path)
    h3_paths.add_run_dir_arg(ap)
    args = ap.parse_args()
    run = h3_paths.run_dir(args.run_dir)
    pkgs = [p.resolve() for p in args.packages]
    if not pkgs:
        for f in sorted((run / "queue/done").glob("*.json")):
            p = Path(json.loads(f.read_text())["ticket"]["package"])
            if p not in pkgs:
                pkgs.append(p)
        for pj in sorted(run.glob("pkgs/*/*/*/package.json")):  # derived（仅 assembly）的 package 没有 ticket
            if json.loads(pj.read_text()).get("derived_from") and pj.parent not in pkgs:
                pkgs.append(pj.parent)

    def safe(pkg_dir: Path) -> str:
        try:
            return process(pkg_dir, run)
        except Exception as e:  # noqa: BLE001 - one bad package must not stop the pass
            return f"ERROR {pkg_dir.name}: {e}"

    with cf.ThreadPoolExecutor(args.workers) as ex:
        for res in ex.map(safe, pkgs):
            print(res, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

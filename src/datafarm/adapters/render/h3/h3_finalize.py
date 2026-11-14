#!/usr/bin/env python3
"""完成一个已渲染 episode：seed 选择 -> 组装 -> 冻结的 EVAL-v1 评分 -> 首 chunk 曝光锁定 -> 评分。

  python h3_finalize.py select --render-recipe <name> --task <task> --episode-tag <tag> --name <selection name> \
      [--choice-file <choice.json>] --seeds s0 s1 ... [--run-dir <run>]
  python h3_finalize.py lock --package <assembled package dir> --name <selection name> [--run-dir <run>]

`select` 依据逐 chunk 的选择（一个独立的 producer 侧 VLM 筛选；h3_screen_prep.py）构建
<run>/recipes/<name>.json 与 <run>/pkgs/<name>/<task>/<tag>，以 min-diff 接缝组装并评分，然后应用 `lock`。当
choice 文件被省略、缺失或无效时（筛选会话失败或写出了部分答案），它回退到
h3_select.py 的像素占用筛选，并在 package 的 `selection.fallback_reason` 中记录原因。
`lock` 写出 <run>/pkgs/exposure-lock-<name>/<task>/<tag> 并对其评分。由宽 FOV wrist
white（package.json wide_wrist）渲染的 package 随后被裁回 white 自身的腕部相机模型（h3_wrist_crop.py），输出到
<run>/pkgs/exposure-lock-<name>-wristcrop/<task>/<tag> 并评分；该 package 才是要打包并审查的那个。
验收仍由独立的盲审完成。
相对路径相对于 run 目录解析。使用 H3_PYTHON 运行（numpy、opencv）；绑定：H3_RUN、H3_EVAL_DINO。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import h3_paths
from h3_assemble import assemble
from h3_exposure_lock import lock_package
from h3_score_and_pack import score
from h3_select import choice_problem, select_seeds
from h3_wrist_crop import crop_back


def score_package(pkg_dir: Path, task: str, run: Path) -> None:
    assembled = json.loads((pkg_dir / "assembled.json").read_text())
    score(assembled["final"], assembled["carrier_clip"], pkg_dir / "score.json", task, run)


def lock(package: Path, name: str, run: Path) -> Path:
    lock_name = f"exposure-lock-{name}"
    recipe_path = run / "recipes" / f"{lock_name}.json"
    if not recipe_path.exists():
        recipe_path.write_text(json.dumps({
            "name": lock_name, "parent": name,
            "single_variable": "output-side first-chunk photometric anchoring of every chunk (h3_exposure_lock.py) "
                               "before hard-cut assembly at the recorded seams; gains only, no geometry/timing change"},
            indent=2))
    dst = lock_package(package, json.loads(recipe_path.read_text()), run)
    score_package(dst, package.parts[-2], run)
    cropped = crop_back(dst, run)   # 宽 FOV 腕部 white：裁回 white 自身的腕部相机模型
    if cropped is not None:
        score_package(cropped, package.parts[-2], run)
        return cropped
    return dst


def usable_choice(choice_file: Path | None, cands: list[Path]) -> tuple[Path | None, str | None]:
    """当 VLM 筛选答案与候选匹配时返回 (choice 文件, None)，否则返回 (None, 无法使用的原因)。"""
    if choice_file is None:
        return None, "no choice file given"
    if not choice_file.is_file():
        return None, f"{choice_file} does not exist"
    chunks = len(json.loads((cands[0] / "job-manifest.json").read_text())["jobs"])
    try:
        problem = choice_problem(json.loads(choice_file.read_text()), chunks, len(cands))
    except ValueError as e:
        problem = f"not JSON ({e})"
    return (choice_file, None) if problem is None else (None, f"{choice_file}: {problem}")


def select(args, run: Path) -> Path:
    base = run / "pkgs" / args.render_recipe / args.task / args.episode_tag
    cands = [base if s == "s0" else base.with_name(f"{base.name}-{s}") for s in args.seeds]
    choice_file, fallback = usable_choice(run / args.choice_file if args.choice_file else None, cands)
    recipe = json.loads((run / "recipes" / f"{args.render_recipe}.json").read_text())
    if choice_file is not None:
        screener = json.loads(choice_file.read_text()).get("screener")
        selection = {"mode": "screen", "seeds": "available offsets",
                     "screen": f"{screener} seed screen (h3_screen_prep.py composites + choice.json, h3.seed-screen/1)"}
        how = "a separate producer-side VLM screen (content errors vs the carrier)"
    else:
        print(f"seed screen unusable ({fallback}); falling back to the occupancy screen", file=sys.stderr)
        selection = {"mode": "occupancy", "seeds": "available offsets", "fallback_reason": fallback,
                     "screen": "h3_select.py carrier-grounded occupancy screen (VLM screen unusable)"}
        how = "the pixel occupancy screen (fallback: no usable VLM screen)"
    recipe.update(name=args.name, parent=args.render_recipe, render_from=args.render_recipe, selection=selection,
                  single_variable=f"per-chunk seed choice among {args.render_recipe} seeds by {how}; acceptance stays "
                                  f"the independent blind review; assembly = {args.render_recipe}")
    (run / "recipes" / f"{args.name}.json").write_text(json.dumps(recipe, indent=2))
    dst = run / "pkgs" / args.name / args.task / args.episode_tag
    if dst.exists():
        shutil.rmtree(dst)
    select_seeds(cands, recipe, choice_file, run, fallback_reason=fallback)
    assemble(dst)
    score_package(dst, args.task, run)
    return lock(dst, args.name, run)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--render-recipe", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--episode-tag", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--choice-file", type=Path,
                   help="the VLM screen's choice.json; omitted/missing/invalid: occupancy screen fallback")
    s.add_argument("--seeds", nargs="+", required=True, help="seed suffixes of the candidates: s0 s1 ...")
    h3_paths.add_run_dir_arg(s)
    k = sub.add_parser("lock")
    k.add_argument("--package", required=True, type=Path)
    k.add_argument("--name", required=True)
    h3_paths.add_run_dir_arg(k)
    args = ap.parse_args()
    run = h3_paths.run_dir(args.run_dir)
    h3_paths.eval_dino()  # 在任何工作前失败关闭
    final = select(args, run) if args.cmd == "select" else lock((run / args.package).resolve(), args.name, run)
    print(json.dumps({"final_package": str(final)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
